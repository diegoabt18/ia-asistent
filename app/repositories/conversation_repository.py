from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.core.database import utcnow
from app.models.conversation import Conversation
from app.models.message import Message


class ConversationRepository:
    """Acceso a conversaciones y mensajes. Cada método abre y cierra su propia sesión."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def exists(self, conversation_id: str) -> bool:
        with self._sf() as s:
            return s.get(Conversation, conversation_id) is not None

    def recent_messages(self, conversation_id: str, limit: int) -> list[Message]:
        """Últimos `limit` mensajes, en orden cronológico."""
        with self._sf() as s:
            rows = s.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id.desc())
                .limit(limit)
            ).all()
        return list(reversed(rows))

    def add_exchange(
        self, conversation_id: str, user_text: str, assistant_text: str, title: str
    ) -> None:
        """Guarda un turno completo (usuario + asistente) en una sola transacción.

        Si la conversación no existe, la crea. Así una conversación nueva solo se
        persiste cuando el modelo respondió correctamente.
        """
        now = utcnow()
        with self._sf() as s:
            conv = s.get(Conversation, conversation_id)
            if conv is None:
                conv = Conversation(
                    id=conversation_id, title=title[:120], created_at=now, updated_at=now
                )
                s.add(conv)
            conv.updated_at = now
            s.add_all(
                [
                    Message(conversation_id=conversation_id, role="user", content=user_text, created_at=now),
                    Message(conversation_id=conversation_id, role="assistant", content=assistant_text, created_at=now),
                ]
            )
            s.commit()

    def list_with_counts(self, limit: int, offset: int) -> list[tuple[Conversation, int]]:
        with self._sf() as s:
            rows = s.execute(
                select(Conversation, func.count(Message.id))
                .outerjoin(Message, Message.conversation_id == Conversation.id)
                .group_by(Conversation.id)
                .order_by(Conversation.updated_at.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        return [(conv, count) for conv, count in rows]

    def get_with_messages(self, conversation_id: str) -> Conversation | None:
        with self._sf() as s:
            return s.scalars(
                select(Conversation)
                .options(selectinload(Conversation.messages))
                .where(Conversation.id == conversation_id)
            ).one_or_none()

    def delete(self, conversation_id: str) -> bool:
        with self._sf() as s:
            conv = s.get(Conversation, conversation_id)
            if conv is None:
                return False
            s.delete(conv)  # los mensajes se borran por cascade="all, delete-orphan"
            s.commit()
            return True
