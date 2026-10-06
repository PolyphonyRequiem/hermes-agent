"""Process-local receipts for plugin attention; not an outbox or delivery ledger."""
from collections import OrderedDict
from threading import RLock
from uuid import uuid4


OUTSTANDING = frozenset({"scheduled", "queued", "processing"})
TERMINAL = frozenset({"handled", "dropped", "refused"})


def result(state, *, request_id=None, generation=None, coalesced=False, reason=None):
    record = {"ok": state in OUTSTANDING or state == "handled", "request_id": request_id,
              "generation": generation, "state": state, "coalesced": coalesced}
    if reason:
        record["reason"] = reason[:160]
    return record


class AttentionRegistry:
    """One host lifetime, atomic admission, bounded terminal retention.

    Never evicts outstanding requests. The routing key includes the profile,
    and this registry belongs to one GatewayRunner instance. No caller can
    choose another host's registry or query another plugin's receipts.
    """
    def __init__(self, *, max_outstanding=1024, max_terminal=1024):
        if max_outstanding < 1 or max_terminal < 1:
            raise ValueError("attention retention limits must be positive")
        self.generation = uuid4().hex
        self._max_outstanding = max_outstanding
        self._max_terminal = max_terminal
        self._lock = RLock()
        self._records = {}
        self._outstanding = {}
        self._terminal = OrderedDict()

    def reserve(self, plugin_id, session_key, attention_key, content):
        for name, value, limit in (("plugin_id", plugin_id, 512), ("session_key", session_key, 512),
                                   ("attention_key", attention_key, 512), ("content", content, 65536)):
            if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
                return result("refused", generation=self.generation, reason=f"invalid_{name}")
        scope = (plugin_id, session_key, attention_key)
        with self._lock:
            existing = self._outstanding.get(scope)
            if existing:
                return self._view(self._records[existing], coalesced=True)
            if len(self._outstanding) >= self._max_outstanding:
                return result("refused", generation=self.generation, reason="attention_capacity")
            request_id = uuid4().hex
            self._records[request_id] = {"scope": scope, "request_id": request_id, "state": "scheduled", "reason": None}
            self._outstanding[scope] = request_id
            return self._view(self._records[request_id])

    def _view(self, record, *, coalesced=False):
        return result(record["state"], request_id=record["request_id"], generation=self.generation,
                      coalesced=coalesced, reason=record["reason"])

    def status(self, plugin_id, request_id):
        with self._lock:
            record = self._records.get(request_id) if isinstance(request_id, str) else None
            if record and record["scope"][0] == plugin_id:
                return self._view(record)
            return result("unknown", generation=self.generation, reason="unknown_or_expired")

    def transition(self, request_id, state, reason=None, *, expected=None):
        if state not in OUTSTANDING | TERMINAL:
            raise ValueError("invalid attention state")
        with self._lock:
            record = self._records.get(request_id)
            if not record or record["state"] in TERMINAL or (expected and record["state"] not in expected):
                return
            record["state"] = state
            record["reason"] = reason[:160] if reason else None
            if state in TERMINAL:
                self._outstanding.pop(record["scope"], None)
                self._terminal[request_id] = None
                while len(self._terminal) > self._max_terminal:
                    expired, _ = self._terminal.popitem(last=False)
                    self._records.pop(expired, None)

    def drop_session(self, session_key, reason):
        with self._lock:
            for scope, request_id in list(self._outstanding.items()):
                if scope[1] == session_key:
                    self.transition(request_id, "dropped", reason)

    def drop_all(self, reason):
        with self._lock:
            for request_id in list(self._outstanding.values()):
                self.transition(request_id, "dropped", reason)


def transition_event(event, state, reason=None, *, expected=None):
    """Transient host-owned callback, never serialized into event metadata."""
    callback = getattr(event, "_plugin_attention_update", None)
    if callback is not None:
        callback(state, reason, expected=expected)
