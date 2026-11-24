"""API gateway. Engines are auto-mounted from app.engines.<key>.api:router if present, so each engine owner
only touches their own package. Missing engines are reported honestly by /capabilities."""
from __future__ import annotations

import importlib
import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.platform import router as platform_router
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.db.base import create_all, import_all_models
from app.engines import ENGINE_KEYS

log = logging.getLogger("aether")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


def create_app() -> FastAPI:
    st = get_settings()
    app = FastAPI(title="AETHER 2.0 API (synthetic data only)", version="2.0.0-dev", openapi_url="/api/v1/openapi.json",
                  docs_url="/api/v1/docs")
    install_error_handlers(app)
    app.add_middleware(CORSMiddleware, allow_origins=[o for o in st.cors_origins.split(",") if o], allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["content-type", "x-csrf-token", "idempotency-key", "x-correlation-id"])

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        cid = request.headers.get("x-correlation-id") or str(uuid.uuid4())
        request.state.correlation_id = cid[:64]
        resp = await call_next(request)
        resp.headers["X-Correlation-ID"] = request.state.correlation_id
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    import_all_models()
    create_all()
    app.include_router(platform_router, prefix="/api/v1")
    mounted: dict[str, bool] = {}
    for key in ENGINE_KEYS:
        try:
            importlib.import_module(f"app.engines.{key}")  # registers services
            mod = importlib.import_module(f"app.engines.{key}.api")
            app.include_router(mod.router, prefix=f"/api/v1/{key}", tags=[key])
            mounted[key] = True
        except ModuleNotFoundError as exc:
            if exc.name not in (f"app.engines.{key}", f"app.engines.{key}.api"):
                raise  # a real import error inside an engine must not be hidden
            mounted[key] = False
    app.state.engines_mounted = mounted
    log.info("engines mounted: %s", mounted)
    return app


app = create_app()
