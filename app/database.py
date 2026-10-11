
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from app.config import DATABASE_URL

def _db_path():
    prefix = "sqlite:///"
    if DATABASE_URL.startswith(prefix):
        return DATABASE_URL[len(prefix):]
    raise ValueError("Only SQLite DATABASE_URL values are supported by this MVP")

DB_PATH = _db_path()

def init_db():
    path = Path(DB_PATH)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            instruction TEXT NOT NULL,
            keywords TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_run_at TEXT,
            interval_minutes INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            title TEXT NOT NULL,
            company TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            score INTEGER NOT NULL DEFAULT 0,
            discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'new',
            match_reasons TEXT NOT NULL DEFAULT '',
            UNIQUE(task_id, source, source_id),
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            item_count INTEGER NOT NULL DEFAULT 0,
            checked_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        CREATE TABLE IF NOT EXISTS preferences (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS assistant_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS whatsapp_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_message_id TEXT NOT NULL UNIQUE,
            direction TEXT NOT NULL,
            contact TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            message_type TEXT NOT NULL DEFAULT 'text',
            status TEXT NOT NULL DEFAULT 'received',
            provider_timestamp TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS google_oauth_tokens (
            id INTEGER PRIMARY KEY CHECK (id=1),
            access_token TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            scopes TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS instagram_oauth_tokens (
            id INTEGER PRIMARY KEY CHECK (id=1),
            access_token TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            scopes TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS source_health (
            source TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            last_checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            checked_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS task_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(task_id, url),
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        ''')
        _migrate_columns(conn)
        conn.execute(
            "INSERT OR IGNORE INTO system_state(key, value) VALUES('emergency_stop', 'false')"
        )
    return True


def _migrate_columns(conn):
    migrations = {
        "tasks": {"interval_minutes": "INTEGER NOT NULL DEFAULT 0"},
        "findings": {
            "status": "TEXT NOT NULL DEFAULT 'new'",
            "match_reasons": "TEXT NOT NULL DEFAULT ''",
        },
        "runs": {"checked_count": "INTEGER NOT NULL DEFAULT 0"},
    }
    for table, columns in migrations.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
