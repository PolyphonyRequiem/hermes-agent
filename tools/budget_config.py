"""Configurable budget constants for tool result persistence.

Per-tool resolution: pinned > config overrides > registry > default.
"""

from dataclasses import dataclass, field
from typing import Dict

# Tools whose thresholds must never be overridden.
# read_file=inf prevents infinite persist->read->persist loops.
PINNED_THRESHOLDS: Dict[str, float] = {
    "read_file": float("inf"),
}

# Defaults matching the current hardcoded values in tool_result_storage.py.
# Kept here as the single source of truth; tool_result_storage.py imports these.
DEFAULT_RESULT_SIZE_CHARS: int = 100_000
DEFAULT_TURN_BUDGET_CHARS: int = 200_000
DEFAULT_PREVIEW_SIZE_CHARS: int = 1_500


# --- Config-tunable per-result / per-turn ceilings -------------------------
#
# The defaults above are correct for large-context models talking to providers
# with generous request limits. But the persistence threshold (when a tool
# result spills to disk instead of riding inline) is also the de-facto guard
# against a provider's *whole-request byte ceiling* — a dimension the
# context-window scaler (``budget_for_context_window``) is blind to. Copilot's
# proxy, for example, rejects a request whose serialized body exceeds roughly
# 5-7 MB with HTTP 413; several 90-99K-char tool results (skill_view,
# session_search, kanban_list) sit *just under* the 100K spill line, never
# persist, accumulate in history, and sum past that ceiling — bricking the
# session with "413 ... Cannot compress further."
#
# These ceilings let an operator lower the spill threshold below 100K so fat
# results persist to disk (preview + read_file path) before they can stack into
# a 413, WITHOUT touching the source. They are read from ``config.yaml``
# (``tool_result_budget`` section); when absent, the DEFAULT_* values above are
# used and behaviour is byte-identical to before. The reader never raises.
#
# Example ``config.yaml``::
#
#     tool_result_budget:
#       max_result_chars: 35000     # spill a single tool result above this
#       max_turn_chars:   90000     # spill the turn's largest results above this aggregate
#
# Resolution composes as a CAP: the configured value clamps whatever the
# context-window scaler produced, so the SMALLER of (config, scaled-default)
# wins. A tiny-model scaled budget is never re-inflated by a larger config
# value, and a large-model default is lowered by a smaller config value.

_cached_budget_overrides: dict | None = None


def _coerce_positive_int(value, default):
    """Return ``value`` as a positive int, or ``default`` on any problem."""
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return default
    return iv if iv > 0 else default


def get_budget_overrides() -> Dict[str, int]:
    """Return operator ceilings from the ``tool_result_budget`` config section.

    Keys: ``max_result_chars`` (per-result spill threshold) and
    ``max_turn_chars`` (per-turn aggregate budget). Missing or invalid entries
    fall through to the ``DEFAULT_*`` constants, so an absent section is
    behaviour-preserving. Cached for the process lifetime to avoid re-reading
    the config file on every tool call; call ``_reset_budget_overrides_cache()``
    after a config hot-reload or in tests.

    This function NEVER raises.
    """
    global _cached_budget_overrides
    if _cached_budget_overrides is not None:
        return _cached_budget_overrides
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        section = cfg.get("tool_result_budget") if isinstance(cfg, dict) else None
        if not isinstance(section, dict):
            section = {}
    except Exception:
        section = {}

    _cached_budget_overrides = {
        "max_result_chars": _coerce_positive_int(
            section.get("max_result_chars"), DEFAULT_RESULT_SIZE_CHARS
        ),
        "max_turn_chars": _coerce_positive_int(
            section.get("max_turn_chars"), DEFAULT_TURN_BUDGET_CHARS
        ),
    }
    return _cached_budget_overrides


def _reset_budget_overrides_cache() -> None:
    """Reset the cached ``tool_result_budget`` overrides (tests / hot-reload)."""
    global _cached_budget_overrides
    _cached_budget_overrides = None


@dataclass(frozen=True)
class BudgetConfig:
    """Immutable budget constants for the 3-layer tool result persistence system.

    Layer 2 (per-result): resolve_threshold(tool_name) -> threshold in chars.
    Layer 3 (per-turn):   turn_budget -> aggregate char budget across all tool
                          results in a single assistant turn.
    Preview:              preview_size -> inline snippet size after persistence.
    """

    default_result_size: int = DEFAULT_RESULT_SIZE_CHARS
    turn_budget: int = DEFAULT_TURN_BUDGET_CHARS
    preview_size: int = DEFAULT_PREVIEW_SIZE_CHARS
    tool_overrides: Dict[str, int] = field(default_factory=dict)

    def resolve_threshold(self, tool_name: str) -> int | float:
        """Resolve the persistence threshold for a tool.

        Priority: pinned -> tool_overrides -> registry per-tool -> default.

        The registry per-tool value is capped at ``default_result_size`` so a
        context-scaled budget (small model) actually constrains tools that
        register a large fixed ``max_result_size_chars`` (web/terminal/x_search
        all register 100K). For the default budget this is a no-op because both
        equal 100K; for a scaled-down budget it prevents a per-tool registry
        value from re-inflating the cap past the model's window (#23767).
        """
        if tool_name in PINNED_THRESHOLDS:
            return PINNED_THRESHOLDS[tool_name]
        if tool_name in self.tool_overrides:
            return self.tool_overrides[tool_name]
        from tools.registry import registry
        registry_value = registry.get_max_result_size(tool_name, default=self.default_result_size)
        if registry_value == float("inf"):
            return registry_value
        return min(registry_value, self.default_result_size)


# Default config -- matches current hardcoded behavior exactly.
DEFAULT_BUDGET = BudgetConfig()


# Token<->char conversion used when scaling the budget to a model's context
# window. Deliberately conservative (a smaller divisor = more chars per token =
# a larger char budget) would UNDER-protect small models, so we use the same
# rough 4-chars-per-token ratio the estimator uses (agent/model_metadata.py).
_CHARS_PER_TOKEN: int = 4

# Fraction of a model's context window we allow a SINGLE tool result to occupy
# before persisting/truncating it, and the fraction the WHOLE turn's tool
# output may occupy. Tool output is not the only thing in the window (system
# prompt, tool schemas, conversation history, the model's own reply all
# compete), so these stay well under 1.0.
_PER_RESULT_WINDOW_FRACTION: float = 0.15
_PER_TURN_WINDOW_FRACTION: float = 0.30

# Floor so even a tiny-but-admitted model still gets a usable preview/result
# rather than a 0-char budget.
_MIN_RESULT_SIZE_CHARS: int = 8_000
_MIN_TURN_BUDGET_CHARS: int = 16_000


def budget_for_context_window(context_length: int | None) -> BudgetConfig:
    """Return a BudgetConfig scaled to the active model's context window.

    The fixed defaults (100K result / 200K turn chars) are correct for large
    (200K+ token) models but blind to small ones: on a 65K-token model a single
    tool result persisted at the 100K-char threshold, or a 200K-char turn
    budget (~50K tokens), can by itself approach or exceed the whole window and
    force an oversized request (#23767).

    Scaling keeps large models byte-identical to today (the proportional value
    is clamped to the existing defaults as a CAP) while shrinking the budget for
    small models proportionally to their window, floored so a usable preview
    always survives.
    """
    if not context_length or context_length <= 0:
        return DEFAULT_BUDGET

    window_chars = context_length * _CHARS_PER_TOKEN
    per_result = int(window_chars * _PER_RESULT_WINDOW_FRACTION)
    per_turn = int(window_chars * _PER_TURN_WINDOW_FRACTION)

    # Clamp: never exceed the historical defaults (so large models are
    # unchanged), never drop below the floor (so tiny models stay usable).
    per_result = max(_MIN_RESULT_SIZE_CHARS, min(per_result, DEFAULT_RESULT_SIZE_CHARS))
    per_turn = max(_MIN_TURN_BUDGET_CHARS, min(per_turn, DEFAULT_TURN_BUDGET_CHARS))

    return BudgetConfig(
        default_result_size=per_result,
        turn_budget=per_turn,
        preview_size=DEFAULT_PREVIEW_SIZE_CHARS,
    )
