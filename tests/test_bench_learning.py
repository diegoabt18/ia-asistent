"""Banco con interpretaciones aprendidas (fase G de Lumi)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from bench.learning import KNOWLEDGE_FILE, Knowledge, normalize_phrase, phrase_similarity
from bench.run import load_cases, parse_args, run_cases, summarize

BENCH = Path(__file__).resolve().parent.parent / "bench"
TRAINING = load_cases()
VALIDATION = load_cases(BENCH / "validation.json")
CONTROL = load_cases(BENCH / "control.json")


def test_normalization_matches_the_server() -> None:
    # Mismos casos que tests/unit/lumi-learning.test.ts del Server.
    assert normalize_phrase("Lumi, por favor dime: ¿Cuánto VENDIMOS hoy?") == "cuanto vendimos hoy"
    assert normalize_phrase("recuérdame a las 9:30") == "recuerdame a 9:30"
    assert phrase_similarity(normalize_phrase("novedades importantes"), normalize_phrase("novedades de hoy")) > 0.3


def test_knowledge_comes_only_from_training_and_never_repeats_validation_or_control() -> None:
    raw = json.loads(KNOWLEDGE_FILE.read_text(encoding="utf-8"))
    training_ids = {c["id"] for c in TRAINING}
    for entry in raw:
        source = entry["source"].removeprefix("variante de ")
        assert source in training_ids, entry
    learned = {normalize_phrase(e["phrase"]) for e in raw}
    held_out = {normalize_phrase(c["message"]) for c in VALIDATION + CONTROL}
    assert not learned & held_out, learned & held_out


def test_exact_match_and_examples() -> None:
    knowledge = Knowledge.load()
    assert knowledge.exact("Lumi, ¿qué productos se están agotando?") == "inventory.low"
    assert knowledge.exact("¿qué productos hay?") is None
    examples = knowledge.examples("qué productos se acabaron en bodega")
    assert 1 <= len(examples) <= 3
    assert all(e["tool"] == "inventory.low" for e in examples)
    # La frase exacta no se manda como ejemplo de sí misma (ya la resuelve la etapa aprendida).
    assert all(normalize_phrase(e["message"]) != normalize_phrase("¿qué me falta reponer?") for e in knowledge.examples("¿qué me falta reponer?"))


def test_runner_resolves_learned_phrases_without_the_model_and_sends_examples() -> None:
    cases = [c for c in TRAINING if c["id"] in {"prod-10", "inc-01", "chat-01"}]
    seen_prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_prompts.append(body["messages"][1]["content"])
        content = json.dumps({"module": "none", "status": "chat", "intents": [], "need": ""})
        return httpx.Response(200, json={"message": {"content": content}, "prompt_eval_count": 900, "eval_count": 20, "done": True})

    args = parse_args(["--knowledge", str(KNOWLEDGE_FILE)])
    args.knowledge_data = Knowledge.load(args.knowledge)  # lo hace main_async

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_cases(client, cases, args, concurrency=1)

    results = asyncio.run(go())
    by_id = {r["id"]: r for r in results}
    assert by_id["prod-10"]["source"] == "aprendido" and by_id["prod-10"]["score"]["correct"]
    # inc-01 aprendido: herramienta correcta y el dato que falta se sigue pidiendo.
    assert by_id["inc-01"]["source"] == "aprendido" and by_id["inc-01"]["got"]["missing"] == ["reference"]
    summary = summarize(results)
    assert summary["resolved_by_learning"] == 2
    assert summary["model_calls"] == len(cases) - 2

