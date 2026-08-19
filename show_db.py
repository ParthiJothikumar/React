"""Print all rows in the local SQLite DB.

Run:  python show_db.py
"""
import os
import sqlite3

from dotenv import load_dotenv

load_dotenv()

path = os.getenv("SQLITE_DB_PATH") or "local.db"
print("DB file:", os.path.abspath(path))

conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row   # so rows print as dicts
cur = conn.cursor()

for table in ("sessions", "conversations"):
    print(f"\n===== {table} =====")
    try:
        rows = cur.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.OperationalError as exc:
        print("  (can't read table:", exc, ")")
        continue
    print(f"  {len(rows)} row(s)")
    for r in rows:
        print("  -", dict(r))

conn.close()
