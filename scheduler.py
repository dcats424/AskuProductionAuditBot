import logging
from datetime import time

from config import (
    HOURLY_PLAN_FIRST_HOUR_START_MINUTE,
    HOURLY_PLAN_WINDOW_START_MINUTE,
    REMINDER_FIRST_HOUR_PLAN_MINUTE,
    REMINDER_HANDOFF_MINUTE,
    REMINDER_LAST_HOUR_SUMMARY_MINUTE,
    REMINDER_PLAN_MINUTE,
    REMINDER_SUMMARY_MINUTE,
    chat_id_for_line,
    configured_lines,
    ethiopian_clock_time_to_pc_time,
)
from reminders import (
    remind_daily_production_plan,
    remind_hourly_plan,
    remind_hourly_summary,
    remind_shift_plan,
    remind_shift_report,
)

logger = logging.getLogger(__name__)


async def setup_shift_schedules(app):
    job_queue = app.job_queue

    # Clear all old jobs first to prevent stale jobs firing at wrong times
    for job in job_queue.jobs():
        job.schedule_removal()
    logger.info("Cleared old jobs from queue")

    logger.info("Setting up shift schedules and reminders...")

    lines = configured_lines() or ["1ltr"]

    # ── Schedule every reminder for EVERY configured line ─────────────────
    for line_key in lines:
        chat_id = chat_id_for_line(line_key)

        # ── DAILY PLAN ──────────────────────────────────────────────────────
        # Ethiopian 01:00 → PC 07:00 (primary, Shift 1 start)
        job_queue.run_daily(
            remind_daily_production_plan,
            time=ethiopian_clock_time_to_pc_time(time(1, 0)),
            data={"chat_id": chat_id},
            name=f"daily_plan_shift1_{line_key}",
        )
        # Fallback for Shift 2 (once-per-day guard inside the function)
        job_queue.run_daily(
            remind_daily_production_plan,
            time=ethiopian_clock_time_to_pc_time(time(13, 0)),
            data={"chat_id": chat_id},
            name=f"daily_plan_shift2_{line_key}",
        )

        # ═══ SHIFT 1 │ Ethiopian 01:00–13:00 │ PC 07:00–19:00 ═══
        job_queue.run_daily(
            remind_shift_plan,
            time=ethiopian_clock_time_to_pc_time(time(1, 2)),
            data={"shift": 1, "chat_id": chat_id},
            name=f"shift1_plan_{line_key}",
        )

        for hour in range(1, 12):
            plan_minute = (
                HOURLY_PLAN_FIRST_HOUR_START_MINUTE
                if hour == 1
                else HOURLY_PLAN_WINDOW_START_MINUTE
            )
            job_queue.run_daily(
                remind_hourly_plan,
                time=ethiopian_clock_time_to_pc_time(time(hour, plan_minute)),
                data={"shift": 1, "hour": hour, "chat_id": chat_id},
                name=f"shift1_hour{hour}_plan_{line_key}",
            )
            job_queue.run_daily(
                remind_hourly_summary,
                time=ethiopian_clock_time_to_pc_time(time(hour, 55)),
                data={"shift": 1, "hour": hour, "chat_id": chat_id},
                name=f"shift1_hour{hour}_summary_{line_key}",
            )

        # Last hour (Hour 12): Plan at :02, Summary at :50, Shift Summary at :55
        job_queue.run_daily(
            remind_hourly_plan,
            time=ethiopian_clock_time_to_pc_time(time(12, 2)),
            data={"shift": 1, "hour": 12, "chat_id": chat_id},
            name=f"shift1_hour12_plan_{line_key}",
        )
        job_queue.run_daily(
            remind_hourly_summary,
            time=ethiopian_clock_time_to_pc_time(time(12, 50)),
            data={"shift": 1, "hour": 12, "chat_id": chat_id},
            name=f"shift1_hour12_summary_{line_key}",
        )
        job_queue.run_daily(
            remind_shift_report,
            time=ethiopian_clock_time_to_pc_time(time(12, 55)),
            data={"shift": 1, "chat_id": chat_id},
            name=f"shift1_report_{line_key}",
        )

        # ═══ SHIFT 2 │ Ethiopian 13:00–01:00 │ PC 19:00–07:00 ═══
        job_queue.run_daily(
            remind_shift_plan,
            time=ethiopian_clock_time_to_pc_time(time(13, 2)),
            data={"shift": 2, "chat_id": chat_id},
            name=f"shift2_plan_{line_key}",
        )

        # Hours 1-5: Ethiopian 13-17 (afternoon/evening before midnight)
        for hour in range(13, 18):
            eth_hour = hour - 12  # display hour 1-5
            plan_minute = (
                HOURLY_PLAN_FIRST_HOUR_START_MINUTE
                if eth_hour == 1
                else HOURLY_PLAN_WINDOW_START_MINUTE
            )
            job_queue.run_daily(
                remind_hourly_plan,
                time=ethiopian_clock_time_to_pc_time(time(hour, plan_minute)),
                data={"shift": 2, "hour": eth_hour, "chat_id": chat_id},
                name=f"shift2_hour{eth_hour}_plan_{line_key}",
            )
            job_queue.run_daily(
                remind_hourly_summary,
                time=ethiopian_clock_time_to_pc_time(time(hour, 55)),
                data={"shift": 2, "hour": eth_hour, "chat_id": chat_id},
                name=f"shift2_hour{eth_hour}_summary_{line_key}",
            )

        # Hours 6-11: Ethiopian 18-23 (evening/overnight)
        for hour in range(18, 24):
            eth_hour = hour - 12  # display hour 6-11
            job_queue.run_daily(
                remind_hourly_plan,
                time=ethiopian_clock_time_to_pc_time(time(hour, 2)),
                data={"shift": 2, "hour": eth_hour, "chat_id": chat_id},
                name=f"shift2_hour{eth_hour}_plan_{line_key}",
            )
            job_queue.run_daily(
                remind_hourly_summary,
                time=ethiopian_clock_time_to_pc_time(time(hour, 55)),
                data={"shift": 2, "hour": eth_hour, "chat_id": chat_id},
                name=f"shift2_hour{eth_hour}_summary_{line_key}",
            )

        # Last hour (Hour 12): Ethiopian 0:00 (midnight) → PC 06:00
        job_queue.run_daily(
            remind_hourly_plan,
            time=ethiopian_clock_time_to_pc_time(time(0, 2)),
            data={"shift": 2, "hour": 12, "chat_id": chat_id},
            name=f"shift2_hour12_plan_{line_key}",
        )
        job_queue.run_daily(
            remind_hourly_summary,
            time=ethiopian_clock_time_to_pc_time(time(0, 50)),
            data={"shift": 2, "hour": 12, "chat_id": chat_id},
            name=f"shift2_hour12_summary_{line_key}",
        )
        job_queue.run_daily(
            remind_shift_report,
            time=ethiopian_clock_time_to_pc_time(time(0, 55)),
            data={"shift": 2, "chat_id": chat_id},
            name=f"shift2_report_{line_key}",
        )

        logger.info(f"Reminder schedule registered for line {line_key} (chat {chat_id})")

    logger.info("✅ All reminders scheduled successfully!")



def get_shift_reminders(shift: int) -> list[tuple[str, str]]:
    """
    Exact schedule per shift in Ethiopian clock (12h):
    - Shift start:  Shift Plan :02, Hourly Plan :05, Hourly Summary :55
    - Normal hours: Hourly Plan :02, Hourly Summary :55
    - Last hour:    Hourly Plan :02, Hourly Summary :50, Shift Summary :55
    """
    if shift == 1:  # Ethiopian 01:00–13:00
        reminders = [
            (f"1:{REMINDER_PLAN_MINUTE:02d} AM", "Shift 1 Plan Reminder"),
        ]
        for h in range(1, 12):
            plan_minute = (
                REMINDER_FIRST_HOUR_PLAN_MINUTE if h == 1 else REMINDER_PLAN_MINUTE
            )
            reminders.append((f"{h}:{plan_minute:02d} AM" if h <= 11 else f"{h-12 if h > 12 else h}:{plan_minute:02d} PM", f"Hour {h} Plan Reminder"))
            reminders.append((f"{h}:{REMINDER_SUMMARY_MINUTE:02d} AM" if h <= 11 else f"{h-12}:{REMINDER_SUMMARY_MINUTE:02d} PM", f"Hour {h} Summary Reminder"))
        # Hour 12: Plan at :02, Summary at :50
        reminders.append((f"12:{REMINDER_PLAN_MINUTE:02d} PM", "Hour 12 Plan Reminder"))
        reminders.append((f"12:{REMINDER_LAST_HOUR_SUMMARY_MINUTE:02d} PM", "Hour 12 Summary Reminder"))
        reminders.append((f"12:{REMINDER_HANDOFF_MINUTE:02d} PM", "Shift 1 Handoff"))
        return reminders
    else:  # shift == 2, Ethiopian 13:00–01:00
        reminders = [
            (f"1:{REMINDER_PLAN_MINUTE:02d} PM", "Shift 2 Plan Reminder"),
        ]
        # Hours 1-11: Ethiopian 13:00–23:00
        for h in range(1, 12):
            eth_hour = h + 12  # 13-23
            if eth_hour >= 24:
                eth_hour -= 24
            ampm = "PM" if eth_hour >= 12 else "AM"
            display_hour = eth_hour % 12 or 12
            plan_minute = (
                REMINDER_FIRST_HOUR_PLAN_MINUTE if h == 1 else REMINDER_PLAN_MINUTE
            )
            reminders.append((f"{display_hour}:{plan_minute:02d} {ampm}", f"Hour {h} Plan Reminder"))
            reminders.append((f"{display_hour}:{REMINDER_SUMMARY_MINUTE:02d} {ampm}", f"Hour {h} Summary Reminder"))
        # Hour 12: Ethiopian 0:00 (midnight)
        reminders.append((f"12:{REMINDER_PLAN_MINUTE:02d} AM", "Hour 12 Plan Reminder"))
        reminders.append((f"12:{REMINDER_LAST_HOUR_SUMMARY_MINUTE:02d} AM", "Hour 12 Summary Reminder"))
        reminders.append((f"12:{REMINDER_HANDOFF_MINUTE:02d} AM", "Shift 2 Handoff"))
        return reminders


