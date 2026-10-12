from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlmodel import JSON, Column, Field, Session, SQLModel, create_engine, select

DEFAULT_DB_PATH = Path("catalogbridge.db")


class JobRow(SQLModel, table=True):
    __tablename__ = "jobs"

    id: str = Field(primary_key=True)
    source: str
    platform: str
    target_id: str
    status: str = "pending"
    store_name: str | None = None
    currency: str | None = None
    product_count: int = 0
    needs_review_count: int = 0
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MappedProductRow(SQLModel, table=True):
    __tablename__ = "mapped_products"

    id: str = Field(primary_key=True)
    job_id: str = Field(index=True)
    source_id: str
    title: str
    target_category_id: str | None = None
    category_confidence: float = 0.0
    needs_review: bool = True
    reviewed: bool = False
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class CorrectionRow(SQLModel, table=True):
    __tablename__ = "corrections"

    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(index=True)
    product_id: str = Field(index=True)
    source_id: str
    title: str
    from_category_id: str | None = None
    to_category_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    note: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


_engine: Engine | None = None


def build_engine(path: Path | str = DEFAULT_DB_PATH, *, echo: bool = False) -> Engine:
    url = path if str(path).startswith("sqlite") else f"sqlite:///{path}"
    engine = create_engine(
        str(url), echo=echo, connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    return engine


def set_engine(engine: Engine) -> None:
    global _engine
    _engine = engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session


def touch(row: JobRow) -> JobRow:
    row.updated_at = datetime.now(UTC)
    return row


__all__ = [
    "CorrectionRow",
    "JobRow",
    "MappedProductRow",
    "Session",
    "build_engine",
    "get_engine",
    "get_session",
    "select",
    "set_engine",
    "touch",
]
