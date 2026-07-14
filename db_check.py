"""Quick Azure SQL connection test using mssql-python.

Reads SQL_CONNECTION_STRING from the environment or a local .env file,
connects, runs a simple query, and checks that the app's tables exist.

Run:  python db_check.py
"""
import os

from dotenv import load_dotenv
import mssql_python

load_dotenv()

conn_str = "Server=tcp:ai-foundry-memory.database.windows.net,1433;Database=ai-foundry-memory-db;Uid=aifoundrymemory;Pwd=Safari@1234#;Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
if not conn_str:
    raise SystemExit("SQL_CONNECTION_STRING is not set (check your .env / environment).")

print("Connecting to Azure SQL...")
try:
    conn = mssql_python.connect(conn_str)
except Exception as exc:
    raise SystemExit(f"Connection FAILED: {exc}")

try:
    cur = conn.cursor()

    # 1. basic connectivity
    cur.execute("SELECT @@VERSION")
    print("Connected. Server:", cur.fetchone()[0].splitlines()[0])

    # 2. do our tables exist?
    cur.execute(
        "SELECT name FROM sys.tables WHERE name IN ('conversations', 'sessions')"
    )
    tables = [r[0] for r in cur.fetchall()]
    print("Tables found:", tables if tables else "(none - run schema.sql first)")

    print("OK - SQL connection works.")
finally:
    conn.close()
