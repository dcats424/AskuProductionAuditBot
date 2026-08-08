import os
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# NOTE:
# This project uses the Ethiopian *clock* system (≈ 6-hour offset from international clock),
# not just a timezone conversion. Example: 1:20 PM international ≈ 7:20 Ethiopian clock.
# We still keep TZ_ETHIOPIA for future use, but shift logic is based on the Ethiopian clock offset.
TZ_ETHIOPIA = ZoneInfo("Africa/Addis_Ababa")

# Ethiopian clock offset: EthiopianClock = InternationalClock - 6 hours
ETHIOPIAN_CLOCK_OFFSET = timedelta(hours=-6)


def to_ethiopian_clock(dt: datetime) -> datetime:
    """Convert international (PC) clock datetime to Ethiopian clock datetime (subtract 6 hours)."""
    return dt + ETHIOPIAN_CLOCK_OFFSET


def ethiopian_clock_time_to_pc_time(t: time) -> time:
    """Convert an Ethiopian clock 'time' to the PC/international clock 'time' (add 6 hours, wrap 24h)."""
    total = (t.hour * 60 + t.minute + 6 * 60) % (24 * 60)
    return time(total // 60, total % 60)


# ---------------- CONFIG ----------------

# Load the .env file first
load_dotenv()

# Then access the variables
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Per-line configuration: one bot serving one group per production line,
# each group writing to its own database. Keys follow the .env style:
#   GROUP_CHAT_ID_1ltr, GROUP_CHAT_ID_2ltr, GROUP_CHAT_ID_0.6ltr, GROUP_CHAT_ID_0.3ltr
#   DB_NAME_1ltr, DB_NAME_2ltr, DB_NAME_0.6ltr, DB_NAME_0.3ltr
LINE_KEYS = ["1ltr", "2ltr", "0.6ltr", "0.3ltr"]


def line_key_for_chat(chat_id: int) -> str | None:
    for key in LINE_KEYS:
        env_id = os.getenv(f"GROUP_CHAT_ID_{key}")
        if env_id and str(chat_id) == env_id.strip():
            return key
    return None


def is_allowed_chat(chat_id: int | None) -> bool:
    """True only for chats mapped to a configured production line (allowlist)."""
    return chat_id is not None and line_key_for_chat(chat_id) is not None


def chat_id_for_line(line_key: str) -> int | None:
    env_id = os.getenv(f"GROUP_CHAT_ID_{line_key}")
    return int(env_id) if env_id else None


def db_name_for_line(line_key: str) -> str | None:
    return os.getenv(f"DB_NAME_{line_key}")


def configured_lines() -> list[str]:
    return [k for k in LINE_KEYS if chat_id_for_line(k) is not None and db_name_for_line(k)]


def default_chat_id() -> int | None:
    lines = configured_lines()
    return chat_id_for_line(lines[0]) if lines else None


# Base DB credentials are shared across all line databases; only the database name differs.
BASE_DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "port": int(os.getenv("DB_PORT", 5432)),
}

# ---------------- AI CONFIG ----------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-oss-120b")

# ── AI call resilience: timeout + retries ────────────────────────────────────
AI_TIMEOUT_SECONDS = 60
AI_MAX_RETRIES = 2
AI_MAX_TOKENS = 1500

# Line / sanitation / AI reminder gating
LINE_STATE_RUNNING = "running"
LINE_STATE_OFF = "line_off"
LINE_STATE_SANITATION = "sanitation"

# ── Chat message auto-cleanup keys ───────────────────────────────────────────
BOT_STATUS_MSG_KEY = "bot_status_msg_id"
HOURLY_PROMPT_MSG_KEY = "hourly_prompt_msg_id"
HOURLY_TWOSTEP_CMD_MSG_KEY = "hourly_two_step_cmd_msg_id"

# ── Scheduling windows / reminder times (Ethiopian clock, minute of hour) ────
HOURLY_PLAN_WINDOW_START_MINUTE = 2
HOURLY_PLAN_FIRST_HOUR_START_MINUTE = 5  # First hour of shift: plan fires at :05
HOURLY_PLAN_WINDOW_END_MINUTE = 30  # Never after :30
HOURLY_SUMMARY_WINDOW_START = 55
HOURLY_SUMMARY_LAST_HOUR_START = 50  # Last hour of shift: :50, normal hours: :55
HOURLY_SUMMARY_WINDOW_END = 59  # Never after :59
SHIFT_SUMMARY_WINDOW_START = 55
SHIFT_SUMMARY_WINDOW_END = 59

REMINDER_PLAN_MINUTE = 2
REMINDER_FIRST_HOUR_PLAN_MINUTE = 5  # First hour of shift: plan fires at :05
REMINDER_SUMMARY_MINUTE = 55
REMINDER_LAST_HOUR_SUMMARY_MINUTE = 50
REMINDER_HANDOFF_MINUTE = 55
BOT_STATUS_LOOKAHEAD_MINUTES = 5
BOT_STATUS_AUTODELETE_SECONDS = 120  # /bot_status + startup messages self-delete after 2 minutes


# ---------------- TIME / SHIFT HELPERS ----------------
def format_date_time_12h(dt: datetime) -> str:
    """Format as dd/mm/yyyy, h:mm AM/PM (12-hour). Converts to Ethiopia if timezone-aware."""
    if dt.tzinfo:
        dt = dt.astimezone(TZ_ETHIOPIA)
    date_str = dt.strftime("%d/%m/%Y")
    hour_12 = dt.hour % 12 or 12
    am_pm = "AM" if dt.hour < 12 else "PM"
    time_str = f"{hour_12}:{dt.minute:02d} {am_pm}"
    return f"{date_str}, {time_str}"


def format_hour_range_12h(start_hour: int) -> str:
    """Format hour range as 12-hour AM/PM (e.g., '12:00 AM–1:00 AM')."""
    end_hour = (start_hour + 1) % 24
    start_12 = start_hour % 12 or 12
    start_am_pm = "AM" if start_hour < 12 else "PM"
    end_12 = end_hour % 12 or 12
    end_am_pm = "AM" if end_hour < 12 else "PM"
    return f"{start_12}:00 {start_am_pm}–{end_12}:00 {end_am_pm}"


def get_shift_duration_minutes(shift: int) -> int:
    """Get default shift duration in minutes based on shift number."""
    return 11 * 60  # 11 hours production per shift (1h rest)


def get_default_production_hours(report_type: str, shift: int = None) -> float:
    """Get default production hours based on report type."""
    if report_type == "hourly":
        return 1.0  # 1 hour for hourly summaries
    elif report_type == "multi_shift":
        return 22.0  # 22 hours total for multi-shift (2 hours for PM)
    elif report_type == "shift" and shift:
        return 11.0  # 11 hours per shift
    else:
        return 1.0  # Default to 1 hour


# ---------------- SHIFT CALCULATION (BY CLOCK) ----------------
def now_ethiopia() -> datetime:
    """
    Current time in Ethiopia (Africa/Addis_Ababa) for shift logic and scheduling.
    Ensures bot_status, reminders, and job queue all use the same clock.
    """
    return datetime.now(TZ_ETHIOPIA)


def get_shift_for_time(dt: datetime | None = None) -> int:
    """
    Map PC/international wall-clock time to shift number.

    Ethiopian shifts (Ethiopian clock):
    - Shift 1: 1:00 AM – 1:00 PM  (12 hours)
    - Shift 2: 1:00 PM – 1:00 AM  (12 hours)

    Converted to PC/international clock (add 6 hours):
    - Shift 1: 07:00 – 19:00
    - Shift 2: 19:00 – 07:00 (next day)
    """
    if dt is None:
        dt = now_ethiopia()
    t = dt.time()
    if time(7, 0) <= t < time(19, 0):
        return 1
    return 2
