import socket

HOST = "ai-foundry-memory.database.windows.net"
PORT = 1433

try:
    print(f"Testing raw TCP connection to {HOST}:{PORT} ...")
    sock = socket.create_connection((HOST, PORT), timeout=10)
    print("SUCCESS: TCP connection established")
    sock.close()
except Exception as e:
    print("FAILED:", type(e).__name__, e)
