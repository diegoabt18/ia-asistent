"""Banco del selector (Fase 1): integridad de los casos, puntuación y ejecutor, sin modelo real."""

import asyncio
import json
from pathlib import Path
from collections import Counter
from datetime import date

import httpx

from bench.catalog import DOMAINS, INTENTS, INTENTS_BY_NAME, output_schema, validate_args
from bench.rules import normalize_selection, route_rules
from bench.run import load_cases, parse_args, run_cases, summarize
from bench.selector import Selection, build_messages, parse_selection, score_case, system_prompt, value_matches

CASES = load_cases()


def answer(case: dict) -> str:
    """Respuesta perfecta del modelo para un caso (formato v2: solo intents)."""
    return json.dumps({"intents": case["expected"]["intents"]})


def system(case: dict, raw: str) -> Selection:
    """Lo que hará el Server: reglas de nivel 0 o normalización de la propuesta del modelo."""
    routed = route_rules(case["message"], case.get("context"))
    return routed if routed is not None else normalize_selection(case["message"], case.get("context"), parse_selection(raw))


# ------------------------------------------------------------------ integridad del banco

def test_bank_has_between_100_and_150_unique_cases() -> None:
    assert 100 <= len(CASES) <= 150
    assert len({c["id"] for c in CASES}) == len(CASES)


def test_bank_covers_every_required_category() -> None:
    counts = Counter(c["category"] for c in CASES)
    for category in [
        "productos", "busqueda_ambigua", "pedidos", "ventas", "produccion", "incompletas",
        "compuestas", "recordatorios", "conversacion", "externas", "contexto", "acciones",
    ]:
        assert counts[category] >= 5, category


def test_every_expectation_is_valid_against_the_catalog() -> None:
    for case in CASES:
        exp = case["expected"]
        for domain in exp["domain"] if isinstance(exp["domain"], list) else [exp["domain"]]:
            assert domain in DOMAINS, case["id"]
        assert len(exp["intents"]) <= 2, case["id"]
        allowed_missing = set(exp.get("missing", []))
        for intent in exp["intents"]:
            assert intent["tool"] in INTENTS_BY_NAME, case["id"]
            errors = [e for e in validate_args(intent["tool"], intent["args"]) if not (e.startswith("falta ") and e[6:] in allowed_missing)]
            assert errors == [], (case["id"], errors)
        for name in allowed_missing:
            assert any(name in INTENTS_BY_NAME[i["tool"]].args for i in exp["intents"]), (case["id"], name)


def test_the_prompt_lists_every_intent_and_stays_small_and_static() -> None:
    prompt = system_prompt()
    # Sin fecha ni datos variables: idéntico en todas las llamadas (caché de prefijo de Ollama).
    assert prompt == system_prompt(date(2030, 1, 1))
    for intent in INTENTS:
        assert intent.name in prompt
    # Un prompt corto deja sitio en num_ctx=4096 y reduce la latencia en CPU.
    assert len(prompt) < 6000


def test_output_schema_only_asks_for_intents() -> None:
    schema = output_schema()
    assert schema["required"] == ["intents"] and set(schema["properties"]) == {"intents"}
    assert set(schema["properties"]["intents"]["items"]["properties"]["tool"]["enum"]) == set(INTENTS_BY_NAME)
    assert schema["properties"]["intents"]["maxItems"] == 2


def test_context_reaches_the_user_message() -> None:
    messages = build_messages("¿cuánto tiene?", {"last_entity": {"type": "producto", "name": "Lumi Vela"}}, date(2026, 11, 16))
    assert "Lumi Vela" in messages[1]["content"]


# ------------------------------------------------------------------ puntuación

def test_the_rules_never_spoil_a_perfect_model_answer() -> None:
    for case in CASES:
        score = score_case(case["expected"], system(case, answer(case)))
        assert score.correct, (case["id"], score.notes)
        assert score.domain_ok, (case["id"], system(case, answer(case)).domain)


def test_wrong_intent_or_argument_is_not_correct() -> None:
    expected = {"domain": "lumia_op", "intents": [{"tool": "orders.count", "args": {"status": "paid"}}]}
    wrong_tool = parse_selection(json.dumps({"intents": [{"tool": "orders.recent", "args": {}}]}))
    wrong_arg = parse_selection(json.dumps({"intents": [{"tool": "orders.count", "args": {"status": "shipped"}}]}))
    bad_enum = parse_selection(json.dumps({"intents": [{"tool": "orders.count", "args": {"status": "paid", "period": "ayer"}}]}))
    assert not score_case(expected, wrong_tool).correct
    assert not score_case(expected, wrong_arg).correct
    assert not score_case(expected, bad_enum).correct  # el Server rechazaría "ayer": no es un periodo válido


def test_unexpected_arguments_are_errors_for_the_raw_model() -> None:
    expected = {"domain": "lumia_op", "intents": [{"tool": "inventory.low", "args": {}}]}
    extra_arg = parse_selection(json.dumps({"intents": [{"tool": "inventory.low", "args": {"sql": "x"}}]}))
    assert not score_case(expected, extra_arg).correct


def test_text_arguments_tolerate_extra_words_and_accents_but_not_other_words() -> None:
    assert value_matches("lumi vela", "Lumi Vela")
    assert value_matches("jabon avena", "jabón de avena")
    assert value_matches("lavanda", "velas de lavanda")
    assert not value_matches("lavanda", "vainilla")
    assert not value_matches(False, 0)
    assert value_matches(5, 5)


def test_malformed_model_output_never_raises() -> None:
    for raw in ["", "no es json", "[]", '{"domain":"x"}', json.dumps({"intents": [{"tool": "drop.database"}] * 3})]:
        selection = parse_selection(raw)
        assert not selection.valid


# ------------------------------------------------------------------ ejecutor (Ollama simulado)

def run_with_fake_ollama(reply) -> dict:
    by_message = {c["message"]: c for c in CASES}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["format"] == output_schema()
        assert body["think"] is False and body["options"]["num_ctx"] == 4096
        message = body["messages"][1]["content"].split('Mensaje: "', 1)[1][:-1]
        return httpx.Response(200, json={"message": {"content": reply(by_message[message])}, "prompt_eval_count": 900, "eval_count": 30, "done": True})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_cases(client, CASES, parse_args([]), concurrency=4)

    return summarize(asyncio.run(go()))


def test_runner_scores_a_perfect_model_at_100_percent() -> None:
    summary = run_with_fake_ollama(answer)
    assert summary["accuracy"] == 1.0
    assert summary["json_valid_rate"] == 1.0
    assert summary["prompt_tokens_avg"] == 900
    # Hora, clima e internet no llegan al modelo.
    assert summary["resolved_by_rules"] >= 7
    assert summary["model_calls"] + summary["resolved_by_rules"] == len(CASES)


def test_runner_reports_invalid_json_as_failures() -> None:
    summary = run_with_fake_ollama(lambda case: "lo siento, no entiendo")
    # Solo aciertan los que resuelven las reglas sin modelo.
    assert summary["correct"] == summary["resolved_by_rules"]
    assert summary["failure_kinds"] == {"json": summary["model_calls"]}


# ------------------------------------------------------------------ reglas del Server (Fase 2)

def norm(message: str, intents: list[dict], context: dict | None = None) -> Selection:
    return normalize_selection(message, context, Selection(None, intents, [], None, []))


def test_closed_values_come_from_the_message_not_from_the_model() -> None:
    got = norm("cuántos pedidos se cancelaron este mes", [{"tool": "orders.count", "args": {"status": "pending", "period": "last_month"}}])
    assert got.intents[0]["args"] == {"status": "cancelled", "period": "this_month"}
    got = norm("pedidos por despachar", [{"tool": "orders.count", "args": {"status": "pending"}}])
    assert got.intents[0]["args"]["status"] == "paid"


def test_invalid_values_are_dropped_and_the_compared_period_is_not_the_requested_one() -> None:
    got = norm("pedidos de hoy", [{"tool": "orders.count", "args": {"status": "today"}}])
    assert got.intents[0]["args"] == {"period": "today"}
    got = norm("compara las ventas de este mes con el mes anterior", [{"tool": "sales.summary", "args": {"compare_to": "last_month"}}])
    assert got.intents[0]["args"] == {"period": "this_month", "compare_to": "previous_period"}
    got = norm("ventas de ayer", [{"tool": "sales.summary", "args": {"period": "yesterday", "compare_to": "today"}}])
    assert "compare_to" not in got.intents[0]["args"]


def test_missing_data_is_computed_by_the_server() -> None:
    got = norm("desactiva el producto", [{"tool": "products.set_active", "args": {"name": ""}}])
    assert got.missing == ["name"] and got.intents[0]["args"] == {"active": False}
    got = norm("Cada semana analiza las ventas", [{"tool": "reminders.create", "args": {"text": "analizar las ventas"}}])
    assert got.intents[0]["args"]["repeat"] == "weekly" and got.missing == ["when"]


def test_writes_and_reminders_need_their_verb() -> None:
    assert norm("borra todos los productos", [{"tool": "products.set_active", "args": {"name": "todos"}}]).intents == []
    assert norm("redacta una respuesta para un cliente", [{"tool": "reminders.create", "args": {"text": "x", "when": "y"}}]).intents == []
    assert norm("páusala", [{"tool": "promotions.set_active", "args": {"name": "navidad"}}]).intents[0]["args"]["active"] is False


def test_references_come_from_the_message_or_the_context() -> None:
    got = norm("estado del ord-1a2b3c4d", [{"tool": "orders.lookup", "args": {"reference": "1a2b"}}])
    assert got.intents[0]["args"]["reference"] == "ORD-1A2B3C4D"
    ctx = {"last_entity": {"type": "pedido", "name": "ORD-CB07203E"}}
    assert norm("márcalo como enviado", [{"tool": "orders.mark_shipped", "args": {}}], ctx).intents[0]["args"]["reference"] == "ORD-CB07203E"


def test_level_0_rules_cover_time_weather_and_web_but_not_mixed_store_requests() -> None:
    assert route_rules("¿Qué hora es?").intents == [{"tool": "time.now", "args": {}}]
    weather = route_rules("¿Cómo estará el clima mañana en Medellín?").intents[0]
    assert weather == {"tool": "weather.get", "args": {"day": "tomorrow", "place": "medellin"}}
    assert route_rules("investiga la empresa Yankee Candle").intents[0]["tool"] == "web.research"
    assert route_rules("busca tendencias en internet y compáralas con mis ventas") is None
    assert route_rules("hola, ¿cómo estás?") is None


# ------------------------------------------------------------------ fallos de la medición v2 (ThinkCentre)
# Respuestas del modelo como las de la medición v2: el sistema debe corregirlas.

def by_id(case_id: str) -> dict:
    return next(c for c in CASES if c["id"] == case_id)


V2_MODEL_ANSWERS = {
    "prod-05": [{"tool": "products.search", "args": {"query": "vela"}}],
    "amb-07": [{"tool": "orders.count", "args": {}}],
    "amb-08": [{"tool": "sales.summary", "args": {"period": "last_month"}}],
    "amb-09": [],
    "ord-09": [{"tool": "payments.pending", "args": {}}],
    "sal-08": [],
    "inc-01": [{"tool": "orders.lookup", "args": {"reference": "mi pedido"}}],
    "inc-02": [{"tool": "orders.mark_shipped", "args": {"reference": "pedido"}}],
    "inc-03": [{"tool": "reminders.create", "args": {"text": "llamar al proveedor", "when": "hoy"}}],
    "inc-04": [{"tool": "promotions.set_active", "args": {"name": "promoción", "active": False}}],
    "inc-05": [{"tool": "payments.pending", "args": {}}],
    "inc-06": [{"tool": "products.set_active", "args": {"name": "producto", "active": False}}],
    "inc-07": [{"tool": "products.stock", "args": {"query": "stock"}}],
    "inc-08": [],
    "inc-09": [{"tool": "reminders.create", "args": {"text": "revisar todos los pedidos cada día", "when": "todos los días"}}],
    "mix-02": [{"tool": "products.stock", "args": {}}, {"tool": "sales.summary", "args": {"period": "today"}}],
    "mix-03": [{"tool": "products.search", "args": {"query": "lavanda"}}, {"tool": "products.stock", "args": {"query": "lavanda"}}],
    "mix-04": [{"tool": "sales.summary", "args": {"period": "this_month"}}],
    "mix-08": [{"tool": "orders.lookup", "args": {"reference": "ORD-CB07203E"}}],
    "mix-10": [{"tool": "sales.summary", "args": {"period": "yesterday"}}],
    "rem-04": [],
    "rem-05": [{"tool": "sales.summary", "args": {"period": "this_week"}}],
    "rem-08": [{"tool": "reminders.create", "args": {"text": "pagar el arriendo", "when": "primer día de cada mes"}}],
    "chat-08": [{"tool": "sales.summary", "args": {"period": "today"}}],
}


def test_the_rules_fix_the_v2_failures() -> None:
    for case_id, intents in V2_MODEL_ANSWERS.items():
        case = by_id(case_id)
        score = score_case(case["expected"], system(case, json.dumps({"intents": intents})))
        assert score.correct, (case_id, score.notes)


def test_invented_texts_are_dropped_but_context_entities_are_kept() -> None:
    assert norm("¿cuánto stock queda?", [{"tool": "products.stock", "args": {"query": "stock"}}]).missing == ["query"]
    ctx = {"last_entity": {"type": "producto", "name": "Difusor Cítrico"}}
    got = norm("desactívalo", [{"tool": "products.set_active", "args": {}}], ctx)
    assert got.intents[0]["args"] == {"name": "Difusor Cítrico", "active": False} and got.missing == []


def test_reminder_dates_and_texts_come_from_the_message() -> None:
    got = norm("recuérdame el viernes a las 3 de la tarde hacer inventario", [{"tool": "reminders.create", "args": {"when": "el lunes"}}])
    assert got.intents[0]["args"] == {"when": "el viernes a las 3 de la tarde", "text": "hacer inventario", "repeat": "none"}
    got = norm("avísame en dos horas que tengo que cerrar caja", [])
    assert got.intents[0]["args"]["when"] == "en dos horas" and got.intents[0]["args"]["text"] == "cerrar caja"


def test_period_words_keep_analytics_out_of_reminders() -> None:
    # "cada día de la semana pasada" es un periodo de análisis, no una rutina.
    got = norm("ventas de cada día de la semana pasada", [{"tool": "sales.summary", "args": {}}])
    assert [i["tool"] for i in got.intents] == ["sales.summary"] and got.intents[0]["args"]["period"] == "last_week"


def test_scoring_accepts_the_same_stem_but_not_different_numbers() -> None:
    assert value_matches("revisa pedidos", "revisar todos los pedidos")
    assert value_matches("despachar", "despacharlos")
    assert not value_matches("9", "19") and not value_matches("vela", "venta")


# ------------------------------------------------------------------ conjunto de control (frases del usuario)

CONTROL = load_cases(Path(__file__).resolve().parents[1] / "bench" / "control.json")


def test_control_cases_are_valid_and_the_rules_never_spoil_a_perfect_answer() -> None:
    assert len({c["id"] for c in CONTROL}) == len(CONTROL)
    for case in CONTROL:
        for intent in case["expected"]["intents"]:
            errors = [e for e in validate_args(intent["tool"], intent["args"]) if not e.startswith("falta ")]
            assert not errors, (case["id"], errors)
        score = score_case(case["expected"], system(case, answer(case)))
        assert score.correct, (case["id"], score.notes)


def test_accepted_alternatives_count_as_correct_but_unlisted_ones_do_not() -> None:
    expected = {"domain": "lumia_op", "intents": [{"tool": "dashboard.today", "args": {}}], "accept": [["initiatives.list"]]}
    assert score_case(expected, parse_selection(json.dumps({"intents": [{"tool": "initiatives.list", "args": {}}]}))).correct
    assert not score_case(expected, parse_selection(json.dumps({"intents": [{"tool": "orders.recent", "args": {}}]}))).correct
