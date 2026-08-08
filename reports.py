import asyncio
import logging
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes

from ai import (
    WEEKLY_REPORT_SYSTEM_PROMPT,
    ai_client,
    ai_generate_multi_shift_summary,
    ai_generate_summary,
)
from config import AI_MODEL, is_allowed_chat
from db import (
    _ensure_hourly_production_table,
    get_db_connection,
    parse_vos_minutes,
)
from kpis import (
    compute_risk_assessment,
    get_latest_hourly_date_for_all_shifts,
    get_latest_hourly_date_for_shift,
    get_pcs_per_pack,
    load_all_shifts_from_hourly_db,
    load_shift_evidence_from_hourly_db,
)
from messaging import (
    _cleanup_command_after_success,
    _queue_failed_messages,
    split_and_send_long_message,
)
from state import line_runtime

logger = logging.getLogger(__name__)


async def all_shift_summary_from_hourly_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """
    /all_shift_summary_hourly [date]
    Generate a multi-shift summary by aggregating hourly data for ALL shifts from the DB.
    Date is optional (DD/MM/YY), defaults to most recent date in database.
    """
    if not is_allowed_chat(update.effective_chat.id if update.effective_chat else None):
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    target_date = None
    if context.args:
        raw = context.args[0].strip()
        for fmt in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                target_date = datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                continue
        if target_date is None:
            sent = await update.message.reply_text("Invalid date. Use DD/MM/YY, e.g. 09/03/26")
            _queue_failed_messages(
                chat_id,
                getattr(update.message, "message_id", None),
                getattr(sent, "message_id", None),
            )
            return

    # If no date specified, get the most recent date from database
    if target_date is None:
        target_date = get_latest_hourly_date_for_all_shifts(chat_id=chat_id)
        if target_date is None:
            sent = await update.message.reply_text(
                "⚠️ No hourly data found in database. Submit some reports first."
            )
            _queue_failed_messages(
                chat_id,
                getattr(update.message, "message_id", None),
                getattr(sent, "message_id", None),
            )
            return

    date_label = target_date.strftime("%d/%m/%Y")

    # Load ALL shifts from the line's OWN hourly DB for this date.
    hourly_evidence = load_all_shifts_from_hourly_db(target_date, chat_id)
    shifts_found = [s for s in (1, 2) if hourly_evidence.get(s)]

    if not shifts_found:
        sent = await update.message.reply_text(
            f"⚠️ No data found for {date_label}.\n"
            "Submit hourly reports for that date first."
        )
        _queue_failed_messages(
            chat_id,
            getattr(update.message, "message_id", None),
            getattr(sent, "message_id", None),
        )
        return

    await update.message.reply_text(
        f"⏳ Generating multi-shift summary from hourly data — "
        f"{date_label} ({len(shifts_found)} shift(s): {shifts_found})..."
    )

    # Swap into rt.evidence temporarily
    rt_swap = line_runtime(chat_id)
    original_evidence = {k: list(v) for k, v in rt_swap.evidence.items()}
    for s in (1, 2):
        rt_swap.evidence[s] = hourly_evidence.get(s, [])
    try:
        hourly_result = await generate_multi_shift_summary_and_post(context, shifts_found, chat_id)
    finally:
        for s in (1, 2):
            rt_swap.evidence[s] = original_evidence[s]

    if hourly_result is True:
        await _cleanup_command_after_success(
            context.bot, chat_id,
            getattr(update.message, "message_id", None),
        )
    else:
        extra = [hourly_result] if isinstance(hourly_result, int) else []
        _queue_failed_messages(
            chat_id, getattr(update.message, "message_id", None), *extra
        )


async def shift_summary_hourly_1_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Generate Shift 1 summary from hourly data"""
    if not is_allowed_chat(update.effective_chat.id if update.effective_chat else None):
        return
    await generate_shift_summary_from_hourly(update, context, 1)


async def shift_summary_hourly_2_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Generate Shift 2 summary from hourly data"""
    if not is_allowed_chat(update.effective_chat.id if update.effective_chat else None):
        return
    await generate_shift_summary_from_hourly(update, context, 2)


async def generate_shift_summary_from_hourly(
    update: Update, context: ContextTypes.DEFAULT_TYPE, shift: int
):
    """Helper function to generate shift summary from hourly data"""
    chat_id = update.effective_chat.id if update.effective_chat else None
    target_date = None
    if context.args:
        raw = context.args[0].strip()
        for fmt in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                target_date = datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                continue
        if target_date is None:
            sent = await update.message.reply_text(
                f"❌ Invalid date format for Shift {shift}.\n"
                "Use DD/MM/YY — e.g. /shift_summary_hourly_{shift} 24/02/26"
            )
            _queue_failed_messages(
                chat_id,
                getattr(update.message, "message_id", None),
                getattr(sent, "message_id", None),
            )
            return
    else:
        # If no date specified, find the most recent date with hourly data for this shift
        target_date = get_latest_hourly_date_for_shift(shift, chat_id)
        if not target_date:
            sent = await update.message.reply_text(
                f"⚠️ No hourly data found for Shift {shift} in the database.\n"
                "Make sure hourly reports were submitted for that shift."
            )
            _queue_failed_messages(
                chat_id,
                getattr(update.message, "message_id", None),
                getattr(sent, "message_id", None),
            )
            return

    # Load shift data from hourly database
    shift_text = load_shift_evidence_from_hourly_db(shift, target_date, chat_id)
    if not shift_text:
        date_str = target_date.strftime("%d/%m/%Y") if target_date else "unknown"
        sent = await update.message.reply_text(
            f"⚠️ No hourly data found for Shift {shift} on {date_str}.\n"
            "Make sure hourly reports were submitted for that shift."
        )
        _queue_failed_messages(
            chat_id,
            getattr(update.message, "message_id", None),
            getattr(sent, "message_id", None),
        )
        return

    date_str = target_date.strftime("%d/%m/%Y") if target_date else "unknown"
    await update.message.reply_text(
        f"⏳ Generating Shift {shift} summary from hourly data for {date_str}..."
    )

    try:
        # Temporarily use the hourly data for AI generation
        rt_gen = line_runtime(chat_id)
        original_evidence = rt_gen.evidence[shift].copy()
        rt_gen.evidence[shift] = [shift_text]

        # Generate AI summary
        ai_text = await ai_generate_summary(shift, chat_id)
        rt_gen.daily_summaries[shift] = ai_text

        # Post to group
        await split_and_send_long_message(
            context.bot, chat_id,
            f"📊 SHIFT {shift} SUMMARY - Date {date_str}\n\n{ai_text}",
        )
        await _cleanup_command_after_success(
            context.bot, chat_id,
            getattr(update.message, "message_id", None),
        )

        # Restore original evidence
        rt_gen.evidence[shift] = original_evidence

    except Exception as e:
        logger.error(f"Error generating shift summary from hourly data: {e}")
        sent = await update.message.reply_text(
            f"❌ Error generating Shift {shift} summary: {e}"
        )
        _queue_failed_messages(
            chat_id,
            getattr(update.message, "message_id", None),
            getattr(sent, "message_id", None),
        )


async def generate_multi_shift_summary_and_post(
    context: ContextTypes.DEFAULT_TYPE,
    included_shifts: list[int],
    chat_id: int | None = None,
) -> int | bool:
    """
    Helper to call multi-shift AI and post into group.
    Returns True when the full summary was posted, the warning message id when
    no summary could be built, or False on failure.
    """
    if chat_id is None:
        raise ValueError("chat_id is required to post a multi-shift summary")
    # Build label directly — never re-scan rt.evidence for this
    if len(included_shifts) == 1:
        label = f"Shift {included_shifts[0]}"
    elif len(included_shifts) == 2:
        label = f"Shifts {included_shifts[0]} and {included_shifts[1]}"
    else:
        label = f"Shifts {', '.join(str(s) for s in included_shifts[:-1])} and {included_shifts[-1]}"

    daily_text = await ai_generate_multi_shift_summary(included_shifts, chat_id)
    if not daily_text:
        try:
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ No complete multi-shift summary available. Please ensure all shifts have data for the same date.",
                parse_mode=None,
            )
            return getattr(sent, "message_id", None) or False
        except Exception as e:
            logger.warning(f"Could not send multi-shift warning: {e}")
            return False

    await split_and_send_long_message(
        context.bot, chat_id,
        f"📘 MULTI-SHIFT PRODUCTION SUMMARY – {label}\n\n{daily_text}",
        parse_mode=None,
    )
    return True


# ---------------- WEEKLY REPORT ----------------
def aggregate_period_from_db(start_date, end_date, chat_id: int | None = None) -> dict | None:
    """
    Aggregate production, downtime, rejects, and VOS over a date range (inclusive)
    from the hourly_production tables (hourly rows summed over the period).
    Returns a dict of period totals (per-hour pcs conversion applied),
    or None if no data exists in the range.
    """
    _ensure_hourly_production_table(chat_id)
    conn = get_db_connection(chat_id)
    cur = conn.cursor()
    try:
        # Single JOIN query — hourly rows left-joined with their downtime events
        # and rejects (avoids N+1: one query, not 1 + 2×rows).
        cur.execute(
            """
            SELECT h.id, h.shift, h.hour, h.product_type, h.plan_pack,
                   h.actual_output_pack, h.available_time, h.vos_info,
                   COALESCE(d.duration_min, 0), COALESCE(d.category, 'MECHANICAL'),
                   COALESCE(r.preform, 0), COALESCE(r.bottle, 0),
                   COALESCE(r.cap, 0), COALESCE(r.label, 0), COALESCE(r.shrink, 0)
            FROM hourly_production h
            LEFT JOIN hourly_downtime_events d ON d.hourly_production_id = h.id
            LEFT JOIN hourly_rejects r ON r.hourly_production_id = h.id
            WHERE h.date BETWEEN %s AND %s
            ORDER BY h.date, h.shift, h.hour
            """,
            (start_date, end_date),
        )
        rows = cur.fetchall()

        if not rows:
            return None

        totals = {
            "plan": 0,
            "actual": 0,
            "available_minutes": 0,
            "output_pcs": 0,
            "vos_minutes": 0,
            "downtime": 0,
            "cat_totals": {"MECHANICAL": 0, "ELECTRICAL": 0, "UTILITY": 0},
            "rejects": {"preform": 0, "bottle": 0, "cap": 0, "label": 0, "shrink": 0.0},
            "products": set(),
            "shift_count": 0,
        }

        # One row per (hour, downtime event, rejects-row); track seen hours so
        # hour-level fields (plan/actual/products) are counted exactly once.
        seen_hours = set()
        for row in rows:
            (
                h_id, shift, hour_num, product_type, plan, actual,
                available_time, vos_info,
                dur, cat, r_preform, r_bottle, r_cap, r_label, r_shrink,
            ) = row

            hour_key = (h_id, shift, hour_num)
            if hour_key not in seen_hours:
                seen_hours.add(hour_key)
                available = available_time if available_time is not None else 60
                totals["plan"] += plan or 0
                totals["actual"] += actual or 0
                totals["available_minutes"] += available
                totals["output_pcs"] += (actual or 0) * get_pcs_per_pack(product_type)
                totals["vos_minutes"] += parse_vos_minutes(vos_info)
                totals["shift_count"] += 1
                if product_type:
                    totals["products"].add(str(product_type).strip())

            dur = dur or 0
            if dur:
                totals["downtime"] += dur
                cat_upper = (cat or "MECHANICAL").upper().strip()
                if cat_upper not in totals["cat_totals"]:
                    cat_upper = "MECHANICAL"
                totals["cat_totals"][cat_upper] += dur

            rej_row = (r_preform, r_bottle, r_cap, r_label, r_shrink)
            if any(rej_row):
                for cat_name, val in zip(
                    ("preform", "bottle", "cap", "label", "shrink"), rej_row
                ):
                    totals["rejects"][cat_name] = round(
                        totals["rejects"][cat_name] + (val or 0), 2
                    )

        return totals
    except Exception as e:
        logger.error(f"aggregate_period_from_db failed: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def format_vos_duration(total_minutes: int) -> str:
    """Format VOS minutes as hours/minutes, e.g. 195 -> '3 hr 15 min'."""
    if total_minutes < 60:
        return f"{total_minutes} min"
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if minutes:
        return f"{hours} hr {minutes} min"
    return f"{hours} hr"


async def weekly_report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /weekly_report [DD/MM/YY]
    Generate a weekly production summary for the calendar week (Mon-Sun) of the
    given date. Defaults to the week of the latest data in the database.
    """
    if not is_allowed_chat(update.effective_chat.id if update.effective_chat else None):
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    target_date = None
    if context.args:
        raw = context.args[0].strip()
        for fmt in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                target_date = datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                continue
        if target_date is None:
            sent = await update.message.reply_text("Invalid date. Use DD/MM/YY, e.g. 15/07/26")
            _queue_failed_messages(
                chat_id,
                getattr(update.message, "message_id", None),
                getattr(sent, "message_id", None),
            )
            return

    if target_date is None:
        target_date = get_latest_hourly_date_for_all_shifts(chat_id=chat_id)
        if target_date is None:
            sent = await update.message.reply_text(
                "⚠️ No hourly data found in database. Submit some reports first."
            )
            _queue_failed_messages(
                chat_id,
                getattr(update.message, "message_id", None),
                getattr(sent, "message_id", None),
            )
            return

    week_start = target_date - timedelta(days=target_date.weekday())  # Monday
    week_end = week_start + timedelta(days=6)  # Sunday

    await _generate_period_report(update, context, chat_id, week_start, week_end, "weekly")


async def _generate_period_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | None,
    start_date,
    end_date,
    period_name: str,
) -> None:
    """
    Generate and post a period (weekly/monthly) production report: AI executive
    narrative, KPIs, downtime analysis, reject analysis, OEE, risk and audit status.
    """
    period_upper = period_name.upper()
    start_label = start_date.strftime("%d/%m/%Y")
    end_label = end_date.strftime("%d/%m/%Y")

    totals = aggregate_period_from_db(start_date, end_date, chat_id=chat_id)
    if not totals:
        sent = await update.message.reply_text(
            f"⚠️ No production data found for {period_name} {start_label} – {end_label}.\n"
            "Submit shift reports first."
        )
        _queue_failed_messages(
            chat_id,
            getattr(update.message, "message_id", None),
            getattr(sent, "message_id", None),
        )
        return

    # ── KPI calculations (same formulas as compute_kpis, period scale) ──────
    total_plan = totals["plan"]
    total_actual = totals["actual"]
    output_pcs = totals["output_pcs"]
    available_minutes = totals["available_minutes"]
    total_downtime = totals["downtime"]
    rejects = totals["rejects"]

    performance = round((total_actual / total_plan) * 100, 1) if total_plan else 0.0
    production_hours = available_minutes / 60
    downtime_hours = total_downtime / 60
    availability = (
        round(((production_hours - downtime_hours) / production_hours) * 100, 1)
        if production_hours > 0
        else 0.0
    )
    defective_qty = rejects.get("preform", 0) + rejects.get("bottle", 0)
    quality = (
        round((output_pcs / (output_pcs + defective_qty)) * 100, 1)
        if (output_pcs + defective_qty) > 0
        else 0.0
    )
    oee = round((performance * availability * quality) / 10000, 2)
    downtime_ratio = (
        round((total_downtime / available_minutes) * 100, 2)
        if available_minutes
        else 0
    )

    reject_percentages = {}
    for category in ["preform", "bottle", "cap", "label", "shrink"]:
        reject_qty = rejects.get(category, 0)
        reject_percentages[category] = (
            round((reject_qty / (reject_qty + output_pcs)) * 100, 1)
            if (reject_qty + output_pcs) > 0
            else 0.0
        )

    dt_totals = totals["cat_totals"]
    dominant_cat = max(dt_totals, key=dt_totals.get) if any(dt_totals.values()) else "N/A"

    # ── Risk score (shared with all report builders) ─────────────────────────
    _, risk_level = compute_risk_assessment(
        performance, downtime_ratio, rejects, output_pcs
    )
    audit_status = "CLOSED" if risk_level in ("LOW", "MODERATE") else "FOLLOW-UP REQUIRED"

    # ── AI executive narrative ──────────────────────────────────────────────
    product_str = ", ".join(sorted(totals["products"])) if totals["products"] else "N/A"
    structured_data = f"""
{period_upper}: {start_label} to {end_label}
SHIFT_COUNT: {totals['shift_count']}
PRODUCT(S): {product_str}
TOTAL_PLAN: {total_plan}
TOTAL_ACTUAL: {total_actual}
TOTAL_AVAILABLE_TIME: {available_minutes} minutes
TOTAL_VOS: {totals['vos_minutes']} minutes
PERFORMANCE: {performance}%
AVAILABILITY: {availability}%
QUALITY: {quality}%
OEE: {oee}%
TOTAL_DOWNTIME: {total_downtime} minutes
DOWNTIME_RATIO: {downtime_ratio}%
DOWNTIME BY CATEGORY:
  MECHANICAL: {dt_totals.get('MECHANICAL', 0)} min
  ELECTRICAL: {dt_totals.get('ELECTRICAL', 0)} min
  UTILITY:    {dt_totals.get('UTILITY', 0)} min
  DOMINANT:   {dominant_cat}
REJECTS_BREAKDOWN:
- Preform: {rejects.get('preform', 0)}
- Bottle:  {rejects.get('bottle', 0)}
- Cap:     {rejects.get('cap', 0)}
- Label:   {rejects.get('label', 0)}
- Shrink:  {rejects.get('shrink', 0)} kg
DEFECTIVE_QUANTITY: {defective_qty}
RISK_LEVEL: {risk_level}
AUDIT_STATUS: {audit_status}
"""

    loop = asyncio.get_running_loop()

    def call_ai():
        return ai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": WEEKLY_REPORT_SYSTEM_PROMPT.replace("weekly", period_name),
                },
                {"role": "user", "content": structured_data},
            ],
            temperature=0.2,
        )

    executive_paragraph = f"{period_name.title()} performance summary unavailable."
    try:
        response = await loop.run_in_executor(None, call_ai)
        executive_paragraph = response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"{period_name.title()} AI narrative failed: {e}")

    # ── Report sections (same format as shift report, period totals) ────────
    vos_display = format_vos_duration(totals["vos_minutes"])
    production_performance = (
        f"📊 PRODUCTION PERFORMANCE\n\n"
        f"  • Product: {product_str}\n"
        f"  • Plan: {total_plan:,} packs\n"
        f"  • Actual: {total_actual:,} packs\n"
        f"  • Available Time: {available_minutes:,} minutes\n"
        f"  • Efficiency: {performance}%\n"
        f"  • VOS: {vos_display} ({period_name} total)"
    )

    downtime_hours_display = round(total_downtime / 60, 1)
    available_hours_display = round(available_minutes / 60, 1)
    downtime_analysis = (
        f"⏱️ DOWNTIME ANALYSIS\n\n"
        f"  • Total Downtime:     {total_downtime} minutes\n"
        f"  • Downtime Ratio:     {downtime_ratio}%({downtime_hours_display}hr) of {available_hours_display}hr(available time)\n"
        f"  • Dominant Category:  {dominant_cat} ({dt_totals.get(dominant_cat, 0)} min)\n"
        f"  • Mechanical:         {dt_totals.get('MECHANICAL', 0)} min\n"
        f"  • Electrical:         {dt_totals.get('ELECTRICAL', 0)} min\n"
        f"  • Utility:            {dt_totals.get('UTILITY', 0)} min"
    )

    reject_table = f"""
🗑 QUALITY – REJECT ANALYSIS
-------------------------

{"Item":<20} {"Reject %":<15} {"Reject Qty":<12}
{"-" * 47}
Preform              {reject_percentages["preform"]:.1f} %           {rejects.get("preform", 0)} pcs
Bottle               {reject_percentages["bottle"]:.1f} %          {rejects.get("bottle", 0)} pcs
Cap                  {reject_percentages["cap"]:.1f} %          {rejects.get("cap", 0)} pcs
Label                {reject_percentages["label"]:.1f} %          {rejects.get("label", 0)} pcs
Shrink               {reject_percentages["shrink"]:.1f} %           {rejects.get("shrink", 0)} kg
"""

    oee_performance = (
        f"📈 OVERALL EQUIPMENT EFFECTIVENESS\n\n"
        f"  • Plan: {total_plan:,} packs\n"
        f"  • Actual: {total_actual:,} packs\n"
        f"  • Defective Quantity: {defective_qty:,} pcs\n"
        f"  • Production Time: {production_hours:.2f} hr ({available_minutes} min)\n"
        f"  • Downtime: {downtime_hours:.2f} hr ({total_downtime} min)\n"
        f"  • Availability: {availability:.1f}%\n"
        f"  • Performance: {performance:.1f}%\n"
        f"  • Quality: {quality:.1f}%\n"
        f"  • OEE: {oee:.2f}%"
    )

    final_report = (
        f"📊 {period_upper} PRODUCTION SUMMARY ({start_label} – {end_label})\n\n"
        f"✅ STATUS: COMPLETE\n\n"
        f"⚠️ RISK LEVEL: {risk_level}\n\n"
        f"📋 EXECUTIVE SUMMARY\n\n"
        f"{executive_paragraph}\n\n"
        f"────────────────────────────\n\n"
        f"{production_performance}"
        f"\n\n────────────────────────────\n\n"
        f"{downtime_analysis}"
        f"\n\n────────────────────────────\n\n"
        f"{reject_table}"
        f"\n\n────────────────────────────\n\n"
        f"{oee_performance}"
        f"\n\n────────────────────────────\n\n"
        f"📌 AUDIT STATUS: {audit_status}"
    )

    await split_and_send_long_message(
        context.bot, chat_id, final_report.strip(), parse_mode=None
    )
    await _cleanup_command_after_success(
        context.bot, chat_id,
        getattr(update.message, "message_id", None),
    )


async def monthly_report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /monthly_report [MM/YY]
    Generate a monthly production summary for the calendar month of the given
    month/year. Defaults to the month of the latest data in the database.
    """
    if not is_allowed_chat(update.effective_chat.id if update.effective_chat else None):
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    target_date = None
    if context.args:
        raw = context.args[0].strip()
        for fmt in ("%m/%y", "%m/%Y"):
            try:
                target_date = datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                continue
        if target_date is None:
            sent = await update.message.reply_text("Invalid month. Use MM/YY, e.g. 07/26")
            _queue_failed_messages(
                chat_id,
                getattr(update.message, "message_id", None),
                getattr(sent, "message_id", None),
            )
            return
    else:
        target_date = get_latest_hourly_date_for_all_shifts(chat_id=chat_id)
        if target_date is None:
            sent = await update.message.reply_text(
                "⚠️ No hourly data found in database. Submit some reports first."
            )
            _queue_failed_messages(
                chat_id,
                getattr(update.message, "message_id", None),
                getattr(sent, "message_id", None),
            )
            return

    month_start = target_date.replace(day=1)
    month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    await _generate_period_report(update, context, chat_id, month_start, month_end, "monthly")


