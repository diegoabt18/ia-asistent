from app.services.citations import validate_citations


def test_keeps_valid_citations_and_reports_them() -> None:
    text, cited = validate_citations("Petro gobernó [1]. Otro dato [2].", source_count=3)
    assert text == "Petro gobernó [1]. Otro dato [2]."
    assert cited == [1, 2]


def test_removes_citations_to_nonexistent_sources() -> None:
    text, cited = validate_citations("Dato inventado [9] y real [1].", source_count=2)
    assert text == "Dato inventado y real [1]."
    assert cited == [1]


def test_no_sources_removes_every_citation() -> None:
    text, cited = validate_citations("Respuesta [1].", source_count=0)
    assert text == "Respuesta."
    assert cited == []
