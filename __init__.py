"""IT Support Orchestrator API package.

The FastAPI application (`app.main:app`) was split out of the original single-file
orchestrator_app.py into focused modules. Runtime behavior and the HTTP contract are
unchanged -- this is purely a structural refactor.

Module map:
    config.py         env vars, flow constants, logging bootstrap
    db.py             SQLite/Azure SQL connection + row->dict helpers
    schemas.py        Pydantic request models
    clients/          outbound I/O (Foundry SDK + the sibling agent Function Apps)
    flow.py           the support-flow state machine (framework-agnostic)
    persistence.py    conversations + sessions CRUD
    routes.py         the HTTP endpoints (APIRouter)
    main.py           builds and exposes the FastAPI `app`
"""
