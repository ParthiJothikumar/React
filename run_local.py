"""Standalone queue test -- no project files needed.
Setup:  pip install azure-storage-queue   (and have Azurite running for the local string)
Run:    python queue_test.py
"""
import json
from azure.storage.queue import QueueClient

# LOCAL Azurite (default). To test your REAL Azure account instead, paste the
# AzureWebJobsStorage connection string from your Function App's env here.
CONN = ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
        "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
        "K1SZFPTOtr/KBHBeksoGMGw==;QueueEndpoint=http://127.0.0.1:10001/devstoreaccount1;")
QUEUE = "diag-jobs"

q = QueueClient.from_connection_string(CONN, QUEUE)
try:
    q.create_queue()          # make the queue if it doesn't exist
except Exception:
    pass

# 1) ENQUEUE a test message
msg = {"job_id": "job_test", "conversation_id": "c1", "tries": 0}
q.send_message(json.dumps(msg))
print("ENQUEUED ->", msg)

# 2) PEEK it back (does NOT remove it) so you can see it's really there
print("IN QUEUE NOW:")
for m in q.peek_messages(max_messages=5):
    print("   -", m.content)

# 3) approximate count (includes hidden messages too)
print("approx message count:", q.get_queue_properties().approximate_message_count)
