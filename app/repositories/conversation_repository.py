from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.core.database import utcnow
from app.core.exceptions import ConversationNotFoundError
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.usage_event import UsageEvent
from app.services.ollama_service import ModelUsage


class ConversationRepository:
    """Acceso a conversaciones, mensajes y consumo.

    Todas las operaciones sobre conversaciones reciben `user_ref`: un usuario solo ve,
    continúa o borra sus propias conversaciones. Para los demás, una conversación ajena
    no existe (404), sin revelar que existe.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def exists(self, conversation_id: str, user_ref: str) -> bool:
        with self._sf() as s:
            return s.scalar(self._owned(select(Conversation.id), conversation_id, user_ref)) is not None

    def recent_messages(self, conversation_id: str, user_ref: str, limit: int) -> list[Message]:
        """Últimos `limit` mensajes de una conversación propia, en orden cronológico."""
        with self._sf() as s:
            if s.scalar(self._owned(select(Conversation.id), conversation_id, user_ref)) is None:
                return []
            rows = s.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id.desc())
                .limit(limit)
            ).all()
        return list(reversed(rows))

    def add_exchange(
        self,
        conversation_id: str,
        user_ref: str,
        user_text: str,
        assistant_text: str,
        title: str,
    ) -> None:
        """Guarda un turno completo (usuario + asistente) en una sola transacción.

        Si la conversación no existe, la crea con `user_ref` como dueño. Si existe y es de
        otro usuario, se rechaza como no encontrada. Así una conversación nueva solo se
        persiste cuando el modelo respondió correctamente.
        """
        now = utcnow()
        with self._sf() as s:
            conv = s.get(Conversation, conversation_id)
            if conv is None:
                conv = Conversation(
                    id=conversation_id,
                    user_ref=user_ref,
                    title=title[:120],
                    created_at=now,
                    updated_at=now,
                )
                s.add(conv)
            elif conv.user_ref != user_ref:
                raise ConversationNotFoundError(conversation_id)
            conv.updated_at = now
            s.add_all(
                [
                    Message(conversation_id=conversation_id, role="user", content=user_text, created_at=now),
                    Message(conversation_id=conversation_id, role="assistant", content=assistant_text, created_at=now),
                ]
            )
            s.commit()

    def list_with_counts(
        self, user_ref: str, limit: int, offset: int
    ) -> list[tuple[Conversation, int]]:
        with self._sf() as s:
            rows = s.execute(
                select(Conversation, func.count(Message.id))
                .outerjoin(Message, Message.conversation_id == Conversation.id)
                .where(Conversation.user_ref == user_ref)
                .group_by(Conversation.id)
                .order_by(Conversation.updated_at.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        return [(conv, count) for conv, count in rows]

    def get_with_messages(self, conversation_id: str, user_ref: str) -> Conversation | None:
        with self._sf() as s:
            return s.scalars(
                select(Conversation)
                .options(selectinload(Conversation.messages))
                .where(Conversation.id == conversation_id, Conversation.user_ref == user_ref)
            ).one_or_none()

    def delete(self, conversation_id: str, user_ref: str) -> bool:
        with self._sf() as s:
            conv = s.scalars(
                select(Conversation).where(
                    Conversation.id == conversation_id, Conversation.user_ref == user_ref
                )
            ).one_or_none()
            if conv is None:
                return False
            s.delete(conv)  # los mensajes se borran por cascade="all, delete-orphan"
            s.commit()
            return True

    def record_usage(self, user_ref: str, endpoint: str, model: str, usage: ModelUsage) -> None:
        with self._sf() as s:
            s.add(
                UsageEvent(
                    user_ref=user_ref,
                    endpoint=endpoint,
                    model=model,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    created_at=utcnow(),
                )
            )
            s.commit()

    def usage_totals(self, user_ref: str, since: datetime) -> dict[str, int]:
        with self._sf() as s:
            row = s.execute(
                select(
                    func.count(UsageEvent.id),
                    func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
                    func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
                ).where(UsageEvent.user_ref == user_ref, UsageEvent.created_at >= since)
            ).one()
        return {"requests": int(row[0]), "prompt_tokens": int(row[1]), "completion_tokens": int(row[2])}

    def purge_usage(self, retention_days: int) -> int:
        """Borra registros de consumo más antiguos que `retention_days`. 0 = no borrar."""
        if retention_days <= 0:
            return 0
        cutoff = utcnow() - timedelta(days=retention_days)
        with self._sf() as s:
            result = s.execute(delete(UsageEvent).where(UsageEvent.created_at < cutoff))
            s.commit()
            return int(result.rowcount or 0)

    @staticmethod
    def _owned(query, conversation_id: str, user_ref: str):
        return query.where(Conversation.id == conversation_id, Conversation.user_ref == user_ref)
