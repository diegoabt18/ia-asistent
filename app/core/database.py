from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_db(url: str) -> tuple[Engine, sessionmaker]:
    """Crea el motor y la fábrica de sesiones. En SQLite crea la carpeta del archivo si falta."""
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        path = url.removeprefix("sqlite:///")
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False} if is_sqlite else {},
        pool_pre_ping=True,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory
