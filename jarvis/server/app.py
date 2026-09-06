"""
server/app.py
-------------
FastAPI app factory for the JARVIS V2 Dell server.

Phase 0 endpoints are intentionally thin — they lock down the infrastructure
contract (config, logging, database, honest status codes) that every later
phase builds on:

    GET /healthz   liveness  — the process is up (always 200)
    GET /readyz    readiness — can we actually serve (db probe, 503 if not)
    GET /          service identity + phase map
    GET /docs      FastAPI's interactive API documentation

The factory pattern (`create_app(config=None)`) keeps the app testable: tests
pass their own ServerConfig pointing at temp directories instead of reaching
into module state.  Nothing here imports the legacy voice runtime.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from . import __version__
from .config import load_config
from .db import db_ok, ensure_db
from .logging_setup import get_logger, setup_logging


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _identity_payload(cfg) -> dict:
    return {
        "service": "jarvis-server",
        "version": __version__,
        "node": cfg.node_name,
        "role": cfg.role,
        "env": cfg.env,
        "phase": "0-infrastructure",
        "docs": "/docs",
        "healthz": "/healthz",
        "readyz": "/readyz",
    }


def create_app(config=None):
    """Build the FastAPI application. `config` may be a ServerConfig or None
    (None -> load_config() from the environment)."""
    # Imported lazily so the rest of the package (config/db/logging) stays
    # importable and testable on machines without FastAPI installed.
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    cfg = config if config is not None else load_config()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logger = setup_logging(cfg)
        ensure_db(cfg)
        logger.info(
            "startup complete: %s listening on http://%s:%d (db=%s)",
            cfg.label, cfg.host, cfg.port, cfg.db_path,
        )
        yield
        logger.info("shutdown complete: %s", cfg.label)

    app = FastAPI(
        title="JARVIS V2 Server",
        description="Always-on JARVIS server (Dell). Phase 0: infrastructure.",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.config = cfg

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def access_log(request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        get_logger("http").info(
            "%s %s -> %d (%.1f ms)",
            request.method, request.url.path, response.status_code, elapsed_ms,
        )
        return response

    @app.get("/", tags=["meta"])
    def index():
        return _identity_payload(cfg)

    @app.get("/healthz", tags=["meta"])
    def healthz():
        return {
            "status": "ok",
            "service": "jarvis-server",
            "version": __version__,
            "node": cfg.node_name,
            "ts": now_iso(),
        }

    @app.get("/readyz", tags=["meta"])
    def readyz():
        database = db_ok(cfg)
        payload = {
            "status": "ready" if database else "degraded",
            "checks": {"database": "ok" if database else "error"},
            "ts": now_iso(),
        }
        return JSONResponse(payload, status_code=200 if database else 503)

    return app
