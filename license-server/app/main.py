from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from . import __version__
from .bootstrap import seed_defaults
from .config import settings
from .db import SessionLocal, init_db
from .routers import admin, auth, billing, heartbeat, public, sandbox
from .ratelimit import install_rate_limits
from .security import init_keys

PANEL_DIR = Path(__file__).resolve().parent / "panel"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_keys()
    seed_defaults()
    yield


app = FastAPI(
    title="X5Agent License Server",
    version=__version__,
    lifespan=lifespan,
)

# Brute-force bound for signup/login (see app/ratelimit.py). Tests disable it.
if settings.rate_limit_enabled:
    install_rate_limits(app)

app.include_router(public.router)
app.include_router(auth.router)
app.include_router(heartbeat.router)
app.include_router(billing.router)
app.include_router(sandbox.router)
app.include_router(admin.router)

# Admin web panel: static assets + one HTML entry point. The panel itself holds
# no secrets; every action goes through the bearer-protected admin API.
app.mount(
    "/admin/assets",
    StaticFiles(directory=str(PANEL_DIR / "assets")),
    name="admin-assets",
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """The central server's own web UI is its admin console."""
    return RedirectResponse(url="/admin", status_code=307)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    # Every browser asks for it; answering here keeps the network clean.
    return Response(status_code=204)


@app.get("/admin", include_in_schema=False)
@app.get("/admin/", include_in_schema=False)
def admin_panel() -> FileResponse:
    return FileResponse(
        PANEL_DIR / "index.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/v1/health")
def health() -> dict:
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="database_unavailable"
        ) from exc
    return {
        "status": "ok",
        "service": "x5agent-license",
        "version": __version__,
        "environment": settings.environment,
    }
