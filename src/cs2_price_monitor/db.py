from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from cs2_price_monitor.config import load_settings
from cs2_price_monitor.models import Base


def build_engine(database_url: str | None = None) -> Engine:
    """Build SQLAlchemy engine."""
    url = database_url or load_settings().database.url
    if url.startswith("sqlite:///"):
        db_path = Path(url.replace("sqlite:///", "", 1))
        if db_path.parent != Path("."):
            db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, future=True)


def build_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Build session factory."""
    actual_engine = engine or build_engine()
    return sessionmaker(bind=actual_engine, expire_on_commit=False, future=True)


def init_db(engine: Engine | None = None) -> None:
    """Create database tables."""
    actual_engine = engine or build_engine()
    Base.metadata.create_all(actual_engine)
