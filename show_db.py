import os, sqlite3

db = os.getenv("SQLITE_DB_PATH") or "orchestrator.db"   # <- your actual path
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=== SESSIONS ===")
for r in cur.execute("SELECT * FROM sessions ORDER BY updated_at DESC"):
    print(dict(r))

print("\n=== CONVERSATIONS ===")
for r in cur.execute(
    "SELECT session_id, seq, stage, question, answer, summary, vars "
    "FROM conversations ORDER BY session_id, seq"
):
    print(dict(r))

conn.close()
