import sys
print("start", flush=True)

import pymssql
print("pymssql imported, version:", pymssql.__version__, flush=True)

SERVER   = "YOURSERVER.database.windows.net"   # or host\instance, or IP
PORT     = 1433
USER     = "YOURUSER"          # Azure SQL: try "user@YOURSERVER" if plain user fails
PASSWORD = "YOURPASSWORD"
DATABASE = "YOURDB"

try:
    print("connecting...", flush=True)
    conn = pymssql.connect(
        server=SERVER,
        port=PORT,
        user=USER,
        password=PASSWORD,
        database=DATABASE,
        login_timeout=10,     # so it fails fast instead of hanging
        timeout=10,
    )
    print("connected OK", flush=True)

    cur = conn.cursor()
    cur.execute("SELECT @@VERSION")
    print("result:", cur.fetchone(), flush=True)

    cur.close()
    conn.close()
    print("done", flush=True)

except Exception as e:
    print("FAILED:", type(e).__name__, e, flush=True)
    sys.exit(1)
