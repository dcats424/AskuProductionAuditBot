import logging
import re
from datetime import datetime

from config import now_ethiopia
from db import _ensure_hourly_production_table, get_db_connection

logger = logging.getLogger(__name__)


def get_latest_hourly_date_for_shift(shift: int, chat_id: int | None = None):
    """
    Find the most recent date that has hourly data for the specified shift.
    Returns date object or None if no data found.
    """
    _ensure_hourly_production_table(chat_id)
    conn = None
    try:
        conn = get_db_connection(chat_id)
        cur = conn.cursor()

        # Find the most recent date with hourly data for this shift
        cur.execute(
            """
            SELECT date FROM hourly_production 
            WHERE shift = %s AND actual_output_pack > 0
            ORDER BY date DESC, hour DESC 
            LIMIT 1
        """,
            (shift,),
        )

        result = cur.fetchone()
        cur.close()
        conn.close()

        if result:
            return result[0]
        return None

    except Exception as e:
        logger.error(f"Error finding latest hourly date for shift {shift}: {e}")
        return None
    finally:
        if conn:
            conn.close()


def get_latest_hourly_date_for_all_shifts(chat_id: int | None = None):
    """
    Find the most recent date that has hourly data for ANY shift.
    Returns date object or None if no data found.
    """
    _ensure_hourly_production_table(chat_id)
    conn = None
    try:
        conn = get_db_connection(chat_id)
        cur = conn.cursor()

        # Find the most recent date with hourly data for any shift
        cur.execute("""
            SELECT date FROM hourly_production 
            WHERE actual_output_pack > 0
            ORDER BY date DESC, hour DESC 
            LIMIT 1
        """)

        result = cur.fetchone()
        cur.close()
        conn.close()

        if result:
            return result[0]
        return None

    except Exception as e:
        logger.error(f"Error finding latest hourly date for all shifts: {e}")
        return None
    finally:
        if conn:
            conn.close()


def get_pcs_per_pack(product_type: str | None) -> int:
    """
    Convert pack size to pcs per pack based on product type.
    - 1Ltr / 2Ltr     -> 6 pcs per pack
    - 0.6Ltr / 0.3Ltr -> 12 pcs per pack
    - Unknown product -> default 6 pcs per pack
    """
    if not product_type:
        return 6
    match = re.search(r"(\d+(?:\.\d+)?)", str(product_type).lower())
    if not match:
        return 6
    volume = float(match.group(1))
    if volume in (0.6, 0.3):
        return 12
    if volume in (1, 2):
        return 6
    return 6


def compute_kpis(
    plan: int,
    actual: int,
    downtime_minutes: int,
    production_hours: float,
    rejects: dict,
    output_pcs: int | None = None,
) -> dict:
    """
    Deterministic KPI calculation engine.
    Returns standardized KPI metrics for all report types.
    output_pcs: actual output converted from packs to pcs (pack size × pcs per pack).
    Only used for reject% and quality% where rejects are in pcs.
    """
    if output_pcs is None:
        output_pcs = actual

    # 1️⃣ Performance % - (Actual Production / Planned Production) * 100
    performance = round((actual / plan) * 100, 1) if plan > 0 else 0.0

    # 2️⃣ Machine Availability % - ((Planned Production hrs - Machine Downtime) * 100) / Production hrs
    downtime_hours = downtime_minutes / 60
    availability = (
        round(((production_hours - downtime_hours) / production_hours) * 100, 1)
        if production_hours > 0
        else 0.0
    )

    # 3️⃣ Quality % - output (pcs) / (reject qty (pcs) + output (pcs)) * 100
    defective_qty = rejects.get("preform", 0) + rejects.get("bottle", 0)
    quality = (
        round((output_pcs / (output_pcs + defective_qty)) * 100, 1)
        if (output_pcs + defective_qty) > 0
        else 0.0
    )

    # 4️⃣ OEE
    oee = round((performance * availability * quality) / 10000, 2)

    # 5️⃣ Reject Percentages - reject qty (pcs) / (reject qty (pcs) + output (pcs)) * 100
    reject_percentages = {}
    for category in ["preform", "bottle", "cap", "label", "shrink"]:
        reject_qty = rejects.get(category, 0)
        reject_pct = (
            round((reject_qty / (reject_qty + output_pcs)) * 100, 1)
            if (reject_qty + output_pcs) > 0
            else 0.0
        )
        reject_percentages[category] = reject_pct

    return {
        "performance": performance,
        "availability": availability,
        "quality": quality,
        "oee": oee,
        "defective_qty": defective_qty,
        "downtime_hours": downtime_hours,
        "reject_percentages": reject_percentages,
    }


# ── Risk assessment thresholds (shared by all report builders) ──────────────
RISK_PERF_LOW = 60.0
RISK_PERF_MID = 75.0
RISK_DT_HIGH = 40.0
RISK_DT_MID = 25.0
RISK_REJ_HIGH = 5.0
RISK_REJ_MID = 2.0
RISK_SCORE_CRITICAL = 7
RISK_SCORE_HIGH = 5
RISK_SCORE_MODERATE = 3


def compute_risk_assessment(
    performance: float,
    downtime_ratio: float,
    rejects: dict,
    output_pcs: int,
    downtime_text: str = "",
) -> tuple[int, str]:
    """
    Deterministic risk score + level from KPIs, shared by every report builder.
    downtime_text: lower-cased downtime descriptions; empty string skips the
    keyword-based checks (preserves the original per-builder behavior).
    """
    risk_score = 0
    if performance < RISK_PERF_LOW:
        risk_score += 3
    elif performance < RISK_PERF_MID:
        risk_score += 2
    if downtime_ratio > RISK_DT_HIGH:
        risk_score += 3
    elif downtime_ratio > RISK_DT_MID:
        risk_score += 2
    total_rejects_count = (
        rejects.get("bottle", 0) + rejects.get("cap", 0) + rejects.get("label", 0)
    )
    if output_pcs > 0:
        reject_ratio = (total_rejects_count / output_pcs) * 100
        if reject_ratio > RISK_REJ_HIGH:
            risk_score += 2
        elif reject_ratio > RISK_REJ_MID:
            risk_score += 1
    if downtime_text:
        if any(w in downtime_text for w in ("misalignment", "wear")):
            risk_score += 1
        if any(w in downtime_text for w in ("short circuit", "breaker")):
            risk_score += 1
        if any(w in downtime_text for w in ("glue", "adhesive")):
            risk_score += 1
    risk_level = (
        "CRITICAL"
        if risk_score >= RISK_SCORE_CRITICAL
        else "HIGH"
        if risk_score >= RISK_SCORE_HIGH
        else "MODERATE"
        if risk_score >= RISK_SCORE_MODERATE
        else "LOW"
    )
    return risk_score, risk_level


# ---------------- COMMANDS ----------------





def load_shift_evidence_from_hourly_db(
    shift: int, target_date=None, chat_id: int | None = None
) -> str | None:
    """
    Load ALL hourly records for a given shift+date from hourly_production,
    aggregate them into a single shift-level text blob that parse_report(),
    parse_downtime_categorized(), parse_rejects() can all understand.

    Returns a reconstructed report string or None if no hourly data found.
    """
    _ensure_hourly_production_table(chat_id)
    conn = None
    try:
        conn = get_db_connection(chat_id)
        cur = conn.cursor()

        if target_date is None:
            # Find the most recent date with hourly data for this shift
            target_date = get_latest_hourly_date_for_shift(shift, chat_id)
            if not target_date:
                cur.close()
                conn.close()
                return None

        from datetime import date as date_type

        if isinstance(target_date, str):
            for fmt in ("%Y-%m-%d", "%d/%m/%y", "%d/%m/%Y"):
                try:
                    target_date = datetime.strptime(target_date, fmt).date()
                    break
                except ValueError:
                    continue
        if not isinstance(target_date, date_type):
            cur.close()
            conn.close()
            return None

        # Fetch all hourly rows for this shift+date
        cur.execute(
            """
            SELECT id, hour, product_type, plan_pack, actual_output_pack,
                   available_time, vos_info
            FROM hourly_production
            WHERE date = %s AND shift = %s
            ORDER BY hour
        """,
            (target_date, shift),
        )
        hourly_rows = cur.fetchall()

        if not hourly_rows:
            cur.close()
            conn.close()
            return None

        # [Rest of the function remains the same...]
        # Aggregate
        total_plan = 0
        total_actual = 0
        total_available = 0
        product_types = set()
        all_vos = []
        # Downtime by category
        agg_downtime = {"MECHANICAL": [], "ELECTRICAL": [], "UTILITY": []}
        # Rejects
        total_rejects = {"preform": 0, "bottle": 0, "cap": 0, "label": 0, "shrink": 0.0}
        hours_included = []

        for row in hourly_rows:
            h_id, hour_num, prod_type, h_plan, h_actual, h_avail, h_vos = row
            total_plan += h_plan or 0
            total_actual += h_actual or 0
            total_available += h_avail or 60
            if prod_type:
                product_types.add(prod_type.strip())
            if h_vos:
                all_vos.append(f"Hr{hour_num}: {h_vos}")
            hours_included.append(hour_num)

            # Fetch downtime for this hour
            cur.execute(
                """
                SELECT description, duration_min, category
                FROM hourly_downtime_events
                WHERE hourly_production_id = %s
            """,
                (h_id,),
            )
            for desc, dur, cat in cur.fetchall():
                cat_upper = (cat or "MECHANICAL").upper().strip()
                if cat_upper not in agg_downtime:
                    cat_upper = "MECHANICAL"
                agg_downtime[cat_upper].append((desc, dur))

            # Fetch rejects for this hour
            cur.execute(
                """
                SELECT preform, bottle, cap, label, shrink
                FROM hourly_rejects
                WHERE hourly_production_id = %s
            """,
                (h_id,),
            )
            rej = cur.fetchone()
            if rej:
                total_rejects["preform"] += rej[0] or 0
                total_rejects["bottle"] += rej[1] or 0
                total_rejects["cap"] += rej[2] or 0
                total_rejects["label"] += rej[3] or 0
                total_rejects["shrink"] += rej[4] or 0.0

        cur.close()

        # Build text blob
        date_str = target_date.strftime("%d/%m/%y")
        shift_label = {1: "1st", 2: "2nd"}.get(shift, "1st")
        product_str = ", ".join(product_types) if product_types else "N/A"

        lines = [
            f"Date {date_str}",
            f"Shift {shift_label}",
            f"Product type {product_str}",
            f"Shift plan = {total_plan}",
            f"Actual output = {total_actual}",
            f"Available time = {total_available}",
        ]

        if all_vos:
            lines.append(f"VOS = {'; '.join(all_vos)}")

        # Downtime with category headers
        for cat in ("MECHANICAL", "ELECTRICAL", "UTILITY"):
            lines.append(cat)
            events = agg_downtime[cat]
            if events:
                for desc, dur in events:
                    lines.append(f"• {desc} {dur} min")
            else:
                lines.append("• None")

        # Rejects
        lines.append(f"Preform = {total_rejects['preform']}")
        lines.append(f"Bottle = {total_rejects['bottle']}")
        lines.append(f"Cap = {total_rejects['cap']}")
        lines.append(f"Label = {total_rejects['label']}")
        lines.append(f"Shrink = {round(total_rejects['shrink'], 2)}")

        text_blob = "\n".join(lines)
        logger.info(
            f"load_shift_evidence_from_hourly_db: shift={shift} date={target_date} "
            f"hours={hours_included} plan={total_plan} actual={total_actual}"
        )
        return text_blob

    except Exception as e:
        logger.error(f"load_shift_evidence_from_hourly_db failed: {e}", exc_info=True)
        return None
    finally:
        if conn:
            conn.close()


def load_all_shifts_from_hourly_db(target_date=None, chat_id: int | None = None) -> dict:
    """
    Load hourly data for ALL shifts on a date, aggregate each shift,
    return {1: [text], 2: [text], "_resolved_date": date}
    compatible with ai_shift_evidence format.
    """
    _ensure_hourly_production_table(chat_id)

    if target_date is None:
        target_date = now_ethiopia().date()

    from datetime import date as date_type

    if isinstance(target_date, str):
        for fmt in ("%Y-%m-%d", "%d/%m/%y", "%d/%m/%Y"):
            try:
                target_date = datetime.strptime(target_date, fmt).date()
                break
            except ValueError:
                continue
    if not isinstance(target_date, date_type):
        return {}

    result = {}
    for shift in (1, 2):
        text = load_shift_evidence_from_hourly_db(shift, target_date, chat_id)
        if text:
            result[shift] = [text]

    if result:
        result["_resolved_date"] = target_date

    return result
