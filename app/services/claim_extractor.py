import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.services.citations import CITATION_PATTERN
from app.services.source_ranking import normalize_url
from app.services.text_analysis import (
    URL_PATTERN,
    content_tokens,
    normalize_text,
    split_sentences,
)

MIN_CLAIM_TOKENS = 2


@dataclass(frozen=True)
class Claim:
    text: str
    cited_sources: tuple[int, ...]


def extract_claims(
    text: str,
    url_index: dict[str, int],
    ignore: Iterable[str] = (),
) -> list[Claim]:
    """Separa la respuesta en afirmaciones, cada una con las fuentes que cita.

    `url_index` relaciona URLs válidas con su fuente. `ignore` son frases que no son
    afirmaciones (por ejemplo, la respuesta de "no encuentro información").
    """
    ignored = {normalize_text(item) for item in ignore}
    claims: list[Claim] = []
    for sentence in split_sentences(text):
        cited = {int(m) for m in CITATION_PATTERN.findall(sentence)}
        for url in URL_PATTERN.findall(sentence):
            key = normalize_url(url.rstrip(".,;:"))
            if key in url_index:
                cited.add(url_index[key])

        plain = URL_PATTERN.sub(" ", CITATION_PATTERN.sub(" ", sentence))
        plain = re.sub(r"\s+([.,;:!?])", r"\1", " ".join(plain.split()))
        if len(content_tokens(plain)) < MIN_CLAIM_TOKENS:
            continue
        if normalize_text(plain) in ignored:
            continue
        claims.append(Claim(text=plain, cited_sources=tuple(sorted(cited))))
    return claims
