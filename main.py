import asyncio
import logging
import sys

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from config import (
    BOT_STARTUP_AUTODELETE_SECONDS,
    BOT_STATUS_MSG_KEY,
    BOT_TOKEN,
    LINE_KEYS,
    TZ_ETHIOPIA,
    chat_id_for_line,
    configured_lines,
    db_name_for_line,
    get_shift_for_time,
    now_ethiopia,
)
from db import bot_state_set
from handlers import (
    bot_status_cmd,
    handle_message,
    hourly_summary_ai_cmd,
    line_off_cmd,
    line_on_cmd,
    sanitation_end_cmd,
    sanitation_start_cmd,
    setup_bot_commands,
)
from messaging import _schedule_bot_status_autodelete
from reminders import catch_up_missed_reminders, connection_watchdog
from reports import (
    all_shift_summary_from_hourly_cmd,
    monthly_report_cmd,
    shift_summary_hourly_1_cmd,
    shift_summary_hourly_2_cmd,
    weekly_report_cmd,
)
from scheduler import setup_shift_schedules
from state import line_runtime_for_line, load_bot_state_from_db

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


async def post_init(app):
    load_bot_state_from_db()
    await setup_bot_commands(app)
    await setup_shift_schedules(app)

    now = now_ethiopia()
    current_shift_by_clock = get_shift_for_time(now)

    lines = configured_lines() or ["1ltr"]

    # On startup: only send catchup daily plan + shift plan + hourly plan if missed
    # Do NOT call recover_missed_reminders_on_reconnect here — that causes
    # shift plan to fire as "missed" before the scheduler gets a chance at :02
    for line_key in lines:
        chat_id = chat_id_for_line(line_key)
        rt_pi = line_runtime_for_line(line_key)
        rt_pi.current_shift = current_shift_by_clock
        logger.info(
            f"Bot started: Synced line {line_key} current_shift to "
            f"{current_shift_by_clock} (clock time: {now.strftime('%H:%M:%S')})"
        )
        await catch_up_missed_reminders(app, current_shift_by_clock, now, chat_id)
        await asyncio.sleep(1)

        startup_msg = (
            f"🤖 Bot Started Successfully — Line {line_key}\n\n"
            f"⏰ Current Time: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"📅 Current Shift: {current_shift_by_clock}\n"
            f"🏭 Line State: {rt_pi.line_state}\n"
            f"✅ Reminders: ACTIVE\n"
            f"🔌 Connection Watchdog: ACTIVE\n\n"
            f"All scheduled reminders are configured.\nUse /bot_status to check current state."
        )
        try:
            sent = await app.bot.send_message(chat_id=chat_id, text=startup_msg)
            startup_message_id = getattr(sent, "message_id", None)
            if startup_message_id:
                bot_state_set(BOT_STATUS_MSG_KEY, str(startup_message_id), chat_id)
                _schedule_bot_status_autodelete(
                    app.job_queue,
                    startup_message_id,
                    chat_id,
                    delay_seconds=BOT_STARTUP_AUTODELETE_SECONDS,
                )
            logger.info(f"Startup message sent to line {line_key}")
        except Exception as e:
            logger.error(f"Failed to send startup message for line {line_key}: {e}")

    # Start background connection watchdog — recovery only fires after real drop
    asyncio.create_task(connection_watchdog(app))
    logger.info("[WATCHDOG] Connection watchdog task created")


# ---------------- LINE / SANITATION CONTROL COMMANDS ----------------

def main():
    # Use Ethiopia timezone so job queue runs at correct local times (aligns with bot_status)
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .defaults(Defaults(tzinfo=TZ_ETHIOPIA))
        .build()
    )

    # Add error handlers to prevent unhandled exceptions
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log Errors caused by Updates."""
        logger.error(f"Exception while handling an update: {context.error}")
        # Don't re-raise the exception to prevent bot from crashing

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("hourly_summary_ai", hourly_summary_ai_cmd))
    app.add_handler(CommandHandler("shift_1_summary", shift_summary_hourly_1_cmd))
    app.add_handler(CommandHandler("shift_2_summary", shift_summary_hourly_2_cmd))
    app.add_handler(
        CommandHandler("all_shift_summary", all_shift_summary_from_hourly_cmd)
    )
    app.add_handler(CommandHandler("weekly_report", weekly_report_cmd))
    app.add_handler(CommandHandler("monthly_report", monthly_report_cmd))
    app.add_handler(CommandHandler("bot_status", bot_status_cmd))
    app.add_handler(CommandHandler("line_off", line_off_cmd))
    app.add_handler(CommandHandler("line_on", line_on_cmd))
    app.add_handler(CommandHandler("sanitation_start", sanitation_start_cmd))
    app.add_handler(CommandHandler("sanitation_end", sanitation_end_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # app.post_init = setup_bot_commands
    app.post_init = post_init
    print("Bot running...")
    print(f"Configured lines: {configured_lines() or ['1ltr']}")
    missing_dbs = [
        k for k in LINE_KEYS
        if chat_id_for_line(k) is not None and db_name_for_line(k) is None
    ]
    if missing_dbs:
        logger.error(
            f"Refusing to start: configured lines missing DB_NAME_<line>: {missing_dbs}"
        )
        sys.exit(1)
    if not configured_lines():
        logger.error("Refusing to start: no production lines configured (GROUP_CHAT_ID_* + DB_NAME_*).")
        sys.exit(1)
    print("Reminders are ACTIVE by default. Use /bot_status to check state.")
    app.run_polling()


if __name__ == "__main__":
    main()
