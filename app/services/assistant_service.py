import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date

from app.core.config import Settings
from app.core.exceptions import ConversationNotFoundError, UpstreamServiceError
from app.prompts.research_prompts import format_sources, research_system_prompt
from app.prompts.system_prompts import SYSTEM_PROMPT
from app.repositories.conversation_repository import ConversationRepository
from app.schemas.research import ResearchSource
from app.services.ollama_service import OllamaService
from app.services.research_service import ResearchService, to_sources
from app.services.verification_service import VerificationResult, to_schema
from app.services.web_extraction_service import PageDocument


def title_from(message: str) -> str:
    text = " ".join(message.split())
    return text[:60] or "Nueva conversación"


def build_messages(
    history: list[tuple[str, str]],
    user_message: str,
    max_chars: int,
    system_prompt: str = SYSTEM_PROMPT,
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
        {"role": "system", "content": system_prompt},
        *kept,
        {"role": "user", "content": user_message},
    ]


@dataclass(frozen=True)
class ChatResult:
    conversation_id: str
    text: str
    sources: list[ResearchSource]
    verification: VerificationResult | None = None


class AssistantService:
    """Coordina historial, investigación opcional, verificación y modelo.

    Un turno solo se guarda cuando el modelo respondió correctamente.
    """

    def __init__(
        self,
        settings: Settings,
        ollama: OllamaService,
        repo: ConversationRepository,
        research: ResearchService,
    ) -> None:
        self._settings = settings
        self._ollama = ollama
        self._repo = repo
        self._research = research

    @property
    def model(self) -> str:
        return self._settings.ollama_model

    async def validate_conversation(self, conversation_id: str) -> None:
        exists = await asyncio.to_thread(self._repo.exists, conversation_id)
        if not exists:
            raise ConversationNotFoundError(conversation_id)

    async def _prepare(
        self, conversation_id: str | None, message: str, use_web: bool
    ) -> tuple[list[dict[str, str]], list[PageDocument]]:
        documents: list[PageDocument] = []
        system_prompt = SYSTEM_PROMPT
        user_content = message
        if use_web:
            documents = (await self._research.gather(message)).documents
            system_prompt = research_system_prompt(date.today())
            prompt_docs = self._research.context_for(message, documents)
            sources = format_sources(prompt_docs) or "(ninguna fuente pudo leerse)"
            user_content = f"Fuentes:\n{sources}\n\nPregunta: {message}"

        history: list[tuple[str, str]] = []
        if conversation_id is not None:
            rows = await asyncio.to_thread(
                self._repo.recent_messages, conversation_id, self._settings.chat_history_messages
            )
            history = [(m.role, m.content) for m in rows]

        # El presupuesto de historial es el mismo aunque el mensaje incluya fuentes.
        budget = self._settings.chat_max_context_chars + len(user_content)
        messages = build_messages(history, user_content, budget, system_prompt)
        return messages, documents

    async def reply(
        self,
        message: str,
        conversation_id: str | None,
        temperature: float,
        max_tokens: int,
        use_web: bool = False,
    ) -> ChatResult:
        if conversation_id is not None:
            await self.validate_conversation(conversation_id)
        conv_id = conversation_id or str(uuid.uuid4())
        messages, documents = await self._prepare(conversation_id, message, use_web)

        raw = await self._ollama.chat(messages, temperature, max_tokens)
        if use_web:
            verification = await self._research.verify_limited(raw, documents)
            text = verification.answer
            sources = to_sources(documents, verification.valid_source_indices)
        else:
            verification, text, sources = None, raw, []

        await asyncio.to_thread(
            self._repo.add_exchange, conv_id, message, text, title_from(message)
        )
        return ChatResult(conversation_id=conv_id, text=text, sources=sources, verification=verification)

    async def stream_reply(
        self,
        message: str,
        conversation_id: str | None,
        temperature: float,
        max_tokens: int,
        use_web: bool = False,
    ) -> AsyncIterator[tuple[str, dict]]:
        """Emite eventos (evento, datos). Si el cliente se desconecta, no se guarda el turno.

        Nota: los tokens se envían tal cual los genera el modelo. La verificación se calcula
        al final; el texto guardado y el evento `done` llevan las citas ya validadas.
        """
        conv_id = conversation_id or str(uuid.uuid4())
        yield "start", {"conversation_id": conv_id}

        parts: list[str] = []
        documents: list[PageDocument] = []
        try:
            messages, documents = await self._prepare(conversation_id, message, use_web)
            if use_web:
                yield "sources", {"sources": [s.model_dump() for s in to_sources(documents, [])]}
            async for piece in self._ollama.chat_stream(messages, temperature, max_tokens):
                parts.append(piece)
                yield "token", {"text": piece}
        except UpstreamServiceError as exc:
            yield "error", {"detail": f"Error en el servicio {exc.service}"}
            return

        raw = "".join(parts)
        if use_web:
            verification = await self._research.verify_limited(raw, documents)
            text = verification.answer
            sources = to_sources(documents, verification.valid_source_indices)
        else:
            verification, text, sources = None, raw, []

        await asyncio.to_thread(
            self._repo.add_exchange, conv_id, message, text, title_from(message)
        )
        done: dict = {
            "conversation_id": conv_id,
            "sources": [s.model_dump() for s in sources],
        }
        if verification is not None:
            done["verification"] = to_schema(verification).model_dump()
        yield "done", done
