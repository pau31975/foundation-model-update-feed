"""FastAPI application entry point with all routes and lifecycle hooks."""

from __future__ import annotations

import logging
import logging.config
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Optional

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import crud
from app.config import settings
from app.db import get_db, init_db
from app.schemas import (
    ChangeType,
    CollectResult,
    FeedPage,
    FeedQuery,
    ModelUpdateCreate,
    ModelUpdateRead,
    Provider,
    Severity,
)
from app.services.collector_service import run_all_collectors

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

_LOG_LEVEL = settings.log_level.upper()

logging.config.dictConfig(
    {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": structlog.stdlib.ProcessorFormatter,
                "processor": structlog.dev.ConsoleRenderer(colors=True),
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "json",
            }
        },
        "root": {"handlers": ["console"], "level": _LOG_LEVEL},
    }
)

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# App directories
# ---------------------------------------------------------------------------

_BASE_DIR = Path(__file__).parent
_TEMPLATES_DIR = _BASE_DIR / "templates"
_STATIC_DIR = _BASE_DIR / "static"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run startup and shutdown logic."""
    logger.info("Starting llm-provider-update-feed", version="1.0.0")
    init_db()
    logger.info("Database ready")
    yield
    logger.info("Shutting down")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="LLM Provider Update Feed",
    description=(
        "A feed service that tracks model lifecycle events "
        "(new models, deprecations, retirements) across major LLM providers."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# Reusable DB dependency alias
DBDep = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health", tags=["ops"], summary="Health check")
def health() -> dict[str, str]:
    """Return service health status."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse, tags=["ui"], include_in_schema=False)
def index(
    request: Request,
    db: DBDep,
    provider: Optional[str] = None,
    severity: Optional[str] = None,
    change_type: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> HTMLResponse:
    """Render the main feed page."""
    query = FeedQuery(
        provider=Provider(provider) if provider else None,
        severity=Severity(severity) if severity else None,
        change_type=ChangeType(change_type) if change_type else None,
        limit=limit,
    )
    rows, total = crud.list_updates(db, query)
    items = [ModelUpdateRead.model_validate(r) for r in rows]

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "items": items,
            "total": total,
            "limit": limit,
            "providers": [p.value for p in Provider],
            "severities": [s.value for s in Severity],
            "change_types": [ct.value for ct in ChangeType],
            "selected_provider": provider or "",
            "selected_severity": severity or "",
            "selected_change_type": change_type or "",
        },
    )


# ---------------------------------------------------------------------------
# REST – feed
# ---------------------------------------------------------------------------


@app.get(
    "/api/updates",
    response_model=FeedPage,
    tags=["feed"],
    summary="List model update events",
)
def list_updates(
    db: DBDep,
    provider: Optional[Provider] = None,
    severity: Optional[Severity] = None,
    change_type: Optional[ChangeType] = None,
    since: Optional[datetime] = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: Optional[str] = None,
) -> FeedPage:
    """Return a paginated list of model-update feed items.

    Supports optional filters on **provider**, **severity**, **change_type**,
    and **since** (ISO 8601 datetime).

    Cursor-based pagination: pass the ``next_cursor`` from the previous
    response to retrieve the next page.
    """
    query = FeedQuery(
        provider=provider,
        severity=severity,
        change_type=change_type,
        since=since,
        limit=limit,
        cursor=cursor,
    )
    rows, total = crud.list_updates(db, query)
    items = [ModelUpdateRead.model_validate(r) for r in rows]

    next_cursor: str | None = None
    if len(items) == limit:
        next_cursor = items[-1].created_at.isoformat()

    return FeedPage(items=items, total=total, limit=limit, next_cursor=next_cursor)


@app.post(
    "/api/updates",
    response_model=ModelUpdateRead,
    status_code=201,
    tags=["feed"],
    summary="Create a model update event manually",
)
def create_update(item: ModelUpdateCreate, db: DBDep) -> ModelUpdateRead:
    """Manually insert a new model update event.

    The endpoint validates all fields and enforces deduplication via a
    content fingerprint.  Returns **409 Conflict** if an identical item
    already exists.
    """
    result = crud.create_update(db, item)
    if result is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Duplicate: an item with the same provider, model, change_type, "
                "effective_at, source_url, and title already exists."
            ),
        )
    return ModelUpdateRead.model_validate(result)


# ---------------------------------------------------------------------------
# REST – collect
# ---------------------------------------------------------------------------


@app.post(
    "/api/collect",
    response_model=CollectResult,
    tags=["ops"],
    summary="Trigger live data collection from all providers",
)
def collect(db: DBDep) -> CollectResult:
    """Run all provider collectors synchronously and return a summary.

    - **added**: number of new items persisted
    - **skipped**: number of duplicate items ignored
    - **errors**: list of per-provider error messages (if any)

    This endpoint is safe to call repeatedly – duplicates are silently skipped.
    """
    logger.info("Manual collection triggered via POST /api/collect")
    return run_all_collectors(db)
