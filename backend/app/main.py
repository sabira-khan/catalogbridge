from __future__ import annotations

from fastapi import FastAPI

from app import __version__
from app.api.routes_health import router as health_router
from app.config import get_settings
from app.logging import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    application = FastAPI(title="CatalogBridge", version=__version__)
    application.include_router(health_router)
    return application


app = create_app()
