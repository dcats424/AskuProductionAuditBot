import asyncio
import logging
from datetime import datetime, time

from telegram.ext import ContextTypes

from config import (
    HOURLY_PLAN_FIRST_HOUR_START_MINUTE,
    HOURLY_PLAN_WINDOW_END_MINUTE,
    HOURLY_PLAN_WINDOW_START_MINUTE,
    HOURLY_SUMMARY_LAST_HOUR_START,
    HOURLY_SUMMARY_WINDOW_END,
    HOURLY_SUMMARY_WINDOW_START,
    LINE_STATE_RUNNING,
    SHIFT_SUMMARY_WINDOW_END,
    SHIFT_SUMMARY_WINDOW_START,
    chat_id_for_line,
    configured_lines,
    default_chat_id,
    format_date_time_12h,
    get_shift_for_time,
    now_ethiopia,
)
from db import _shift_had_any_production, bot_state_get, bot_state_set
from messaging import _delete_bot_status_msg
from state import line_runtime

logger = logging.getLogger(__name__)


async def send_or_queue_reminder(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    parse_mode: str | None = "Markdown",
    meta: dict | None = None,
    chat_id: int | None = None,
) -> str:
    """
    Central dispatch for all scheduled reminders.

    meta: optional dict with reminder frame info for tracking/deletion:
          {"kind": "hourly_plan"|"hourly_summary", "shift": int, "hour": int}

    Suppression rules (line OFF / sanitation ON):
    ─────────────────────────────────────────────
    CASE 1 — Line was OFF or sanitation ON at shift start, never changed:
        Never send any reminder (entire shift ignored).
        Detected by: line state non-running AND shift_had_production[shift] is False.

    CASE 2 — Line turned OFF during shift:
        Allow exactly ONE next scheduled reminder after the OFF event.
        After that reminder fires, suppress all remaining hourly reminders.
        Shift summary is NEVER suppressed (production occurred).

    CASE 3 — OFF near shift end (final hour has both hourly summary + shift summary):
        Both must execute. Shift summary is never suppressed.

    CASE 4 — Line ON: all reminders fire normally.

    AI audit block: queues ALL reminders (flush on audit end).
    """
    rt = line_runtime(chat_id)
    if chat_id is None:
        chat_id = default_chat_id()
        if chat_id is None:
            logger.error("send_or_queue_reminder: no chat_id available")
            return "failed"

    now = now_ethiopia()
    shift_now = get_shift_for_time(now)
    date_now = now.date()

    # ── Classify reminder type ──────────────────────────────────────────────
    is_shift_summary = "Handoff Report" in text
    is_hourly_summary = "Hourly Summary Reminder" in text
    is_any_summary = is_shift_summary or is_hourly_summary

    is_planning_reminder = (
        "Daily Production Plan Reminder" in text
        or ("Plan Reminder" in text and "Hourly" not in text)
        or "Hourly Plan Reminder" in text
    )

    # ── Line state checks ───────────────────────────────────────────────────
    line_is_inactive = rt.line_state != LINE_STATE_RUNNING

    if line_is_inactive:
        # CASE 1: Line was OFF/sanitation ON the ENTIRE shift (no production at all).
        # Suppress everything including summaries.
        # Also check DB (survives restart) — in-memory flag resets to False on boot.
        if not rt.shift_had_production.get(shift_now, False) and not _shift_had_any_production(
            shift_now, date_now.isoformat(), chat_id
        ):
            logger.info(
                f"[SUPPRESS-CASE1] Entire shift inactive, no production — "
                f"suppressing: Shift {shift_now} | {text[:60]}"
            )
            return "suppressed"

        # CASE 2/3: Production occurred before the OFF event.
        # Shift summary must NEVER be suppressed.
        if is_shift_summary:
            # Always let shift summary through — production occurred.
            logger.info(
                f"[ALLOW-SHIFT-SUMMARY] Shift {shift_now} summary allowed "
                f"(production occurred before OFF)"
            )
            # Fall through to send below.

        elif is_hourly_summary:
            # Hourly summary: apply one-reminder rule.
            # Allow the FIRST one after OFF, suppress the rest.
            if rt.next_reminder_allowed and not rt.one_reminder_fired:
                rt.one_reminder_fired = True
                rt.next_reminder_allowed = False
                logger.info(
                    f"[ALLOW-ONE] First reminder after OFF — "
                    f"Shift {shift_now} hourly summary allowed"
                )
                # Fall through to send below.
            else:
                logger.info(
                    f"[SUPPRESS-CASE2] Post-one-reminder suppression — "
                    f"Shift {shift_now} | {text[:60]}"
                )
                return "suppressed"

        elif is_planning_reminder:
            # Planning reminders: always suppressed when line is inactive.
            # Exception: if this is the ONE allowed reminder slot and nothing has fired yet,
            # let a plan reminder through (e.g. OFF happened right before :02 plan time).
            if rt.next_reminder_allowed and not rt.one_reminder_fired:
                rt.one_reminder_fired = True
                rt.next_reminder_allowed = False
                logger.info(
                    f"[ALLOW-ONE] First reminder after OFF (plan) — "
                    f"Shift {shift_now} allowed"
                )
                # Fall through to send below.
            else:
                logger.info(
                    f"[SUPPRESS] Planning reminder suppressed (line {rt.line_state}): "
                    f"Shift {shift_now}"
                )
                return "suppressed"

        else:
            # Unknown reminder type while line inactive — suppress.
            logger.info(f"[SUPPRESS] Unknown type while line inactive, suppressing")
            return "suppressed"

    # ── AI audit block: queue (but never drop) ─────────────────────────────
    if rt.ai_block:
        rt.pending_reminders.append(
            {
                "text": text,
                "parse_mode": parse_mode,
                "created_at": now,
                "shift": shift_now,
                "date": date_now,
                "mute_type": "ai",
                "meta": meta,
            }
        )
        logger.info(
            f"Reminder queued (AI muted): Shift {shift_now} at {now.strftime('%H:%M:%S')}"
        )
        return "queued"

    # ── Send immediately ────────────────────────────────────────────────────
    logger.info(
        f"Sending reminder to group: Shift {shift_now} at {now.strftime('%H:%M:%S')}"
    )
    try:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
        )
        if meta:
            _record_reminder_message(meta, sent.message_id, chat_id)
            if meta.get("kind") == "hourly_plan":
                await _delete_bot_status_msg(context.bot, chat_id)
        return "sent"
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return "failed"


def _reminder_msg_key(kind: str, shift: int, hour: int) -> str:
    """bot_state key storing the message_id of a sent hourly reminder."""
    return f"reminder_msg_{kind}_{shift}_{hour}"


def _record_reminder_message(meta: dict, message_id: int, chat_id: int | None = None) -> None:
    """Store the Telegram message_id of a sent hourly reminder for later deletion."""
    kind = meta.get("kind")
    shift = meta.get("shift")
    hour = meta.get("hour")
    if not kind or not shift or not hour:
        return
    bot_state_set(_reminder_msg_key(kind, shift, hour), str(message_id), chat_id)
    logger.info(f"Tracked reminder message: {kind} shift={shift} hour={hour} id={message_id}")


def _previous_frame(shift: int, hour: int) -> tuple:
    """Frame (shift, hour) before the given one; wraps shift boundary (hour 1 ← prev shift hour 12)."""
    if hour > 1:
        return shift, hour - 1
    prev_shift = 2 if shift == 1 else 1
    return prev_shift, 12


async def delete_reminder_frame(bot, shift: int, hour: int, chat_id: int | None = None) -> None:
    """
    Delete the hourly plan + summary reminder messages of a given (shift, hour) frame.
    Failures (already deleted, too old, network) are logged and ignored — never crashes.
    """
    if chat_id is None:
        chat_id = default_chat_id()
    for kind in ("hourly_plan", "hourly_summary"):
        key = _reminder_msg_key(kind, shift, hour)
        msg_id_str = bot_state_get(key, chat_id)
        if not msg_id_str:
            continue
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(msg_id_str))
            logger.info(f"Deleted old {kind} reminder shift={shift} hour={hour} (id={msg_id_str})")
        except Exception as e:
            logger.warning(
                f"Could not delete {kind} reminder shift={shift} hour={hour} (id={msg_id_str}): {e}"
            )
        bot_state_set(key, "", chat_id)


async def flush_pending_reminders(bot, reason: str | None = None, chat_id: int | None = None) -> None:
    """
    Flush queued reminders.
    - reason="ai":   send ALL AI-muted reminders regardless of time/shift
    - reason="line": send AI-muted reminders only; drop all line-muted items (no backlog)
    Expired hourly plan/summary items (frame already passed) are dropped, never sent.
    """
    rt = line_runtime(chat_id)
    if chat_id is None:
        chat_id = default_chat_id()

    if not rt.pending_reminders:
        return

    now = now_ethiopia()
    current_shift_num = get_shift_for_time(now)
    current_hour_num = get_current_hour_number(current_shift_num, now)

    to_send = []
    remaining = []

    for item in rt.pending_reminders:
        meta = item.get("meta")
        if meta and meta.get("kind") in ("hourly_plan", "hourly_summary"):
            item_shift = meta.get("shift")
            item_hour = meta.get("hour")
            frame_passed = (item_shift, item_hour) < (current_shift_num, current_hour_num)
            if frame_passed:
                logger.info(
                    f"Expired queued reminder dropped: {meta.get('kind')} "
                    f"shift={item_shift} hour={item_hour} "
                    f"(now shift={current_shift_num} hour={current_hour_num})"
                )
                continue
        if reason == "ai":
            if item.get("mute_type") == "ai":
                to_send.append(item)
            else:
                remaining.append(item)
        else:
            if item.get("mute_type") == "ai":
                to_send.append(item)
            # line-muted items are intentionally dropped here — no else/remaining

    rt.pending_reminders = remaining  # only non-AI items remain (empty for reason="line")

    if not to_send:
        return

    def _reminder_priority(item):
        text = item.get("text", "")
        if "Daily Production Plan" in text:
            return 0
        if "Plan Reminder" in text and "Hourly" not in text:
            return 1  # Shift plan
        if "Handoff Report" in text:
            return 2  # Shift summary
        if "Hourly Plan" in text:
            return 3
        if "Hourly Summary" in text:
            return 4
        return 5

    to_send.sort(
        key=lambda x: (_reminder_priority(x), x.get("created_at", datetime.min))
    )

    for item in to_send:
        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=item["text"],
                parse_mode=item.get("parse_mode"),
            )
            meta = item.get("meta")
            if meta:
                _record_reminder_message(meta, sent.message_id, chat_id)
                if meta.get("kind") == "hourly_plan":
                    prev_shift, prev_hour = _previous_frame(
                        meta["shift"], meta["hour"]
                    )
                    await delete_reminder_frame(bot, prev_shift, prev_hour, chat_id)
                    await _delete_bot_status_msg(bot, chat_id)
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"flush_pending_reminders: failed to send item: {e}")



# ---------------- RECONNECTION / MISSED REMINDER RECOVERY ----------------
_last_successful_send: datetime | None = None
_recovery_task_running = False


async def recover_missed_reminders_on_reconnect(app, chat_id: int | None = None) -> None:
    """
    Called when internet reconnects. Sends ONLY reminders still inside their
    valid time windows. If current time is outside a reminder's window, skip
    permanently (no late sends).

    Line state rules:
    - CASE 1: Line OFF/sanitation the ENTIRE shift (no production at all)
              → suppress EVERYTHING including summaries
    - CASE 2: Line turned OFF during shift (production occurred before OFF)
              → planning reminders suppressed, summaries still fire
    - Line running normally → all reminders fire normally
    """
    rt = line_runtime(chat_id)
    if chat_id is None:
        chat_id = default_chat_id()
        if chat_id is None:
            logger.error("recover_missed_reminders_on_reconnect: no chat_id")
            return
    now = now_ethiopia()
    today_iso = now.date().isoformat()
    current_shift_num = get_shift_for_time(now)
    current_minutes = now.hour * 60 + now.minute
    if current_shift_num == 2 and now.hour < 7:
        current_minutes += 24 * 60
    shift_start_minutes = {1: 7 * 60, 2: 19 * 60}
    start = shift_start_minutes[current_shift_num]
    if current_shift_num == 2 and now.hour < 7:
        start = 19 * 60
    minutes_into_shift = current_minutes - start

    line_is_active = rt.line_state == LINE_STATE_RUNNING

    # Check if this shift had ANY production at all
    # Checks both in-memory (survives within session) and DB (survives restart)
    shift_has_production = rt.shift_had_production.get(
        current_shift_num, False
    ) or _shift_had_any_production(current_shift_num, today_iso, chat_id)

    logger.info(
        f"[RECOVERY] Reconnected at {now.strftime('%H:%M')} "
        f"Shift {current_shift_num} | line_state={rt.line_state} | "
        f"shift_has_production={shift_has_production}"
    )

    # ── CASE 1: Line OFF entire shift with zero production ───────────────────
    # Suppress absolutely everything — no reminders at all
    if not line_is_active and not shift_has_production:
        logger.info(
            f"[RECOVERY] CASE 1: Line OFF entire shift, no production — "
            f"suppressing all recovery reminders for Shift {current_shift_num}"
        )
        return

    sent_count = 0

    # ── 1. Daily Plan ────────────────────────────────────────────────────────
    # Planning reminder — only send if line is currently active
    if line_is_active:
        if not bot_state_get(f"daily_plan_{today_iso}", chat_id) and not bot_state_get(
            f"daily_plan_catchup_{today_iso}", chat_id
        ):
            header = f"📅 {format_date_time_12h(now)}\n\n"
            text = (
                header
                + "📆 *Daily Production Plan Reminder* _(missed — reconnected)_\n\n"
                + "Please share today's overall production plan:\n"
                + "- Products and SKUs by shift\n"
                + "- Target packs per shift\n"
                + "- Any known constraints (utilities, materials, manpower)."
            )
            try:
                await app.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="Markdown"
                )
                bot_state_set(f"daily_plan_{today_iso}", "1", chat_id)
                bot_state_set(f"daily_plan_catchup_{today_iso}", "1", chat_id)
                rt.last_plan_date = now.date()
                bot_state_set("daily_plan_last_date", today_iso, chat_id)
                sent_count += 1
                await asyncio.sleep(1)
                logger.info("[RECOVERY] Daily plan sent")
            except Exception as e:
                logger.error(f"[RECOVERY] Daily plan failed: {e}")
    else:
        logger.info("[RECOVERY] Daily plan skipped — line OFF/sanitation (CASE 2)")

    # ── 2. Shift Plan ────────────────────────────────────────────────────────
    # Planning reminder — only send if line is currently active
    if line_is_active:
        recovery_key = f"shift_plan_recovery_{today_iso}_{current_shift_num}"
        fired_key = f"shift_plan_fired_{today_iso}_{current_shift_num}"
        catch_key = f"shift_plan_catchup_{today_iso}_{current_shift_num}"
        if (
            not bot_state_get(recovery_key, chat_id)
            and not bot_state_get(fired_key, chat_id)
            and not bot_state_get(catch_key, chat_id)
        ):
            header = f"📅 {format_date_time_12h(now)}\n\n"
            text = (
                header
                + f"📋 *Shift {current_shift_num} Plan Reminder* _(missed)_\n\n"
                + "- Product type\n"
                + "- Shift plan (packs)\n"
                + "- Expected manpower / constraints"
            )
            try:
                await app.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="Markdown"
                )
                bot_state_set(recovery_key, "1", chat_id)
                bot_state_set(fired_key, "1", chat_id)
                rt.plan_sent_today[current_shift_num] = now.date()
                sent_count += 1
                await asyncio.sleep(1)
                logger.info(f"[RECOVERY] Shift {current_shift_num} plan sent")
            except Exception as e:
                logger.error(f"[RECOVERY] Shift plan failed: {e}")
    else:
        logger.info(
            f"[RECOVERY] Shift {current_shift_num} plan skipped "
            f"— line OFF/sanitation (CASE 2)"
        )

    # ── 3. Hourly Plan ───────────────────────────────────────────────────────
    # Planning reminder — only send if line is currently active
    current_hour_num = get_current_hour_number(current_shift_num, now)
    if line_is_active:
        sched_key = (
            f"hourly_plan_scheduled_{today_iso}_{current_shift_num}_{current_hour_num}"
        )
        catch_key = f"hourly_plan_{today_iso}_{current_shift_num}_{current_hour_num}"
        # Only check catch_key — sched_key may have been written while line was OFF
        # (scheduler ran but message was suppressed), so it's not a reliable sent indicator
        if not bot_state_get(catch_key, chat_id) and is_in_hourly_plan_window(
            current_shift_num, current_hour_num, now
        ):
            header = f"📅 {format_date_time_12h(now)}\n\n"
            text = (
                header
                + f"⏰ *Hourly Plan – Shift {current_shift_num}, Hour {current_hour_num}*"
                + " _(missed — reconnected)_\n\n"
                + "Please share the plan for this hour:\n"
                + "- Production target\n"
                + "- Any scheduled maintenance or adjustments\n"
                + "- Expected challenges"
            )
            try:
                sent = await app.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="Markdown"
                )
                bot_state_set(sched_key, "1", chat_id)
                bot_state_set(catch_key, "1", chat_id)
                _record_reminder_message(
                    {
                        "kind": "hourly_plan",
                        "shift": current_shift_num,
                        "hour": current_hour_num,
                    },
                    sent.message_id,
                    chat_id,
                )
                prev_shift, prev_hour = _previous_frame(current_shift_num, current_hour_num)
                await delete_reminder_frame(app.bot, prev_shift, prev_hour, chat_id)
                sent_count += 1
                await asyncio.sleep(1)
                logger.info(
                    f"[RECOVERY] Hourly plan Shift {current_shift_num} "
                    f"Hr {current_hour_num} sent"
                )
            except Exception as e:
                logger.error(f"[RECOVERY] Hourly plan failed: {e}")
    else:
        logger.info(
            f"[RECOVERY] Hourly plan Shift {current_shift_num} Hr {current_hour_num} "
            f"skipped — line OFF/sanitation (CASE 2)"
        )

    # ── 4. Hourly Summary ────────────────────────────────────────────────────
    # Summary reminder — always send (CASE 1 with line OFF + no production
    # already returned early above)
    if is_in_hourly_summary_window(now, current_shift_num, current_hour_num):
        sched_key = f"hourly_summary_scheduled_{today_iso}_{current_shift_num}_{current_hour_num}"
        catch_key = (
            f"hourly_summary_{today_iso}_{current_shift_num}_{current_hour_num}"
        )
        if not bot_state_get(sched_key, chat_id) and not bot_state_get(catch_key, chat_id):
            header = f"📅 {format_date_time_12h(now)}\n\n"
            text = (
                header
                + f"📝 *Hourly Summary – Shift {current_shift_num}, Hour {current_hour_num}*"
                + " _(missed — reconnected)_\n\n"
                + "Please provide hourly production data:\n"
                + "- Actual output for this hour\n"
                + "- Downtime events (if any)\n"
                + "- Rejects (if any)\n"
                + "- Operator notes\n\n"
                + "💡 AI will generate an hourly summary after you submit the data."
            )
            try:
                sent = await app.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="Markdown"
                )
                bot_state_set(sched_key, "1", chat_id)
                bot_state_set(catch_key, "1", chat_id)
                _record_reminder_message(
                    {
                        "kind": "hourly_summary",
                        "shift": current_shift_num,
                        "hour": current_hour_num,
                    },
                    sent.message_id,
                    chat_id,
                )
                sent_count += 1
                await asyncio.sleep(1)
                logger.info(
                    f"[RECOVERY] Hourly summary Shift {current_shift_num} "
                    f"Hr {current_hour_num} sent"
                )
            except Exception as e:
                logger.error(f"[RECOVERY] Hourly summary failed: {e}")

    # ── 5. Shift Summary ─────────────────────────────────────────────────────
    # Summary reminder — always send (CASE 1 with line OFF + no production
    # already returned early above)
    if is_in_shift_summary_window(current_shift_num, now):
        fired_key = f"shift_report_fired_{today_iso}_{current_shift_num}"
        recovery_key = f"shift_report_recovery_{today_iso}_{current_shift_num}"
        if not bot_state_get(fired_key, chat_id) and not bot_state_get(recovery_key, chat_id):
            header = f"📅 {format_date_time_12h(now)}\n\n"
            text = (
                header
                + f"📊 *Shift {current_shift_num} Handoff Report*\n\n"
                + "- How did the shift go?\n"
                + "- Any issues or challenges for the next shift?\n"
                + "- What should the next shift be aware of?\n"
                + "- Status: All clear / Needs attention"
            )
            try:
                await app.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="Markdown"
                )
                bot_state_set(recovery_key, "1", chat_id)
                bot_state_set(fired_key, "1", chat_id)
                sent_count += 1
                await asyncio.sleep(1)
                logger.info(f"[RECOVERY] Shift {current_shift_num} report sent")
            except Exception as e:
                logger.error(f"[RECOVERY] Shift report failed: {e}")

    if sent_count > 0:
        logger.info(f"[RECOVERY] Sent {sent_count} missed reminder(s)")
    else:
        logger.info("[RECOVERY] No missed reminders to send")


async def connection_watchdog(app) -> None:
    """
    Background task: pings Telegram every 60 seconds.
    When it detects a reconnect after failure, calls recover_missed_reminders_on_reconnect().
    """
    global _last_successful_send, _recovery_task_running
    _recovery_task_running = True
    was_offline = False

    logger.info("[WATCHDOG] Connection watchdog started")

    while True:
        await asyncio.sleep(60)  # check every 60 seconds
        try:
            await app.bot.get_me()  # lightweight ping
            if was_offline:
                logger.info(
                    "[WATCHDOG] Connection restored! Running missed reminder recovery..."
                )
                was_offline = False
                for line_chat in (chat_id_for_line(k) for k in configured_lines()):
                    try:
                        await recover_missed_reminders_on_reconnect(app, line_chat)
                    except Exception as e:
                        logger.error(
                            f"[WATCHDOG] Recovery failed for chat {line_chat}: {e}"
                        )
            _last_successful_send = now_ethiopia()
        except Exception as e:
            if not was_offline:
                logger.warning(f"[WATCHDOG] Connection lost: {e}")
            was_offline = True



async def remind_shift_plan(context: ContextTypes.DEFAULT_TYPE):
    chat_id = (context.job.data or {}).get("chat_id")
    rt_rsp = line_runtime(chat_id)
    shift = context.job.data["shift"]
    now = now_ethiopia()
    today = now.date()
    today_iso = today.isoformat()

    key = f"shift_plan_fired_{today_iso}_{shift}"
    if bot_state_get(key, chat_id):
        logger.info(f"Shift {shift} plan already fired today, skipping")
        return

    if rt_rsp.line_state != LINE_STATE_RUNNING:
        logger.info(
            f"Shift {shift} plan suppressed (line={rt_rsp.line_state}) "
            f"— key NOT written, catchup will resend on line_on"
        )
        return

    header = f"📅 {format_date_time_12h(now)}\n\n"
    text = (
        header
        + f"📋 *Shift {shift} Plan Reminder*\n\n"
        + "- Product type\n"
        + "- Shift plan (packs)\n"
        + "- Expected manpower / constraints"
    )
    result = await send_or_queue_reminder(
        context, text, parse_mode="Markdown", chat_id=chat_id
    )
    if result in ("sent", "queued"):
        bot_state_set(key, "1", chat_id)
        rt_rsp.plan_sent_today[shift] = today
        logger.info(f"Shift {shift} plan reminder fired by scheduler at :02")
    else:
        logger.warning(
            f"Shift {shift} plan reminder NOT marked sent (delivery failed) — will retry on reconnect"
        )


async def remind_shift_report(context: ContextTypes.DEFAULT_TYPE):
    chat_id = (context.job.data or {}).get("chat_id")
    shift = context.job.data["shift"]
    now = now_ethiopia()
    if not is_in_shift_summary_window(shift, now):
        logger.info(
            f"Shift {shift} summary outside window (min={now.minute}), skipping"
        )
        return

    today_iso = now.date().isoformat()
    key = f"shift_report_fired_{today_iso}_{shift}"
    if bot_state_get(key, chat_id):
        logger.info(f"Shift {shift} report already fired today, skipping")
        return

    header = f"📅 {format_date_time_12h(now)}\n\n"
    text = (
        header
        + f"📊 *Shift {shift} Handoff Report*\n\n"
        + "- How did the shift go?\n"
        + "- Any issues or challenges for the next shift?\n"
        + "- What should the next shift be aware of?\n"
        + "- Status: All clear / Needs attention"
    )
    result = await send_or_queue_reminder(
        context, text, parse_mode="Markdown", chat_id=chat_id
    )

    # Write key ONLY if actually sent or queued (not failed)
    if result in ("sent", "queued"):
        bot_state_set(key, "1", chat_id)
        logger.info(f"Shift {shift} report reminder fired by scheduler at :55")
    else:
        logger.warning(
            f"Shift {shift} report reminder NOT marked sent (delivery failed) — will retry on reconnect"
        )


async def remind_hourly_plan(context: ContextTypes.DEFAULT_TYPE):
    now = now_ethiopia()
    job_data = context.job.data or {}
    chat_id = job_data.get("chat_id")
    rt_rhp = line_runtime(chat_id)

    shift = job_data.get("shift") or get_shift_for_time(now)
    hour = job_data.get("hour") or get_current_hour_number(shift, now)
    today_iso = now.date().isoformat()

    if not is_in_hourly_plan_window(shift, hour, now):
        logger.info(
            f"Hourly plan Shift {shift} Hour {hour} outside window (min={now.minute}), skipping"
        )
        return

    sched_key = f"hourly_plan_scheduled_{today_iso}_{shift}_{hour}"
    catch_key = f"hourly_plan_{today_iso}_{shift}_{hour}"

    if bot_state_get(catch_key, chat_id):
        logger.info(f"Hourly plan Shift {shift} Hour {hour} already sent, skipping")
        return

    # Line inactive — do NOT write DB key so catchup on line_on can resend
    if rt_rhp.line_state != LINE_STATE_RUNNING:
        logger.info(
            f"Hourly plan Shift {shift} Hour {hour} suppressed (line={rt_rhp.line_state}) "
            f"— key NOT written, catchup will resend on line_on"
        )
        return

    header = f"📅 {format_date_time_12h(now)}\n\n"
    text = (
        header
        + f"⏰ *Hourly Plan Reminder – Shift {shift}, Hour {hour}*\n\n"
        + "Please share the plan for this hour:\n"
        + "- Production target\n"
        + "- Any scheduled maintenance or adjustments\n"
        + "- Expected challenges"
    )

    # ✅ Capture result
    meta = {"kind": "hourly_plan", "shift": shift, "hour": hour}
    success = await send_or_queue_reminder(
        context, text, parse_mode="Markdown", meta=meta, chat_id=chat_id
    )

    # ✅ Write DB ONLY if message actually sent
    if success in ("sent", "queued"):
        bot_state_set(sched_key, "1", chat_id)
        bot_state_set(catch_key, "1", chat_id)
        logger.info(f"Hourly plan confirmed sent: Shift {shift} Hour {hour}")
        # New hour started → remove the previous hour's plan + summary reminders
        if success == "sent":
            prev_shift, prev_hour = _previous_frame(shift, hour)
            await delete_reminder_frame(context.bot, prev_shift, prev_hour, chat_id)
    else:
        logger.warning(
            f"Hourly plan NOT marked sent (delivery failed): Shift {shift} Hour {hour}"
        )


async def remind_hourly_summary(context: ContextTypes.DEFAULT_TYPE):
    now = now_ethiopia()
    job_data = context.job.data or {}
    chat_id = job_data.get("chat_id")
    shift = job_data.get("shift") or get_shift_for_time(now)
    hour = job_data.get("hour") or get_current_hour_number(shift, now)

    if not is_in_hourly_summary_window(now, shift, hour):
        logger.info(f"Hourly summary outside window (min={now.minute}), skipping")
        return

    today_iso = now.date().isoformat()
    sched_key = f"hourly_summary_scheduled_{today_iso}_{shift}_{hour}"
    catch_key = f"hourly_summary_{today_iso}_{shift}_{hour}"

    if bot_state_get(sched_key, chat_id):
        logger.info(f"Hourly summary Shift {shift} Hour {hour} already sent, skipping")
        return

    header = f"📅 {format_date_time_12h(now)}\n\n"
    text = (
        header
        + f"📝 *Hourly Summary Reminder – Shift {shift}, Hour {hour}*\n\n"
        + "Please provide hourly production data:\n"
        + "- Actual output for this hour\n"
        + "- Downtime events (if any)\n"
        + "- Rejects (if any)\n"
        + "- Operator notes\n\n"
        + "💡 AI will generate an hourly summary after you submit the data."
    )
    result = await send_or_queue_reminder(
        context,
        text,
        parse_mode="Markdown",
        meta={"kind": "hourly_summary", "shift": shift, "hour": hour},
        chat_id=chat_id,
    )

    # ✅ Only write DB keys if actually sent or queued (not failed)
    if result in ("sent", "queued"):
        bot_state_set(sched_key, "1", chat_id)
        bot_state_set(catch_key, "1", chat_id)
        logger.info(f"Hourly summary fired: Shift {shift} Hour {hour}")
    else:
        logger.warning(
            f"Hourly summary NOT marked sent (delivery failed): Shift {shift} Hour {hour}"
        )


async def remind_daily_production_plan(context: ContextTypes.DEFAULT_TYPE):
    chat_id = (context.job.data or {}).get("chat_id")
    rt_rdp = line_runtime(chat_id)
    now = now_ethiopia()
    today = now.date()
    today_iso = today.isoformat()

    key = f"daily_plan_{today_iso}"
    if bot_state_get(key, chat_id):
        logger.info("Daily plan already sent today (scheduled job), skipping")
        return

    if rt_rdp.line_state != LINE_STATE_RUNNING:
        logger.info(
            f"Daily plan suppressed (line={rt_rdp.line_state}) "
            f"— key NOT written, catchup will resend on line_on"
        )
        return

    header = f"📅 {format_date_time_12h(now)}\n\n"
    text = (
        header
        + "📆 *Daily Production Plan Reminder*\n\n"
        + "Please share today's overall production plan:\n"
        + "- Products and SKUs by shift\n"
        + "- Target packs per shift\n"
        + "- Any known constraints (utilities, materials, manpower)."
    )
    result = await send_or_queue_reminder(
        context, text, parse_mode="Markdown", chat_id=chat_id
    )
    if result in ("sent", "queued"):
        bot_state_set(key, "1", chat_id)
        rt_rdp.last_plan_date = today
        bot_state_set("daily_plan_last_date", today_iso, chat_id)
        logger.info("Daily plan reminder fired by scheduler")
    else:
        logger.warning(
            "Daily plan reminder NOT marked sent (delivery failed) — will retry on reconnect"
        )



async def send_daily_plan_if_needed(
    bot, now: datetime, skip_window_check: bool = False, chat_id: int | None = None
) -> bool:
    """
    Send daily plan catchup if not yet sent today.
    skip_window_check: when True (line_on, sanitation_end, reboot), post regardless of time.
    Otherwise only post within first 45 min of shift 1.
    """
    if chat_id is None:
        chat_id = default_chat_id()
    rt_sdp = line_runtime(chat_id)
    today = now.date()
    today_iso = today.isoformat()

    # Only skip if the catchup key was set — meaning we actually delivered it.
    # Do NOT skip based on daily_plan_{today_iso} alone — scheduler may have
    # been suppressed while line was OFF and never written that key (with the
    # new fix), but old stale keys from previous session could still exist.
    if bot_state_get(f"daily_plan_catchup_{today_iso}", chat_id):
        logger.info("Daily plan already sent today (catchup check), skipping")
        return False

    if not skip_window_check and not is_in_daily_plan_recovery_window(now):
        logger.info("Daily plan catchup: outside window, skipping")
        return False

    header = f"📅 {format_date_time_12h(now)}\n\n"
    daily_plan_text = (
        header
        + "📆 *Daily Production Plan Reminder*\n\n"
        + "Please share today's overall production plan:\n"
        + "- Products and SKUs by shift\n"
        + "- Target packs per shift\n"
        + "- Any known constraints (utilities, materials, manpower)."
    )
    try:
        await bot.send_message(
            chat_id=chat_id, text=daily_plan_text, parse_mode="Markdown"
        )
        # Only mark as sent AFTER successful delivery
        bot_state_set(f"daily_plan_catchup_{today_iso}", "1", chat_id)
        bot_state_set(f"daily_plan_{today_iso}", "1", chat_id)
        rt_sdp.last_plan_date = today
        bot_state_set("daily_plan_last_date", today_iso, chat_id)
        logger.info("Daily plan reminder sent (catchup)")
        return True
    except Exception as e:
        logger.error(f"Failed to send daily plan reminder (catchup): {e}")
        return False


async def send_shift_plan_if_needed(
    bot,
    current_shift_num: int,
    now: datetime,
    skip_window_check: bool = False,
    chat_id: int | None = None,
) -> bool:
    """Send shift plan for current shift if not yet sent (once per shift)."""
    if chat_id is None:
        chat_id = default_chat_id()
    rt_ssp = line_runtime(chat_id)
    today = now.date()
    today_iso = today.isoformat()

    actual_shift = get_shift_for_time(now)
    if actual_shift != current_shift_num:
        return False

    catch_key = f"shift_plan_catchup_{today_iso}_{current_shift_num}"
    fired_key = f"shift_plan_fired_{today_iso}_{current_shift_num}"

    # Only skip based on catch_key (confirmed delivery).
    # fired_key alone is NOT reliable — scheduler may have been suppressed
    # while line was OFF and never written it (with the new fix), but a
    # stale fired_key from a previous run could still block the catchup.
    if bot_state_get(catch_key, chat_id):
        logger.info(f"Shift {current_shift_num} plan already sent, skipping catchup")
        return False

    if not skip_window_check:
        shift_start_minutes = {1: 7 * 60, 2: 19 * 60}
        start = shift_start_minutes[current_shift_num]
        current_minutes = now.hour * 60 + now.minute
        if current_shift_num == 2 and now.hour < 7:
            current_minutes += 24 * 60
            start = 19 * 60
        minutes_into_shift = current_minutes - start
        if minutes_into_shift < 0 or minutes_into_shift > 45:
            return False

    header = f"📅 {format_date_time_12h(now)}\n\n"
    text = (
        header
        + f"📋 *Shift {current_shift_num} Plan Reminder*\n\n"
        + "- Product type\n"
        + "- Shift plan (packs)\n"
        + "- Expected manpower / constraints"
    )
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        # Only mark as sent AFTER successful delivery
        bot_state_set(catch_key, "1", chat_id)
        bot_state_set(fired_key, "1", chat_id)
        rt_ssp.plan_sent_today[current_shift_num] = today
        logger.info(f"Shift {current_shift_num} plan sent (catchup)")
        return True
    except Exception as e:
        logger.error(f"Failed to send shift plan (catchup): {e}")
        return False


def get_current_hour_number(current_shift_num: int, now: datetime) -> int:
    """
    Get the current hour number within the shift.

    PC/international shift windows:
    - Shift 1: 07:00–19:00 (12 hours)
    - Shift 2: 19:00–07:00 (12 hours, wraps midnight)
    """
    minutes = now.hour * 60 + now.minute

    if current_shift_num == 1:
        start = 7 * 60
        shift_hours = 12
    else:
        start = 19 * 60
        shift_hours = 12
        if minutes < 7 * 60:
            minutes += 24 * 60

    elapsed = max(0, minutes - start)
    hour_num = int(elapsed // 60) + 1
    if hour_num < 1:
        hour_num = 1
    if hour_num > shift_hours:
        hour_num = shift_hours
    return hour_num


async def send_current_hour_plan(
    bot,
    current_shift_num: int,
    now: datetime,
    force_if_late: bool = False,
    chat_id: int | None = None,
) -> bool:
    """
    Send hourly plan for the current hour if not already sent.
    force_if_late=True: send even if outside the normal :02-:30 window
                        (used when line turns ON mid-hour after being OFF).
    Never sends at :55+ (summary window).
    """
    if chat_id is None:
        chat_id = default_chat_id()
    current_hour = get_current_hour_number(current_shift_num, now)
    today_iso = now.date().isoformat()

    # Never send during summary window — too late for a plan
    if now.minute >= 55:
        logger.info(
            f"Hourly plan Shift {current_shift_num} Hour {current_hour} "
            f"skipped — in summary window (min={now.minute})"
        )
        return False

    # Normal window check — skip only if NOT forcing late send
    if not force_if_late and not is_in_hourly_plan_window(
        current_shift_num, current_hour, now
    ):
        logger.info(
            f"Hourly plan Shift {current_shift_num} Hour {current_hour} "
            f"outside window (min={now.minute}), skipping"
        )
        return False

    sched_key = f"hourly_plan_scheduled_{today_iso}_{current_shift_num}_{current_hour}"
    catch_key = f"hourly_plan_{today_iso}_{current_shift_num}_{current_hour}"

    # Only skip if catch_key set — confirmed actual delivery
    if bot_state_get(catch_key, chat_id):
        logger.info(
            f"Hourly plan Shift {current_shift_num} Hour {current_hour} already sent, skipping"
        )
        return False

    late_note = " _(late — line resumed)_" if force_if_late and now.minute > 30 else ""
    header = f"📅 {format_date_time_12h(now)}\n\n"
    text = (
        header
        + f"⏰ *Hourly Plan Reminder – Shift {current_shift_num}, Hour {current_hour}*"
        + f"{late_note}\n\n"
        + "Please share the plan for this hour:\n"
        + "- Production target\n"
        + "- Any scheduled maintenance or adjustments\n"
        + "- Expected challenges"
    )
    try:
        sent = await bot.send_message(
            chat_id=chat_id, text=text, parse_mode="Markdown"
        )
        bot_state_set(catch_key, "1", chat_id)
        bot_state_set(sched_key, "1", chat_id)
        _record_reminder_message(
            {"kind": "hourly_plan", "shift": current_shift_num, "hour": current_hour},
            sent.message_id,
            chat_id=chat_id,
        )
        prev_shift, prev_hour = _previous_frame(current_shift_num, current_hour)
        await delete_reminder_frame(bot, prev_shift, prev_hour, chat_id)
        logger.info(
            f"Hourly plan sent (catchup{'/late' if force_if_late else ''}): "
            f"Shift {current_shift_num} Hour {current_hour}"
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send hourly plan (catchup): {e}")
        return False


async def handle_partial_hours_on_line_resume(
    bot,
    current_shift_num: int,
    line_off_time: datetime,
    line_on_time: datetime,
    chat_id: int | None = None,
):
    """Handle partial hours when line comes back on after being off."""
    if chat_id is None:
        chat_id = default_chat_id()
    if not line_off_time:
        return

    off_time = line_off_time.time()
    on_time = line_on_time.time()
    current_hour = get_current_hour_number(current_shift_num, line_on_time)

    # Calculate production time (time when line was ON) in the hour when line went off
    # We need to check if there was MORE than 20 minutes of production in that hour
    off_minutes = off_time.hour * 60 + off_time.minute
    on_minutes = on_time.hour * 60 + on_time.minute

    # Get hour start time for the hour when line went off
    hour_start_minutes = off_time.hour * 60  # Start of the hour (e.g., 3:00 = 180)
    hour_end_minutes = hour_start_minutes + 60  # End of the hour (e.g., 4:00 = 240)

    # Calculate production time in the hour when line went off
    if off_time.hour == on_time.hour:
        # Same hour: production = time before off + time after on (remaining in hour)
        production_before_off = off_minutes - hour_start_minutes
        production_after_on = hour_end_minutes - on_minutes
        total_production_minutes = production_before_off + production_after_on
    else:
        # Different hour: only count production before line went off in that hour
        # (Line came back in a different hour, so that hour is done)
        total_production_minutes = off_minutes - hour_start_minutes

    # Only send if there was >20 min production AND we're in hourly summary window (:55-:59 or :50-:59 last hour)
    if total_production_minutes > 20 and is_in_hourly_summary_window(
        line_on_time, current_shift_num, current_hour
    ):
        # Check if hourly summary was already sent for this hour
        today_iso = line_on_time.date().isoformat()
        key = f"hourly_summary_{today_iso}_{current_shift_num}_{current_hour}"
        if not bot_state_get(key, chat_id):
            header = f"📅 {format_date_time_12h(line_on_time)}\n\n"
            hourly_summary_text = (
                header
                + f"📝 *Hourly Summary Reminder – Shift {current_shift_num}, Hour {current_hour}*\n\n"
                + f"⚠️ Partial hour production ({total_production_minutes} min active production)\n\n"
                + "Please provide hourly production data:\n"
                + "- Actual output for this period\n"
                + "- Downtime events (if any)\n"
                + "- Rejects (if any)\n"
                + "- Operator notes\n\n"
                + "💡 AI will generate an hourly summary after you submit the data."
            )
            try:
                sent = await bot.send_message(
                    chat_id=chat_id,
                    text=hourly_summary_text,
                    parse_mode="Markdown",
                )
                bot_state_set(key, "1", chat_id)
                _record_reminder_message(
                    {
                        "kind": "hourly_summary",
                        "shift": current_shift_num,
                        "hour": current_hour,
                    },
                    sent.message_id,
                    chat_id=chat_id,
                )
                logger.info(
                    f"Partial hour summary reminder sent for Shift {current_shift_num}, Hour {current_hour} ({total_production_minutes} min production)"
                )
            except Exception as e:
                logger.error(f"Failed to send partial hour summary: {e}")
        else:
            logger.info(
                f"Hourly summary already sent for Shift {current_shift_num} Hour {current_hour} today, skipping"
            )

    # If in shift summary window (:55-:59 at shift end), also send shift summary reminder
    if is_in_shift_summary_window(current_shift_num, line_on_time):
        # Check if shift summary was already sent for this shift today
        today_iso = line_on_time.date().isoformat()
        key = f"shift_report_{today_iso}_{current_shift_num}"
        if not bot_state_get(key, chat_id):
            header = f"📅 {format_date_time_12h(line_on_time)}\n\n"
            shift_summary_text = (
                header
                + f"📊 *Shift {current_shift_num} Handoff Report*\n\n"
                + "- How did the shift go?\n"
                + "- Any issues or challenges for the next shift?\n"
                + "- What should the next shift be aware of?\n"
                + "- Status: All clear / Needs attention"
            )
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=shift_summary_text,
                    parse_mode="Markdown",
                )
                bot_state_set(key, "1", chat_id)
                logger.info(
                    f"Shift {current_shift_num} summary reminder sent (near shift end after line resume)"
                )
            except Exception as e:
                logger.error(f"Failed to send shift summary reminder: {e}")


def is_near_shift_end(current_shift_num: int, now: datetime) -> bool:
    """True if within last 20 minutes of shift end. Used for partial-hour logic only."""
    t = now.time()
    if current_shift_num == 1:  # PC ends 19:00
        return time(18, 40) <= t < time(19, 0)
    # Shift 2 ends at 07:00
    return time(6, 40) <= t < time(7, 0)



# ---------------- STRICT TIMING WINDOWS ----------------
# All reminder execution must stay within these windows. Never execute outside.



def is_in_hourly_plan_window(shift: int, hour: int, now: datetime) -> bool:
    """Hourly Plan: :02-:30 (normal) or :05-:30 (first hour of shift). Never after :30."""
    m = now.minute
    if m > HOURLY_PLAN_WINDOW_END_MINUTE:
        return False
    if hour == 1:
        return HOURLY_PLAN_FIRST_HOUR_START_MINUTE <= m <= HOURLY_PLAN_WINDOW_END_MINUTE
    return HOURLY_PLAN_WINDOW_START_MINUTE <= m <= HOURLY_PLAN_WINDOW_END_MINUTE


def is_in_hourly_summary_window(
    now: datetime, shift: int | None = None, hour: int | None = None
) -> bool:
    """Hourly Summary: :55-:59 (normal hours), :50-:59 (last hour of shift). Never after :59."""
    m = now.minute
    if m > HOURLY_SUMMARY_WINDOW_END:
        return False
    shift_hours = {1: 12, 2: 12}
    is_last = (
        shift is not None and hour is not None and (hour == shift_hours.get(shift, 0))
    )
    start = HOURLY_SUMMARY_LAST_HOUR_START if is_last else HOURLY_SUMMARY_WINDOW_START
    return start <= m <= HOURLY_SUMMARY_WINDOW_END


def is_in_shift_summary_window(shift: int, now: datetime) -> bool:
    """Shift Summary: strict :55-:59 at end of shift only. Never outside that window."""
    t = now.time()
    m = now.minute
    if m < SHIFT_SUMMARY_WINDOW_START or m > SHIFT_SUMMARY_WINDOW_END:
        return False
    if shift == 1:
        return time(18, 55) <= t <= time(18, 59)
    return time(6, 55) <= t <= time(6, 59)


def is_in_daily_plan_recovery_window(now: datetime) -> bool:
    """Daily Plan recovery: first 45 min of shift 1 only (once per calendar day)."""
    shift = get_shift_for_time(now)
    if shift != 1:
        return False
    minutes = now.hour * 60 + now.minute
    start = 7 * 60
    return 0 <= (minutes - start) <= 45


def is_in_shift_plan_recovery_window(shift: int, now: datetime) -> bool:
    """Shift Plan recovery: first 45 min of the shift only."""
    actual_shift = get_shift_for_time(now)
    if actual_shift != shift:
        return False
    shift_start = {1: 7 * 60, 2: 19 * 60}
    minutes = now.hour * 60 + now.minute
    if shift == 2 and now.hour < 7:
        minutes += 24 * 60
    start = shift_start[shift]
    if shift == 2 and now.hour < 7:
        start = 19 * 60
    return 0 <= (minutes - start) <= 45



async def catch_up_missed_reminders(
    app, current_shift_num: int, now: datetime, chat_id: int | None = None
):
    """
    On bot startup: send Daily Plan and Shift Plan if not posted; then any missed hourly reminders.
    Respects line state — planning reminders suppressed if line is OFF/sanitation.
    """
    if chat_id is None:
        chat_id = default_chat_id()
    rt_cu = line_runtime(chat_id)
    today_iso = now.date().isoformat()
    line_is_active = rt_cu.line_state == LINE_STATE_RUNNING
    shift_has_production = rt_cu.shift_had_production.get(
        current_shift_num, False
    ) or _shift_had_any_production(current_shift_num, today_iso, chat_id)

    logger.info(
        f"[STARTUP-CATCHUP] Shift {current_shift_num} chat={chat_id} | "
        f"line_state={rt_cu.line_state} | shift_has_production={shift_has_production}"
    )

    # CASE 1: Line OFF entire shift, no production — suppress everything
    if not line_is_active and not shift_has_production:
        logger.info(
            "[STARTUP-CATCHUP] CASE 1: no production, suppressing all catchup reminders"
        )
        return

    # 1. Daily plan — only if line active
    if line_is_active:
        await send_daily_plan_if_needed(
            app.bot, now, skip_window_check=True, chat_id=chat_id
        )
        await asyncio.sleep(1)
    else:
        logger.info("[STARTUP-CATCHUP] Daily plan skipped — line OFF (CASE 2)")

    # 2. Shift plan — only if line active
    if line_is_active:
        await send_shift_plan_if_needed(
            app.bot, current_shift_num, now, skip_window_check=True, chat_id=chat_id
        )
        await asyncio.sleep(1)
    else:
        logger.info(
            f"[STARTUP-CATCHUP] Shift {current_shift_num} plan skipped — line OFF (CASE 2)"
        )

    # 3. Hourly plan — only if line active
    if line_is_active:
        await send_current_hour_plan(
            app.bot, current_shift_num, now, chat_id=chat_id
        )
        await asyncio.sleep(1)
    else:
        logger.info(f"[STARTUP-CATCHUP] Hourly plan skipped — line OFF (CASE 2)")

    # 4. Hourly summary — always send (CASE 1 with line OFF + no production already returned)
    current_hour_num = get_current_hour_number(current_shift_num, now)
    if is_in_hourly_summary_window(now, current_shift_num, current_hour_num):
        sched_key = f"hourly_summary_scheduled_{today_iso}_{current_shift_num}_{current_hour_num}"
        catch_key = (
            f"hourly_summary_{today_iso}_{current_shift_num}_{current_hour_num}"
        )
        if not bot_state_get(sched_key, chat_id) and not bot_state_get(catch_key, chat_id):
            header = f"📅 {format_date_time_12h(now)}\n\n"
            text = (
                header
                + f"📝 *Hourly Summary Reminder – Shift {current_shift_num}, Hour {current_hour_num}*\n\n"
                + "Please provide hourly production data:\n"
                + "- Actual output for this hour\n"
                + "- Downtime events (if any)\n"
                + "- Rejects (if any)\n"
                + "- Operator notes\n\n"
                + "💡 AI will generate an hourly summary after you submit the data."
            )
            try:
                sent = await app.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="Markdown"
                )
                bot_state_set(sched_key, "1", chat_id)
                bot_state_set(catch_key, "1", chat_id)
                _record_reminder_message(
                    {
                        "kind": "hourly_summary",
                        "shift": current_shift_num,
                        "hour": current_hour_num,
                    },
                    sent.message_id,
                    chat_id=chat_id,
                )
                logger.info(
                    f"[STARTUP-CATCHUP] Hourly summary sent: "
                    f"Shift {current_shift_num} Hr {current_hour_num}"
                )
            except Exception as e:
                logger.error(f"[STARTUP-CATCHUP] Hourly summary failed: {e}")
            await asyncio.sleep(1)

    # 5. Shift summary — always send (CASE 1 with line OFF + no production already returned)
    if is_in_shift_summary_window(current_shift_num, now):
        fired_key = f"shift_report_fired_{today_iso}_{current_shift_num}"
        recovery_key = f"shift_report_recovery_{today_iso}_{current_shift_num}"
        if not bot_state_get(fired_key, chat_id) and not bot_state_get(recovery_key, chat_id):
            header = f"📅 {format_date_time_12h(now)}\n\n"
            text = (
                header
                + f"📊 *Shift {current_shift_num} Handoff Report*\n\n"
                + "- How did the shift go?\n"
                + "- Any issues or challenges for the next shift?\n"
                + "- What should the next shift be aware of?\n"
                + "- Status: All clear / Needs attention"
            )
            try:
                await app.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="Markdown"
                )
                bot_state_set(fired_key, "1", chat_id)
                bot_state_set(recovery_key, "1", chat_id)
                logger.info(
                    f"[STARTUP-CATCHUP] Shift {current_shift_num} summary sent"
                )
            except Exception as e:
                logger.error(f"[STARTUP-CATCHUP] Shift summary failed: {e}")



async def _hourly_reminder_block_timeout(bot, delay: int = 900, chat_id: int | None = None):
    """Auto-release ai_block after delay seconds if user never completes hourly input."""
    await asyncio.sleep(delay)
    rt_to = line_runtime(chat_id)
    if rt_to.ai_block:
        rt_to.ai_block = False
        await flush_pending_reminders(bot, reason="ai", chat_id=chat_id)
        logger.info("⏰ Hourly reminder block auto-released after 15min timeout")


