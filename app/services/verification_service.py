import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from app.core.config import Settings
from app.schemas.verification import ClaimOut, VerificationOut
from app.services.citation_validator import CitationValidator
from app.services.claim_extractor import extract_claims
from app.services.source_comparator import ClaimAssessment, ComparisonRules, SourceComparator
from app.services.web_extraction_service import PageDocument

VerificationStatus = Literal["VERIFIED", "PARTIALLY_VERIFIED", "UNVERIFIED"]


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus
    answer: str
    valid_citations: int
    total_citations: int
    valid_source_indices: tuple[int, ...]
    claims: tuple[ClaimAssessment, ...]
    truncated: bool = False

    @property
    def unsupported_claims(self) -> list[str]:
        return [claim.text for claim in self.claims if claim.status != "supported"]


def _status(claims: tuple[ClaimAssessment, ...]) -> VerificationStatus:
    if not claims:
        return "UNVERIFIED"
    supported = sum(1 for claim in claims if claim.status == "supported")
    if supported == 0:
        return "UNVERIFIED"
    if supported == len(claims):
        return "VERIFIED"
    return "PARTIALLY_VERIFIED"


class VerificationService:
    """Orquesta la verificación: referencias → afirmaciones → contraste → estado final.

    No usa el modelo de lenguaje para decidir el resultado.
    """

    def __init__(
        self,
        validator: CitationValidator | None = None,
        comparator: SourceComparator | None = None,
        max_seconds: float | None = None,
    ) -> None:
        self._validator = validator or CitationValidator()
        self._comparator = comparator or SourceComparator()
        self._max_seconds = max_seconds

    def verify(
        self,
        answer: str,
        documents: list[PageDocument],
        ignore: Iterable[str] = (),
    ) -> VerificationResult:
        report = self._validator.validate(answer, documents)
        claims = extract_claims(report.cleaned_text, report.url_index, ignore)

        deadline = None if self._max_seconds is None else time.monotonic() + self._max_seconds
        assessments: list[ClaimAssessment] = []
        truncated = False
        for claim in claims:
            if deadline is not None and time.monotonic() >= deadline:
                # Sin tiempo para contrastar: la afirmación no queda respaldada.
                truncated = True
                assessments.append(
                    ClaimAssessment(
                        claim.text,
                        "unsupported",
                        claim.cited_sources,
                        (),
                        (),
                        ("tiempo máximo de verificación agotado",),
                    )
                )
                continue
            assessments.append(self._comparator.assess_one(claim, documents))

        results = tuple(assessments)
        return VerificationResult(
            status=_status(results),
            answer=report.cleaned_text,
            valid_citations=report.valid_references,
            total_citations=report.total_references,
            valid_source_indices=report.valid_source_indices,
            claims=results,
            truncated=truncated,
        )


def verification_service_from_settings(settings: Settings) -> VerificationService:
    rules = ComparisonRules(
        support_threshold=settings.verification_support_threshold,
        contradiction_threshold=settings.verification_contradiction_threshold,
        min_independent_sources=settings.verification_min_independent_sources,
        detect_contradictions=settings.verification_detect_contradictions,
        fragment_chars=settings.evidence_fragment_chars,
        fragment_overlap_chars=settings.evidence_fragment_overlap_chars,
        max_fragments_per_source=settings.evidence_max_fragments_per_source,
        max_fragments_per_claim=settings.evidence_max_fragments_per_claim,
    )
    return VerificationService(
        CitationValidator(), SourceComparator(rules), max_seconds=settings.verification_max_seconds
    )


def to_schema(result: VerificationResult) -> VerificationOut:
    return VerificationOut(
        status=result.status,
        valid_citations=result.valid_citations,
        total_citations=result.total_citations,
        unsupported_claims=result.unsupported_claims,
        truncated=result.truncated,
        claims=[
            ClaimOut(
                text=claim.text,
                status=claim.status,
                cited_sources=list(claim.cited_sources),
                supporting_sources=list(claim.supporting_sources),
                contradicting_sources=list(claim.contradicting_sources),
                reasons=list(claim.reasons),
            )
            for claim in result.claims
        ],
    )
