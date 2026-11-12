from app.services.evidence import document_fragments, select_context, split_fragments
from app.services.text_analysis import clean_page_text
from app.services.web_extraction_service import PageDocument

ANSWER_SENTENCE = "El presidente asumió el cargo el 7 de agosto de 2022 según el registro oficial."


def _doc(url: str, text: str) -> PageDocument:
    return PageDocument(url=url, domain=url.split("/")[2], title="Título", published_date=None, text=text)


def test_fragments_respect_size_limit() -> None:
    text = " ".join(f"Frase número {i} con algunas palabras de relleno." for i in range(200))
    fragments = split_fragments(text, size=300, overlap=60)
    assert len(fragments) > 1
    assert all(len(fragment) <= 300 for fragment in fragments)


def test_overlap_repeats_context_between_fragments() -> None:
    sentences = [f"Frase número {i} con datos." for i in range(60)]
    fragments = split_fragments(" ".join(sentences), size=300, overlap=100)
    assert len(fragments) > 1
    first_sentences = set(fragments[0].split(". "))
    second_sentences = set(fragments[1].split(". "))
    assert first_sentences & second_sentences, "consecutive fragments should share at least one sentence"


def test_very_long_sentence_is_cut_to_size() -> None:
    fragments = split_fragments("palabra " * 500, size=200, overlap=0)
    assert all(len(fragment) <= 200 for fragment in fragments)
    assert len(fragments) > 1


def test_clean_page_text_removes_menu_items_and_repeated_lines() -> None:
    raw = f"Inicio\nContacto\n{ANSWER_SENTENCE}\n{ANSWER_SENTENCE}\nMenú"
    assert clean_page_text(raw) == ANSWER_SENTENCE


def test_page_without_useful_content_yields_no_fragments() -> None:
    cleaned = clean_page_text("Inicio\nMenú\nContacto")
    assert cleaned == ""
    assert document_fragments(_doc("https://a.com/1", cleaned), 1, 800, 120, 40) == []


def test_repeated_text_inside_a_page_is_not_fragmented_twice() -> None:
    text = "Dato repetido en la página. " * 200
    fragments = document_fragments(_doc("https://a.com/1", text), 1, 800, 120, 40)
    assert len({f.text for f in fragments}) == len(fragments)


def test_fragments_keep_source_index_url_title_and_domain() -> None:
    doc = _doc("https://presidencia.gov.co/inicio", ANSWER_SENTENCE)
    (fragment,) = document_fragments(doc, 3, 800, 120, 40)
    assert (fragment.source_index, fragment.url, fragment.title, fragment.domain) == (
        3,
        "https://presidencia.gov.co/inicio",
        "Título",
        "presidencia.gov.co",
    )


def test_max_fragments_per_source_is_enforced() -> None:
    text = " ".join(f"Frase única número {i} sobre algo distinto." for i in range(300))
    assert len(document_fragments(_doc("https://a.com/1", text), 1, 200, 0, max_fragments=5)) == 5


def test_context_keeps_source_order_and_numbering() -> None:
    filler = "Texto genérico sin relación con el tema. " * 60
    docs = [
        _doc("https://a.com/1", "Información sobre otro asunto por completo."),
        _doc("https://b.com/2", filler + ANSWER_SENTENCE),
    ]
    context = select_context("¿Quién asumió el cargo presidencial?", docs, 6000, 800, 120)
    assert [d.url for d in context] == [d.url for d in docs]
    assert "asumió el cargo" in context[1].text


def test_context_respects_character_budget() -> None:
    long_text = " ".join(f"Dato número {i} de la fuente." for i in range(2000))
    context = select_context("dato fuente", [_doc("https://a.com/1", long_text)], 1000, 800, 120)
    assert len(context[0].text) <= 1000 + 800


def test_context_does_not_repeat_the_same_fragment_across_sources() -> None:
    docs = [_doc("https://a.com/1", ANSWER_SENTENCE), _doc("https://b.com/2", ANSWER_SENTENCE)]
    context = select_context("presidente cargo", docs, 6000, 800, 120)
    combined = context[0].text + context[1].text
    assert combined.count(ANSWER_SENTENCE) == 1
