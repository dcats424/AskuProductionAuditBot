import logging
import re
from datetime import datetime

from config import now_ethiopia

logger = logging.getLogger(__name__)


def parse_report(text: str):
    t = text.lower()
    date_match = re.search(r"date\s*(\d{1,2}/\d{1,2}/\d{2,4})", t)
    date = (
        datetime.strptime(date_match.group(1), "%d/%m/%y").date()
        if date_match
        else now_ethiopia().date()
    )
    shift_match = re.search(r"shift\s*(?:=)?\s*(1st|2nd)", t)
    if not shift_match:
        raise ValueError("Shift not found")
    shift = {"1st": 1, "2nd": 2}[shift_match.group(1)]
    product_match = re.search(r"product type\s*(.+)", t)
    product_type = product_match.group(1).strip() if product_match else None
    plan_match = re.search(r"shift plan\s*=\s*([\d,]+)", t)
    if not plan_match:
        raise ValueError("Shift plan missing")
    plan = int(plan_match.group(1).replace(",", ""))
    actual_match = re.search(r"actual(?:\s+output)?\s*=\s*([\d,]+)", t)
    if not actual_match:
        raise ValueError("Actual output missing")
    actual = int(actual_match.group(1).replace(",", ""))

    # Parse Available Time (machine active time in minutes)
    # Look for patterns like "available time = 420" or "available = 420 min" or "avail = 420"
    available_time_match = re.search(r"available(?:\s+time)?\s*=?\s*(\d+)", t)
    available_time = None
    if available_time_match:
        available_time = int(available_time_match.group(1))
    # If not found, return None - calling functions will set their own defaults
    # Inside parse_report(), before the return statement:

    # Calculate efficiency based on available time
    efficiency = round((actual / plan) * 100, 1) if plan else 0
    vos_match = re.search(r"vos\s*=\s*([\d,]+)", t)
    vos = int(vos_match.group(1).replace(",", "")) if vos_match else None
    return {
        "date": date,
        "shift": shift,
        "product_type": product_type,
        "plan": plan,
        "actual": actual,
        "efficiency": efficiency,
        "available_time": available_time,
        "vos": vos,
    }


def parse_vos(text: str):
    """Parse vos (line off) information separately"""
    t = text.lower()
    vos_info = None

    # Look for vos line
    vos_match = re.search(r"vos\s*=\s*(.+)", t)
    if vos_match:
        vos_info = vos_match.group(1).strip()

    return vos_info


def parse_rejects(text: str):
    t = text.lower()

    def int_val(pattern):
        m = re.search(pattern, t)
        return int(m.group(1).replace(",", "")) if m else 0

    def float_val(pattern):
        m = re.search(pattern, t)
        return float(m.group(1)) if m else 0

    return {
        "preform": int_val(r"preform\s*=\s*([\d,]+)"),
        "bottle": int_val(r"bottle\s*=\s*([\d,]+)"),
        "cap": int_val(r"cap\s*=\s*([\d,]+)"),
        "label": int_val(r"(?:label|lable)\s*=\s*([\d,]+)"),
        "shrink": float_val(r"shrink\s*=\s*([\d.]+)"),
    }


def parse_operator_notes(text: str):
    t = text.lower()
    keywords = [
        "going on",
        "still",
        "replacement",
        "repair",
        "adjustment",
        "alarm",
        "closing pb",
        "orbit alarm",
        "seal",
        "clutch",
        "bearing",
        "blower",
        "mold",
        "conveyor",
    ]
    return [line.strip() for line in t.split("\n") if any(k in line for k in keywords)]


def detect_repeated_faults(downtime, operator_notes):
    combined = (
        " ".join(d["description"] for d in downtime) + " " + " ".join(operator_notes)
    )
    machines = ["blower", "mold", "conveyor", "clutch", "bearing"]
    return [
        f"{m} repeated {combined.count(m)} times"
        for m in machines
        if combined.count(m) >= 2
    ]


# ---------------- KPI CALCULATION ENGINE ----------------

DOWNTIME_CATEGORIES = {
    "MECHANICAL": ["mechanical", "machine", "technical"],
    "ELECTRICAL": ["electrical", "electric"],
    "UTILITY": ["utility", "utilities"],
}
def parse_downtime_categorized(text: str) -> dict:
    """
    Parses downtime events from input text grouped under:
    MECHANICAL, ELECTRICAL, UTILITY

    Input format expected:
        MECHANICAL
        • Some event description (245 min)
        ELECTRICAL
        • None
        UTILITY
        • Low pressure shortage problem (20 min)
    """
    result = {"MECHANICAL": [], "ELECTRICAL": [], "UTILITY": []}
    current = None  # no default — wait for a real header

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        lower = line.lower()

        if not line:
            continue

        # ── Step 1: Detect category header ────────────────────────────────
        matched_cat = None
        for cat, keywords in DOWNTIME_CATEGORIES.items():
            if any(kw in lower for kw in keywords):
                matched_cat = cat
                break

        if matched_cat:
            # Strip everything except the keyword to confirm it's a header
            cleaned = lower
            cleaned = re.sub(r"\(?\d+\s*(?:min|minutes?|\')\)?", "", cleaned)
            cleaned = re.sub(r"[\[\]()\-—•:\d]+", "", cleaned)
            for kw in DOWNTIME_CATEGORIES[matched_cat]:
                cleaned = cleaned.replace(kw, "")
            cleaned = cleaned.strip()

            if len(cleaned) <= 5:
                current = matched_cat
                continue  # it's a header, not an event

        # ── Step 2: Skip if no active category yet ────────────────────────
        if current is None:
            continue

        # ── Step 3: Skip "None" placeholder lines ─────────────────────────
        if re.match(r"^[•\-\*]?\s*none\s*$", lower):
            continue

        # ── Step 4: Parse bullet event lines ──────────────────────────────
        m = re.search(r"\(?(\d+)\s*(?:min|minutes?|\')\)?", lower)
        if m:
            duration = int(m.group(1))

            # Clean description
            desc = re.sub(r"^[•\-\*]\s*", "", line)
            desc = re.sub(r"^\d+\.\s*", "", desc)
            desc = re.sub(
                r"\s*\(?\d+\s*(?:min|minutes?|\')\)?\s*$", "", desc, flags=re.IGNORECASE
            ).strip()

            if len(desc) > 2:
                result[current].append(
                    {
                        "description": desc,
                        "duration": duration,
                        "category": current,
                    }
                )

    # ── Compute totals per category ────────────────────────────────────────
    result["_totals"] = {
        cat: sum(e["duration"] for e in result[cat])
        for cat in ("MECHANICAL", "ELECTRICAL", "UTILITY")
    }
    return result


def format_downtime_category_block(categorized: dict) -> str:
    icons = {
        "MECHANICAL": "⚙️",
        "ELECTRICAL": "🔌",
        "UTILITY": "🏭",
    }
    lines = []
    for cat in ("MECHANICAL", "ELECTRICAL", "UTILITY"):
        items = categorized.get(cat, [])
        totals = categorized["_totals"]
        icon = icons[cat]
        total = totals.get(cat, 0)

        lines.append(f"\n\n  {icon} {cat} — {total} min")
        if items:
            for item in items:
                lines.append(f"    • {item['description']} ({item['duration']} min)")
        else:
            lines.append(f"    • None")

    return "\n".join(lines)


def build_downtime_analysis_block(categorized: dict, available_time: int) -> str:
    """
    Builds the full DOWNTIME ANALYSIS summary block.
    """
    totals = categorized.get("_totals", {})
    mech_total = totals.get("MECHANICAL", 0)
    elec_total = totals.get("ELECTRICAL", 0)
    util_total = totals.get("UTILITY", 0)
    total_dt = mech_total + elec_total + util_total

    # Downtime ratio
    ratio = (total_dt / available_time * 100) if available_time > 0 else 0.0

    # Dominant category
    cat_map = {
        "MECHANICAL": mech_total,
        "ELECTRICAL": elec_total,
        "UTILITY": util_total,
    }
    dominant_cat = max(cat_map, key=cat_map.get)
    dominant_total = cat_map[dominant_cat]

    block = (
        f"\n⏱️ DOWNTIME ANALYSIS\n\n"
        f"  • Total Downtime:     {total_dt} minutes\n"
        f"  • Downtime Ratio:     {ratio:.2f}% of available time\n"
        f"  • Dominant Category:  {dominant_cat} ({dominant_total} min)\n"
        f"  • Mechanical:         {mech_total} min\n"
        f"  • Electrical:         {elec_total} min\n"
        f"  • Utility:            {util_total} min\n"
        f"{format_downtime_category_block(categorized)}"
    )
    return block


def flatten_categorized_downtime(categorized: dict) -> list:
    """
    Flattens the categorized downtime dict into a simple list of event dicts.
    Each item: {"description": str, "duration": int, "category": str}
    """
    flat = []
    for cat in ("MECHANICAL", "ELECTRICAL", "UTILITY"):
        flat.extend(categorized.get(cat, []))
    return flat
