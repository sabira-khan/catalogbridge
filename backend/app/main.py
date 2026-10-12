from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes_health import router as health_router
from app.api.routes_review import router as review_router
from app.config import get_settings
from app.db import build_engine, set_engine
from app.logging import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    set_engine(build_engine(settings.database_path))
    application = FastAPI(title="CatalogBridge", version=__version__)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    application.include_router(health_router)
    application.include_router(review_router)
    return application


app = create_app()
