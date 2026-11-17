import json
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, utcnow
from app.models.conversation import Conversation


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    # Referencias estructuradas del mensaje (JSON): fuentes web o registros del panel. Null si no tiene.
    references_json: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    @property
    def references(self) -> list[dict]:
        """Referencias guardadas. Un valor ilegible se trata como ausente: el mensaje se muestra igual."""
        if not self.references_json:
            return []
        try:
            value = json.loads(self.references_json)
        except ValueError:
            return []
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
