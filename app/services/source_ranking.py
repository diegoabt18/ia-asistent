import re
from collections import Counter
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.schemas.search import SearchResult

# Redes sociales: normalmente muros de login o contenido sin fuente verificable.
SKIP_DOMAINS = ("instagram.com", "facebook.com", "x.com", "twitter.com", "tiktok.com", "youtube.com")
REFERENCE_DOMAINS = ("wikipedia.org", "wikidata.org", "britannica.com")
OFFICIAL_PATTERN = re.compile(r"\.(gov|gob|edu|mil|int)(\.[a-z]{2})?$")
TRACKING_PREFIXES = ("utm_", "fbclid", "gclid")
CATEGORY_ORDER = {"oficial": 0, "referencia": 1, "otro": 2}


def domain_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def _matches(domain: str, suffixes: tuple[str, ...]) -> bool:
    return any(domain == s or domain.endswith("." + s) for s in suffixes)


def classify(domain: str) -> str:
    if OFFICIAL_PATTERN.search(domain):
        return "oficial"
    if _matches(domain, REFERENCE_DOMAINS):
        return "referencia"
    return "otro"


def normalize_url(url: str) -> str:
    """Forma canónica para detectar duplicados: sin www, sin fragmento, sin parámetros de rastreo."""
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    query = [
        (k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(TRACKING_PREFIXES)
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ""))


def rank_and_dedupe(
    results: list[SearchResult], max_total: int, max_per_domain: int = 2
) -> list[SearchResult]:
    """Quita redes sociales y duplicados, prioriza fuentes oficiales y limita páginas por dominio."""
    indexed = sorted(
        enumerate(results),
        key=lambda pair: (CATEGORY_ORDER[classify(domain_of(pair[1].url))], pair[0]),
    )
    seen: set[str] = set()
    per_domain: Counter[str] = Counter()
    kept: list[SearchResult] = []
    for _, result in indexed:
        domain = domain_of(result.url)
        if _matches(domain, SKIP_DOMAINS):
            continue
        key = normalize_url(result.url)
        if key in seen or per_domain[domain] >= max_per_domain:
            continue
        seen.add(key)
        per_domain[domain] += 1
        kept.append(result)
        if len(kept) >= max_total:
            break
    return kept
