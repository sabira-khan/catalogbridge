from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app import __version__
from app.config import Settings, get_settings

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str
    llm_model: str
    embed_model: str
    llm_configured: bool


@router.get("/health")
def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=__version__,
        llm_model=settings.llm_model,
        embed_model=settings.embed_model,
        llm_configured=settings.llm_configured,
    )
