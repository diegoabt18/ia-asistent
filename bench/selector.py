"""Prompt del selector, lectura de su respuesta y puntuación frente a lo esperado.

Funciones puras: se prueban sin modelo (tests/test_bench.py).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from app.prompts.selector_prompt import build_system_prompt, build_user_prompt, parse_output
from bench.catalog import INTENTS_BY_NAME, MODULES, tool_specs, validate_args

def system_prompt(today: date | None = None) -> str:
    """Prompt de producción (app/prompts/selector_prompt.py) con el catálogo del banco. Es estático:
    sin fecha ni datos variables. Las fechas las interpreta el Server, no el modelo."""
    return build_system_prompt(tool_specs(), MODULES)


def context_line(context: dict | None) -> str | None:
    """La línea de contexto que redacta el Server con la entidad activa."""
    entity = (context or {}).get("last_entity")
    return f"último {entity['type']} mencionado: {entity['name']}" if entity else None


def user_prompt(message: str, context: dict | None = None) -> str:
    return build_user_prompt(message, context_line(context))


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
    # tool | chat | missing | ambiguous. Lo que propone el modelo; el Server fija el definitivo.
    status: str = "chat"
    need: str = ""
    module: str = "none"

    @property
    def valid(self) -> bool:
        return not self.errors


def parse_selection(raw: str) -> Selection:
    """Interpreta el JSON del modelo con el lector de producción. Nunca lanza: los problemas quedan
    en `errors`. El dominio y los datos que faltan los deduce el Server (bench/rules.py)."""
    selection, errors = parse_output(raw, INTENTS_BY_NAME, MODULES)
    return Selection(None, selection["intents"], [], None, errors, selection["status"], selection["need"], selection["module"])


# ---------------------------------------------------------------- puntuación

def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 :#-]", " ", text.lower())).strip()


def same_word(a: str, b: str) -> bool:
    """Misma palabra o la misma raíz ("revisa"/"revisar", "vela"/"velas", "despachar"/"despacharlos").
    Las palabras cortas y los números tienen que coincidir exactamente."""
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4:
        return False
    stem = min(5, len(a), len(b))
    return a[:stem] == b[:stem]


def value_matches(expected: object, actual: object) -> bool:
    """Texto: todas las palabras esperadas aparecen (o su raíz) en lo devuelto. Resto: igualdad exacta."""
    if isinstance(expected, str) and isinstance(actual, str):
        want = set(normalize(expected).split())
        got = set(normalize(actual).split())
        return bool(want) and all(any(same_word(w, g) for g in got) for w in want)
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
    # Solo cuenta si el caso indica el estado esperado (chat, missing…).
    status_ok: bool = True

    @property
    def correct(self) -> bool:
        return self.json_valid and self.intent_ok and self.args_ok and self.missing_ok and self.status_ok


def score_case(expected: dict, selection: Selection) -> Score:
    notes = list(selection.errors)
    status_ok = "status" not in expected or selection.status == expected["status"]
    if not status_ok:
        notes.append(f"estado {selection.status!r} ≠ {expected['status']!r}")
    domains = expected["domain"] if isinstance(expected["domain"], list) else [expected["domain"]]
    domain_ok = selection.domain in domains

    want_tools = sorted(i["tool"] for i in expected.get("intents", []))
    got_tools = sorted(i["tool"] for i in selection.intents)
    intent_ok = want_tools == got_tools
    # Frases ambiguas: otras combinaciones de herramientas también razonables (sus argumentos
    # solo se validan contra el catálogo).
    alternative = next((sorted(alt) for alt in expected.get("accept", []) if sorted(alt) == got_tools), None)
    if not intent_ok and alternative is not None:
        errors = [e for i in selection.intents for e in validate_args(i["tool"], i["args"]) if not e.startswith("falta ")]
        notes.extend(errors)
        return Score(selection.valid, domain_ok, True, not errors, True, notes, status_ok)
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

    return Score(selection.valid, domain_ok, intent_ok, args_ok, missing_ok, notes, status_ok)
