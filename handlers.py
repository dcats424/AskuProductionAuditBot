import asyncio
import logging
import re

from telegram import BotCommand, Update
from telegram.ext import ContextTypes

from ai import (
    MAX_VALIDATION_ROUNDS,
    VALIDATION_STATE_APPROVED,
    VALIDATION_STATE_FOLLOWUP,
    VALIDATION_STATE_PENDING,
    VALIDATION_STATE_REJECTED,
    _release_summary_after_validation,
    ai_generate_hourly_summary_from_text,
    evaluate_operator_answer,
    validate_and_question_hourly,
)
from config import (
    BOT_STATUS_LOOKAHEAD_MINUTES,
    BOT_STATUS_MSG_KEY,
    LINE_STATE_OFF,
    LINE_STATE_RUNNING,
    LINE_STATE_SANITATION,
    format_date_time_12h,
    get_shift_for_time,
    is_allowed_chat,
    now_ethiopia,
    to_ethiopian_clock,
)
from db import bot_state_set, save_hourly_to_database

from messaging import (
    _cleanup_command_after_success,
    _cleanup_hourly_two_step,
    _purge_failed_messages,
    _queue_failed_messages,
    _schedule_bot_status_autodelete,
    _store_hourly_two_step_ids,
    _try_delete_message,
)
from parsing import (
    flatten_categorized_downtime,
    parse_downtime_categorized,
    parse_rejects,
    parse_report,
    parse_vos,
)
from reminders import (
    _hourly_reminder_block_timeout,
    flush_pending_reminders,
    get_current_hour_number,
    send_current_hour_plan,
    send_daily_plan_if_needed,
    send_shift_plan_if_needed,
)
from scheduler import get_shift_reminders
from state import line_runtime

logger = logging.getLogger(__name__)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if update.effective_user.is_bot:
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if not is_allowed_chat(chat_id):
        return
    rt_hm = line_runtime(chat_id)
    text = update.message.text.strip()

    # ═══════════════════════════════════════════════════════════════════
    # PRIORITY 1: Check if there's an active validation session waiting
    # for an operator answer. This may take priority over everything.
    # ═══════════════════════════════════════════════════════════════════
    active_validation_key = rt_hm.active_validation_key
    if active_validation_key and not text.startswith("/"):
        session = rt_hm.validation_sessions.get(active_validation_key)
        if session and session["state"] in (
            VALIDATION_STATE_PENDING,
            VALIDATION_STATE_FOLLOWUP,
        ):
            # Operator is answering validation questions
            session["conversation"].append(
                {
                    "role": "operator_answer",
                    "content": text,
                }
            )
            session["rounds"] += 1
            session.setdefault("_msg_ids_to_delete", []).append(
                getattr(update.message, "message_id", None)
            )

            eval_result = await evaluate_operator_answer(
                session_key=active_validation_key,
                operator_answer=text,
                validation_result=session["validation_result"],
                conversation_history=session["conversation"],
            )

            verdict = eval_result["verdict"]
            reasoning = eval_result["reasoning"]

            session["conversation"].append(
                {
                    "role": "ai_evaluation",
                    "content": f"VERDICT: {verdict}\n{reasoning}",
                }
            )

            if verdict == "ACCEPTED":
                # ✅ Operator's answer is convincing — approve and release summary
                session["state"] = VALIDATION_STATE_APPROVED
                session["verdict"] = "APPROVED"
                session["verdict_reasoning"] = reasoning

                # Clear the active session
                rt_hm.active_validation_key = None

                # Now generate and post the summary (validation recap follows after)
                await _release_summary_after_validation(context, session, chat_id)
                return

            elif verdict == "FOLLOW_UP":
                # ❓ Need more info — ask follow-up
                session["state"] = VALIDATION_STATE_FOLLOWUP
                follow_up = eval_result.get(
                    "follow_up_question", "Please provide more details."
                )

                session["conversation"].append(
                    {
                        "role": "ai_question",
                        "content": follow_up,
                    }
                )

                remaining = MAX_VALIDATION_ROUNDS - session["rounds"]
                sent_fu = await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🔄 FOLLOW-UP REQUIRED\n\n"
                        f"{reasoning}\n\n"
                        f"❓ {follow_up}\n\n"
                        f"⏳ {remaining} attempt(s) remaining before final verdict."
                    ),
                )
                session.setdefault("_msg_ids_to_delete", []).append(
                    getattr(sent_fu, "message_id", None)
                )
                return

            elif verdict == "REJECTED":
                # ❌ Not convincing — declare unaccounted loss
                session["state"] = VALIDATION_STATE_REJECTED
                session["verdict"] = "REJECTED"
                session["verdict_reasoning"] = reasoning

                rt_hm.active_validation_key = None

                # Generate summary WITH the rejection notice (recap follows after)
                await _release_summary_after_validation(context, session, chat_id)
                return

        else:
            # Session expired or invalid — clean up
            rt_hm.active_validation_key = None


    # ═══════════════════════════════════════════════════════════════════
    # PRIORITY 3: Hourly summary two-step
    # ═══════════════════════════════════════════════════════════════════
    pending_hour = rt_hm.hourly_summary_pending
    if pending_hour and text and not text.startswith("/"):
        rt_hm.hourly_summary_pending = False
        try:
            # Parse shift and hour from the report data
            production_data = parse_report(text)
            current_shift_num = production_data["shift"]

            # Extract hour number from the report text
            hour_match = re.search(r"hour\s*[:=]?\s*(\d+)", text.lower())
            if hour_match:
                hour_slot = int(hour_match.group(1))
            else:
                # Fallback to current hour if not found in report
                now = now_ethiopia()
                hour_slot = get_current_hour_number(current_shift_num, now)

            hour_label = f"Shift {current_shift_num}, Hour {hour_slot}"
            report_date = production_data.get("date")
            if report_date:
                hour_label = f"Date {report_date.strftime('%d/%m/%Y')} — {hour_label}"

            # Save hourly data to database
            try:
                categorized_dt = parse_downtime_categorized(text)
                downtime = flatten_categorized_downtime(categorized_dt)
                rejects = parse_rejects(text)
                vos_info = parse_vos(text)
                saved_id = save_hourly_to_database(
                    data=production_data,
                    downtime=downtime,
                    rejects=rejects,
                    hour_number=hour_slot,
                    vos_info=vos_info,
                    shift_override=current_shift_num,
                    chat_id=chat_id,
                )
            except Exception as e:
                saved_id = None
                logger.warning(f"Hourly DB save skipped: {e}")

            if saved_id is None:
                logger.error(
                    f"Hourly DB save FAILED: shift={current_shift_num}, hour={hour_slot}"
                )
                sent = await update.message.reply_text(
                    "⚠️ Your hourly report was NOT saved to the database (DB error).\n"
                    "Please try again in a few minutes."
                )
                _queue_failed_messages(
                    chat_id,
                    getattr(update.message, "message_id", None),
                    getattr(sent, "message_id", None),
                )
            else:
                logger.info(
                    f"Hourly data saved: shift={current_shift_num}, hour={hour_slot}"
                )

            # Validate production — may BLOCK summary
            validation = await validate_and_question_hourly(
                context, text, current_shift_num, hour_slot, chat_id
            )
            await asyncio.sleep(1)

            if validation and validation.get("_blocked"):
                session_key = validation["_session_key"]
                rt_hm.active_validation_key = session_key
                rt_va = rt_hm.validation_sessions
                rt_va[session_key]["_pending_hour"] = hour_slot
                rt_va[session_key]["_report_text"] = text
                rt_va[session_key]["_hour_label"] = hour_label
                rt_va[session_key]["_report_message_id"] = getattr(
                    update.message, "message_id", None
                )
                return
            else:
                ai_summary = await ai_generate_hourly_summary_from_text(text)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"📝 HOURLY SUMMARY ({hour_label})\n\n{ai_summary}",
                )
                # Flush queued reminders after hourly summary completes
                rt_hm.ai_block = False
                await flush_pending_reminders(context.bot, reason="ai", chat_id=chat_id)
                # Clean up: purge failed attempts, delete prompt + command + pasted report
                await _purge_failed_messages(context.bot, chat_id)
                await _cleanup_hourly_two_step(context.bot, chat_id)
                await _try_delete_message(
                    context.bot, chat_id,
                    getattr(update.message, "message_id", None),
                )
        except Exception as e:
            logger.error(f"Error generating hourly summary: {e}")
            sent = await update.message.reply_text(f"❌ Error: {e}")
            _queue_failed_messages(
                chat_id,
                getattr(update.message, "message_id", None),
                getattr(sent, "message_id", None),
            )
        return
# ---------------- SCHEDULER ----------------
# ---------------- BOT SETUP ----------------
async def setup_bot_commands(app):
    commands = [
        BotCommand("hourly_summary_ai", "Hourly AI summary (optional: hour 0-23)"),
        BotCommand("shift_1_summary", "Shift 1 summary from hourly data"),
        BotCommand("shift_2_summary", "Shift 2 summary from hourly data"),
        BotCommand("all_shift_summary", "AI summary across both shifts"),
        BotCommand("weekly_report", "Weekly production summary (Mon-Sun)"),
        BotCommand("monthly_report", "Monthly production summary (MM/YY)"),
        BotCommand("bot_status", "Check bot status and reminder state"),
        BotCommand("line_off", "Set line OFF (queue all reminders)"),
        BotCommand("line_on", "Set line ON (flush queued reminders)"),
        BotCommand("sanitation_start", "Start sanitation (queue all reminders)"),
        BotCommand("sanitation_end", "End sanitation (flush queued reminders)"),
    ]
    await app.bot.set_my_commands(commands)



async def line_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_chat(update.effective_chat.id if update.effective_chat else None):
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    rt_off = line_runtime(chat_id)
    now = now_ethiopia()
    rt_off.line_state = LINE_STATE_OFF
    rt_off.off_since = now
    # Allow exactly ONE next scheduled reminder after this OFF event, then suppress.
    rt_off.next_reminder_allowed = True
    rt_off.one_reminder_fired = False
    bot_state_set("line_state", rt_off.line_state, chat_id)
    bot_state_set("line_off_since", now.isoformat(), chat_id)

    current_shift_num = get_shift_for_time(now)
    rt_off.shift_had_production[current_shift_num] = True  # production existed before this OFF

    await update.message.reply_text(
        "🚫 Line is OFF — production has stopped. Send /line_on to resume."
    )


async def line_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_chat(update.effective_chat.id if update.effective_chat else None):
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    rt_on = line_runtime(chat_id)
    now = now_ethiopia()
    rt_on.line_state = LINE_STATE_RUNNING
    rt_on.off_since = None
    rt_on.next_reminder_allowed = True
    rt_on.one_reminder_fired = False
    bot_state_set("line_state", rt_on.line_state, chat_id)
    bot_state_set("line_off_since", "", chat_id)

    current_shift_by_clock = get_shift_for_time(now)
    rt_on.current_shift = current_shift_by_clock
    rt_on.shift_had_production[current_shift_by_clock] = True

    await update.message.reply_text("✅ Line is ON — production resumed.")

    # 1. Daily plan
    await send_daily_plan_if_needed(
        context.bot, now, skip_window_check=True, chat_id=chat_id
    )
    await asyncio.sleep(1)

    # 2. Shift plan
    await send_shift_plan_if_needed(
        context.bot, current_shift_by_clock, now, skip_window_check=True, chat_id=chat_id
    )
    await asyncio.sleep(1)

    # 3. Hourly plan — force send even if past :30 window
    #    (operator needs it regardless of what minute line turned ON)
    #    Only skipped if we're in summary window (:55+) — too late for a plan
    await send_current_hour_plan(
        context.bot, current_shift_by_clock, now, force_if_late=True, chat_id=chat_id
    )
    await asyncio.sleep(1)

    # 4. Flush any AI-muted queued reminders
    await flush_pending_reminders(context.bot, reason="line", chat_id=chat_id)


async def sanitation_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_chat(update.effective_chat.id if update.effective_chat else None):
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    rt_san = line_runtime(chat_id)
    now = now_ethiopia()
    rt_san.line_state = LINE_STATE_SANITATION
    rt_san.off_since = now
    # Allow exactly ONE next scheduled reminder after sanitation starts, then suppress.
    rt_san.next_reminder_allowed = True
    rt_san.one_reminder_fired = False
    bot_state_set("line_state", rt_san.line_state, chat_id)
    bot_state_set("line_off_since", now.isoformat(), chat_id)

    current_shift_num = get_shift_for_time(now)
    rt_san.shift_had_production[current_shift_num] = (
        True  # production existed before sanitation
    )

    await update.message.reply_text(
        "🧼 Sanitation started — production is paused for cleaning. Send /sanitation_end to resume."
    )


async def sanitation_end_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_chat(update.effective_chat.id if update.effective_chat else None):
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    rt_send = line_runtime(chat_id)
    now = now_ethiopia()
    rt_send.line_state = LINE_STATE_RUNNING
    rt_send.off_since = None
    rt_send.next_reminder_allowed = True
    rt_send.one_reminder_fired = False
    bot_state_set("line_state", rt_send.line_state, chat_id)
    bot_state_set("line_off_since", "", chat_id)

    current_shift_by_clock = get_shift_for_time(now)
    rt_send.current_shift = current_shift_by_clock
    rt_send.shift_had_production[current_shift_by_clock] = True

    await update.message.reply_text("✅ Sanitation finished — production resumed.")

    # 1. Daily plan
    await send_daily_plan_if_needed(
        context.bot, now, skip_window_check=True, chat_id=chat_id
    )
    await asyncio.sleep(1)

    # 2. Shift plan
    await send_shift_plan_if_needed(
        context.bot, current_shift_by_clock, now, skip_window_check=True, chat_id=chat_id
    )
    await asyncio.sleep(1)

    # 3. Hourly plan — force send even if past :30 window
    await send_current_hour_plan(
        context.bot, current_shift_by_clock, now, force_if_late=True, chat_id=chat_id
    )
    await asyncio.sleep(1)

    # 4. Flush AI-muted queued reminders
    await flush_pending_reminders(context.bot, reason="line", chat_id=chat_id)



def _parse_hour_arg(args: list) -> tuple[int | None, str]:
    """
    If first arg is a number 0-23, treat as clock hour (e.g. 3 = 3:00-4:00).
    Returns (hour_or_none, report_text).
    """
    if not args:
        return None, ""
    try:
        first = int(args[0])
        if 0 <= first <= 23:
            return first, " ".join(args[1:]).strip()
    except ValueError:
        pass
    return None, " ".join(args).strip()


async def hourly_summary_ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Two ways to use:
    1) Two-step: Send /hourly_summary_ai. Bot asks for the report. Send the report in your next message.
    2) One message: /hourly_summary_ai Date 18/02/26 Shift 2nd ... (full report text)
    No need to start a separate AI audit — this command works on its own.
    """
    if not is_allowed_chat(update.effective_chat.id if update.effective_chat else None):
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    report_text = " ".join(context.args).strip() if context.args else ""

    if not report_text:
        # User sent just /hourly_summary_ai → wait for next message
        line_runtime(chat_id).hourly_summary_pending = True
        line_runtime(chat_id).ai_block = True
        sent = await update.message.reply_text(
            "✅ Please send your hourly report in the *next message* (same format as shift report):\n"
            "Date, Shift, Product type, Hour number, Available time, Shift plan, Actual, Downtime, Rejects.",
            parse_mode="Markdown",
        )
        _store_hourly_two_step_ids(
            chat_id,
            getattr(update.message, "message_id", None),
            getattr(sent, "message_id", None),
        )
        # Auto-release block after 15 minutes as safety timeout
        asyncio.create_task(_hourly_reminder_block_timeout(context.bot, chat_id=chat_id))
        return

    # Process the report immediately
    try:
        # Parse shift and hour from input data
        try:
            h_prod_check = parse_report(report_text)
            current_shift_num = h_prod_check["shift"]
            # Extract hour number from the report text
            hour_match = re.search(r"hour\s*[:=]?\s*(\d+)", report_text.lower())
            if hour_match:
                hour_slot = int(hour_match.group(1))
            else:
                # Fallback to current hour if not found in report
                now = now_ethiopia()
                hour_slot = get_current_hour_number(current_shift_num, now)
        except Exception as e:
            sent = await update.message.reply_text(
                f"❌ Error parsing your input: {e}\n\n"
                "Please ensure your report includes 'Shift = 1st/2nd' and 'Hour number X'"
            )
            _queue_failed_messages(
                chat_id,
                getattr(update.message, "message_id", None),
                getattr(sent, "message_id", None),
            )
            return

        hour_label = f"Shift {current_shift_num}, Hour {hour_slot}"
        report_date = h_prod_check.get("date")
        if report_date:
            hour_label = f"Date {report_date.strftime('%d/%m/%Y')} — {hour_label}"

        try:
            h_prod = parse_report(report_text)
            h_cat_dt = parse_downtime_categorized(report_text)
            h_downtime = flatten_categorized_downtime(h_cat_dt)
            h_rejects = parse_rejects(report_text)
            h_vos = parse_vos(report_text)
            saved_id = save_hourly_to_database(
                h_prod,
                h_downtime,
                h_rejects,
                hour_number=hour_slot,
                vos_info=h_vos,
                shift_override=current_shift_num,
                chat_id=chat_id,
            )
        except Exception as e:
            saved_id = None
            logger.warning(f"Hourly DB save skipped in command: {e}")

        if saved_id is None:
            logger.error(
                f"Hourly DB save FAILED in /hourly_summary_ai: "
                f"shift={current_shift_num}, hour={hour_slot}"
            )
            sent = await update.message.reply_text(
                "⚠️ Your report was NOT saved to the database (DB error).\n"
                "Please try again in a few minutes."
            )
            _queue_failed_messages(
                chat_id,
                getattr(update.message, "message_id", None),
                getattr(sent, "message_id", None),
            )
        else:
            logger.info(
                f"Hourly data saved (command): shift={current_shift_num}, hour={hour_slot}"
            )

        validation = await validate_and_question_hourly(
            context, report_text, current_shift_num, hour_slot, chat_id
        )
        await asyncio.sleep(1)

        if validation and validation.get("_blocked"):
            session_key = validation["_session_key"]
            rt_hsa = line_runtime(chat_id)
            rt_hsa.active_validation_key = session_key
            rt_va = rt_hsa.validation_sessions
            rt_va[session_key]["_pending_hour"] = hour_slot
            rt_va[session_key]["_report_text"] = report_text
            rt_va[session_key]["_hour_label"] = hour_label
            rt_va[session_key]["_report_message_id"] = getattr(
                update.message, "message_id", None
            )
            return
        else:
            ai_summary = await ai_generate_hourly_summary_from_text(report_text)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📝 HOURLY SUMMARY ({hour_label})\n\n{ai_summary}",
            )
            await update.message.reply_text(
                f"✅ Hourly summary for {hour_label} posted to group."
            )
            await _cleanup_command_after_success(
                context.bot, chat_id,
                getattr(update.message, "message_id", None),
            )
    except Exception as e:
        logger.error(f"Error generating hourly summary: {e}")
        sent = await update.message.reply_text(f"❌ Error generating hourly summary: {e}")
        _queue_failed_messages(
            chat_id,
            getattr(update.message, "message_id", None),
            getattr(sent, "message_id", None),
        )


async def bot_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check bot status and reminder state"""
    if not is_allowed_chat(update.effective_chat.id if update.effective_chat else None):
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    rt_stat = line_runtime(chat_id)
    now_pc = now_ethiopia()
    now_eth = to_ethiopian_clock(now_pc)
    current_shift_by_clock = get_shift_for_time(now_pc)

    # Get all reminders for current shift
    all_reminders = get_shift_reminders(current_shift_by_clock)

    # Filter to show only future reminders (or current if within 5 minutes)
    # Compare reminder times in Ethiopian clock domain (what get_shift_reminders returns)
    current_hour_24 = now_eth.hour
    current_minute = now_eth.minute

    def time_to_minutes(time_str: str) -> int:
        """Convert '1:02 AM' format to minutes since midnight."""
        parts = time_str.split()
        time_part = parts[0]
        am_pm = parts[1]
        hour, minute = map(int, time_part.split(":"))
        if am_pm == "PM" and hour != 12:
            hour += 12
        elif am_pm == "AM" and hour == 12:
            hour = 0
        return hour * 60 + minute

    current_minutes = current_hour_24 * 60 + current_minute
    upcoming_reminders = []
    for time_str, desc in all_reminders:
        reminder_minutes = time_to_minutes(time_str)
        if (
            reminder_minutes >= current_minutes - BOT_STATUS_LOOKAHEAD_MINUTES
        ):  # Show if within 5 min past or future
            upcoming_reminders.append((time_str, desc))

    # If no upcoming reminders, show all reminders for the shift
    if not upcoming_reminders:
        upcoming_reminders = all_reminders

    status_text = (
        f"🤖 *Bot Status*\n\n"
        f"⏰ Current Time (Ethiopian clock): {format_date_time_12h(now_eth)}\n"
        f"⏰ PC Time: {format_date_time_12h(now_pc)}\n"
        f"📅 Current Shift (by clock): {current_shift_by_clock}\n"
        f"🔄 Active Shift (bot state): {rt_stat.current_shift}\n"
        f"🏭 Line State: {rt_stat.line_state}\n"
        f"🤖 AI Audit Block: {'Yes' if rt_stat.ai_block else 'No'}\n"
        f"📋 Queued Reminders: {len(rt_stat.pending_reminders)}\n"
        f"✅ Reminders Active: {'Yes' if rt_stat.line_state == LINE_STATE_RUNNING and not rt_stat.ai_block else 'No — reminders are QUEUED'}\n\n"
    )

    if upcoming_reminders:
        status_text += f"⏰ *Shift {current_shift_by_clock} Reminders:*\n"
        for time_str, desc in upcoming_reminders:
            status_text += f"  • {time_str} - {desc}\n"
    else:
        status_text += (
            f"⏰ *Shift {current_shift_by_clock} Reminders:* None scheduled\n"
        )

    if rt_stat.pending_reminders:
        status_text += "\n📬 *Pending reminders:*\n"
        for i, item in enumerate(rt_stat.pending_reminders[:5], 1):  # Show first 5
            mute_type = item.get("mute_type", "unknown")
            shift = item.get("shift", "?")
            status_text += f"  {i}. Shift {shift} ({mute_type})\n"
        if len(rt_stat.pending_reminders) > 5:
            status_text += f"  ... and {len(rt_stat.pending_reminders) - 5} more\n"

    try:
        sent = await update.message.reply_text(status_text, parse_mode="Markdown")
    except Exception:
        # Markdown failed — strip all formatting and send plain
        plain = status_text.replace("*", "").replace("_", "").replace("`", "")
        sent = await update.message.reply_text(plain)

    # Remember the status message (deleted at the next hourly plan reminder as a
    # safety net) and auto-delete both it and the /bot_status command message
    # after 2 minutes.
    status_message_id = getattr(sent, "message_id", None)
    bot_state_set(
        BOT_STATUS_MSG_KEY, str(status_message_id or ""), chat_id
    )
    _schedule_bot_status_autodelete(
        context.job_queue,
        status_message_id,
        chat_id,
        command_message_id=getattr(update.message, "message_id", None),
    )
