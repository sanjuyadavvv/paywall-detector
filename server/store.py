"""SQLite persistence for classifications, community votes, and rate limits."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "freeornot.db"
CLASSIFIED_JSON = BASE_DIR / "classified_sites.json"
REPORTS_JSON = BASE_DIR / "reports.json"
RATE_JSON = BASE_DIR / "report_rate.json"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_migrated = False


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
    return _conn


def init_db() -> None:
    global _migrated
    with _lock:
        db = _connect()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS classified (
                domain TEXT PRIMARY KEY,
                classified_at REAL NOT NULL,
                source TEXT,
                result_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                stance TEXT NOT NULL,
                report_type TEXT,
                url TEXT,
                note TEXT,
                last_reported_at REAL NOT NULL,
                UNIQUE(domain, fingerprint)
            );
            CREATE INDEX IF NOT EXISTS idx_reports_domain ON reports(domain);
            CREATE TABLE IF NOT EXISTS rate_events (
                key TEXT NOT NULL,
                ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rate_key_ts ON rate_events(key, ts);
            """
        )
        if not _migrated:
            _migrate_json_unlocked(db)
            _migrated = True


def _migrate_json_unlocked(db: sqlite3.Connection) -> None:
    classified_n = db.execute("SELECT COUNT(*) AS n FROM classified").fetchone()["n"]
    if classified_n == 0 and CLASSIFIED_JSON.exists():
        try:
            data = json.loads(CLASSIFIED_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        if isinstance(data, dict):
            for domain, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO classified(domain, classified_at, source, result_json) VALUES (?,?,?,?)",
                    (
                        str(domain).lower(),
                        float(entry.get("classified_at") or time.time()),
                        entry.get("source") or "cache",
                        json.dumps(entry.get("result") or entry),
                    ),
                )

    reports_n = db.execute("SELECT COUNT(*) AS n FROM reports").fetchone()["n"]
    if reports_n == 0 and REPORTS_JSON.exists():
        try:
            rows = json.loads(REPORTS_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            rows = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                domain = str(row.get("domain") or "").lower()
                fingerprint = row.get("fingerprint")
                if not domain or not fingerprint:
                    continue
                ts = row.get("last_reported_at")
                if ts is None:
                    ts = row.get("reportedAt") or time.time()
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    ts = time.time()
                if ts > 1e12:
                    ts = ts / 1000.0
                stance = "free" if str(row.get("report_type") or "").startswith("false") else "paid"
                if row.get("stance") in ("paid", "free"):
                    stance = row["stance"]
                try:
                    db.execute(
                        """
                        INSERT OR IGNORE INTO reports
                        (domain, fingerprint, stance, report_type, url, note, last_reported_at)
                        VALUES (?,?,?,?,?,?,?)
                        """,
                        (
                            domain,
                            fingerprint,
                            stance,
                            row.get("report_type") or "paywall_before_export",
                            row.get("url"),
                            row.get("note"),
                            ts,
                        ),
                    )
                except sqlite3.Error:
                    continue


def load_classified() -> dict:
    init_db()
    with _lock:
        rows = _connect().execute(
            "SELECT domain, classified_at, source, result_json FROM classified"
        ).fetchall()
    out = {}
    for row in rows:
        try:
            result = json.loads(row["result_json"])
        except json.JSONDecodeError:
            continue
        out[row["domain"]] = {
            "classified_at": row["classified_at"],
            "source": row["source"],
            "result": result,
        }
    return out


def classified_count() -> int:
    init_db()
    with _lock:
        return int(_connect().execute("SELECT COUNT(*) AS n FROM classified").fetchone()["n"])


def get_classified(domain: str) -> dict | None:
    init_db()
    with _lock:
        row = _connect().execute(
            "SELECT domain, classified_at, source, result_json FROM classified WHERE domain = ?",
            (domain,),
        ).fetchone()
    if not row:
        return None
    try:
        result = json.loads(row["result_json"])
    except json.JSONDecodeError:
        return None
    return {
        "classified_at": row["classified_at"],
        "source": row["source"],
        "result": result,
    }


def upsert_classified(domain: str, source: str, result: dict, classified_at: float | None = None) -> None:
    init_db()
    with _lock:
        _connect().execute(
            "INSERT OR REPLACE INTO classified(domain, classified_at, source, result_json) VALUES (?,?,?,?)",
            (domain, classified_at if classified_at is not None else time.time(), source, json.dumps(result)),
        )


def delete_classified(domain: str) -> None:
    init_db()
    with _lock:
        _connect().execute("DELETE FROM classified WHERE domain = ?", (domain,))


def load_fresh_reports(now: float, ttl_seconds: float) -> list[dict]:
    init_db()
    cutoff = now - ttl_seconds
    with _lock:
        rows = _connect().execute(
            """
            SELECT domain, fingerprint, stance, report_type, url, note, last_reported_at
            FROM reports WHERE last_reported_at >= ?
            """,
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def find_report(domain: str, fingerprints: set[str], now: float, ttl_seconds: float) -> dict | None:
    init_db()
    cutoff = now - ttl_seconds
    if not fingerprints:
        return None
    placeholders = ",".join("?" * len(fingerprints))
    params = [domain, cutoff, *fingerprints]
    with _lock:
        row = _connect().execute(
            f"""
            SELECT domain, fingerprint, stance, report_type, url, note, last_reported_at
            FROM reports
            WHERE domain = ? AND last_reported_at >= ? AND fingerprint IN ({placeholders})
            LIMIT 1
            """,
            params,
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
    with _lock:
        _connect().execute(
            """
            INSERT INTO reports (domain, fingerprint, stance, report_type, url, note, last_reported_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(domain, fingerprint) DO UPDATE SET
                stance = excluded.stance,
                report_type = excluded.report_type,
                url = excluded.url,
                note = excluded.note,
                last_reported_at = excluded.last_reported_at
            """,
            (domain, fingerprint, stance, report_type, url, note, now),
        )


def vote_counts(domain: str, now: float, ttl_seconds: float) -> dict:
    init_db()
    cutoff = now - ttl_seconds
    with _lock:
        rows = _connect().execute(
            """
            SELECT stance, COUNT(DISTINCT fingerprint) AS n
            FROM reports
            WHERE domain = ? AND last_reported_at >= ?
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
    cutoff = now - window
    with _lock:
        _connect().execute("DELETE FROM rate_events WHERE ts < ?", (cutoff,))


def record_rate(key: str, now: float, window: float, max_events: int) -> tuple[bool, int]:
    init_db()
    cutoff = now - window
    with _lock:
        db = _connect()
        db.execute("DELETE FROM rate_events WHERE key = ? AND ts < ?", (key, cutoff))
        n = int(
            db.execute(
                "SELECT COUNT(*) AS n FROM rate_events WHERE key = ? AND ts >= ?",
                (key, cutoff),
            ).fetchone()["n"]
        )
        if n >= max_events:
            return False, 0
        db.execute("INSERT INTO rate_events(key, ts) VALUES (?,?)", (key, now))
        return True, max_events - n - 1
