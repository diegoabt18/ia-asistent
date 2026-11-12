from app.services.citation_validator import CitationValidator
from app.services.claim_extractor import extract_claims
from app.services.source_comparator import ComparisonRules, SourceComparator
from app.services.verification_service import VerificationService, to_schema
from app.services.web_extraction_service import PageDocument

NO_INFO = "No encuentro esa información en las fuentes."

OFFICIAL_TEXT = (
    "El presidente de Colombia asumió el cargo el 7 de agosto de 2022 según el registro oficial."
)
UNRELATED_TEXT = "Los precios del café subieron durante la última semana en varias regiones."


def _doc(url: str, text: str, domain: str | None = None) -> PageDocument:
    host = domain or url.split("/")[2]
    return PageDocument(url=url, domain=host, title="Título", published_date=None, text=text)


def _service() -> VerificationService:
    return VerificationService(CitationValidator(), SourceComparator(ComparisonRules()))


# --- Validación de referencias -------------------------------------------------

def test_answer_without_references_has_no_valid_citations() -> None:
    report = CitationValidator().validate("Texto sin referencias.", [_doc("https://a.com/1", OFFICIAL_TEXT)])
    assert report.total_references == 0
    assert report.valid_references == 0
    assert report.valid_source_indices == ()


def test_valid_marker_is_accepted() -> None:
    report = CitationValidator().validate("Dato [1].", [_doc("https://a.com/1", OFFICIAL_TEXT)])
    assert report.valid_source_indices == (1,)
    assert report.valid_references == 1


def test_invented_marker_is_removed_and_counted_as_invalid() -> None:
    report = CitationValidator().validate("Dato [7].", [_doc("https://a.com/1", OFFICIAL_TEXT)])
    assert report.cleaned_text == "Dato."
    assert report.total_references == 1
    assert report.valid_references == 0


def test_url_not_from_retrieved_sources_is_removed() -> None:
    report = CitationValidator().validate(
        "Dato según https://inventada.com/x.", [_doc("https://a.com/1", OFFICIAL_TEXT)]
    )
    assert "inventada" not in report.cleaned_text
    assert report.valid_references == 0
    assert report.total_references == 1


def test_url_of_retrieved_source_is_valid() -> None:
    report = CitationValidator().validate(
        "Dato según https://www.a.com/1.", [_doc("https://a.com/1", OFFICIAL_TEXT)]
    )
    assert report.valid_source_indices == (1,)
    assert report.valid_references == 1


# --- Extracción de afirmaciones ---------------------------------------------------

def test_claims_keep_their_cited_sources() -> None:
    claims = extract_claims("Dato con cita [1]. Otra frase sin cita.", url_index={})
    assert [c.cited_sources for c in claims] == [(1,), ()]


def test_no_info_sentence_is_not_a_claim() -> None:
    assert extract_claims(NO_INFO, url_index={}, ignore=(NO_INFO,)) == []


def test_very_short_sentences_are_not_claims() -> None:
    assert extract_claims("Sí [1].", url_index={}) == []


# --- Contraste de fuentes ---------------------------------------------------------

def test_claim_matching_its_source_is_supported() -> None:
    docs = [_doc("https://presidencia.gov.co/x", OFFICIAL_TEXT)]
    result = _service().verify(
        "El presidente asumió el cargo el 7 de agosto de 2022 [1].", docs, ignore=(NO_INFO,)
    )
    assert result.status == "VERIFIED"
    assert result.claims[0].supporting_sources == (1,)


def test_claim_contradicted_by_numbers_is_flagged() -> None:
    docs = [_doc("https://a.com/1", "Petro asumió la presidencia en 2018 según registros.")]
    result = _service().verify("Petro asumió la presidencia en 2022 [1].", docs)
    assert result.claims[0].status == "contradicted"
    assert result.status == "UNVERIFIED"
    assert result.unsupported_claims == ["Petro asumió la presidencia en 2022."]


def test_claim_without_evidence_is_unverified() -> None:
    docs = [_doc("https://a.com/1", UNRELATED_TEXT)]
    result = _service().verify("El clima tendrá lluvias intensas en Bogotá [1].", docs)
    assert result.claims[0].status == "unsupported"
    assert result.status == "UNVERIFIED"


def test_same_domain_does_not_count_as_independent_support() -> None:
    docs = [
        _doc("https://a.com/1", OFFICIAL_TEXT, domain="a.com"),
        _doc("https://a.com/2", OFFICIAL_TEXT, domain="a.com"),
    ]
    service = VerificationService(
        CitationValidator(), SourceComparator(ComparisonRules(min_independent_sources=2))
    )
    result = service.verify("El presidente asumió el cargo el 7 de agosto de 2022 [1][2].", docs)
    assert result.claims[0].status == "unsupported"
    assert result.status == "UNVERIFIED"


# --- Estado final -----------------------------------------------------------------

def test_mixed_claims_are_partially_verified() -> None:
    docs = [_doc("https://a.com/1", OFFICIAL_TEXT), _doc("https://b.com/2", UNRELATED_TEXT)]
    answer = (
        "El presidente asumió el cargo el 7 de agosto de 2022 [1]. "
        "El clima tendrá lluvias intensas en Bogotá durante el fin de semana [2]."
    )
    result = _service().verify(answer, docs)
    assert result.status == "PARTIALLY_VERIFIED"


def test_no_sources_is_unverified() -> None:
    result = _service().verify("Respuesta [1].", [])
    assert result.status == "UNVERIFIED"
    assert result.valid_citations == 0


def test_url_alone_does_not_verify_an_answer() -> None:
    docs = [_doc("https://a.com/1", OFFICIAL_TEXT)]
    result = _service().verify("Mira https://a.com/1 para más.", docs)
    assert result.status == "UNVERIFIED"


def test_no_info_answer_is_unverified() -> None:
    result = _service().verify(NO_INFO, [_doc("https://a.com/1", OFFICIAL_TEXT)], ignore=(NO_INFO,))
    assert result.status == "UNVERIFIED"
    assert result.claims == ()


def test_schema_conversion_keeps_status_and_counts() -> None:
    docs = [_doc("https://a.com/1", OFFICIAL_TEXT)]
    result = _service().verify("El presidente asumió el cargo el 7 de agosto de 2022 [1].", docs)
    out = to_schema(result)
    assert out.status == "VERIFIED"
    assert out.valid_citations == 1
    assert out.total_citations == 1
    assert out.claims[0].cited_sources == [1]
