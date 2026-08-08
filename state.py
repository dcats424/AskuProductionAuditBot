import logging
from datetime import datetime

from config import (
    LINE_KEYS,
    LINE_STATE_OFF,
    LINE_STATE_RUNNING,
    LINE_STATE_SANITATION,
    configured_lines,
    line_key_for_chat,
)
from db import bot_state_get

logger = logging.getLogger(__name__)


# ---------------- PER-LINE RUNTIME ----------------
# All in-memory shift/reminder/validation state is per production line,
# keyed by the Telegram group chat_id of each line. A single bot process
# serves all 4 line groups, so NO line may share state with another.


class LineRuntime:
    """Mutable per-line runtime state (evidence, shift counters, reminders...)."""

    __slots__ = (
        "evidence",
        "daily_summaries",
        "current_shift",
        "shift_closed",
        "pending_reminders",
        "plan_sent_today",
        "last_plan_date",
        "ai_block",
        "line_state",
        "off_since",
        "active_validation_key",
        "validation_sessions",
        "hourly_summary_pending",
        "shift_summary_pending",
        "next_reminder_allowed",
        "one_reminder_fired",
        "shift_had_production",
    )

    def __init__(self):
        self.evidence = {1: [], 2: []}
        self.daily_summaries = {1: None, 2: None}
        self.current_shift = 1
        self.shift_closed = {1: False, 2: False}
        self.pending_reminders = []
        self.plan_sent_today = {1: None, 2: None}
        self.last_plan_date = None
        self.ai_block = False
        self.line_state = LINE_STATE_RUNNING
        self.off_since = None
        self.active_validation_key = None
        self.validation_sessions = {}
        self.hourly_summary_pending = False
        self.shift_summary_pending = None
        self.next_reminder_allowed = True
        self.one_reminder_fired = False
        self.shift_had_production = {1: False, 2: False}


_line_runtimes: dict[str, LineRuntime] = {}


def line_runtime(chat_id: int | None = None) -> LineRuntime:
    """Return the LineRuntime for a chat's production line.
    Falls back to the first configured line (or a fresh default) when
    the chat is unknown — keeps single-line deployments working."""
    key = line_key_for_chat(chat_id) if chat_id is not None else None
    if key is None:
        lines = configured_lines()
        key = lines[0] if lines else "1ltr"
    rt = _line_runtimes.get(key)
    if rt is None:
        rt = LineRuntime()
        _line_runtimes[key] = rt
    return rt


def line_runtime_for_line(line_key: str) -> LineRuntime:
    key = line_key if line_key in LINE_KEYS else "1ltr"
    rt = _line_runtimes.get(key)
    if rt is None:
        rt = LineRuntime()
        _line_runtimes[key] = rt
    return rt


def load_bot_state_from_db(chat_id: int | None = None) -> None:
    """Load daily_plan_last_date, shift_plan_sent_today, line_state from the line's DB."""
    rt = line_runtime(chat_id)
    try:
        v = bot_state_get("daily_plan_last_date", chat_id)
        if v:
            rt.last_plan_date = datetime.strptime(v, "%Y-%m-%d").date()
        for i in (1, 2):
            v = bot_state_get(f"shift_plan_sent_{i}", chat_id)
            if v:
                rt.plan_sent_today[i] = datetime.strptime(v, "%Y-%m-%d").date()
        v = bot_state_get("line_state", chat_id)
        if v and v in (LINE_STATE_RUNNING, LINE_STATE_OFF, LINE_STATE_SANITATION):
            rt.line_state = v
        # off_since is not loaded (stays None after reboot) so partial-hour logic only uses current session
        logger.info("Loaded bot state from database")
    except Exception as e:
        logger.warning(f"load_bot_state_from_db: {e}")
