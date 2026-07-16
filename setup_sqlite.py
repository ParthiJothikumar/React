"""Create the local SQLite tables for offline testing.

Run once:  python setup_sqlite.py
Then set SQLITE_DB_PATH=local.db in your .env and run the app; it will use
SQLite instead of Azure SQL. Delete local.db anytime to start fresh.
"""
import os
import sqlite3

from dotenv import load_dotenv

load_dotenv()

path = os.getenv("SQLITE_DB_PATH") or "local.db"

conn = sqlite3.connect(path)
conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id                        TEXT    NOT NULL,
        user_id                   TEXT    NOT NULL,
        conversation_id           TEXT,
        stage                     TEXT,
        vars                      TEXT,          -- JSON string
        question                  TEXT,
        answer                    TEXT,
        session_id                TEXT,
        seq                       INTEGER,
        previous_conversation_id  TEXT,
        summary                   TEXT,
        title                     TEXT,
        rolled_over               INTEGER,
        created_at                TEXT,
        updated_at                TEXT,
        PRIMARY KEY (user_id, id)
    );

    CREATE TABLE IF NOT EXISTS sessions (
        id                        TEXT    NOT NULL,
        session_id                TEXT,
        user_id                   TEXT    NOT NULL,
        current_conversation_id   TEXT,
        title                     TEXT,
        created_at                TEXT,
        updated_at                TEXT,
        PRIMARY KEY (user_id, id)
    );
    """
)
conn.commit()
conn.close()
print(f"SQLite tables created at: {os.path.abspath(path)}")
