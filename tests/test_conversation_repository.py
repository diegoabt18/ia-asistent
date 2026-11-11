import uuid

import pytest

import app.models  # noqa: F401
from app.core.database import Base, create_db
from app.repositories.conversation_repository import ConversationRepository


@pytest.fixture
def repo(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    engine, factory = create_db(url)
    Base.metadata.create_all(engine)
    yield ConversationRepository(factory)
    engine.dispose()


def test_add_exchange_creates_conversation_with_two_messages(repo) -> None:
    conv_id = str(uuid.uuid4())
    repo.add_exchange(conv_id, "hola", "¡hola!", "hola")
    assert repo.exists(conv_id)
    messages = repo.recent_messages(conv_id, limit=10)
    assert [(m.role, m.content) for m in messages] == [("user", "hola"), ("assistant", "¡hola!")]


def test_recent_messages_returns_latest_in_chronological_order(repo) -> None:
    conv_id = str(uuid.uuid4())
    repo.add_exchange(conv_id, "1", "r1", "t")
    repo.add_exchange(conv_id, "2", "r2", "t")
    messages = repo.recent_messages(conv_id, limit=2)
    assert [m.content for m in messages] == ["2", "r2"]


def test_delete_removes_conversation_and_its_messages(repo) -> None:
    conv_id = str(uuid.uuid4())
    repo.add_exchange(conv_id, "hola", "¡hola!", "hola")
    assert repo.delete(conv_id) is True
    assert repo.exists(conv_id) is False
    assert repo.recent_messages(conv_id, limit=10) == []
    assert repo.delete(conv_id) is False


def test_list_with_counts_reports_message_count(repo) -> None:
    conv_id = str(uuid.uuid4())
    repo.add_exchange(conv_id, "hola", "¡hola!", "hola")
    rows = repo.list_with_counts(limit=10, offset=0)
    assert len(rows) == 1
    conv, count = rows[0]
    assert conv.id == conv_id
    assert count == 2
