import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

import app.models  # noqa: F401
from app.core.database import Base, create_db, init_schema, utcnow
from app.core.exceptions import ConversationNotFoundError
from app.models.usage_event import UsageEvent
from app.repositories.conversation_repository import ConversationRepository
from app.services.ollama_service import ModelUsage

ALICE = "user-alice"
BOB = "user-bob"


@pytest.fixture
def repo(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    engine, factory = create_db(url)
    init_schema(engine)
    yield ConversationRepository(factory)
    engine.dispose()


def test_add_exchange_creates_conversation_with_two_messages(repo) -> None:
    conv_id = str(uuid.uuid4())
    repo.add_exchange(conv_id, ALICE, "hola", "¡hola!", "hola")
    assert repo.exists(conv_id, ALICE)
    messages = repo.recent_messages(conv_id, ALICE, limit=10)
    assert [(m.role, m.content) for m in messages] == [("user", "hola"), ("assistant", "¡hola!")]


def test_recent_messages_returns_latest_in_chronological_order(repo) -> None:
    conv_id = str(uuid.uuid4())
    repo.add_exchange(conv_id, ALICE, "1", "r1", "t")
    repo.add_exchange(conv_id, ALICE, "2", "r2", "t")
    messages = repo.recent_messages(conv_id, ALICE, limit=2)
    assert [m.content for m in messages] == ["2", "r2"]


def test_delete_removes_conversation_and_its_messages(repo) -> None:
    conv_id = str(uuid.uuid4())
    repo.add_exchange(conv_id, ALICE, "hola", "¡hola!", "hola")
    assert repo.delete(conv_id, ALICE) is True
    assert repo.exists(conv_id, ALICE) is False
    assert repo.recent_messages(conv_id, ALICE, limit=10) == []
    assert repo.delete(conv_id, ALICE) is False


def test_list_with_counts_reports_message_count(repo) -> None:
    conv_id = str(uuid.uuid4())
    repo.add_exchange(conv_id, ALICE, "hola", "¡hola!", "hola")
    rows = repo.list_with_counts(ALICE, limit=10, offset=0)
    assert len(rows) == 1
    conv, count = rows[0]
    assert conv.id == conv_id
    assert count == 2


def test_other_user_cannot_see_read_continue_or_delete_a_conversation(repo) -> None:
    conv_id = str(uuid.uuid4())
    repo.add_exchange(conv_id, ALICE, "secreto", "respuesta", "secreto")

    assert repo.exists(conv_id, BOB) is False
    assert repo.recent_messages(conv_id, BOB, limit=10) == []
    assert repo.get_with_messages(conv_id, BOB) is None
    assert repo.list_with_counts(BOB, limit=10, offset=0) == []
    assert repo.delete(conv_id, BOB) is False
    # Y no puede continuarla escribiendo en ella.
    with pytest.raises(ConversationNotFoundError):
        repo.add_exchange(conv_id, BOB, "intruso", "x", "t")

    # El dueño sigue teniendo su conversación intacta.
    assert repo.exists(conv_id, ALICE)
    assert len(repo.recent_messages(conv_id, ALICE, limit=10)) == 2


def test_conversations_without_owner_are_not_accessible(repo) -> None:
    conv_id = str(uuid.uuid4())
    repo.add_exchange(conv_id, ALICE, "hola", "¡hola!", "hola")
    # Simula una conversación heredada de antes de la identidad por usuario.
    with repo._sf() as s:
        s.execute(text("UPDATE conversations SET user_ref = NULL WHERE id = :id"), {"id": conv_id})
        s.commit()
    assert repo.exists(conv_id, ALICE) is False
    assert repo.list_with_counts(ALICE, limit=10, offset=0) == []


def test_usage_totals_are_per_user_and_within_window(repo) -> None:
    repo.record_usage(ALICE, "chat", "qwen", ModelUsage(prompt_tokens=100, completion_tokens=20))
    repo.record_usage(ALICE, "research", "qwen", ModelUsage(prompt_tokens=50, completion_tokens=10))
    repo.record_usage(BOB, "chat", "qwen", ModelUsage(prompt_tokens=999, completion_tokens=999))

    since = utcnow() - timedelta(days=1)
    assert repo.usage_totals(ALICE, since) == {
        "requests": 2,
        "prompt_tokens": 150,
        "completion_tokens": 30,
    }
    assert repo.usage_totals(ALICE, utcnow() + timedelta(days=1)) == {
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


def test_purge_usage_removes_only_records_older_than_retention(repo) -> None:
    repo.record_usage(ALICE, "chat", "qwen", ModelUsage(prompt_tokens=1, completion_tokens=1))
    with repo._sf() as s:
        old = UsageEvent(
            user_ref=ALICE,
            endpoint="chat",
            model="qwen",
            prompt_tokens=1,
            completion_tokens=1,
            created_at=utcnow() - timedelta(days=200),
        )
        s.add(old)
        s.commit()

    assert repo.purge_usage(retention_days=90) == 1
    assert repo.usage_totals(ALICE, utcnow() - timedelta(days=365))["requests"] == 1
    assert repo.purge_usage(retention_days=0) == 0


def test_init_schema_migrates_a_database_created_before_user_ref(tmp_path) -> None:
    url = f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    engine, factory = create_db(url)
    # Esquema anterior: conversations sin user_ref.
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE conversations (id VARCHAR(36) PRIMARY KEY, title VARCHAR(120) NOT NULL,"
                " created_at DATETIME, updated_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, title, created_at, updated_at)"
                " VALUES ('legacy-1', 'antigua', '2026-01-01', '2026-01-01')"
            )
        )

    init_schema(engine)
    init_schema(engine)  # idempotente

    repo = ConversationRepository(factory)
    # La conversación heredada queda sin dueño y no es accesible para nadie.
    assert repo.exists("legacy-1", ALICE) is False
    assert repo.list_with_counts(ALICE, limit=10, offset=0) == []
    engine.dispose()
