"""FastAPI application.

Read-only by construction: there is no write endpoint, no order placement path, and
no credential intake. The pipeline writes to the database; this service only reads.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pmvl_shared.config import get_settings
from pmvl_shared.logging_setup import get_logger, setup_logging

from .deps import DISCLAIMER
from .routers import arbitrage, case_study, markets, opportunities, performance, system

log = get_logger(__name__)

setup_logging()
settings = get_settings()

app = FastAPI(
    title="Prediction Market Value Lab API",
    version="0.1.0",
    description=(
        "Read-only research API over Kalshi and Polymarket. "
        f"{DISCLAIMER}"
    ),
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return a generic error body.

    Exception text can contain provider URLs and query parameters; those stay in the
    server log rather than being echoed to a browser.
    """
    log.exception("unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "internal error", "path": request.url.path},
    )


app.include_router(system.router)
app.include_router(opportunities.router)
app.include_router(arbitrage.router)
app.include_router(markets.router)
app.include_router(performance.router)
app.include_router(case_study.router)


@app.get("/", tags=["system"])
def root() -> dict[str, Any]:
    return {
        "name": "Prediction Market Value Lab",
        "version": "0.1.0",
        "mode": "read-only research platform",
        "disclaimer": DISCLAIMER,
        "docs": "/docs",
        "endpoints": [
            "/health", "/system", "/system/config", "/system/eligibility",
            "/methodology", "/opportunities", "/opportunities/summary",
            "/opportunities/watchlist", "/arbitrage", "/markets", "/markets/{id}",
            "/backtest", "/track-record", "/case-study",
        ],
    }
