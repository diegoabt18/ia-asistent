from app.services.web_extraction_service import extract_document

ARTICLE = (
    "Este es un párrafo de prueba con suficiente texto para que el extractor lo considere "
    "contenido principal de la página. "
) * 6


def _html(body: str) -> str:
    return (
        "<html><head><title>Noticia de prueba</title>"
        "<meta name='date' content='2026-06-21'></head>"
        f"<body><article><h1>Titular</h1><p>{body}</p></article>"
        "<footer>pie de página</footer></body></html>"
    )


def test_extracts_main_text_title_and_date() -> None:
    doc = extract_document(_html(ARTICLE), "https://ejemplo.com/noticia", max_chars=1500)
    assert doc is not None
    assert doc.domain == "ejemplo.com"
    assert doc.published_date == "2026-06-21"
    assert "párrafo de prueba" in doc.text
    assert len(doc.text) <= 1500


def test_returns_none_when_page_has_too_little_text() -> None:
    doc = extract_document(_html("muy corto"), "https://ejemplo.com/x", max_chars=1500)
    assert doc is None
