"""Interpretaciones aprendidas en el banco (fase G).

Puerto de server/src/modules/lumi/learning/domain/knowledge.types.ts y de la selección de
ejemplos de learning.service.ts:
- coincidencia exacta de la frase normalizada → la herramienta aprendida, sin modelo;
- hasta 3 interpretaciones parecidas (Jaccard ≥ 0,34) como ejemplos en el mensaje del selector.

El archivo de interpretaciones (bench/knowledge.json) se escribe SOLO a partir de frases de
entrenamiento. Nunca contiene frases de validación ni de control: medir con ellas sería trampa.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from bench.catalog import INTENTS_BY_NAME

KNOWLEDGE_FILE = Path(__file__).with_name("knowledge.json")
EXAMPLES_LIMIT = 3
EXAMPLE_MIN_SIMILARITY = 0.34

FILLERS = frozenset(
    (
        "por favor porfa porfavor lumi oye hola buenas buenos dias tardes noches me puedes podrias dime decime "
        "muestrame mostrar ensename quiero queria necesito ver saber el la los las un una unos unas de del al y"
    ).split()
)


def plain_text(text: str) -> str:
    """Igual que plainText del Server: minúsculas, sin tildes, solo letras, números, espacios, «:» y «-»."""
    text = unicodedata.normalize("NFD", str(text)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 :-]", " ", text)).strip()


def normalize_phrase(text: str) -> str:
    words = (re.sub(r"^[:-]+|[:-]+$", "", w) for w in plain_text(text).split(" "))
    return " ".join(w for w in words if w and w not in FILLERS)


def phrase_similarity(a: str, b: str) -> float:
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return 0.0
    shared = len(wa & wb)
    return shared / (len(wa) + len(wb) - shared)


@dataclass(frozen=True)
class Interpretation:
    phrase: str
    tool: str
    norm: str


class Knowledge:
    def __init__(self, items: list[Interpretation]):
        self.items = items
        self._exact: dict[str, str] = {}
        for item in items:
            self._exact.setdefault(item.norm, item.tool)

    @classmethod
    def load(cls, path: Path = KNOWLEDGE_FILE) -> "Knowledge":
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = []
        for entry in raw:
            tool = entry["tool"]
            if tool not in INTENTS_BY_NAME:
                raise ValueError(f"herramienta desconocida en {path.name}: {tool}")
            items.append(Interpretation(entry["phrase"], tool, normalize_phrase(entry["phrase"])))
        return cls(items)

    def exact(self, message: str) -> str | None:
        norm = normalize_phrase(message)
        return self._exact.get(norm) if norm else None

    def examples(self, message: str) -> list[dict]:
        norm = normalize_phrase(message)
        if not norm:
            return []
        scored = sorted(
            ((phrase_similarity(norm, i.norm), i) for i in self.items),
            key=lambda pair: pair[0],
            reverse=True,
        )
        out, seen = [], set()
        for score, item in scored:
            if score < EXAMPLE_MIN_SIMILARITY or score >= 1 or item.norm in seen:
                continue
            seen.add(item.norm)
            out.append({"message": item.phrase, "tool": item.tool, "args": {}})
            if len(out) >= EXAMPLES_LIMIT:
                break
        return out
