from dataclasses import dataclass
from typing import Literal

from app.services.claim_extractor import Claim
from app.services.contradiction_rules import analyze_pair
from app.services.evidence import Fragment, coverage, document_fragments
from app.services.text_analysis import content_tokens, numbers, split_sentences
from app.services.web_extraction_service import PageDocument

ClaimStatus = Literal["supported", "contradicted", "unsupported", "no_citation"]


@dataclass(frozen=True)
class ComparisonRules:
    """Reglas configurables. Cambiarlas no requiere tocar el comparador."""

    support_threshold: float = 0.6
    contradiction_threshold: float = 0.6
    min_independent_sources: int = 1
    detect_contradictions: bool = True
    fragment_chars: int = 800
    fragment_overlap_chars: int = 120
    max_fragments_per_source: int = 40
    max_fragments_per_claim: int = 3


@dataclass(frozen=True)
class ClaimAssessment:
    text: str
    status: ClaimStatus
    cited_sources: tuple[int, ...]
    supporting_sources: tuple[int, ...]
    contradicting_sources: tuple[int, ...]
    reasons: tuple[str, ...] = ()


class SourceComparator:
    """Contrasta cada afirmación con los fragmentos de las fuentes que la citan (solo léxico).

    Para cada fuente citada:
    1. Se divide en fragmentos y se toman los más parecidos por solape de palabras.
    2. Un fragmento respalda la afirmación si contiene sus cifras y el solape supera el umbral.
    3. Se buscan contradicciones frase a frase con `contradiction_rules`.

    Sin modelos adicionales: no hay embeddings ni inferencia de lenguaje.
    """

    def __init__(self, rules: ComparisonRules | None = None) -> None:
        self._rules = rules or ComparisonRules()

    def assess(self, claims: list[Claim], documents: list[PageDocument]) -> list[ClaimAssessment]:
        return [self.assess_one(claim, documents) for claim in claims]

    def assess_one(self, claim: Claim, documents: list[PageDocument]) -> ClaimAssessment:
        if not claim.cited_sources:
            return ClaimAssessment(claim.text, "no_citation", (), (), ())

        rules = self._rules
        claim_tokens = content_tokens(claim.text)
        claim_numbers = numbers(claim.text)
        supporting: list[int] = []
        contradicting: list[int] = []
        reasons: list[str] = []
        numbers_found = not claim_numbers

        for index in claim.cited_sources:
            if not 1 <= index <= len(documents):
                continue
            fragments = document_fragments(
                documents[index - 1],
                index,
                rules.fragment_chars,
                rules.fragment_overlap_chars,
                rules.max_fragments_per_source,
            )
            if not fragments:
                reasons.append(f"fuente {index} sin texto útil")
                continue

            if any(claim_numbers <= numbers(f.text) for f in fragments):
                numbers_found = True
            if self._supports(claim_tokens, claim_numbers, fragments):
                supporting.append(index)

            found, notes = self._contradictions(claim.text, claim_tokens, fragments)
            if found:
                contradicting.append(index)
                reasons.extend(f"contradicción en fuente {index}: {item}" for item in found)
            reasons.extend(f"discrepancia en fuente {index}: {item}" for item in notes[:3])

        if claim_numbers and not numbers_found:
            reasons.append("cifras de la afirmación no aparecen en las fuentes citadas")

        independent_domains = {documents[i - 1].domain for i in supporting}
        if contradicting:
            status: ClaimStatus = "contradicted"
        elif supporting and len(independent_domains) >= rules.min_independent_sources:
            status = "supported"
        else:
            status = "unsupported"
            if not reasons:
                reasons.append("sin evidencia suficiente")

        return ClaimAssessment(
            text=claim.text,
            status=status,
            cited_sources=claim.cited_sources,
            supporting_sources=tuple(supporting),
            contradicting_sources=tuple(contradicting),
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def _supports(self, claim_tokens: set[str], claim_numbers: set[str], fragments: list[Fragment]) -> bool:
        limit = self._rules.max_fragments_per_claim
        scores = {i: coverage(claim_tokens, f.text) for i, f in enumerate(fragments)}
        best = sorted(scores, key=lambda i: (-scores[i], i))[:limit]
        return any(
            scores[i] > 0
            and claim_numbers <= numbers(fragments[i].text)
            and scores[i] >= self._rules.support_threshold
            for i in best
        )

    def _contradictions(
        self, claim_text: str, claim_tokens: set[str], fragments: list[Fragment]
    ) -> tuple[list[str], list[str]]:
        if not self._rules.detect_contradictions:
            return [], []
        found: list[str] = []
        notes: list[str] = []
        for fragment in fragments:
            for sentence in split_sentences(fragment.text):
                if coverage(claim_tokens, sentence) < self._rules.contradiction_threshold:
                    continue
                findings, discrepancies = analyze_pair(claim_text, sentence)
                found.extend(f"{f.kind}: {f.detail}" for f in findings)
                notes.extend(discrepancies)
        return list(dict.fromkeys(found)), list(dict.fromkeys(notes))
