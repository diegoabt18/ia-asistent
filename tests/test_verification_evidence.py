from app.services.citation_validator import CitationValidator
from app.services.source_comparator import ComparisonRules, SourceComparator
from app.services.verification_service import VerificationService
from app.services.web_extraction_service import PageDocument


def _doc(text: str, url: str = "https://fuente.gov.co/x") -> PageDocument:
    return PageDocument(url=url, domain=url.split("/")[2], title="Título", published_date=None, text=text)


def _service(max_seconds: float | None = None) -> VerificationService:
    return VerificationService(CitationValidator(), SourceComparator(ComparisonRules()), max_seconds=max_seconds)


def test_wrong_percentage_is_flagged_as_contradiction() -> None:
    docs = [_doc("La tasa de desempleo en Colombia fue 12% en 2022.")]
    result = _service().verify("La tasa de desempleo en Colombia fue 10% en 2022 [1].", docs)
    assert result.claims[0].status == "contradicted"
    assert result.status == "UNVERIFIED"
    assert any("percentage" in reason for reason in result.claims[0].reasons)


def test_different_region_is_unsupported_not_contradicted() -> None:
    docs = [_doc("El desempleo en Perú fue 12% en 2022.")]
    result = _service().verify("El desempleo en Colombia fue 10% en 2022 [1].", docs)
    assert result.claims[0].status == "unsupported"
    assert any(reason.startswith("discrepancia") for reason in result.claims[0].reasons)


def test_evidence_after_the_first_1500_characters_is_found() -> None:
    filler = "Texto genérico de la página sin relación con el asunto. " * 40
    docs = [_doc(filler + "El presidente asumió el cargo el 7 de agosto de 2022 según el registro oficial.")]
    assert len(docs[0].text) > 1500
    result = _service().verify("El presidente asumió el cargo el 7 de agosto de 2022 [1].", docs)
    assert result.claims[0].status == "supported"


def test_time_limit_leaves_remaining_claims_unsupported() -> None:
    docs = [_doc("El presidente asumió el cargo el 7 de agosto de 2022 según el registro oficial.")]
    result = _service(max_seconds=0).verify("El presidente asumió el cargo el 7 de agosto de 2022 [1].", docs)
    assert result.truncated is True
    assert result.status == "UNVERIFIED"
    assert result.claims[0].reasons == ("tiempo máximo de verificación agotado",)


def test_result_without_limits_is_not_truncated() -> None:
    docs = [_doc("El presidente asumió el cargo el 7 de agosto de 2022 según el registro oficial.")]
    result = _service().verify("El presidente asumió el cargo el 7 de agosto de 2022 [1].", docs)
    assert result.truncated is False


def test_source_without_useful_text_is_reported() -> None:
    docs = [_doc("")]
    result = _service().verify("El presidente asumió el cargo el 7 de agosto de 2022 [1].", docs)
    assert result.claims[0].status == "unsupported"
    assert "fuente 1 sin texto útil" in result.claims[0].reasons
