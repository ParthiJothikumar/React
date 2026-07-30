import azure.functions as func
from fastapi import FastAPI                          # (1) new import

from app.main import app

root = FastAPI()                                     # (3) a wrapper app
root.mount("/workflow", app)                # (4) THE key line

async def main(req: func.HttpRequest, context: func.Context) -> func.HttpResponse:
    return await func.AsgiMiddleware(root).handle_async(req, context)   # (5) pass root
