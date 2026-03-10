"""SQLAlchemy engine and session helpers."""

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


def _ensure_data_dir() -> None:
    """Create the data directory if needed (SQLite file path)."""
    url = settings.database_url
    if url.startswith("sqlite:///"):
        db_path = url.removeprefix("sqlite:///")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)


_ensure_data_dir()

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},  # needed for SQLite + FastAPI
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


def get_db():
    """FastAPI dependency that yields a DB session and ensures cleanup."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables (call once on startup)."""
    # Import models so they register on Base.metadata
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
