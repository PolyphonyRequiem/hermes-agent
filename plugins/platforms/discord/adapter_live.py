"""Opt-in Discord host glue for the primary Live stream. No bot client of its own."""
from __future__ import annotations

import asyncio
import audioop
import json
import logging
import math
import threading
import time
import uuid
from collections import Counter, deque
from pathlib import Path

import discord

from agent.secret_scope import current_secret_scope, get_secret
from tools.voice_live_stream import LiveStream, ProjectStatusBackend
from tools.voice_output_transform import create_output_transform, validate_transform

logger = logging.getLogger(__name__)


def private_audience(channel, operator_id, bot_id):
    """Do not broadcast engineering context to third-party channel occupants."""
    try:
        return set(channel.voice_states) <= {int(operator_id), int(bot_id)}
    except (AttributeError, TypeError, ValueError):
        return False


class ProviderDurationError(ValueError):
    """Advertised provider expiry is invalid or has already elapsed."""


def get_scoped_secret(name: str):
    """Installed-fork equivalent: an installed profile scope is authoritative."""
    scope = current_secret_scope()
    return scope.get(name) if scope is not None else get_secret(name)


class PCMSource(discord.AudioSource):
    """Discord's 20ms pull source, with a half-second hard queue limit."""
    def __init__(self):
        self._pcm = bytearray()
        self._lock = threading.Lock()
        self._closed = False
        self._rate_state = None
        self._last_audible_at = 0.0
        self._segments = deque()
        self.age_expired = False
        self.stats = Counter()

    def append(self, pcm: bytes) -> None:
        self.append_timed(pcm, None, None)

    def append_timed(self, pcm: bytes, source_start, budget) -> None:
        if not pcm:
            return
        with self._lock:
            if self._closed:
                return
            converted, self._rate_state = audioop.ratecv(pcm, 2, 1, 24000, 48000, self._rate_state)
            converted = audioop.tostereo(converted, 2, 1, 1)
            if len(self._pcm) + len(converted) > 96000:
                self.stats['overflow'] += 1
                raise BufferError('Live playback queue exceeded 500ms')
            self._pcm.extend(converted)
            self._segments.append([len(converted), source_start, budget])
            self.stats['appended_bytes'] += len(converted)
            self.stats['queue_peak_bytes'] = max(self.stats['queue_peak_bytes'], len(self._pcm))

    def read(self) -> bytes:
        with self._lock:
            if self._closed:
                return b''
            if self._segments and self._segments[0][1] is not None:
                age = time.monotonic() - self._segments[0][1]
                self.stats['queue_age_peak_ms'] = max(self.stats['queue_age_peak_ms'], round(age * 1000))
                if age > self._segments[0][2]:
                    self.stats['age_dropped_bytes'] += len(self._pcm)
                    self.age_expired = True
                    self._pcm.clear()
                    self._segments.clear()
                    self._rate_state = None
            frame = bytes(self._pcm[:3840])
            del self._pcm[:3840]
            consumed = len(frame)
            while consumed and self._segments:
                segment = self._segments[0]
                count = min(consumed, segment[0])
                segment[0] -= count
                consumed -= count
                if segment[1] is not None:
                    segment[1] += count / 192000
                if not segment[0]:
                    self._segments.popleft()
            self.stats['underrun_frames'] += len(frame) < 3840
            self.stats['read_frames'] += 1
            self.stats['pulled_bytes'] += len(frame)
            if frame and audioop.rms(frame, 2) > 500:
                self._last_audible_at = time.monotonic()
            return frame.ljust(3840, b'\x00')

    @property
    def last_audible_at(self) -> float:
        with self._lock:
            return self._last_audible_at

    def clear(self) -> None:
        with self._lock:
            self.stats['cleared_bytes'] += len(self._pcm)
            self._pcm.clear()
            self._segments.clear()
            self._rate_state = None

    def consume_age_fault(self) -> bool:
        with self._lock:
            expired = self.age_expired
            self.age_expired = False
            return expired

    def cleanup(self) -> None:
        with self._lock:
            self._closed = True
            self.stats['cleared_bytes'] += len(self._pcm)
            self._pcm.clear()
            self._segments.clear()
            self.age_expired = False
            self._rate_state = None

    def is_opus(self) -> bool:
        return False


class DiscordLiveSession:
    def __init__(self, adapter, channel, cfg, notify):
        self.adapter, self.channel, self.cfg, self.notify = adapter, channel, cfg, notify
        self.source = PCMSource()
        self._transform_notice = None
        self.transform = create_output_transform(cfg.get('output_transform'), self.source.append,
            self.source.clear, emit_timed=self._converted_output, on_fallback=self._conversion_fallback)
        if cfg['backend'] == 'git_status':
            backend = ProjectStatusBackend(Path(cfg['project']))
        else:
            from gateway.live_agent import HermesBackend
            backend = HermesBackend.from_adapter(adapter, cfg)
        self.stream = LiveStream(backend, self.output,
                                 backend_timeout=None if cfg['backend'] == 'hermes' else cfg['backend_seconds'],
                                 model=cfg.get('model', 'gpt-live-1'), voice=cfg.get('voice', 'marin'))
        self.closed = False
        self.task = None
        self.startup: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self._startup_timed_out = False
        self.vc = None
        self.receiver = None
        self._voice_ws = None
        self._input = deque()
        self._input_bytes = 0
        self._input_lock = threading.Lock()
        self._overflow = False
        self._rate_state = None
        self.started = time.monotonic()
        self.last_activity = self.started
        self._provider_deadline = None
        self._speaking_until = 0.0
        self._quiet = False
        self._diagnostic_id = uuid.uuid4().hex[:12]
        self.stats = Counter()
        self.exit_reason = None
        self._playback_error = None
        self._playback_ended = False
        self._reported_closed = False
        self.presence = None
        self.presence_generation = None

    def diagnostics(self, phase: str) -> None:
        # A bounded local log at startup, every 5s, and close. No audio, text,
        # provider bodies, credentials, guild/user IDs or remote exception messages.
        with self.source._lock:
            playback = dict(self.source.stats)
        with self._input_lock:
            host = dict(self.stats)
        logger.info('Live diagnostics %s', json.dumps({
            'id': self._diagnostic_id, 'phase': phase,
            'elapsed_ms': round((time.monotonic() - self.started) * 1000),
            'reason': self.exit_reason, 'stream_reason': self.stream.exit_reason,
            'error_type': self.stream.error, 'playback_error_type': self._playback_error,
            'finalized': self.stream.finalized, 'host': host,
            'receiver': dict(self.receiver.live_stats) if self.receiver else {},
            'provider': dict(self.stream.events), 'stream': dict(self.stream.stats),
            'playback': playback, 'output_transform': self.transform.status(),
        }, sort_keys=True))

    def playback_finished(self, error) -> None:
        # Called on discord.py's AudioPlayer thread. Never log exception text.
        if not self.closed:
            self._playback_error = type(error).__name__ if error else None
            self._playback_ended = True

    def input(self, ssrc: int, pcm: bytes) -> None:
        # SocketReader thread: one bounded handoff, never one unbounded loop callback per packet.
        with self._input_lock:
            if self.closed:
                return
            self.stats['input_pcm_frames'] += 1
            self.stats['input_pcm_bytes'] += len(pcm)
            if self._input_bytes + len(pcm) > 192000:
                self.stats['input_overflow'] += 1
                self.stats['capture_dropped_bytes'] += len(pcm)
                self._overflow = True
                return
            self._input.append((ssrc, pcm))
            self._input_bytes += len(pcm)
            # Activity belongs to capture, not to a later drain of queued audio.
            # Match the pump's stereo->mono weighting and speech RMS threshold.
            if audioop.rms(audioop.tomono(pcm, 2, 0.5, 0.5), 2) > 500:
                self.last_activity = time.monotonic()

    def output(self, pcm: bytes) -> None:
        if not self._audience_safe():
            self.exit_reason = 'audience_changed'
            self.quiet()
            return
        if not self.closed and not self._quiet and time.monotonic() >= self._speaking_until:
            self.transform.append(pcm)
        else:
            self.stats['gate_dropped_bytes'] += len(pcm)

    def _converted_output(self, pcm, source_start, budget):
        if not self._audience_safe():
            self.exit_reason = 'audience_changed'
            self.quiet()
            return
        if not self.closed and not self._quiet and time.monotonic() >= self._speaking_until:
            self.source.append_timed(pcm, source_start, budget)
        else:
            self.stats['gate_dropped_bytes'] += len(pcm)

    def _conversion_fallback(self, reason):
        # At most one notice/task per session. Safe codes, no provider text.
        async def notice():
            try:
                await asyncio.wait_for(self.notify('Voice conversion unavailable (' + reason +
                    '); switched to native Marin at the next fresh audio chunk. Old queued audio was dropped, not replayed. '
                    'Use /live conversion status for state.'), 5)
            except Exception:
                logger.warning('Live conversion fallback notification unavailable')
        if not self.closed and self._transform_notice is None:
            self._transform_notice = asyncio.create_task(notice())

    def observe_activity(self, now: float, rms: int, *, captured: bool = True) -> None:
        if rms <= 500:
            return
        self.stats['voiced_mixed_frames'] += 1
        # Barge-in interrupts speech only. Backend cancellation is an explicit
        # /live cancel or session-close action, never inferred from speech RMS.
        self._speaking_until = now + 0.2
        if captured:
            with self._input_lock:
                self.last_activity = now
        self.transform.clear()

    def quiet(self) -> None:
        # No audio generation IDs / done boundary on Live's primary WebSocket.
        # A hard speech-cancel stays latched until an explicit new session.
        self._quiet = True
        self.transform.clear()

    def limit_reason(self, now: float) -> str | None:
        if self._provider_deadline is not None and now >= self._provider_deadline:
            return 'provider_expired'
        if self.cfg['session_seconds'] > 0 and now - self.started >= self.cfg['session_seconds']:
            return 'session_limit'
        # Only captured speech and PCM actually pulled by Discord count. Backend
        # tokens/results/pending work do not extend a mutual-silence deadline.
        with self._input_lock:
            last_activity = self.last_activity
        if now - max(last_activity, self.source.last_audible_at) >= self.cfg['inactivity_seconds']:
            return 'mutual_silence'
        return None

    def revoke(self):
        """Invalidate synchronously, before the owner awaits any teardown/locks."""
        if self.closed:
            return
        self.exit_reason = self.exit_reason or 'owner_revoked'
        self.closed = True
        self.transform.revoke()
        self.source.cleanup()
        if self._transform_notice is not None:
            self._transform_notice.cancel()
        if self.receiver:
            self.receiver.stop()
        for task in (self.startup, self.task):
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()

    def _audience_safe(self):
        if self.cfg.get('allow_listeners') is True:
            return self.cfg['operator_id'] in getattr(self.channel, 'voice_states', {})
        return self.cfg['backend'] != 'hermes' or private_audience(
            self.channel, self.cfg['operator_id'], getattr(self.adapter._client.user, 'id', None))

    def _require_owner(self):
        if (self.closed or self.adapter._disconnecting
                or self.adapter._live_voice_sessions.get(self.channel.guild.id) is not self
                or (self.presence is not None and not self.presence.valid(self.presence_generation))):
            raise asyncio.CancelledError

    def unregister(self):
        guild_id = self.channel.guild.id
        for mapping, value in ((self.adapter._live_voice_sessions, self),
                               (self.adapter._voice_clients, self.vc),
                               (self.adapter._voice_receivers, self.receiver)):
            if mapping.get(guild_id) is value:
                mapping.pop(guild_id, None)

    async def start(self, key: str):
        try:
            await self._start(key)
        except BaseException as exc:
            self.exit_reason = self.exit_reason or 'startup_error'
            self.stream.error = self.stream.error or type(exc).__name__
            self.diagnostics('startup_failed')
            raise
        finally:
            if self._startup_timed_out:
                # Keep the closed reservation until late native work drains. A
                # Discord disconnect changes guild state, not just a local registry.
                assert self._close_task is not None
                try:
                    await asyncio.shield(self._close_task)
                except Exception:
                    pass  # A failed first cleanup must not skip late resources.
                try:
                    await self._release_resources()
                finally:
                    self.unregister()

    def _require_provider_duration(self):
        # GPT-Live docs specify expires_at, but publish no numeric maximum.
        # Do not borrow the unrelated Realtime API's advertised 60-minute bound.
        expires = self.stream.expires_at
        if expires is None:
            return
        if (type(expires) not in (int, float) or not math.isfinite(expires)
                or expires <= time.time()):
            self.exit_reason = 'provider_duration_unsupported'
            raise ProviderDurationError('Provider expiry must be a finite future timestamp')
        # Freeze the wall-clock expiry onto the monotonic clock. Rechecking after
        # Discord's handshake may shorten this deadline, never extend it.
        now = time.monotonic()
        deadline = now + (expires - time.time())
        self._provider_deadline = (deadline if self._provider_deadline is None
                                   else min(self._provider_deadline, deadline))
        if self._provider_deadline <= now:
            self.exit_reason = 'provider_duration_unsupported'
            raise ProviderDurationError('Provider expiry elapsed before audio readiness')

    async def _start(self, key: str):
        from plugins.platforms.discord.adapter import VoiceReceiver
        self._require_owner()
        if not self._audience_safe():
            raise PermissionError('Engineering voice requires a private operator/bot audience; use the DM instead')
        if self.cfg.get('output_transform', {}).get('provider', 'none') != 'none':
            await self.notify('Voice conversion warming locally; not ready yet. No paid voice session has started.')
            self._require_owner()
        await self.transform.start()
        self._require_owner()
        if self.cfg['backend'] == 'hermes':
            self.stream.envelope = await self.stream.backend.prepare()
            self._require_owner()
        await self.stream.start(key)
        self.diagnostics('provider_ready')
        self._require_owner()
        self._require_provider_duration()
        session = self

        class OwnedVoiceClient(discord.VoiceClient):
            def __init__(self, client, channel):
                super().__init__(client, channel)
                # Connectable registers this instance BEFORE awaiting its handshake.
                session.vc = self
                # discord.py does not retain SPEAKING mappings. Subscribe before
                # connect() can receive the initial announcements; no UDP capture
                # starts until the handshake and owner check have completed.
                session.receiver = VoiceReceiver(self, allowed_user_ids={str(session.cfg['operator_id'])},
                                                  on_pcm=session.input)
                session.receiver._install_speaking_hook(self._connection)

            def cleanup(self):
                key, _ = self.channel._get_voice_client_key()
                if self.client._connection._get_voice_client(key) is self:
                    super().cleanup()

        self.vc = await self.channel.connect(timeout=15, reconnect=False, cls=OwnedVoiceClient)
        self._require_owner()
        # A slow Discord handshake can consume the provider's remaining lifetime.
        # An explicit local cap need not fit; whichever deadline comes first ends audio.
        self._require_provider_duration()
        self._voice_ws = self.vc._connection.ws
        self.adapter._voice_clients[self.channel.guild.id] = self.vc
        if self.receiver is None:
            self.receiver = VoiceReceiver(self.vc, allowed_user_ids={str(self.cfg['operator_id'])},
                                          on_pcm=self.input)
        self.receiver.start()
        self.adapter._voice_receivers[self.channel.guild.id] = self.receiver
        self.vc.play(self.source, after=self.playback_finished)
        self.started = self.last_activity = time.monotonic()
        self.diagnostics('discord_ready')
        self.task = asyncio.create_task(self._pump())
        local_limit = (f"Local session limit {self.cfg['session_seconds']} seconds"
                       if self.cfg['session_seconds'] else 'No local session cap')
        await self.notify('Live voice active; only the configured operator\'s mapped audio is sent to OpenAI. '
                          'Approvals and clarifications arrive in the DM. /live stop ends audio, not admitted work. '
                          f'{local_limit}; provider expiry and mutual silence still end audio. '
                          'No automatic rollover; billing applies even during silence.')

    def _drain_capture(self, pending, lag):
        """Bound the handoff before copying; discard discontinuities atomically."""
        with self._input_lock:
            pending_bytes = sum(map(len, pending.values()))
            reason = None
            if lag > 0.25:
                reason = 'capture_lag'
            elif self._overflow:
                reason = 'input_overflow'
            elif (pending_bytes + self._input_bytes > 192000
                    or len(pending.keys() | {ssrc for ssrc, _ in self._input}) > 16):
                reason = 'decoder_backlog'
            if reason:
                self.stats[reason + '_recoveries'] += 1
                self.stats['capture_lag_peak_ms'] = max(
                    self.stats['capture_lag_peak_ms'], round(max(0, lag) * 1000))
                self.stats['capture_dropped_bytes'] += self._input_bytes
                self.stats['pending_dropped_bytes'] += pending_bytes
                self._input.clear()
                pending.clear()
                self._overflow = False
                self._rate_state = None
            else:
                while self._input:
                    ssrc, pcm = self._input.popleft()
                    pending.setdefault(ssrc, bytearray()).extend(pcm)
            self._input_bytes = 0
        # Recovery is not speech and does not cancel admitted backend work.
        # last_activity / last_audible_at stay untouched.
        return reason is not None

    async def _pump(self):
        vc = self.vc
        assert vc is not None  # started only after Discord join
        pending = {}
        deadline = time.monotonic()
        next_report = deadline + 5
        stage = 'pump_error'
        try:
            while not self.closed:
                now = time.monotonic()
                if not self._audience_safe():
                    self.exit_reason = 'audience_changed'
                    self.quiet()
                    break
                if now >= next_report:
                    self.diagnostics('running')
                    next_report = now + 5
                if self.source.consume_age_fault() and hasattr(self.transform, 'fallback'):
                    self.transform.fallback('playback_media_age_limit')
                if self.stream.done.is_set():
                    self.exit_reason = self.stream.exit_reason or 'provider_done'
                    break
                if not vc.is_connected():
                    self.exit_reason = 'discord_disconnected'
                    break
                if vc.channel.id != self.channel.id or vc._connection.ws is not self._voice_ws:
                    self.exit_reason = 'discord_connection_replaced'
                    break
                if self._playback_ended:
                    self.exit_reason = 'playback_error' if self._playback_error else 'playback_ended'
                    break
                reason = self.limit_reason(now)
                if reason:
                    self.exit_reason = reason
                    break
                stage = 'decoder_backlog'
                if self._drain_capture(pending, now - deadline):
                    # Do not replay missed ticks. Resume on a fresh 20ms boundary.
                    deadline = now + 0.02
                    await asyncio.sleep(max(0, deadline - time.monotonic()))
                    continue
                stage = 'input_conversion_error'
                mixed = bytes(3840)
                for ssrc, buf in list(pending.items()):
                    frame = bytes(buf[:3840]).ljust(3840, b'\x00')
                    del buf[:3840]
                    mixed = audioop.add(mixed, frame, 2)
                    if not buf:
                        del pending[ssrc]
                mono = audioop.tomono(mixed, 2, 0.5, 0.5)
                pcm, self._rate_state = audioop.ratecv(mono, 2, 1, 48000, 24000, self._rate_state)
                # Mixing still gates/clears playback, but draining old speech
                # must not grant a new processing-time inactivity window.
                self.observe_activity(now, audioop.rms(pcm, 2), captured=False)
                stage = 'input_send_error'
                await self.stream.send_audio(pcm)
                deadline += 0.02
                await asyncio.sleep(max(0, deadline - time.monotonic()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.exit_reason = stage
            self.stream.error = type(exc).__name__
        finally:
            if not self.closed:
                await self.adapter.leave_voice_channel(self.channel.guild.id)
                await self.notify(f'Live voice stopped ({self.exit_reason}). ' + ('Final usage confirmed.' if self.stream.finalized
                                                          else 'Final usage unconfirmed.'))

    async def close(self):
        self.revoke()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(asyncio.current_task()))
        await asyncio.shield(self._close_task)

    async def _close(self, caller):
        try:
            if self.startup and self.startup is not caller:
                done, _ = await asyncio.wait({self.startup}, timeout=5)
                self._startup_timed_out = not done
                self.startup.add_done_callback(
                    lambda task: None if task.cancelled() else task.exception())
            if self.task and self.task is not caller:
                await asyncio.gather(self.task, return_exceptions=True)
            await self._release_resources()
        except Exception as exc:
            self.stream.error = self.stream.error or type(exc).__name__
            self.stats['cleanup_errors'] += 1
            raise
        finally:
            if not self._startup_timed_out:
                self.unregister()
            if not self._reported_closed:
                self._reported_closed = True
                self.diagnostics('closed')

    async def _release_resources(self):
        try:
            await self.transform.close()
        finally:
            # Converter failure must never strand the paid provider or Discord.
            await self._release_host_resources()

    async def _release_host_resources(self):
        if self._transform_notice is not None:
            await asyncio.gather(self._transform_notice, return_exceptions=True)
        try:
            await self.stream.close()
        finally:
            try:
                if self.vc:
                    self.vc.stop()
                    disconnect = asyncio.create_task(self.vc.disconnect(force=True))
                    try:
                        # wait_for waits for cancellation acknowledgement, which
                        # discord.py may defer while waiting for a gateway event.
                        done, _ = await asyncio.wait({disconnect}, timeout=5)
                        if done:
                            await disconnect
                        else:
                            raise TimeoutError('Discord voice disconnect timed out')
                    finally:
                        if not disconnect.done():
                            disconnect.cancel()
                        disconnect.add_done_callback(
                            lambda task: None if task.cancelled() else task.exception())
                        # Also unregister on disconnect failure. OwnedVoiceClient's
                        # identity guard makes delayed native cleanup safe too.
                        self.vc.cleanup()
            finally:
                with self._input_lock:
                    self._input.clear()
                    self._input_bytes = 0


def validate_config(raw: dict) -> dict:
    if raw.get('enabled') is not True:
        raise ValueError('Live voice is disabled')
    cfg = dict(raw)
    cfg['output_transform'] = validate_transform(cfg.get('output_transform'))
    cfg['allow_listeners'] = cfg.get('allow_listeners', False)
    if type(cfg['allow_listeners']) is not bool:
        raise ValueError('live_voice.allow_listeners must be a boolean')
    cfg['autojoin'] = cfg.get('autojoin', False)
    if type(cfg['autojoin']) is not bool:
        raise ValueError('live_voice.autojoin must be a boolean')
    for name in ('operator_id', 'dm_channel_id', 'guild_id', 'voice_channel_id'):
        if not str(cfg.get(name, '')).isdigit() or int(cfg[name]) <= 0:
            raise ValueError(f'live_voice.{name} is required')
        cfg[name] = int(cfg[name])
    additional = cfg.get('additional_channels', [])
    if not isinstance(additional, list):
        raise ValueError('live_voice.additional_channels must be a list')
    bindings, guilds, channels = [], {cfg['guild_id']}, {cfg['voice_channel_id']}
    for binding in additional:
        if not isinstance(binding, dict) or set(binding) != {'guild_id', 'voice_channel_id'}:
            raise ValueError('live_voice.additional_channels accepts only guild/channel bindings')
        if any(not str(v).isdigit() or int(v) <= 0 for v in binding.values()):
            raise ValueError('Invalid live_voice.additional_channels identity')
        binding = {key: int(value) for key, value in binding.items()}
        if binding['guild_id'] in guilds or binding['voice_channel_id'] in channels:
            raise ValueError('live_voice.additional_channels requires distinct guilds and channels')
        guilds.add(binding['guild_id'])
        channels.add(binding['voice_channel_id'])
        bindings.append(binding)
    cfg['additional_channels'] = bindings
    cfg['session_seconds'] = cfg.get('session_seconds', 0)
    if type(cfg['session_seconds']) is not int or cfg['session_seconds'] < 0:
        raise ValueError('Invalid live_voice.session_seconds')
    for name, default, maximum in (('inactivity_seconds', 60, 120),):
        value = cfg.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
            raise ValueError(f'Invalid live_voice.{name}')
        cfg[name] = value
    cfg['thinking_initial_delay'] = int(cfg.get('thinking_initial_delay', 15))
    if cfg['thinking_initial_delay'] < 1:
        raise ValueError('Invalid live_voice.thinking_initial_delay')
    cfg['thinking_repeat_interval'] = int(cfg.get('thinking_repeat_interval', 60))
    if cfg['thinking_repeat_interval'] < 5:
        raise ValueError('Invalid live_voice.thinking_repeat_interval')
    cfg['backend'] = cfg.get('backend', 'hermes')
    if cfg['backend'] not in ('hermes', 'git_status'):
        raise ValueError('live_voice.backend must be hermes or explicit git_status')
    cfg['backend_seconds'] = cfg.get('backend_seconds', 40 if cfg['backend'] == 'hermes' else 5)
    if type(cfg['backend_seconds']) is not int or not 1 <= cfg['backend_seconds'] <= 45:
        raise ValueError('Invalid live_voice.backend_seconds')
    if cfg['backend'] == 'git_status' and not cfg.get('project'):
        raise ValueError('live_voice.project is required for git_status')
    # Legacy read_only_docs no longer controls Hermes. Its owning profile does.
    cfg.pop('read_only_docs', None)
    return cfg


class LivePresence:
    """One occupancy latch, owned by the authenticated Discord event stream.

    Replayed/startup cache never starts paid work. Once occupied, terminal stops
    stay stopped until an outside->inside transition or explicit DM start.
    """
    def __init__(self, cfg):
        self.cfg = cfg
        self.ready = False
        self.present = False
        self.generation = 0

    def valid(self, generation):
        return self.ready and self.present and generation == self.generation

    def seed(self, client):
        self.ready = self.present = False
        self.generation += 1
        guild = client.get_guild(self.cfg['guild_id'])
        if guild is None:
            raise ValueError('Configured guild is unavailable')
        channel = guild.get_channel(self.cfg['voice_channel_id'])
        if channel is None or channel.type != discord.ChannelType.voice:
            raise ValueError('Configured voice channel is unavailable')
        # Public native API explicitly supports an unavailable Member cache;
        # Guild has no voice_states property. No privileged members intent needed.
        self.present = self.cfg['operator_id'] in channel.voice_states
        self.ready = True


async def _join_requested_existing_call(adapter, presence):
    """One explicit operator-requested join, not replay-driven autojoin."""
    notify = adapter._client.get_partial_messageable(presence.cfg['dm_channel_id']).send
    try:
        await start_session(adapter, presence.cfg, notify, presence, presence.generation)
    except Exception as exc:
        logger.warning('Requested existing call start failed (%s)', type(exc).__name__)
        await notify('Requested voice join failed; use /live start to retry.')


def presence_ready(adapter):
    raw = adapter.config.extra.get('live_voice', {})
    if raw.get('enabled') is not True or raw.get('autojoin') is not True:
        return
    stage = 'validate_config'
    try:
        presence = getattr(adapter, '_live_presence', None)
        if presence is None:
            presence = adapter._live_presence = LivePresence(validate_config(raw))
        if not hasattr(adapter, '_live_presences'):
            cfg = presence.cfg
            adapter._live_presences = [presence] + [LivePresence(cfg | binding)
                for binding in cfg.get('additional_channels', [])]
        join_once = raw.pop('join_existing_once', False) is True
        stage = 'seed'
        # Fail closed if any explicit binding cannot be seeded. No guild scan.
        for item in live_presences(adapter):
            item.ready = item.present = False
            item.generation += 1
        for item in live_presences(adapter):
            item.seed(adapter._client)
        for item in live_presences(adapter):
            if join_once and item.present:
                asyncio.create_task(_join_requested_existing_call(adapter, item))
                join_once = False
            logger.info('Live presence initialized: guild=%s channel=%s operator_configured=True '
                        'present=%s ready=True', item.cfg['guild_id'],
                        item.cfg['voice_channel_id'], item.present)
    except Exception as exc:
        # Report code location, not arbitrary exception text/locals (may be private).
        for item in live_presences(adapter):
            item.ready = item.present = False
        tb = exc.__traceback__
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        site = (f'{Path(tb.tb_frame.f_code.co_filename).name}:'
                f'{tb.tb_frame.f_code.co_name}:{tb.tb_lineno}') if tb else 'unknown'
        logger.warning('Live presence unavailable: stage=%s error=%s site=%s discord_py=%s',
                       stage, type(exc).__name__, site, discord.__version__)


def live_presences(adapter):
    primary = getattr(adapter, '_live_presence', None)
    return getattr(adapter, '_live_presences', [primary] if primary is not None else [])


async def presence_disconnected(adapter):
    for presence in live_presences(adapter):
        presence.ready = False
        presence.generation += 1
    # Revoke before awaiting: resumed/replayed events cannot race paid startup.
    sessions = list(adapter._live_voice_sessions.values())
    for session in sessions:
        session.exit_reason = session.exit_reason or 'discord_gateway_disconnected'
        session.revoke()
    await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)


async def presence_resumed(adapter):
    presence_ready(adapter)


async def handle_voice_state_update(adapter, member, before, after):
    guild_id = member.guild.id
    session = adapter._live_voice_sessions.get(guild_id)
    before_id = getattr(before.channel, 'id', None)
    after_id = getattr(after.channel, 'id', None)
    if (session is not None and session.cfg['backend'] == 'hermes'
            and session.cfg.get('allow_listeners') is not True
            and member.id not in (session.cfg['operator_id'], getattr(adapter._client.user, 'id', None))
            and after_id == session.channel.id):
        session.exit_reason = 'audience_changed'
        session.quiet()
        await session.close()
        await session.notify('Engineering voice stopped because another account joined. Authorized work continues privately in the DM.')
        return
    if member.id == getattr(adapter._client.user, 'id', None):
        # Our own disconnect emits this event while the closing reservation lives.
        # Preserve the initiating reason; only an active call can be moved/kicked.
        if (session is not None and not session.closed
                and before_id != after_id and after_id != session.channel.id):
            session.exit_reason = 'bot_moved_or_kicked'
            await session.close()
        return
    presence = next((p for p in live_presences(adapter)
                     if p.cfg['guild_id'] == guild_id
                     and p.cfg['voice_channel_id'] in (before_id, after_id)), None)
    if (presence is None or not presence.ready or adapter._disconnecting
            or member.bot or member.id != presence.cfg['operator_id']
            or guild_id != presence.cfg['guild_id'] or before_id == after_id):
        return
    entered = after_id == presence.cfg['voice_channel_id']
    if entered == presence.present:
        return  # duplicates and transitions wholly outside the designated channel
    presence.present = entered
    presence.generation += 1
    if not entered:
        if session is not None:
            session.exit_reason = 'operator_left'
            await session.close()
            await session.notify('Live voice stopped (operator_left). ' + (
                'Final usage confirmed.' if session.stream.finalized else 'Final usage unconfirmed.'))
        return
    notify = adapter._client.get_partial_messageable(presence.cfg['dm_channel_id']).send
    try:
        await start_session(adapter, presence.cfg, notify, presence, presence.generation)
    except Exception as exc:
        logger.warning('Live presence start failed (%s: %s)', type(exc).__name__, exc, exc_info=True)
        await notify(f'Live voice unavailable ({type(exc).__name__}: {exc}). Leave/re-enter or use /live start to retry.')


async def start_session(adapter, cfg, notify, presence=None, generation=None):
    """Single normal factory for explicit DM and trusted presence starts."""
    if presence is not None and not presence.valid(generation):
        return
    # One Live call/converter across the explicit bindings, not one per guild.
    # Revoke before awaiting the lock so a stalled old startup cannot win a hop.
    for previous in list(adapter._live_voice_sessions.values()):
        if previous.cfg['guild_id'] != cfg['guild_id']:
            previous.exit_reason = previous.exit_reason or 'operator_switched_channel'
            previous.revoke()
    for other in live_presences(adapter):
        if other is not presence and other.cfg['guild_id'] != cfg['guild_id']:
            other.present = False
            other.generation += 1
    # Close revoked cross-guild startups OUTSIDE the admission lock: startup
    # may suppress cancellation, so its existing bounded close must run now.
    # A retained late-startup reservation rejects retries rather than queueing
    # them behind a lock that only that same native startup can release.
    for previous in list(adapter._live_voice_sessions.values()):
        if previous._startup_timed_out:
            raise ValueError('Previous Live startup is still closing')
        if previous.cfg['guild_id'] != cfg['guild_id']:
            await previous.close()
            if previous._startup_timed_out:
                raise ValueError('Previous Live startup is still closing')
    lock = getattr(adapter, '_live_start_lock', None)
    if lock is None:
        lock = adapter._live_start_lock = asyncio.Lock()
    async with lock:
        if presence is not None and not presence.valid(generation):
            return
        for previous in list(adapter._live_voice_sessions.values()):
            if previous.closed or previous.cfg['guild_id'] != cfg['guild_id']:
                await previous.close()
        # Timed-out native startup keeps a reservation until late cleanup drains.
        if any(g != cfg['guild_id'] for g in adapter._live_voice_sessions):
            raise ValueError('Previous Live call is still closing')
        if any(g != cfg['guild_id'] for g in adapter._voice_clients):
            raise ValueError('Another voice connection is active')
        await _start_session(adapter, cfg, notify, presence, generation)


async def _start_session(adapter, cfg, notify, presence=None, generation=None):
    cfg = dict(cfg)
    cfg['output_transform'] = dict(cfg.get('output_transform', {}))
    cfg['output_transform']['provider'] = getattr(adapter, '_live_conversion_provider',
        cfg['output_transform'].get('provider', 'none'))
    guild_id = cfg['guild_id']
    sessions = adapter._live_voice_sessions
    async with adapter._voice_locks.setdefault(guild_id, asyncio.Lock()):
        previous = sessions.get(guild_id)
        if previous is not None and previous.closed:
            await previous.close()
        if presence is not None and not presence.valid(generation):
            return  # the initiating occupancy was revoked while waiting for lock
        if adapter._disconnecting or adapter._client is None:
            raise ValueError('Discord owner is disconnecting')
        if guild_id in adapter._voice_clients or guild_id in sessions:
            raise ValueError('Voice is already active; stop it before starting Live')
        channel = adapter._client.get_channel(cfg['voice_channel_id'])
        if channel is None or channel.guild.id != guild_id or channel.type != discord.ChannelType.voice:
            raise ValueError('Configured voice channel is unavailable')
        key = get_scoped_secret('VOICE_TOOLS_OPENAI_KEY') or get_scoped_secret('OPENAI_API_KEY')
        if not key:
            raise ValueError('No scoped OpenAI audio key configured')
        session = DiscordLiveSession(adapter, channel, cfg, notify)
        session.presence, session.presence_generation = presence, generation
        sessions[guild_id] = session
        session.startup = asyncio.create_task(session.start(key))
    try:
        await session.startup
    except BaseException as exc:
        caller = asyncio.current_task()
        assert caller is not None
        owner_revoked = session.closed and not caller.cancelling()
        try:
            await session.close()
        except Exception:
            pass  # Cleanup must not replace the original failure/cancellation.
        if isinstance(exc, asyncio.CancelledError) and owner_revoked:
            return
        raise


async def handle_message(adapter, message) -> bool:
    """Called after normal Discord admission, before model/legacy voice dispatch."""
    text = message.content.strip()
    if text.split(' ', 1)[0] != '/live':
        return False
    raw = adapter.config.extra.get('live_voice', {})
    # Never let a model-provided identity authorize joining/capture.
    if (message.guild is not None or message.author.bot
            or str(message.author.id) != str(raw.get('operator_id', ''))
            or str(message.channel.id) != str(raw.get('dm_channel_id', ''))):
        return True
    try:
        cfg = validate_config(raw)
        # Commands address the sole current call, not the legacy primary guild.
        active = next(iter(adapter._live_voice_sessions.values()), None)
        selected = next((p for p in live_presences(adapter) if p.ready and p.present), None)
        if active is not None:
            cfg = active.cfg
        elif selected is not None:
            cfg = selected.cfg
        guild_id = cfg['guild_id']
        sessions = adapter._live_voice_sessions
        if text.startswith('/live conversion '):
            choice = text.removeprefix('/live conversion ')
            if choice not in ('on', 'off', 'status'):
                raise ValueError('Unknown conversion control')
            session = sessions.get(guild_id)
            if choice != 'status':
                selection = 'seed_vc' if choice == 'on' else 'none'
                validate_transform(cfg['output_transform'] | {'provider': selection})
                adapter._live_conversion_provider = selection
                if choice == 'off' and session is not None and hasattr(session.transform, 'fallback'):
                    session.transform.fallback('operator_off')
            selected = getattr(adapter, '_live_conversion_provider', cfg['output_transform']['provider'])
            status = session.transform.status() if session is not None else {
                'actual': 'none', 'state': 'idle', 'queue_age_seconds': 0.0, 'error': None}
            await message.channel.send(f"Voice conversion selected={selected}; actual={status['actual']}; "
                f"state={status['state']}; queue_age={status['queue_age_seconds']:.3f}s; "
                f"error={status.get('error') or 'none'}. On applies next session; off revokes queued conversion now. "
                'Selection lasts until gateway restart; no paid session started by this command.')
            return True
        if text == '/live stop':
            for presence in live_presences(adapter):
                presence.generation += 1
            session = sessions.get(guild_id)
            if session is not None:
                session.exit_reason = session.exit_reason or 'operator_stop'
            await adapter.leave_voice_channel(guild_id)
            detail = '' if session is None else (
                f' ({session.exit_reason}; ' +
                ('final usage confirmed' if session.stream.finalized else 'final usage unconfirmed') + ')')
            await message.channel.send(f'Live voice stopped{detail}.')
            return True
        if text in ('/live quiet', '/live cancel'):
            session = sessions.get(guild_id)
            if session is not None:
                if text == '/live quiet':
                    session.quiet()
                    await message.channel.send('Playback muted until a new session; backend work is unchanged.')
                else:
                    session.transform.clear()
                    cancel = getattr(session.stream.backend, 'cancel', None)
                    if cancel is not None:
                        await cancel()
                    session.stream.supersede()
                    await message.channel.send('Voice request cancellation requested through normal gateway semantics. '
                                               'Committed side effects are not undone. Queued playback cleared.')
            return True
        if text != '/live start':
            await message.channel.send('Use /live start, /live stop, /live quiet (mute speech), /live cancel (cancel backend and clear queued speech), or /live conversion on|off|status. Starting incurs OpenAI session billing.')
            return True
        presence = selected if cfg['autojoin'] else None
        if cfg['autojoin'] and (presence is None or not presence.ready or not presence.present):
            raise ValueError('Configured operator must be present after Discord is ready')
        await start_session(adapter, cfg, message.channel.send, presence,
                            presence.generation if presence else None)
    except Exception as exc:
        # Do not reflect arbitrary provider exceptions (may carry input or authentication headers).
        await message.channel.send(f'Live voice unavailable ({type(exc).__name__}). Check local preflight configuration.')
    return True


def main() -> int:
    """Offline preflight only. Paid joins require admitted DM or opt-in trusted presence."""
    import argparse
    import ctypes.util
    import importlib.metadata
    import json
    import yaml
    from gateway.config import PlatformConfig

    from tools.voice_live_stream import BackendRequest

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument('--preflight', type=Path, required=True, help='non-secret YAML settings file')
    args = parser.parse_args()
    try:
        settings = yaml.safe_load(args.preflight.read_text(encoding='utf-8'))
        platform = PlatformConfig.from_dict(settings['platforms']['discord'])
        cfg = validate_config(platform.extra['live_voice'])
        # The installed Linux fork loads Opus inline during Discord connect.
        # Offline preflight must exercise the codec without connecting a bot.
        if not discord.opus.is_loaded():
            opus_path = ctypes.util.find_library('opus')
            if opus_path:
                discord.opus.load_opus(opus_path)
        if not discord.opus.is_loaded():
            raise RuntimeError('Opus unavailable')
        if cfg['backend'] == 'git_status':
            backend = ProjectStatusBackend(Path(cfg['project']))
            result = asyncio.run(backend.run(BackendRequest('preflight', 0, 'project status'), asyncio.Event()))
            detail = {'project_status': result.status, 'detail': result.content}
            okay = result.status == 'completed'
        else:
            # Hermes work belongs to the existing gateway conversation. Offline
            # validation cannot prove its authenticated route or run an agent.
            okay = True
            detail = {
                'execution_owner': 'originating_gateway_conversation',
                'ingress': 'normal_adapter_turn_receipt',
                'gateway_route': 'validated at runtime before paid audio starts',
                'agent_reasoning_tested': False,
            }
        print(json.dumps({
            **detail, 'backend': cfg['backend'],
            'session_seconds': cfg['session_seconds'], 'inactivity_seconds': cfg['inactivity_seconds'],
            'discord_py': importlib.metadata.version('discord.py'),
            'websockets': importlib.metadata.version('websockets'), 'opus_loaded': True,
            'api_tested': False, 'discord_listened': False,
            'credentials_and_channel_permissions': 'not checked',
        }))
        return 0 if okay else 1
    except Exception as exc:
        print(json.dumps({'preflight': 'failed', 'error_type': type(exc).__name__,
                          'api_tested': False, 'discord_listened': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
