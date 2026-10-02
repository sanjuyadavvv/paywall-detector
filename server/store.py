"""Neon (Postgres) persistence for classifications, community votes, and rate limits.

Same public functions as the old SQLite store, so server.py needs no changes.
Requires DATABASE_URL (use Neon's *pooled* connection string, host contains "-pooler").
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL")

_init_lock = threading.Lock()
_initialized = False


def _connect() -> psycopg.Connection:
    """Open a short-lived connection. On serverless, per-request connections
    through Neon's pooler are the safe pattern (no stale connections)."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        autocommit=True,
        # The pooled endpoint uses PgBouncer (transaction mode), which does
        # not support server-side prepared statements.
        prepare_threshold=None,
        connect_timeout=10,
    )


SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS classified (
        domain TEXT PRIMARY KEY,
        classified_at DOUBLE PRECISION NOT NULL,
        source TEXT,
        result_json JSONB NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reports (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        domain TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        stance TEXT NOT NULL,
        report_type TEXT,
        url TEXT,
        note TEXT,
        last_reported_at DOUBLE PRECISION NOT NULL,
        UNIQUE (domain, fingerprint)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reports_domain ON reports(domain)",
    """
    CREATE TABLE IF NOT EXISTS rate_events (
        key TEXT NOT NULL,
        ts DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rate_key_ts ON rate_events(key, ts)",
)


def init_db() -> None:
    """Create tables once per process (cheap no-op afterwards)."""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        with _connect() as db:
            with db.transaction():
                # Serialise concurrent cold starts creating the same tables.
                db.execute("SELECT pg_advisory_xact_lock(727274)")
                for stmt in SCHEMA:
                    db.execute(stmt)
        _initialized = True


def load_classified() -> dict:
    init_db()
    with _connect() as db:
        rows = db.execute(
            "SELECT domain, classified_at, source, result_json FROM classified"
        ).fetchall()
    return {
        r["domain"]: {
            "classified_at": r["classified_at"],
            "source": r["source"],
            "result": r["result_json"],
        }
        for r in rows
    }


def classified_count() -> int:
    init_db()
    with _connect() as db:
        return int(db.execute("SELECT COUNT(*) AS n FROM classified").fetchone()["n"])


def get_classified(domain: str) -> dict | None:
    init_db()
    with _connect() as db:
        row = db.execute(
            "SELECT classified_at, source, result_json FROM classified WHERE domain = %s",
            (domain,),
        ).fetchone()
    if not row:
        return None
    return {
        "classified_at": row["classified_at"],
        "source": row["source"],
        "result": row["result_json"],
    }


def upsert_classified(domain: str, source: str, result: dict, classified_at: float | None = None) -> None:
    import time

    init_db()
    ts = classified_at if classified_at is not None else time.time()
    with _connect() as db:
        db.execute(
            """
            INSERT INTO classified (domain, classified_at, source, result_json)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (domain) DO UPDATE SET
                classified_at = EXCLUDED.classified_at,
                source = EXCLUDED.source,
                result_json = EXCLUDED.result_json
            """,
            (domain, ts, source, Jsonb(result)),
        )


def delete_classified(domain: str) -> None:
    init_db()
    with _connect() as db:
        db.execute("DELETE FROM classified WHERE domain = %s", (domain,))


def load_fresh_reports(now: float, ttl_seconds: float) -> list[dict]:
    init_db()
    cutoff = now - ttl_seconds
    with _connect() as db:
        rows = db.execute(
            """
            SELECT domain, fingerprint, stance, report_type, url, note, last_reported_at
            FROM reports WHERE last_reported_at >= %s
            """,
            (cutoff,),
        ).fetchall()
    return list(rows)


def find_report(domain: str, fingerprints: set[str], now: float, ttl_seconds: float) -> dict | None:
    init_db()
    if not fingerprints:
        return None
    cutoff = now - ttl_seconds
    with _connect() as db:
        row = db.execute(
            """
            SELECT domain, fingerprint, stance, report_type, url, note, last_reported_at
            FROM reports
            WHERE domain = %s AND last_reported_at >= %s AND fingerprint = ANY(%s)
            LIMIT 1
            """,
            (domain, cutoff, list(fingerprints)),
        ).fetchone()
    return dict(row) if row else None


def upsert_report(
    domain: str,
    fingerprint: str,
    stance: str,
    report_type: str,
    url,
    note,
    now: float,
) -> None:
    init_db()
    with _connect() as db:
        db.execute(
            """
            INSERT INTO reports (domain, fingerprint, stance, report_type, url, note, last_reported_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (domain, fingerprint) DO UPDATE SET
                stance = EXCLUDED.stance,
                report_type = EXCLUDED.report_type,
                url = EXCLUDED.url,
                note = EXCLUDED.note,
                last_reported_at = EXCLUDED.last_reported_at
            """,
            (domain, fingerprint, stance, report_type, url, note, now),
        )


def vote_counts(domain: str, now: float, ttl_seconds: float) -> dict:
    init_db()
    cutoff = now - ttl_seconds
    with _connect() as db:
        rows = db.execute(
            """
            SELECT stance, COUNT(DISTINCT fingerprint) AS n
            FROM reports
            WHERE domain = %s AND last_reported_at >= %s
            GROUP BY stance
            """,
            (domain, cutoff),
        ).fetchall()
    paid = 0
    free = 0
    for row in rows:
        if row["stance"] == "free":
            free = int(row["n"])
        else:
            paid = int(row["n"])
    return {"paid": paid, "free": free, "net_paid": paid - free, "total": paid + free}


def prune_rate(now: float, window: float) -> None:
    init_db()
    with _connect() as db:
        db.execute("DELETE FROM rate_events WHERE ts < %s", (now - window,))


def record_rate(key: str, now: float, window: float, max_events: int) -> tuple[bool, int]:
    init_db()
    cutoff = now - window
    with _connect() as db:
        with db.transaction():
            # A Python threading.Lock can't protect across serverless instances,
            # so lock per key inside the database instead.
            db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
            db.execute("DELETE FROM rate_events WHERE key = %s AND ts < %s", (key, cutoff))
            n = int(
                db.execute(
                    "SELECT COUNT(*) AS n FROM rate_events WHERE key = %s AND ts >= %s",
                    (key, cutoff),
                ).fetchone()["n"]
            )
            if n >= max_events:
                return False, 0
            db.execute("INSERT INTO rate_events (key, ts) VALUES (%s, %s)", (key, now))
            return True, max_events - n - 1