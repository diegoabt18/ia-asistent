import asyncio
import time

from app.core.config import Settings
from app.prompts.selector_prompt import (
    build_system_prompt,
    build_user_prompt,
    output_schema,
    parse_output,
)
from app.repositories.conversation_repository import ConversationRepository
from app.schemas.select import SelectIntent, SelectRequest, SelectResponse
from app.services.ollama_service import OllamaService


class SelectorService:
    """Etapa 2 del Intent Engine: el modelo propone herramienta, conversación o capacidad que falta.

    Una sola llamada con salida JSON restringida. No guarda historial. Nunca lanza por una salida
    del modelo mal formada: los problemas vuelven en `errors` y el Server decide.
    """

    def __init__(self, settings: Settings, ollama: OllamaService, repo: ConversationRepository) -> None:
        self._settings = settings
        self._ollama = ollama
        self._repo = repo

    async def select(self, user_ref: str, payload: SelectRequest) -> SelectResponse:
        tools = [t.model_dump() for t in payload.tools]
        names = [t["name"] for t in tools]
        messages = [
            {"role": "system", "content": build_system_prompt(tools, payload.modules)},
            {
                "role": "user",
                "content": build_user_prompt(
                    payload.message, payload.context, [e.model_dump() for e in payload.examples]
                ),
            },
        ]
        started = time.perf_counter()
        completion = await self._ollama.structured(
            messages,
            output_schema(names, payload.modules),
            max_tokens=self._settings.selector_max_tokens,
            num_ctx=self._settings.selector_num_ctx,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        await asyncio.to_thread(
            self._repo.record_usage, user_ref, "select", self._settings.ollama_model, completion.usage
        )
        selection, errors = parse_output(completion.text, names, payload.modules)
        return SelectResponse(
            status=selection["status"],
            intents=[SelectIntent(**i) for i in selection["intents"]],
            need=selection["need"],
            module=selection["module"],
            errors=errors,
            model=self._settings.ollama_model,
            latency_ms=round(latency_ms, 1),
            prompt_tokens=completion.usage.prompt_tokens,
            completion_tokens=completion.usage.completion_tokens,
        )
