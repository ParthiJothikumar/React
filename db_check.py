import os, sqlite3

db = os.getenv("SQLITE_DB_PATH") or "orchestrator.db"   # <- your actual path
conn = sqlite3.connect(db)
cur = conn.cursor()
for t in ("conversations", "sessions"):
    cur.execute(f"DELETE FROM {t}")
    print(f"cleared {t}: {cur.rowcount} rows")
conn.commit()
conn.close()


import os, sqlite3

db = os.getenv("SQLITE_DB_PATH") or "orchestrator.db"
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=== SESSIONS ===")
for r in cur.execute(
    "SELECT id, current_conversation_id, title, updated_at "
    "FROM sessions ORDER BY updated_at DESC"
):
    print(dict(r))

print("\n=== CONVERSATIONS ===")
for r in cur.execute(
    "SELECT session_id, seq, stage, question, answer, vars "
    "FROM conversations ORDER BY session_id, seq"
):
    print(dict(r))

conn.close()
