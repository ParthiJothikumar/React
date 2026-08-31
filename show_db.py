"""Print all rows in the local SQLite DB.

Run:  python show_db.py
"""
import os
import sqlite3

from dotenv import load_dotenv

load_dotenv()

MAX_CHARS = 200


def _short(value):
    """Trim one column value for display."""
    text = str(value)
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + f"...(+{len(text) - MAX_CHARS})"


path = os.getenv("SQLITE_DB_PATH") or "local.db"
print("DB file:", os.path.abspath(path))

conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row   # so rows print as dicts
cur = conn.cursor()

# Read the table list from the DB instead of hardcoding it. The old fixed list
# ("sessions", "conversations") silently omitted conversation_turns and agent_calls,
# so tables added later were invisible here -- which is the opposite of what a
# "print all rows" script is for.
tables = [
    r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
]

for table in tables:
    print(f"\n===== {table} =====")
    try:
        rows = cur.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.OperationalError as exc:
        print("  (can't read table:", exc, ")")
        continue
    print(f"  {len(rows)} row(s)")
    for r in rows:
        # agent_calls stores whole agent responses, so a row can be thousands of
        # characters; truncate per column to keep the output readable.
        print("  -", {k: _short(r[k]) for k in r.keys()})

conn.close()
