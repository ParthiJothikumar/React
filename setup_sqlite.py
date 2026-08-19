"""Create the local SQLite tables for offline testing.

Run once:  python setup_sqlite.py
Then set SQLITE_DB_PATH=local.db in your .env and run the app; it will use
SQLite instead of Azure SQL. Delete local.db anytime to start fresh.

The schema is IMPORTED from app.db rather than copied here. It used to be duplicated,
and the copy had already drifted (it was missing job_baseline). One definition means the
tables this script creates are always the ones the app expects.

Note the app also creates these tables on first connect, so running this is optional --
it's here for when you want a database file ready before starting anything.
"""
import os
import sqlite3

from dotenv import load_dotenv

from app.db import SQLITE_SCHEMA

load_dotenv()

path = os.getenv("SQLITE_DB_PATH") or "local.db"

conn = sqlite3.connect(path)
conn.executescript(SQLITE_SCHEMA)
conn.commit()
conn.close()
print(f"SQLite tables created at: {os.path.abspath(path)}")
