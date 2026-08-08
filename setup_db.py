"""Create the 4 per-line production databases with their full schema.

Run once:  python3 setup_db.py

Uses the same DB_USER/DB_PASSWORD from .env (must have CREATEDB rights).
Databases are created if missing; tables are created idempotently.
"""

import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PORT = int(os.getenv("DB_PORT", 5432))

LINES = ["1ltr", "2ltr", "0.6ltr", "0.3ltr"]

HOURLY_TABLES = [
    """
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
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(date, shift, hour)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hourly_downtime_events (
        id SERIAL PRIMARY KEY,
        hourly_production_id INTEGER REFERENCES hourly_production(id) ON DELETE CASCADE,
        description TEXT,
        duration_min INTEGER,
        category TEXT DEFAULT 'MECHANICAL'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hourly_rejects (
        id SERIAL PRIMARY KEY,
        hourly_production_id INTEGER REFERENCES hourly_production(id) ON DELETE CASCADE,
        preform INTEGER DEFAULT 0,
        bottle INTEGER DEFAULT 0,
        cap INTEGER DEFAULT 0,
        label INTEGER DEFAULT 0,
        shrink REAL DEFAULT 0
    )
    """,
]

HOURLY_INDEXES = [
    """
    CREATE INDEX IF NOT EXISTS idx_hourly_date
    ON hourly_production (date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hourly_shift_date
    ON hourly_production (shift, date DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hourly_dt_hpid
    ON hourly_downtime_events (hourly_production_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hourly_rej_hpid
    ON hourly_rejects (hourly_production_id)
    """,
]

BOT_STATE_TABLE = """
    CREATE TABLE IF NOT EXISTS bot_state (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    )
    """


def db_names():
    return {line: f"Asku_Production_line_{line}" for line in LINES}


def database_exists(conn, name: str) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        return cur.fetchone() is not None
    finally:
        cur.close()


def create_databases():
    print("Connecting to maintenance DB 'postgres' ...")
    conn = psycopg2.connect(
        host=DB_HOST, database="postgres", user=DB_USER, password=DB_PASSWORD, port=DB_PORT
    )
    conn.autocommit = True
    try:
        for line, name in db_names().items():
            if database_exists(conn, name):
                print(f"  [skip] {name} already exists")
                continue
            cur = conn.cursor()
            try:
                cur.execute(f'CREATE DATABASE "{name}"')
                print(f"  [ok]   created {name}  (line {line})")
            finally:
                cur.close()
    finally:
        conn.close()


def create_tables():
    for line, name in db_names().items():
        print(f"Creating tables in {name} ...")
        conn = psycopg2.connect(
            host=DB_HOST, database=name, user=DB_USER, password=DB_PASSWORD, port=DB_PORT
        )
        cur = conn.cursor()
        try:
            for ddl in HOURLY_TABLES + HOURLY_INDEXES:
                cur.execute(ddl)
            cur.execute(BOT_STATE_TABLE)
            conn.commit()
            print(f"  [ok]   all 4 tables ready in {name}")
        except Exception as e:
            conn.rollback()
            print(f"  [FAIL] {name}: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            cur.close()
            conn.close()


if __name__ == "__main__":
    if not (DB_HOST and DB_USER and DB_PASSWORD):
        print("Missing DB_* env vars. Check .env", file=sys.stderr)
        sys.exit(1)
    create_databases()
    create_tables()
    print("Done.")
