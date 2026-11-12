import re
from dataclasses import dataclass

from app.services.citations import CITATION_PATTERN, validate_citations
from app.services.source_ranking import normalize_url
from app.services.text_analysis import URL_PATTERN
from app.services.web_extraction_service import PageDocument


@dataclass(frozen=True)
class CitationReport:
    cleaned_text: str
    valid_source_indices: tuple[int, ...]
    # URL normalizada -> índice de la fuente recuperada a la que corresponde.
    url_index: dict[str, int]
    total_references: int
    valid_references: int


class CitationValidator:
    """Comprueba que cada referencia (marcador [n] o URL) apunte a una fuente recuperada.

    Una URL inventada por el modelo se elimina del texto. Que haya una URL no basta para
    considerar la respuesta verificada: eso lo decide VerificationService.
    """

    def validate(self, text: str, documents: list[PageDocument]) -> CitationReport:
        known = {normalize_url(doc.url): index for index, doc in enumerate(documents, 1)}
        url_index: dict[str, int] = {}
        invalid_urls: set[str] = set()

        def check_url(match: re.Match[str]) -> str:
            raw = match.group(0)
            stripped = raw.rstrip(".,;:")
            trailing = raw[len(stripped):]
            key = normalize_url(stripped)
            if key in known:
                url_index[key] = known[key]
                return raw
            invalid_urls.add(key)
            return trailing

        without_urls = re.sub(r"[ \t]{2,}", " ", URL_PATTERN.sub(check_url, text))
        marker_refs = {int(m) for m in CITATION_PATTERN.findall(without_urls)}
        cleaned, valid_markers = validate_citations(without_urls, len(documents))

        valid_sources = tuple(sorted(set(valid_markers) | set(url_index.values())))
        total = len(marker_refs) + len(url_index) + len(invalid_urls)
        valid = len(valid_markers) + len(url_index)
        return CitationReport(
            cleaned_text=cleaned,
            valid_source_indices=valid_sources,
            url_index=url_index,
            total_references=total,
            valid_references=valid,
        )
