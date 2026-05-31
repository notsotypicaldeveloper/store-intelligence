import os
import sqlite3
from contextlib import contextmanager


def _get_db_path() -> str:
    return os.environ.get("DB_PATH", "events.db")


def _connect() -> sqlite3.Connection:
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables and indexes; safe to call on every startup (idempotent)."""
    db_path = _get_db_path()
    if db_path not in (":memory:", "") and os.path.dirname(db_path):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id    TEXT PRIMARY KEY,
                store_id    TEXT NOT NULL,
                camera_id   TEXT NOT NULL,
                visitor_id  TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                timestamp   TEXT NOT NULL,
                zone_id     TEXT,
                dwell_ms    INTEGER DEFAULT 0,
                is_staff    INTEGER DEFAULT 0,
                confidence  REAL NOT NULL,
                metadata    TEXT DEFAULT '{}',
                ingested_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_ev_store   ON events(store_id);
            CREATE INDEX IF NOT EXISTS idx_ev_visitor ON events(visitor_id);
            CREATE INDEX IF NOT EXISTS idx_ev_ts      ON events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_ev_type    ON events(event_type);
        """)
