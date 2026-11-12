"""Fragmentación y recuperación léxica de evidencia.

Trabaja sobre texto ya extraído y no usa modelos: el coste es lineal en el tamaño de la página.
"""

from dataclasses import dataclass, replace

from app.services.text_analysis import content_tokens, normalize_text, split_sentences
from app.services.web_extraction_service import PageDocument

CONTEXT_SEPARATOR = " […] "


@dataclass(frozen=True)
class Fragment:
    source_index: int  # 1-based, el mismo número que [n] en la respuesta
    url: str
    title: str
    domain: str
    text: str


def split_fragments(text: str, size: int, overlap: int) -> list[str]:
    """Agrupa frases en fragmentos de hasta `size` caracteres, con `overlap` caracteres de solape.

    Una frase más larga que `size` se corta a la fuerza para no superar el límite.
    """
    size = max(1, size)
    overlap = min(max(0, overlap), size // 2)

    pieces: list[str] = []
    for sentence in split_sentences(text):
        while len(sentence) > size:
            pieces.append(sentence[:size])
            sentence = sentence[size:]
        if sentence:
            pieces.append(sentence)

    fragments: list[str] = []
    current: list[str] = []
    for piece in pieces:
        if current and len(" ".join([*current, piece])) > size:
            fragments.append(" ".join(current))
            carry: list[str] = []
            for previous in reversed(current):
                if len(" ".join([previous, *carry])) > overlap:
                    break
                carry.insert(0, previous)
            current = carry
            if current and len(" ".join([*current, piece])) > size:
                current = []
        current.append(piece)
    if current:
        fragments.append(" ".join(current))
    return fragments


def document_fragments(
    doc: PageDocument, source_index: int, size: int, overlap: int, max_fragments: int
) -> list[Fragment]:
    """Fragmentos de una fuente, sin repetir textos idénticos dentro de la misma página."""
    seen: set[str] = set()
    result: list[Fragment] = []
    for text in split_fragments(doc.text, size, overlap):
        if len(result) >= max_fragments:
            break
        key = normalize_text(text)
        if key in seen:
            continue
        seen.add(key)
        result.append(Fragment(source_index, doc.url, doc.title, doc.domain, text))
    return result


def coverage(claim_tokens: set[str], text: str) -> float:
    """Fracción de los términos de la afirmación que aparecen en el texto."""
    if not claim_tokens:
        return 0.0
    return len(claim_tokens & content_tokens(text)) / len(claim_tokens)


def select_context(
    question: str,
    documents: list[PageDocument],
    budget_chars: int,
    size: int,
    overlap: int,
) -> list[PageDocument]:
    """Texto que se envía al modelo: fragmentos más relevantes para la pregunta, dentro de un presupuesto.

    Conserva el orden y el número de cada fuente: cada [n] sigue apuntando a la misma página.
    El mejor fragmento de cada fuente se incluye siempre, y no se repite un fragmento ya elegido
    en otra fuente.
    """
    question_tokens = content_tokens(question)
    pieces_per_doc = [split_fragments(doc.text, size, overlap) for doc in documents]
    rankings = [
        sorted(range(len(pieces)), key=lambda i, p=pieces: (-coverage(question_tokens, p[i]), i))
        for pieces in pieces_per_doc
    ]

    seen: set[str] = set()
    chosen: list[set[int]] = [set() for _ in documents]
    used = 0

    for doc_index, ranking in enumerate(rankings):
        for piece_index in ranking:
            text = pieces_per_doc[doc_index][piece_index]
            key = normalize_text(text)
            if key in seen:
                continue
            seen.add(key)
            chosen[doc_index].add(piece_index)
            used += len(text)
            break

    pool = sorted(
        (
            (coverage(question_tokens, pieces_per_doc[d][i]), d, i)
            for d, ranking in enumerate(rankings)
            for i in ranking
            if i not in chosen[d]
        ),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    for _, doc_index, piece_index in pool:
        text = pieces_per_doc[doc_index][piece_index]
        key = normalize_text(text)
        if key in seen or used + len(text) > budget_chars:
            continue
        seen.add(key)
        chosen[doc_index].add(piece_index)
        used += len(text)

    return [
        replace(
            doc,
            text=CONTEXT_SEPARATOR.join(pieces_per_doc[d][i] for i in sorted(chosen[d])),
        )
        for d, doc in enumerate(documents)
    ]
