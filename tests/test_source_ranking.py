from app.schemas.search import SearchResult
from app.services.source_ranking import classify, normalize_url, rank_and_dedupe


def _r(url: str) -> SearchResult:
    return SearchResult(title=url, url=url, snippet="")


def test_normalize_url_removes_www_fragment_trailing_slash_and_tracking() -> None:
    assert (
        normalize_url("https://www.Example.com/page/?utm_source=x&id=2#seccion")
        == "https://example.com/page?id=2"
    )


def test_classify_detects_official_and_reference_domains() -> None:
    assert classify("presidencia.gov.co") == "oficial"
    assert classify("cgfm.mil.co") == "oficial"
    assert classify("es.wikipedia.org") == "referencia"
    assert classify("blog.ejemplo.com") == "otro"


def test_rank_puts_official_first_and_drops_social_and_duplicates() -> None:
    results = [
        _r("https://blog.ejemplo.com/a"),
        _r("https://www.instagram.com/perfil"),
        _r("https://presidencia.gov.co/inicio"),
        _r("https://blog.ejemplo.com/a/"),
        _r("https://es.wikipedia.org/wiki/Colombia"),
    ]
    ranked = rank_and_dedupe(results, max_total=10)
    urls = [r.url for r in ranked]
    assert urls == [
        "https://presidencia.gov.co/inicio",
        "https://es.wikipedia.org/wiki/Colombia",
        "https://blog.ejemplo.com/a",
    ]


def test_rank_limits_pages_per_domain_and_total() -> None:
    results = [_r(f"https://mismo.com/p{i}") for i in range(5)]
    ranked = rank_and_dedupe(results, max_total=10, max_per_domain=2)
    assert len(ranked) == 2
    assert len(rank_and_dedupe(results + [_r("https://otro.com/x")], max_total=2)) == 2
