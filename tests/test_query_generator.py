from app.services.query_generator import QueryGenerator
from app.services.text_analysis import normalize_text


def test_original_question_is_always_first_and_unchanged() -> None:
    question = "¿Cuáles son las tendencias de ventas de velas aromáticas en Colombia?"
    queries = QueryGenerator(max_queries=4).generate(question)
    assert queries[0] == question


def test_generates_templated_queries_from_long_question() -> None:
    queries = QueryGenerator(max_queries=4).generate(
        "¿Cuáles son las tendencias de ventas de velas aromáticas en Colombia?"
    )
    assert len(queries) == 4
    assert queries[1] == "Tendencias de ventas de velas aromáticas en Colombia"
    assert queries[2].startswith("Tendencias de ventas de velas aromáticas en Colombia")


def test_short_question_keeps_only_the_original() -> None:
    assert QueryGenerator(max_queries=4).generate("¿Clima?") == ["¿Clima?"]


def test_long_topic_is_truncated_to_ten_words() -> None:
    long_question = " ".join(f"palabra{i}" for i in range(25)) + "?"
    queries = QueryGenerator(max_queries=4).generate(long_question)
    for query in queries[1:]:
        topic_part = query.split(" estadísticas")[0].split(" datos")[0].split(" fuentes")[0]
        assert len(topic_part.split()) <= 10


def test_duplicates_are_removed_ignoring_case_accents_and_spaces() -> None:
    queries = QueryGenerator(max_queries=6).generate("  Tendencias   de velas  ")
    keys = [normalize_text(q) for q in queries]
    assert len(keys) == len(set(keys))
    assert queries[0] == "Tendencias de velas"


def test_respects_max_queries() -> None:
    queries = QueryGenerator(max_queries=2).generate(
        "¿Cuáles son las tendencias de ventas de velas aromáticas en Colombia?"
    )
    assert len(queries) == 2
    assert queries[0].startswith("¿Cuáles son")


def test_collapses_spaces_in_original() -> None:
    assert QueryGenerator(max_queries=1).generate("  hola   mundo  ") == ["hola mundo"]
