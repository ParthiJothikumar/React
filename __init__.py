import azure.functions as func

from orchestrator_app import app


async def main(req: func.HttpRequest, context: func.Context) -> func.HttpResponse:
    """Single entry point: forward every HTTP request into the FastAPI app."""
    return await func.AsgiMiddleware(app).handle_async(req, context)
