"""Call-owned explicit result routing; identity and visual controls stay in DM."""
from contextvars import ContextVar
import asyncio
import json

LIVE_FOLLOWUP: ContextVar = ContextVar('live_followup', default=None)
TOOL_NAME = 'select_followup_topic'
SCHEMA = {'name': TOOL_NAME, 'description':
    'Explicitly select a stable work-topic key before starting asynchronous or durable work. '
    'Results go to its Home thread, never DM. Do not select for ordinary voice conversation. '
    'Selection persists for this call until explicitly changed; approvals remain in DM.',
    'parameters': {'type': 'object', 'properties': {'topic_key': {'type': 'string'}},
                   'required': ['topic_key'], 'additionalProperties': False}}


class CallFollowup:
    def __init__(self, backend):
        self.backend = backend
        self.loop = asyncio.get_running_loop()
        self.route = None

    def select(self, topic_key):
        try:
            if asyncio.get_running_loop() is self.loop:
                raise RuntimeError('Topic selection must run in the agent tool thread')
        except RuntimeError as exc:
            if str(exc) != 'no running event loop':
                raise
        return asyncio.run_coroutine_threadsafe(
            self.backend.select_followup(topic_key), self.loop).result(timeout=60)

    def delivery_env(self, name):
        route = self.route or {}
        return {
            'HERMES_SESSION_CHAT_ID': route.get('chat_id', ''),
            'HERMES_SESSION_THREAD_ID': route.get('thread_id', ''),
            'HERMES_SESSION_SCOPE_ID': route.get('scope_id', ''),
            'HERMES_SESSION_CHAT_TYPE': 'group' if route else '',
            'HERMES_SESSION_CHAT_NAME': 'Home work topic' if route else '',
        }.get(name)

    async def send(self, content, **kwargs):
        from gateway.platforms.base import SendResult
        route = self.route
        if not route:
            return SendResult(success=True)
        # Revalidate exact destination. Never retry in DM or a new thread.
        await self.backend._topics.validate(route['thread_id'])
        result = await self.backend.adapter.send(route['chat_id'], content,
            metadata={'thread_id': route['thread_id']})
        if not result.success:
            raise RuntimeError('Selected follow-up delivery failed')
        return result


class ResultAdapter:
    """Only text result methods are redirected; approval/clarify stay native."""
    def __init__(self, adapter, context):
        self._adapter, self._context = adapter, context

    def __getattr__(self, name):
        return getattr(self._adapter, name)

    async def send(self, chat_id, content, **kwargs):
        return await self._context.send(content)

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        return await self._context.send(content)

    async def send_exec_approval(self, *args, **kwargs):
        return await self._adapter.send_exec_approval(*args, **kwargs)

    async def send_clarify(self, *args, **kwargs):
        return await self._adapter.send_clarify(*args, **kwargs)


def admission_error(name, args):
    context = LIVE_FOLLOWUP.get()
    if context is None:
        return None
    durable = ((name == 'delegate_task' and args.get('action', 'spawn') == 'spawn') or name == 'kanban_create'
        or (name == 'terminal' and args.get('background'))
        or (name == 'cronjob' and args.get('action') in ('create', 'update', 'edit', 'run', 'resume')))
    if durable and not context.route:
        return 'Select a follow-up topic before async/durable work; DM result delivery is forbidden.'
    if durable:
        # These operations capture the selected origin via get_session_env.
        # Do not let an explicit alternate delivery override that binding.
        if any(args.get(k) not in (None, '', 'origin') for k in ('deliver', 'delivery', 'origin')):
            return 'Live work must use the selected topic origin, not an alternate delivery.'
    return None


def select_handler(args, **kwargs):
    context = LIVE_FOLLOWUP.get()
    from agent.delegation_context import is_delegated_child_context
    if context is None or is_delegated_child_context():
        return json.dumps({'error': 'Topic selection is only available in an authenticated live call'})
    try:
        from hermes_constants import get_hermes_home
        if get_hermes_home().resolve() != context.backend.home:
            raise PermissionError('Owning profile mismatch')
        return json.dumps({'success': True, 'origin': context.select(args.get('topic_key'))})
    except Exception as exc:
        return json.dumps({'error': 'Topic selection failed', 'error_type': type(exc).__name__})


async def deliver_detached_result(runner, text, event):
    """Deliver a persisted Live delegation to its immutable topic, never DM."""
    from pathlib import Path
    from gateway.config import Platform
    from gateway.live_followup_topics import FollowupTopics, HOME_CHANNEL_ID, HOME_GUILD_ID
    from agent.redact import redact_sensitive_text
    from agent.secret_scope import build_profile_secret_scope
    route = event.get('live_result_origin')
    if not isinstance(route, dict):
        return False
    adapter = runner.adapters.get(Platform.DISCORD)
    if adapter is None:
        return False
    try:
        cfg = adapter.config.extra.get('live_voice', {})
        operator = str(cfg.get('operator_id') or '')
        if not operator or str(event.get('user_id')) != operator:
            raise PermissionError('Detached result operator mismatch')
        if str(route.get('chat_id')) != str(HOME_CHANNEL_ID) or str(route.get('scope_id')) != str(HOME_GUILD_ID):
            raise PermissionError('Detached result scope mismatch')
        home = Path(adapter._live_profile_home).resolve()
        source = adapter.build_source(str(HOME_CHANNEL_ID), user_id=operator, chat_type='group')
        if (source.profile_route_rejected or not runner._is_user_authorized(source)
                or Path(runner._resolve_profile_home_for_source(source)).resolve() != home):
            raise PermissionError('Detached result profile unauthorized')
        topics = FollowupTopics(adapter._client, home, operator)
        await topics.validate(route['thread_id'])
        safe = str(text)
        for value in build_profile_secret_scope(home).values():
            if isinstance(value, str) and len(value) >= 8:
                safe = safe.replace(value, '[redacted]')
        safe = redact_sensitive_text(safe, force=True, redact_url_credentials=True)
        result = await adapter.send(str(HOME_CHANNEL_ID), safe,
                                    metadata={'thread_id':str(route['thread_id'])})
        return bool(result.success)
    except Exception:
        # Existing durable completion ledger retries/retains failure. Never
        # claim receipt or reroute elsewhere after unavailable permissions.
        return False


from tools.registry import registry
registry.register(name=TOOL_NAME, toolset='live_followup', schema=SCHEMA,
    handler=select_handler, check_fn=lambda: LIVE_FOLLOWUP.get() is not None)
