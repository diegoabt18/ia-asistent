"""Prompt del selector, lectura de su respuesta y puntuación frente a lo esperado.

Funciones puras: se prueban sin modelo (tests/test_bench.py).
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from bench.catalog import INTENTS_BY_NAME, MAX_INTENTS, catalog_lines, validate_args

SYSTEM_PROMPT = """Eres el clasificador de Lumi, el asistente del panel de administración de una tienda.
No respondas al usuario. Devuelve SOLO un JSON {"intents": [...]} con 0 a 2 intenciones del catálogo.

Reglas:
- Si el mensaje nombra un producto, un aroma o un tipo de producto (vela, difusor, jabón, lavanda,
  coco...), aunque sea una sola palabra o una pregunta como "¿tenemos...?", es products.search.
  Si pregunta cuánto queda o cuántas unidades hay, es products.stock.
- Saludos, conversación, redactar textos, dar ideas o preguntas generales: intents vacío.
- Si pregunta por qué cambian sus ventas o pide compararlas, usa sales.summary igualmente.
- args: solo los del catálogo, con los valores permitidos. Si falta un dato, omite el argumento.
- "ese", "este", "lo", "la": usa la entidad del contexto.

Catálogo:
{catalog}

Ejemplos:
Mensaje: "hola, buenos días"
{"intents":[]}
Mensaje: "¿tienen velas de coco?"
{"intents":[{"tool":"products.search","args":{"query":"velas de coco"}}]}
Mensaje: "eucalipto"
{"intents":[{"tool":"products.search","args":{"query":"eucalipto"}}]}
Mensaje: "cuánto queda del jabón de miel"
{"intents":[{"tool":"products.stock","args":{"query":"jabón de miel"}}]}
Mensaje: "recuérdame el martes a las 4 de la tarde pagar la luz"
{"intents":[{"tool":"reminders.create","args":{"text":"pagar la luz","when":"martes a las 4 de la tarde"}}]}
Mensaje: "¿cuántos pedidos están por despachar?"
{"intents":[{"tool":"orders.count","args":{"status":"paid"}}]}
Mensaje: "en qué estado está la orden"
{"intents":[{"tool":"orders.lookup","args":{}}]}
"""


def system_prompt(today: date | None = None) -> str:
    """Prompt de sistema IDÉNTICO en todas las llamadas (sin fecha ni datos variables), para que
    Ollama pueda reutilizar su caché de prefijo. Las fechas las interpreta el Server, no el modelo."""
    return SYSTEM_PROMPT.replace("{catalog}", "\n".join(catalog_lines()))


def user_prompt(message: str, context: dict | None = None) -> str:
    """Mensaje del usuario con el contexto mínimo del turno anterior (lo que guardará el Server)."""
    parts = []
    if context:
        entity = context.get("last_entity")
        if entity:
            parts.append(f"Contexto: último {entity['type']} mencionado: {entity['name']}")
    parts.append(f'Mensaje: "{message}"')
    return "\n".join(parts)


def build_messages(message: str, context: dict | None, today: date) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt(today)},
        {"role": "user", "content": user_prompt(message, context)},
    ]


# ---------------------------------------------------------------- lectura de la respuesta

@dataclass
class Selection:
    domain: str | None
    intents: list[dict]
    missing: list[str]
    confidence: str | None
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


def parse_selection(raw: str) -> Selection:
    """Interpreta el JSON del modelo. Nunca lanza: los problemas quedan en `errors`."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return Selection(None, [], [], None, ["JSON inválido"])
    if not isinstance(data, dict):
        return Selection(None, [], [], None, ["la raíz no es un objeto"])
    errors = []
    intents_raw = data.get("intents")
    intents: list[dict] = []
    if not isinstance(intents_raw, list):
        errors.append("intents no es una lista")
    else:
        if len(intents_raw) > MAX_INTENTS:
            errors.append(f"más de {MAX_INTENTS} intenciones")
        for item in intents_raw[:MAX_INTENTS]:
            if not isinstance(item, dict) or item.get("tool") not in INTENTS_BY_NAME:
                errors.append(f"intención desconocida: {item!r}"[:120])
                continue
            intents.append({"tool": item["tool"], "args": item.get("args") if isinstance(item.get("args"), dict) else {}})
    # El dominio y los datos que faltan los deduce el Server (bench/rules.py), no el modelo.
    return Selection(None, intents, [], None, errors)


# ---------------------------------------------------------------- puntuación

def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 :#-]", " ", text.lower())).strip()


def value_matches(expected: object, actual: object) -> bool:
    """Texto: todas las palabras esperadas aparecen en lo devuelto. Resto: igualdad exacta."""
    if isinstance(expected, str) and isinstance(actual, str):
        want = set(normalize(expected).split())
        got = set(normalize(actual).split())
        return bool(want) and want <= got
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    return expected == actual


@dataclass
class Score:
    json_valid: bool
    domain_ok: bool
    intent_ok: bool
    args_ok: bool
    missing_ok: bool
    notes: list[str]

    @property
    def correct(self) -> bool:
        return self.json_valid and self.intent_ok and self.args_ok and self.missing_ok


def score_case(expected: dict, selection: Selection) -> Score:
    notes = list(selection.errors)
    domains = expected["domain"] if isinstance(expected["domain"], list) else [expected["domain"]]
    domain_ok = selection.domain in domains

    want_tools = sorted(i["tool"] for i in expected.get("intents", []))
    got_tools = sorted(i["tool"] for i in selection.intents)
    intent_ok = want_tools == got_tools
    if not intent_ok:
        notes.append(f"intenciones {got_tools} ≠ {want_tools}")

    expected_missing = [normalize(m) for m in expected.get("missing", [])]
    args_ok = intent_ok
    if intent_ok:
        for want in expected.get("intents", []):
            got = next(i for i in selection.intents if i["tool"] == want["tool"])
            for key, value in want.get("args", {}).items():
                if not value_matches(value, got["args"].get(key)):
                    args_ok = False
                    notes.append(f"{want['tool']}.{key}={got['args'].get(key)!r} ≠ {value!r}")
            # Lo que el Server rechazaría: argumentos fuera del catálogo o mal tipados. Los
            # obligatorios que faltan solo cuentan si no se esperaba que faltaran.
            for error in validate_args(want["tool"], got["args"]):
                if error.startswith("falta ") and normalize(error[6:]) in expected_missing:
                    continue
                args_ok = False
                notes.append(f"{want['tool']}: {error}")

    got_missing = {normalize(m) for m in selection.missing}
    if expected_missing:
        missing_ok = set(expected_missing) <= got_missing
    else:
        # Un "falta" inventado haría que Lumi preguntara algo innecesario.
        missing_ok = not got_missing
    if not missing_ok:
        notes.append(f"missing {sorted(got_missing)} ≠ {sorted(expected_missing)}")

    return Score(selection.valid, domain_ok, intent_ok, args_ok, missing_ok, notes)
