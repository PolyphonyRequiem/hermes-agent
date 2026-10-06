"""Call-owned FIFO over the normal gateway agent and platform delivery."""
from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
import uuid

from agent.redact import redact_sensitive_text
from gateway.platforms.base import MessageEvent, MessageType
from tools.voice_live_stream import BackendRequest, BackendResult, HostEnvelope


class HermesBackend:
    @classmethod
    def from_adapter(cls, adapter, cfg):
        return cls(adapter, cfg)

    def __init__(self, adapter, cfg):
        self.adapter = adapter
        self.home = Path(adapter._live_profile_home).resolve(strict=True)
        self.operator_id = str(cfg['operator_id'])
        self.chat_id = str(cfg['dm_channel_id'])
        self.timeout = float(cfg['backend_seconds'])
        self.thinking_initial_delay = int(cfg.get('thinking_initial_delay', 15))
        self.thinking_repeat_interval = int(cfg.get('thinking_repeat_interval', 60))
        self.backend_model = cfg.get('backend_model')
        self.backend_reasoning_effort = cfg.get('backend_reasoning_effort')
        if self.backend_model is not None and (
                not isinstance(self.backend_model, str) or not self.backend_model.strip()):
            raise ValueError('Invalid live_voice.backend_model')
        if self.backend_reasoning_effort is not None and self.backend_reasoning_effort not in (
                'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
            raise ValueError('Invalid live_voice.backend_reasoning_effort')
        self._runtime_initialized = False
        self.session_id = 'live-' + uuid.uuid4().hex
        self.envelope = None
        self._closed = False
        self._seen = set()
        self._queue = deque()
        self._worker = None
        self._receipts = []
        self.followup = None
        self._topics = None

    async def select_followup(self, topic_key):
        self._source()
        from gateway.live_followup_topics import FollowupTopics, HOME_CHANNEL_ID, HOME_GUILD_ID
        runner = self.adapter.gateway_runner
        target = self.adapter.build_source(str(HOME_CHANNEL_ID), user_id=self.operator_id,
            chat_type='group')
        if (target.profile_route_rejected
                or Path(runner._resolve_profile_home_for_source(target)).resolve() != self.home
                or not runner._is_user_authorized(target)):
            raise PermissionError('Home profile route unavailable or unauthorized')
        if self._topics is None:
            managers = getattr(self.adapter, '_live_followup_managers', None)
            if managers is None:
                managers = self.adapter._live_followup_managers = {}
            key = (str(self.home), self.operator_id)
            self._topics = managers.setdefault(key, FollowupTopics(
                self.adapter._client, self.home, self.operator_id))
        route = await self._topics.select(topic_key)
        route['scope_id'] = str(HOME_GUILD_ID)
        self.followup.route = route
        return dict(route)

    def _source(self):
        source = self.adapter.build_source(self.chat_id, user_id=self.operator_id, chat_type='dm')
        runner = getattr(self.adapter, 'gateway_runner', None)
        if (runner is None or not self.adapter._message_handler
                or source.profile_route_rejected
                or Path(runner._resolve_profile_home_for_source(source)).resolve() != self.home
                or not runner._is_user_authorized(source)):
            raise PermissionError('Owning gateway route unavailable or unauthorized')
        source.conversation_id = self.session_id
        return source

    async def prepare(self):
        if self._closed:
            raise RuntimeError('Live backend is detached')
        source = self._source()
        if self.followup is None:
            from gateway.live_followup_context import CallFollowup
            self.followup = CallFollowup(self)
        key = self.adapter.gateway_runner._session_key_for_source(source)
        if not self._runtime_initialized:
            # Use the normal per-conversation override path, never profile globals.
            runner = self.adapter.gateway_runner
            if self.backend_model is not None:
                override = {'model': self.backend_model}
                if hasattr(self.adapter, 'config') and hasattr(self.adapter.config, 'model'):
                    provider = getattr(self.adapter.config.model, 'provider', None)
                    if provider:
                        override['provider'] = provider
                runner._session_state(key).conversation.model_override = override
            if self.backend_reasoning_effort is not None:
                runner._set_session_reasoning_override(key, {
                    'enabled': self.backend_reasoning_effort != 'none',
                    'effort': self.backend_reasoning_effort})
            self._runtime_initialized = True
        if not hasattr(self.adapter, '_live_call_sources'):
            self.adapter._live_call_sources = {}
        self.adapter._live_call_sources[key] = source
        instructions_content = None
        instructions_file = self.home / 'live_voice_instructions.md'
        if instructions_file.is_file():
            try:
                instructions_content = instructions_file.read_text(encoding='utf-8').strip()
            except Exception:
                pass

        if self.envelope is None:
            if instructions_content:
                self.envelope = HostEnvelope(
                    identity=self.home.name.replace('-', ' '),
                    context=instructions_content,
                    capabilities=''
                )
            else:
                self.envelope = HostEnvelope(
                    identity=self.home.name.replace('-', ' ')[:80],
                    context='This call has its own fresh engineering conversation. Within-call context '
                        'belongs to the backend, not to an ordinary DM conversation.',
                    capabilities='The same configured engineering agent, skills, memory and enabled tools. '
                        'Normal security and confirmations apply. Voice approval is not supported by this '
                        'bridge: announce required approval and ask the operator to inspect and answer '
                        'the exact prompt in the DM. Never infer approval from speech. '
                        'Hangup detaches audio, not already admitted work.')
        return self.envelope

    async def _drain(self):
        try:
            while self._queue:
                event, receipt = self._queue.popleft()
                if receipt.cancelled:
                    receipt.finish(status='cancelled')
                    continue
                try:
                    self._source()  # revalidate route and auth before each turn
                    receipt.started = True
                    from gateway.live_followup_context import LIVE_FOLLOWUP
                    token = LIVE_FOLLOWUP.set(self.followup)
                    try:
                        text = await self.adapter.execute_owned_turn(event)
                    finally:
                        LIVE_FOLLOWUP.reset(token)
                    receipt.finish(text)
                except asyncio.CancelledError:
                    receipt.finish(status='cancelled')
                    raise
                except PermissionError:
                    receipt.finish('Engineering route unavailable or unauthorized.', status='denied')
                except Exception:
                    receipt.finish('Gateway execution failed; completion is not confirmed.', status='failed')
        finally:
            while self._queue:
                _, receipt = self._queue.popleft()
                receipt.finish(status='cancelled')
            if self._closed:
                self._release_source()

    def _release_source(self):
        sources = getattr(self.adapter, '_live_call_sources', {})
        for key, source in list(sources.items()):
            if source.conversation_id == self.session_id:
                sources.pop(key, None)

    async def run(self, request: BackendRequest, cancelled: asyncio.Event) -> BackendResult:
        if self._closed or cancelled.is_set():
            return BackendResult('detached', 'No new work admitted. Already admitted work may continue.')
        if request.request_id in self._seen:
            return BackendResult('duplicate', 'Already admitted; no new action ran.')
        if not request.transcript.strip():
            return BackendResult('ignored', 'No fresh authenticated user speech to act on.')
        try:
            await self.prepare()
            source = self._source()
        except PermissionError:
            return BackendResult('denied', 'Engineering route unavailable or unauthorized. No work admitted.')
        if len(self._queue) >= 32:
            return BackendResult('rejected', 'This call has too many pending turns. No work admitted.')
        self._seen.add(request.request_id)
        event = MessageEvent(text='[Spoken user request via Discord transport]\n' + request.transcript,
            source=source, message_type=MessageType.TEXT, allow_gateway_control=False,
            metadata={'input_origin':'discord_live', 'live_request_id':self.session_id + ':' + request.request_id},
            channel_prompt=self.adapter._resolve_channel_prompt(self.chat_id))
        from gateway.turn_receipt import TurnReceipt
        receipt = TurnReceipt()
        receipt.admitted = True
        self._receipts.append(receipt)
        self._queue.append((event, receipt))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain())
            self.adapter._background_tasks.add(self._worker)
            self._worker.add_done_callback(self.adapter._background_tasks.discard)
        try:
            text = await asyncio.wait_for(asyncio.shield(receipt.future), self.timeout)
        except TimeoutError:
            from tools.approval import has_blocking_approval
            key = self.adapter.gateway_runner._session_key_for_source(source)
            if has_blocking_approval(key):
                return BackendResult('pending', 'Approval is required for this call’s pending operation. '
                    'Voice approval is unsupported: inspect the exact command in the DM and use its buttons '
                    'or reply to that prompt with /approve or /deny. Work has NOT been approved.')
            return BackendResult('pending', 'Work or a question is pending in this call’s independent session. '
                'Audio timeout did not cancel it. Check the exact control prompt in the DM.')
        from agent.secret_scope import build_profile_secret_scope
        safe = str(text)
        for value in build_profile_secret_scope(self.home).values():
            if isinstance(value, str) and len(value) >= 8:
                safe = safe.replace(value, '[redacted]')
        safe = redact_sensitive_text(safe, force=True, redact_url_credentials=True)
        return BackendResult(receipt.status, safe[:1000])

    async def wait_idle(self):
        if self._worker is not None:
            await asyncio.shield(self._worker)

    async def close(self):
        self._closed = True
        if self._worker is None or self._worker.done():
            self._release_source()

    async def cancel(self):
        # Explicit cancellation only. The normal gateway stop cancels pending
        # approvals, tools and background work for THIS conversation key.
        self._closed = True
        for _, receipt in self._queue:
            receipt.cancelled = True
        source = self._source()
        key = self.adapter.gateway_runner._session_key_for_source(source)
        # Stop also wakes tool threads waiting for human input. Reject only
        # this call's concrete pending operations using the normal resolvers.
        from tools.approval import resolve_gateway_approval
        from tools.clarify_gateway import clear_session
        resolve_gateway_approval(key, 'deny', resolve_all=True)
        clear_session(key)
        if any(r.started and not r.future.done() for r in self._receipts):
            await self.adapter.handle_message(MessageEvent(text='/stop', source=source))
        if self._worker is None or self._worker.done():
            self._release_source()
