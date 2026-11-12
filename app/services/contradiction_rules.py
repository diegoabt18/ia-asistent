"""Reglas de contradicción entre una afirmación y una frase de la fuente.

Principio: dos cifras distintas NO son una contradicción por sí solas. Un valor solo se compara
cuando coinciden indicador, período, región y unidad. Si no se puede determinar, se registra
como discrepancia, nunca como contradicción.
"""

import math
import re
from dataclasses import dataclass

from app.services.text_analysis import content_tokens, normalize_text

INDICATOR_SIMILARITY = 0.5
YEAR = re.compile(r"\b(1[89]\d{2}|2[01]\d{2})\b")
QUANTITY = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)*)(\s?%|\s?por ciento)?", re.IGNORECASE)
CAPITALIZED_NON_REGIONS = frozenset(
    {"el", "la", "los", "las", "un", "una", "en", "segun", "pero", "tras", "sin", "con", "por", "para", "del", "de"}
)

# Patrones sobre texto normalizado (minúsculas, sin acentos). "(?<!no )" evita contar
# "no hay" o "no aumentó" como su afirmación contraria.
INCREASE = re.compile(r"\b(?<!no )(aument\w*|sub(?:e|io|ieron|en)|crec(?:e|io|ieron|en)|increment\w*|alz(?:a|o|aron))\b")
DECREASE = re.compile(r"\b(?<!no )(disminu\w*|baj(?:a|aron|an)|cay(?:o|eron|e)|reduc\w*|reduj\w*|descend\w*)\b")
TRUE = re.compile(r"\b(?<!no )(verdader\w*|cierto|cierta|ciertos|ciertas|correct[oa]s?)\b")
FALSE = re.compile(r"\b(falso|falsa|falsos|falsas|incorrect[oa]s?|erroneo|erronea)\b")
EXISTENCE = re.compile(r"\b(?<!no )(existe|existen|existio|existia|hay)\b")
ABSENCE = re.compile(r"\b(no (?:existe|existen|hay|existia)|inexistente|inexistentes|ausencia|carece|ausente)\b")
GREATER = re.compile(r"\b(?<!no )(mayor|mayores|superior|superiores|supera|supero|excede|excedio|excedieron)\b")
LESSER = re.compile(r"\b(?<!no )(menor|menores|inferior|inferiores|por debajo)\b")
POSITIVE = re.compile(r"\bpositiv[oa]s?\b")
NEGATIVE = re.compile(r"\bnegativ[oa]s?\b")

OPPOSITE_PAIRS = (
    ("increase_decrease", INCREASE, DECREASE),
    ("true_false", TRUE, FALSE),
    ("existence_absence", EXISTENCE, ABSENCE),
    ("greater_lesser", GREATER, LESSER),
    ("positive_negative", POSITIVE, NEGATIVE),
)


@dataclass(frozen=True)
class RuleFinding:
    kind: str
    detail: str


@dataclass(frozen=True)
class Quantity:
    value: float
    unit: str  # "%" o ""
    raw: str


@dataclass(frozen=True)
class SentenceContext:
    indicator: frozenset[str]
    periods: frozenset[str]
    regions: frozenset[str]
    quantities: tuple[Quantity, ...]


def _parse_number(raw: str) -> float | None:
    text = raw
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", text):  # "1.200" es miles, no decimales
        text = text.replace(".", "")
    text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _regions(sentence: str) -> frozenset[str]:
    """Nombres propios aproximados: palabras con mayúscula que no están al inicio de la frase."""
    words = re.findall(r"[^\W\d_]+", sentence)
    regions = set()
    for position, word in enumerate(words):
        normalized = normalize_text(word)
        if position == 0 or not word[0].isupper() or normalized in CAPITALIZED_NON_REGIONS:
            continue
        regions.add(normalized)
    return frozenset(regions)


def sentence_context(sentence: str) -> SentenceContext:
    periods = frozenset(YEAR.findall(sentence))
    quantities: list[Quantity] = []
    for match in QUANTITY.finditer(sentence):
        raw, unit = match.group(1), ("%" if match.group(2) else "")
        if not unit and YEAR.fullmatch(raw):
            continue  # un año no es una cantidad
        value = _parse_number(raw)
        if value is not None:
            quantities.append(Quantity(value=value, unit=unit, raw=raw))
    regions = _regions(sentence)
    stripped = QUANTITY.sub(" ", sentence)
    indicator = frozenset(content_tokens(stripped) - regions)
    return SentenceContext(indicator, periods, regions, tuple(quantities))


def _not_comparable(claim: SentenceContext, source: SentenceContext, qc: Quantity, qs: Quantity) -> str | None:
    """Motivo por el que dos cifras no pueden compararse, o None si son comparables."""
    if qc.unit != qs.unit:
        return "unidades distintas"
    if not claim.indicator or not source.indicator:
        return "indicador no determinado"
    shared = len(claim.indicator & source.indicator) / len(claim.indicator | source.indicator)
    if shared < INDICATOR_SIMILARITY:
        return "indicador distinto"
    if not claim.periods or not source.periods:
        return "período no determinado"
    if claim.periods != source.periods:
        return "período distinto"
    if not claim.regions or not source.regions:
        return "región no determinada"
    if claim.regions != source.regions:
        return "región distinta"
    return None


def analyze_pair(claim: str, source: str) -> tuple[list[RuleFinding], list[str]]:
    """Compara una afirmación con una frase de la fuente.

    Devuelve (contradicciones, discrepancias). Las discrepancias explican por qué una cifra
    no se pudo contrastar; no cuentan como contradicción.
    """
    claim_norm = normalize_text(claim)
    source_norm = normalize_text(source)
    findings: list[RuleFinding] = []
    notes: list[str] = []

    for kind, pattern_a, pattern_b in OPPOSITE_PAIRS:
        match_a = pattern_a.search(claim_norm)
        match_b = pattern_b.search(source_norm)
        if match_a and match_b and not pattern_b.search(claim_norm) and not pattern_a.search(source_norm):
            findings.append(RuleFinding(kind, f"{match_a.group(0)} frente a {match_b.group(0)}"))

    claim_ctx = sentence_context(claim)
    source_ctx = sentence_context(source)

    if (
        claim_ctx.periods
        and source_ctx.periods
        and claim_ctx.periods.isdisjoint(source_ctx.periods)
        and not claim_ctx.quantities
        and not source_ctx.quantities
    ):
        # Mismo suceso con otra fecha. Solo si la afirmación entera está en la fuente.
        if len(claim_ctx.indicator) >= 2 and claim_ctx.indicator <= source_ctx.indicator:
            findings.append(
                RuleFinding("date", f"{', '.join(sorted(claim_ctx.periods))} frente a {', '.join(sorted(source_ctx.periods))}")
            )
        else:
            notes.append("fechas distintas sobre un indicador no coincidente")

    for qc in claim_ctx.quantities:
        for qs in source_ctx.quantities:
            reason = _not_comparable(claim_ctx, source_ctx, qc, qs)
            if reason:
                notes.append(reason)
                continue
            if not math.isclose(qc.value, qs.value, rel_tol=1e-9):
                kind = "percentage" if qc.unit else "numeric"
                findings.append(RuleFinding(kind, f"{qc.raw}{qc.unit} frente a {qs.raw}{qs.unit}"))

    return findings, list(dict.fromkeys(notes))
