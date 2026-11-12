from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
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


def init_schema(engine: Engine) -> None:
    """Crea las tablas que falten y añade las columnas nuevas a bases ya existentes.

    `create_all` no altera tablas existentes, así que la columna `user_ref` de
    `conversations` se añade aquí si la base es anterior a la identidad por usuario.
    """
    # Importa los modelos para que estén registrados en Base.metadata.
    import app.models  # noqa: F401

    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    if "conversations" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("conversations")}
    if "user_ref" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE conversations ADD COLUMN user_ref VARCHAR(64)"))
    index_names = {i["name"] for i in inspector.get_indexes("conversations")}
    if "ix_conversations_user_ref" not in index_names:
        with engine.begin() as conn:
            conn.execute(
                text("CREATE INDEX ix_conversations_user_ref ON conversations (user_ref)")
            )
