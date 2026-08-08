import logging
import re

import psycopg2

from config import BASE_DB_CONFIG, db_name_for_line, line_key_for_chat

logger = logging.getLogger(__name__)


# ---------------- DATABASE ----------------
def db_config_for_chat(chat_id: int | None = None) -> dict:
    """Return DB config targeting the database for the given chat's production line.
    Raises ValueError when no line database can be resolved (fail-fast)."""
    if chat_id is None:
        raise ValueError("chat_id is required to resolve a production line database")
    line = line_key_for_chat(chat_id)
    if not line:
        raise ValueError(f"chat {chat_id} is not mapped to any configured production line")
    name = db_name_for_line(line)
    if not name:
        raise ValueError(f"line '{line}' has no DB_NAME_{line} configured")
    cfg = dict(BASE_DB_CONFIG)
    cfg["database"] = name
    return cfg


def get_db_connection(chat_id: int | None = None):
    """Get a fresh database connection for the given chat's production line."""
    return psycopg2.connect(**db_config_for_chat(chat_id))


def get_clean_db_connection(chat_id: int | None = None):
    """Get a clean database connection, ensuring no aborted transactions"""
    conn = psycopg2.connect(**db_config_for_chat(chat_id))
    try:
        # Test if connection is clean by running a simple query
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        return conn
    except Exception:
        # If connection is in aborted state, close and create new one
        try:
            conn.close()
        except Exception:
            pass
        return psycopg2.connect(**db_config_for_chat(chat_id))


# Schema checks (CREATE/ALTER) are expensive; run them once per DB per process,
# not on every read/write. A fresh deploy picks up any new migrations automatically.
_SCHEMA_CHECKED: set[str] = set()


def _schema_done(chat_id: int | None) -> bool:
    db_name = db_config_for_chat(chat_id).get("database")
    return db_name in _SCHEMA_CHECKED


def _mark_schema_done(chat_id: int | None) -> None:
    db_name = db_config_for_chat(chat_id).get("database")
    _SCHEMA_CHECKED.add(db_name)


def _ensure_bot_state_table(chat_id: int | None = None):
    """Create bot_state table if it does not exist (checked once per DB per process)."""
    if _schema_done(chat_id):
        return
    conn = get_db_connection(chat_id)
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        _mark_schema_done(chat_id)
    except Exception as e:
        conn.rollback()
        logger.warning(f"Could not ensure bot_state table: {e}")
    finally:
        cur.close()
        conn.close()


def bot_state_get(key: str, chat_id: int | None = None) -> str | None:
    """Get value for a bot_state key. Returns None if not set."""
    _ensure_bot_state_table(chat_id)
    conn = get_db_connection(chat_id)
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM bot_state WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.warning(f"bot_state_get failed: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def bot_state_set(key: str, value: str, chat_id: int | None = None) -> None:
    """Set value for a bot_state key."""
    _ensure_bot_state_table(chat_id)
    conn = get_db_connection(chat_id)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO bot_state (key, value, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
        """,
            (key, value),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning(f"bot_state_set failed: {e}")
    finally:
        cur.close()
        conn.close()


def _ensure_hourly_production_table(chat_id: int | None = None):
    """Create hourly_production + related tables if they don't exist.
    Also migrates old schemas (drops stale NOT NULL columns the code doesn't use).
    Runs once per DB per process (see _schema_done)."""
    if _schema_done(chat_id):
        return
    conn = get_db_connection(chat_id)
    cur = conn.cursor()
    try:
        # 1. Create tables if brand new
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hourly_production (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                shift INTEGER NOT NULL,
                hour INTEGER NOT NULL,
                product_type TEXT,
                plan_pack INTEGER DEFAULT 0,
                actual_output_pack INTEGER DEFAULT 0,
                available_time INTEGER DEFAULT 60,
                vos_info TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, shift, hour)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hourly_downtime_events (
                id SERIAL PRIMARY KEY,
                hourly_production_id INTEGER REFERENCES hourly_production(id) ON DELETE CASCADE,
                description TEXT,
                duration_min INTEGER,
                category TEXT DEFAULT 'MECHANICAL'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hourly_rejects (
                id SERIAL PRIMARY KEY,
                hourly_production_id INTEGER REFERENCES hourly_production(id) ON DELETE CASCADE,
                preform INTEGER DEFAULT 0,
                bottle INTEGER DEFAULT 0,
                cap INTEGER DEFAULT 0,
                label INTEGER DEFAULT 0,
                shrink REAL DEFAULT 0
            )
        """)

        # 2. Schema migration: fix old columns that the code doesn't populate
        #    If hour_start or hour_end exist with NOT NULL, make them nullable
        #    so INSERT doesn't fail when we don't provide values.
        cur.execute("""
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'hourly_production'
              AND column_name IN ('hour_start', 'hour_end')
        """)
        old_columns = cur.fetchall()

        for col_name, is_nullable in old_columns:
            if is_nullable == "NO":
                cur.execute(
                    f"ALTER TABLE hourly_production ALTER COLUMN {col_name} DROP NOT NULL"
                )
                logger.info(
                    f"Schema migration: made hourly_production.{col_name} nullable"
                )

        # 3. Ensure hour column exists (old schema might use hour_number)
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'hourly_production' AND column_name = 'hour'
        """)
        if not cur.fetchone():
            cur.execute("""
                ALTER TABLE hourly_production ADD COLUMN hour INTEGER
            """)
            logger.info("Schema migration: added hour column to hourly_production")

        # 4. Drop hour_plan column if it exists (replaced by available_time)
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'hourly_production' AND column_name = 'hour_plan'
        """)
        if cur.fetchone():
            cur.execute("ALTER TABLE hourly_production DROP COLUMN hour_plan")
            logger.info(
                "Schema migration: dropped hour_plan column (replaced by available_time)"
            )

        # 5. Ensure available_time has DEFAULT 60
        cur.execute("""
            SELECT column_default
            FROM information_schema.columns
            WHERE table_name = 'hourly_production' AND column_name = 'available_time'
        """)
        result = cur.fetchone()
        if result and result[0] != "60":
            cur.execute(
                "ALTER TABLE hourly_production ALTER COLUMN available_time SET DEFAULT 60"
            )
            logger.info("Schema migration: set available_time DEFAULT 60")

        # 6. Ensure UNIQUE constraint on (date, shift, hour) exists
        cur.execute("""
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_name = 'hourly_production'
              AND constraint_type = 'UNIQUE'
        """)
        constraints = [row[0] for row in cur.fetchall()]

        # Check if any unique constraint covers (date, shift, hour)
        has_correct_unique = False
        for cname in constraints:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.constraint_column_usage
                WHERE constraint_name = %s
            """,
                (cname,),
            )
            cols = {row[0] for row in cur.fetchall()}
            if cols == {"date", "shift", "hour"}:
                has_correct_unique = True
                break

        if not has_correct_unique:
            # Drop old unique constraints that might conflict
            for cname in constraints:
                try:
                    cur.execute(
                        f"ALTER TABLE hourly_production DROP CONSTRAINT IF EXISTS {cname}"
                    )
                    logger.info(f"Schema migration: dropped old constraint {cname}")
                except Exception:
                    pass

            try:
                cur.execute("""
                    ALTER TABLE hourly_production
                    ADD CONSTRAINT hourly_production_date_shift_hour_key
                    UNIQUE (date, shift, hour)
                """)
                logger.info("Schema migration: added UNIQUE(date, shift, hour)")
            except Exception as e:
                logger.warning(
                    f"Could not add unique constraint (may already exist): {e}"
                )

        # 7. Performance indexes for the report/aggregation queries
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_hourly_date
            ON hourly_production (date)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_hourly_shift_date
            ON hourly_production (shift, date DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_hourly_dt_hpid
            ON hourly_downtime_events (hourly_production_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_hourly_rej_hpid
            ON hourly_rejects (hourly_production_id)
        """)

        # 8. Add updated_at column where missing (per-table timestamps convention)
        for tbl in ("hourly_production", "production"):
            cur.execute("SELECT to_regclass(%s)", (tbl,))
            if not cur.fetchone()[0]:
                continue
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = %s AND column_name = 'updated_at'
                """,
                (tbl,),
            )
            if not cur.fetchone():
                cur.execute(
                    f"ALTER TABLE {tbl} ADD COLUMN updated_at "
                    "TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP"
                )
                logger.info(f"Schema migration: added updated_at to {tbl}")

        conn.commit()
        _mark_schema_done(chat_id)
        logger.info("Hourly production tables ensured (with schema migration)")
    except Exception as e:
        conn.rollback()
        logger.error(f"_ensure_hourly_production_table failed: {e}", exc_info=True)
    finally:
        cur.close()
        conn.close()


def parse_vos_minutes(vos_value) -> int:
    """
    Parses VOS field which can be:
      - int/float  : 20       → 20
      - numeric str: "20"     → 20
      - text       : "power off 20 min" → 20
      - None / 0 / empty      → 0
    """
    if vos_value is None:
        return 0

    if isinstance(vos_value, (int, float)):
        return int(vos_value)

    try:
        return int(str(vos_value).strip())
    except ValueError:
        pass

    match = re.search(r"\b(\d+)\b", str(vos_value))
    if match:
        return int(match.group(1))

    return 0


def save_to_database(
    data, downtime, rejects, vos_info=None, shift_override: int | None = None,
    chat_id: int | None = None,
):
    """Save production data. Uses shift_override if provided (for /shift_summary_N)."""
    conn = get_db_connection(chat_id)
    cur = conn.cursor()
    shift = shift_override if shift_override is not None else data["shift"]

    SHIFT_DEFAULT_MINUTES = {1: 660, 2: 660}
    available_time = data.get("available_time")
    if available_time is None:
        available_time = SHIFT_DEFAULT_MINUTES.get(shift, 420)

    try:
        cur.execute(
            """
            INSERT INTO production
            (date, shift, product_type, shift_plan_pack, actual_output_pack, vos_info, available_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (date, shift) DO UPDATE SET
                product_type = EXCLUDED.product_type,
                shift_plan_pack = EXCLUDED.shift_plan_pack,
                actual_output_pack = EXCLUDED.actual_output_pack,
                vos_info = EXCLUDED.vos_info,
                available_time = EXCLUDED.available_time,
                updated_at = CURRENT_TIMESTAMP
            RETURNING id
        """,
            (
                data["date"],
                shift,
                data["product_type"],
                data["plan"],
                data["actual"],
                vos_info,
                available_time,
            ),
        )
        production_id = cur.fetchone()[0]

        cur.execute(
            "DELETE FROM downtime_events WHERE production_id = %s", (production_id,)
        )
        cur.execute("DELETE FROM rejects WHERE production_id = %s", (production_id,))

        for d in downtime:
            cur.execute(
                """
                INSERT INTO downtime_events
                (production_id, description, duration_min, category)
                VALUES (%s, %s, %s, %s)
            """,
                (
                    production_id,
                    d["description"],
                    d["duration"],
                    d.get("category", "MECHANICAL"),
                ),
            )

        cur.execute(
            """
            INSERT INTO rejects
            (production_id, preform, bottle, cap, label, shrink)
            VALUES (%s, %s, %s, %s, %s, %s)
        """,
            (
                production_id,
                rejects["preform"],
                rejects["bottle"],
                rejects["cap"],
                rejects["label"],
                rejects["shrink"],
            ),
        )

        conn.commit()
        logger.info(
            f"Saved shift {shift} to DB — available_time={available_time} min "
            f"({'default' if data.get('available_time') is None else 'provided'})"
        )
        return production_id
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()
        conn.close()


def save_hourly_to_database(
    data: dict,
    downtime: list,
    rejects: dict,
    hour_number: int,
    vos_info: str = None,
    shift_override: int = None,
    chat_id: int | None = None,
) -> int | None:
    """
    Save ONE HOUR of production data to hourly_production table.
    Same pattern as save_to_database but for hourly granularity.
    """
    _ensure_hourly_production_table(chat_id)
    conn = get_clean_db_connection(chat_id)
    cur = conn.cursor()
    shift = shift_override if shift_override is not None else data["shift"]
    available_time = data.get("available_time") or 60
    if vos_info:
        vos_minutes = parse_vos_minutes(vos_info)
        available_time = available_time - vos_minutes
        available_time = max(available_time, 0)

    try:
        cur.execute(
            """
            INSERT INTO hourly_production
            (date, shift, hour, product_type, plan_pack, actual_output_pack,
             available_time, vos_info)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (date, shift, hour) DO UPDATE SET
                product_type = EXCLUDED.product_type,
                plan_pack = EXCLUDED.plan_pack,
                actual_output_pack = EXCLUDED.actual_output_pack,
                available_time = EXCLUDED.available_time,
                vos_info = EXCLUDED.vos_info,
                updated_at = CURRENT_TIMESTAMP
            RETURNING id
        """,
            (
                data["date"],
                shift,
                hour_number,
                data["product_type"],
                data["plan"],
                data["actual"],
                available_time,
                vos_info,
            ),
        )
        hourly_id = cur.fetchone()[0]

        cur.execute(
            "DELETE FROM hourly_downtime_events WHERE hourly_production_id = %s",
            (hourly_id,),
        )
        cur.execute(
            "DELETE FROM hourly_rejects WHERE hourly_production_id = %s", (hourly_id,)
        )

        for d in downtime:
            cur.execute(
                """
                INSERT INTO hourly_downtime_events
                (hourly_production_id, description, duration_min, category)
                VALUES (%s, %s, %s, %s)
            """,
                (
                    hourly_id,
                    d["description"],
                    d["duration"],
                    d.get("category", "MECHANICAL"),
                ),
            )

        cur.execute(
            """
            INSERT INTO hourly_rejects
            (hourly_production_id, preform, bottle, cap, label, shrink)
            VALUES (%s, %s, %s, %s, %s, %s)
        """,
            (
                hourly_id,
                rejects.get("preform", 0),
                rejects.get("bottle", 0),
                rejects.get("cap", 0),
                rejects.get("label", 0),
                rejects.get("shrink", 0),
            ),
        )

        conn.commit()
        logger.info(
            f"Saved hourly data: shift={shift} hour={hour_number} to DB (id={hourly_id})"
        )
        return hourly_id
    except Exception as e:
        conn.rollback()
        logger.error(f"save_hourly_to_database failed: {e}")
        return None
    finally:
        cur.close()
        conn.close()

def _shift_had_any_production(
    shift: int, date_iso: str, chat_id: int | None = None
) -> bool:
    """
    Check if a shift had ANY production (even partial).
    Returns True if there was any production for the shift.
    """
    try:
        conn = get_db_connection(chat_id)
        cur = conn.cursor()

        # Check main production table for any actual output
        cur.execute(
            "SELECT actual_output_pack FROM production WHERE date = %s AND shift::text = %s::text",
            (date_iso, shift),
        )
        result = cur.fetchone()

        # If main production record exists with >0 output, return True
        if result is not None and result[0] > 0:
            cur.close()
            conn.close()
            return True

        # Also try to check hourly production table for more granular data if it exists
        try:
            cur.execute(
                "SELECT COUNT(*) FROM hourly_production WHERE date = %s AND shift = %s AND actual_output_pack > 0",
                (date_iso, shift),
            )
            hourly_count = cur.fetchone()[0]
            if hourly_count > 0:
                cur.close()
                conn.close()
                return True
        except Exception:
            # hourly_production table might not exist, that's fine
            pass

        cur.close()
        conn.close()

        return False
    except Exception as e:
        logger.error(f"Error checking shift production: {e}")
        # On error, assume there was production to avoid missing summaries
        return True


