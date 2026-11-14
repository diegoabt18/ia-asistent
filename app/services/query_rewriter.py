import logging
import re
from datetime import date

from app.prompts.research_prompts import format_recent_conversation
from app.services.ollama_service import OllamaService
from app.services.text_analysis import normalize_text

logger = logging.getLogger(__name__)

MIN_QUERY_CHARS = 3
MAX_QUERY_CHARS = 150
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
_PREFIX = re.compile(r"^(?:consulta|búsqueda|busqueda|query)\s*\d*\s*[:：]\s*", re.IGNORECASE)


def rewrite_instructions(max_queries: int, today: date) -> str:
    return (
        f"Hoy es {today.isoformat()}. Eres un generador de consultas para un buscador web. "
        f"Escribe entre 1 y {max_queries} consultas de búsqueda en español, una por línea, "
        "sin numeración, sin comillas y sin explicaciones. "
        "Cada consulta debe entenderse por sí sola: si la pregunta usa referencias como 'esa', 'eso', "
        "'lo' o es una corrección, resuélvelas con la conversación reciente. "
        "Incluye el lugar, la fecha o el periodo cuando la pregunta los implique. "
        "Usa palabras clave, no frases completas."
    )


def parse_queries(text: str, max_queries: int) -> list[str]:
    """Une las líneas del modelo en consultas limpias: sin viñetas, sin duplicados, con tamaño razonable."""
    queries: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = _PREFIX.sub("", _BULLET.sub("", raw)).strip().strip("\"'«»` ")
        line = " ".join(line.split())
        if not (MIN_QUERY_CHARS <= len(line) <= MAX_QUERY_CHARS):
            continue
        key = normalize_text(line)
        if not key or key in seen:
            continue
        seen.add(key)
        queries.append(line)
        if len(queries) >= max_queries:
            break
    return queries


async def rewrite_queries(
    ollama: OllamaService,
    question: str,
    history: list[tuple[str, str]],
    max_queries: int,
    max_history_chars: int,
    today: date,
) -> list[str]:
    """Consultas de búsqueda que escribe el modelo. Lista vacía si no se pudo obtener ninguna."""
    conversation = format_recent_conversation(history, max_history_chars)
    messages = [
        {"role": "system", "content": rewrite_instructions(max_queries, today)},
        {"role": "user", "content": f"{conversation}Pregunta actual: {question}"},
    ]
    completion = await ollama.chat(messages, temperature=0.0, max_tokens=120)
    return parse_queries(completion.text, max_queries)
