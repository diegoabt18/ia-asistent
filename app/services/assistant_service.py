import asyncio
import uuid
from collections.abc import AsyncIterator

from app.core.config import Settings
from app.core.exceptions import ConversationNotFoundError, UpstreamServiceError
from app.prompts.system_prompts import SYSTEM_PROMPT
from app.repositories.conversation_repository import ConversationRepository
from app.services.ollama_service import OllamaService


def title_from(message: str) -> str:
    text = " ".join(message.split())
    return text[:60] or "Nueva conversación"


def build_messages(
    history: list[tuple[str, str]], user_message: str, max_chars: int
) -> list[dict[str, str]]:
    """Construye el prompt: sistema + historial reciente que quepa en `max_chars` + mensaje actual.

    El historial más antiguo se descarta primero. Función pura para poder probarla sin Ollama.
    """
    kept: list[dict[str, str]] = []
    total = len(user_message)
    for role, content in reversed(history):
        if total + len(content) > max_chars:
            break
        kept.append({"role": role, "content": content})
        total += len(content)
    kept.reverse()
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *kept,
        {"role": "user", "content": user_message},
    ]


class AssistantService:
    """Coordina historial, contexto y modelo. La conversación nueva se guarda solo si el modelo respondió."""

    def __init__(
        self,
        settings: Settings,
        ollama: OllamaService,
        repo: ConversationRepository,
    ) -> None:
        self._settings = settings
        self._ollama = ollama
        self._repo = repo

    @property
    def model(self) -> str:
        return self._settings.ollama_model

    async def validate_conversation(self, conversation_id: str) -> None:
        exists = await asyncio.to_thread(self._repo.exists, conversation_id)
        if not exists:
            raise ConversationNotFoundError(conversation_id)

    async def _build_prompt(self, conversation_id: str, message: str) -> list[dict[str, str]]:
        rows = await asyncio.to_thread(
            self._repo.recent_messages, conversation_id, self._settings.chat_history_messages
        )
        history = [(m.role, m.content) for m in rows]
        return build_messages(history, message, self._settings.chat_max_context_chars)

    async def reply(
        self,
        message: str,
        conversation_id: str | None,
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, str]:
        if conversation_id is not None:
            await self.validate_conversation(conversation_id)
        conv_id = conversation_id or str(uuid.uuid4())
        messages = await self._build_prompt(conv_id, message) if conversation_id else build_messages(
            [], message, self._settings.chat_max_context_chars
        )
        text = await self._ollama.chat(messages, temperature, max_tokens)
        await asyncio.to_thread(
            self._repo.add_exchange, conv_id, message, text, title_from(message)
        )
        return conv_id, text

    async def stream_reply(
        self,
        message: str,
        conversation_id: str | None,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[tuple[str, dict]]:
        """Emite eventos (evento, datos). Si el cliente se desconecta, no se guarda el turno."""
        conv_id = conversation_id or str(uuid.uuid4())
        messages = await self._build_prompt(conv_id, message) if conversation_id else build_messages(
            [], message, self._settings.chat_max_context_chars
        )
        yield "start", {"conversation_id": conv_id}

        parts: list[str] = []
        try:
            async for piece in self._ollama.chat_stream(messages, temperature, max_tokens):
                parts.append(piece)
                yield "token", {"text": piece}
        except UpstreamServiceError as exc:
            yield "error", {"detail": f"Error en el servicio {exc.service}"}
            return

        await asyncio.to_thread(
            self._repo.add_exchange, conv_id, message, "".join(parts), title_from(message)
        )
        yield "done", {"conversation_id": conv_id}
