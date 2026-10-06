"""Session-owned bounded PCM transform. Heavy providers run outside Hermes.

PCM1 protocol: network-order !4sBQQI header (magic, kind, epoch, sequence,
payload bytes). READY=1 JSON geometry; AUDIO=2 PCM16LE/24k mono; RESET=3;
OUTPUT=4 float32LE/24k mono; STOP=5; ERROR=6 bounded diagnostic code.
No transcript, credentials, network listeners or neural imports here.
"""
from __future__ import annotations

import array
import asyncio
from collections import Counter, deque
import json
import math
import os
from pathlib import Path
import struct
import sys
import time

HEADER = struct.Struct('!4sBQQI')
MAX_PAYLOAD = 65536
ERROR_CODES = frozenset({b'converter_unavailable', b'converter_gpu_headroom',
                        b'converter_resource_policy'})


def validate_transform(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError('output_transform must be a mapping')
    cfg = dict(raw)
    cfg.setdefault('provider', 'none')
    if cfg['provider'] not in ('none', 'seed_vc'):
        raise ValueError('output_transform provider must be none or seed_vc')
    budget = cfg.setdefault('max_queue_seconds', 1.0)
    if type(budget) not in (int, float) or not math.isfinite(budget) or not .25 <= budget <= 1.0:
        raise ValueError('output_transform max_queue_seconds must be 0.25..1.0')
    if cfg['provider'] != 'none':
        for key in ('python_path', 'root_path', 'reference_path'):
            value = cfg.get(key)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError('output_transform requires absolute ' + key)
    return cfg


def child_environment(root):
    # Deliberately do not inherit HOME, PYTHONPATH, scoped keys or gateway env.
    root = Path(root)
    env = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'CUDA_VISIBLE_DEVICES': '0',
           'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
           'HF_HUB_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1',
           'PYTHON_DOTENV_DISABLED': '1',
           'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'}
    for name in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                 'NUMEXPR_NUM_THREADS', 'NUMBA_NUM_THREADS', 'MAX_JOBS'):
        env[name] = '4'
    for name, suffix in {'HOME': 'runtime-home', 'TMPDIR': 'tmp', 'TEMP': 'tmp', 'TMP': 'tmp',
                         'XDG_CACHE_HOME': 'cache', 'HF_HOME': 'cache/huggingface',
                         'HF_HUB_CACHE': 'cache/huggingface/hub', 'TORCH_HOME': 'cache/torch',
                         'NUMBA_CACHE_DIR': 'cache/numba', 'MODELSCOPE_CACHE': 'cache/modelscope',
                         'MPLCONFIGDIR': 'cache/matplotlib', 'CUDA_CACHE_PATH': 'cache/cuda',
                         'TRITON_CACHE_DIR': 'cache/triton'}.items():
        env[name] = str(root / suffix)
    return env


class IdentityTransform:
    process = None

    def __init__(self, emit, clear):
        self.emit, self.clear_output = emit, clear
        self.closed = False
        self.epoch = 0

    async def start(self):
        pass

    def append(self, pcm):
        if not self.closed:
            self.emit(pcm)

    def clear(self):
        self.epoch += 1
        self.clear_output()

    def revoke(self):
        self.closed = True
        self.clear()

    async def close(self):
        self.closed = True

    def status(self):
        return {'selected': 'none', 'actual': 'native', 'state': 'closed' if self.closed else 'ready',
                'queue_age_seconds': 0.0, 'epoch': self.epoch}


class ProcessTransform(IdentityTransform):
    def __init__(self, cfg, emit, clear, emit_timed, on_fallback):
        super().__init__(emit, clear)
        self.cfg = cfg
        self.emit_timed = emit_timed
        self.on_fallback = on_fallback
        self.state = 'warming'
        self.actual = 'none'
        self.error = None
        self.stats = Counter()
        self._tasks = []
        self._spawn_task = None
        self._close_task = None
        self._ready = asyncio.Event()
        self._wake = asyncio.Event()
        self._send = deque()
        self._pending = {}
        self._partial = bytearray()
        self._partial_start = None
        self._render = deque()
        self._seq = 0
        self._block = 8640
        self._algorithmic = .08
        self._budget = cfg['max_queue_seconds']
        self._failed = False
        self._prebuffer = .10
        self._next_play = 0.0
        self.queue_age = 0.0
        self._last_input = 0.0
        self._drain_count = 2

    async def start(self):
        if self.closed or self._spawn_task is not None:
            raise RuntimeError('Transform is single use')
        if self._failed:
            return  # Off selected while startup was awaiting its warmup notice.
        try:
            root = Path(self.cfg['root_path'])
            command = root / 'runtime/seed_vc_worker.py'
            if not command.is_file() or not Path(self.cfg['reference_path']).is_file():
                raise FileNotFoundError('Local output transform unavailable')
            self._spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(
                self.cfg['python_path'], '-u', str(command), '--root', str(root),
                '--reference', self.cfg['reference_path'],
                cwd=root, env=child_environment(root), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_PAYLOAD, start_new_session=True))
            # Retain ownership even if cancellation arrives during process creation.
            self.process = await asyncio.shield(self._spawn_task)
            if self.closed:
                raise asyncio.CancelledError
            self._tasks = [asyncio.create_task(self._reader()), asyncio.create_task(self._writer()),
                           asyncio.create_task(self._playout())]
            await asyncio.wait_for(self._ready.wait(), 60)
        except asyncio.CancelledError:
            self.revoke()
            await self.close()
            raise
        except Exception as exc:
            self.fallback(type(exc).__name__)
            await self._shutdown()

    def _frame(self, kind, epoch, seq=0, payload=b''):
        if len(payload) > MAX_PAYLOAD:
            raise ValueError('Transform frame too large')
        self._send.append(HEADER.pack(b'PCM1', kind, epoch, seq, len(payload)) + payload)
        self._wake.set()

    def append(self, pcm, *, _padding=False):
        if self.closed or not pcm:
            return
        if self.actual == 'native':
            self.emit(pcm)
            return
        if self.state != 'ready':
            self.stats['warming_dropped_bytes'] += len(pcm)
            return
        if len(pcm) % 2 or len(pcm) > 48000:
            self.fallback('input_frame_limit')
            return  # Never replay the offending/current provider chunk natively.
        now = time.monotonic()
        queued = len(self._partial) + len(self._pending) * self._block + len(self._render) * self._block
        if not _padding:
            self._last_input = now
            self._drain_count = 0
        if queued + len(pcm) > 48000 * self._budget:
            self.fallback('queue_limit')
            return
        if not self._partial:
            self._partial_start = now - len(pcm) / 48000
        self._partial.extend(pcm)
        while len(self._partial) >= self._block:
            data = bytes(self._partial[:self._block])
            del self._partial[:self._block]
            self._seq += 1
            stamp = self._partial_start
            self._partial_start += self._block / 48000
            self._pending[self._seq] = (self.epoch, stamp)
            self._frame(2, self.epoch, self._seq, data)
        if not self._partial:
            self._partial_start = None
        self.stats['input_bytes'] += len(pcm)

    def clear(self):
        super().clear()
        self._send.clear()
        self._pending.clear()
        self._render.clear()
        self._partial.clear()
        self._partial_start = None
        self._next_play = 0.0
        self._last_input = 0.0
        self._drain_count = 2
        if not self.closed and not self._failed:
            self._frame(3, self.epoch)

    def fallback(self, reason='operator_off'):
        if self._failed or self.closed:
            return
        self._failed = True
        self.clear()
        self.error = reason
        self.state, self.actual = 'fallback', 'native'
        self.stats['fallbacks'] += 1
        self._ready.set()
        self._terminate()
        # No raw history is retained. Only a later on_audio callback can cross
        # the fresh native boundary; old/in-flight converted frames cannot.
        if self.on_fallback is not None:
            self.on_fallback(reason)

    def _terminate(self):
        if self.process is not None and self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass

    def revoke(self):
        super().revoke()
        self.state, self.actual = 'closed', 'none'
        self._ready.set()
        self._terminate()

    async def _read_frame(self):
        raw = await self.process.stdout.readexactly(HEADER.size)
        magic, kind, epoch, seq, length = HEADER.unpack(raw)
        if magic != b'PCM1' or length > MAX_PAYLOAD:
            raise ValueError('Transform framing')
        data = await self.process.stdout.readexactly(length)
        # Worker errors are terminal, including before READY and after RESET.
        # The wire deliberately uses epoch/seq zero for process-wide errors.
        if kind == 6:
            if epoch != 0 or seq != 0:
                raise ValueError('Transform error framing')
            code = data if data in ERROR_CODES else b'converter_unavailable'
            self.fallback(code.decode('ascii'))
        return kind, epoch, seq, data

    async def _reader(self):
        try:
            kind, epoch, seq, data = await self._read_frame()
            if self._failed:
                return
            if kind != 1 or epoch != 0 or seq != 0:
                raise ValueError('Transform not ready')
            info = json.loads(data)
            if info.get('block_samples') != 4320 or info.get('algorithmic_seconds') != .08:
                raise ValueError('Unsupported transform geometry')
            if self.closed or self._failed:
                return
            self.state, self.actual = 'ready', 'converted'
            self._ready.set()
            while not self.closed and not self._failed:
                kind, epoch, seq, data = await self._read_frame()
                if self._failed:
                    return
                if epoch < self.epoch:
                    self.stats['stale_outputs'] += 1
                    continue
                if (kind != 4 or epoch != self.epoch or not self._pending
                        or seq != next(iter(self._pending)) or len(data) != self._block * 2):
                    raise ValueError('Transform output order or shape')
                expected_epoch, stamp = self._pending.pop(seq)
                if expected_epoch != epoch:
                    raise ValueError('Transform epoch')
                values = array.array('f')
                values.frombytes(data)
                if sys.byteorder != 'little':
                    values.byteswap()
                if not all(math.isfinite(v) for v in values):
                    raise ValueError('Nonfinite transform output')
                peak = max(map(abs, values), default=0.0)
                # Fixed safe gain, with explicit per-block peak limiting. No
                # implicit PCM16 saturation; retain float samples until here.
                gain = min(.8, .98 / peak) if peak else .8
                self.stats['limited_blocks'] += gain < .8
                self.stats['peak_float_milli'] = max(self.stats['peak_float_milli'], round(peak * 1000))
                pcm = array.array('h', (round(v * gain * 32767) for v in values))
                if sys.byteorder != 'little':
                    pcm.byteswap()
                now = time.monotonic()
                play_at = max(now + (self._prebuffer if not self._next_play else 0), self._next_play)
                self._next_play = play_at + self._block / 48000
                self._render.append((epoch, stamp - self._algorithmic, play_at, pcm.tobytes()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fallback(type(exc).__name__)

    async def _writer(self):
        try:
            while not self.closed and not self._failed:
                await self._wake.wait()
                self._wake.clear()
                while self._send and not self.closed and not self._failed:
                    self.process.stdin.write(self._send.popleft())
                    await asyncio.wait_for(self.process.stdin.drain(), 1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fallback(type(exc).__name__)

    async def _playout(self):
        try:
            while not self.closed and not self._failed:
                now = time.monotonic()
                # A pause is not a semantic response-done event. Two bounded
                # zero blocks flush the causal tail without future source data.
                if (self.state == 'ready' and self._drain_count < 2
                        and now - self._last_input >= .20 + self._drain_count * .18):
                    self.append(bytes(self._block - len(self._partial)), _padding=True)
                    self._drain_count += 1
                stamps = [s - self._algorithmic for _, s in self._pending.values()]
                stamps.extend(s for _, s, _, _ in self._render)
                if self._partial_start is not None:
                    stamps.append(self._partial_start - self._algorithmic)
                self.queue_age = max(0.0, now - min(stamps)) if stamps else 0.0
                self.stats['queue_age_peak_ms'] = max(self.stats['queue_age_peak_ms'], round(self.queue_age * 1000))
                if self.queue_age > self._budget:
                    self.fallback('media_age_limit')
                    break
                while self._render and self._render[0][2] <= now:
                    epoch, stamp, _, pcm = self._render.popleft()
                    if epoch == self.epoch and not self.closed and not self._failed:
                        if self.emit_timed is not None:
                            self.emit_timed(pcm, stamp, self._budget)
                        else:
                            self.emit(pcm)
                        self.stats['output_bytes'] += len(pcm)
                await asyncio.sleep(.01)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fallback(type(exc).__name__)
        finally:
            if self._failed:
                await self._shutdown()

    async def _shutdown(self):
        if self._spawn_task is not None and self.process is None:
            # create_subprocess_exec is OS-local and completes even if our caller
            # was cancelled. Never lose the returned child ownership handle.
            try:
                self.process = await asyncio.shield(self._spawn_task)
            except Exception:
                pass
        proc = self.process
        self._terminate()
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), .5)
            except TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            finally:
                proc._transport.close()
            await asyncio.wait_for(proc.wait(), 2)

    async def close(self):
        if self._close_task is None:
            self.revoke()
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._shutdown()

    def status(self):
        return {'selected': self.cfg['provider'], 'actual': self.actual, 'state': self.state,
                'error': self.error, 'queue_age_seconds': round(self.queue_age, 3),
                'epoch': self.epoch, 'stats': dict(self.stats)}


def create_output_transform(config, emit, clear, *, emit_timed=None, on_fallback=None):
    cfg = validate_transform(config)
    if cfg['provider'] == 'none':
        return IdentityTransform(emit, clear)
    return ProcessTransform(cfg, emit, clear, emit_timed, on_fallback)
