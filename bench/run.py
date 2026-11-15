"""Ejecuta el banco contra un Ollama real y escribe un informe (JSON + Markdown).

Uso (desde la carpeta local-ai-assistant):

    python -m bench.run --base-url http://127.0.0.1:11434 --model qwen3.5:2b-q4_K_M

Opciones útiles:
    --limit 10            solo los N primeros casos (prueba rápida)
    --category pedidos    solo una categoría
    --concurrency 2       además de la pasada secuencial, mide con N llamadas simultáneas
    --skip-cancel         no ejecutar la prueba de cancelación
    --omit-think          no enviar `think` (modelos sin razonamiento, p. ej. llama3)

Solo lee: no toca la API del asistente, ni bases de datos, ni la tienda. Las respuestas del modelo
se guardan en bench/results/ (carpeta ignorada por Git).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

import httpx

from bench.catalog import output_schema
from bench.rules import finalize, normalize_selection, route_rules
from bench.selector import Selection, build_messages, normalize, parse_selection, score_case

HERE = Path(__file__).parent
CASES_FILE = HERE / "cases.json"
RESULTS_DIR = HERE / "results"
# Fecha fija: así "hoy" y "esta semana" no cambian entre ejecuciones y los informes son comparables.
BENCH_TODAY = date(2026, 11, 16)


@dataclass
class CallResult:
    raw: str
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    load_ms: float
    error: str | None = None


def load_cases(path: Path = CASES_FILE) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def request_body(messages: list[dict], args: argparse.Namespace, stream: bool = False) -> dict:
    body = {
        "model": args.model,
        "messages": messages,
        "stream": stream,
        "format": output_schema(),
        "keep_alive": args.keep_alive,
        "options": {"temperature": 0, "num_predict": args.num_predict, "num_ctx": args.num_ctx},
    }
    if not args.omit_think:
        # Clasificar no necesita razonamiento: sin thinking, menos tokens y menos latencia.
        body["think"] = False
    return body


async def call_model(client: httpx.AsyncClient, messages: list[dict], args: argparse.Namespace) -> CallResult:
    start = time.perf_counter()
    try:
        r = await client.post(f"{args.base_url}/api/chat", json=request_body(messages, args), timeout=args.timeout)
        r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        return CallResult("", (time.perf_counter() - start) * 1000, 0, 0, 0, f"{type(exc).__name__}: {exc}"[:300])
    return CallResult(
        raw=body.get("message", {}).get("content", ""),
        latency_ms=(time.perf_counter() - start) * 1000,
        prompt_tokens=int(body.get("prompt_eval_count") or 0),
        completion_tokens=int(body.get("eval_count") or 0),
        load_ms=(body.get("load_duration") or 0) / 1e6,
    )


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


async def run_cases(client: httpx.AsyncClient, cases: list[dict], args: argparse.Namespace, concurrency: int) -> list[dict]:
    slots = asyncio.Semaphore(concurrency)

    async def one(case: dict) -> dict:
        message, context = case["message"], case.get("context")
        # Nivel 0: hora, clima e internet sin modelo.
        routed = route_rules(message, context)
        if routed is not None:
            score = score_case(case["expected"], routed)
            return {
                "id": case["id"], "category": case["category"], "message": message, "expected": case["expected"],
                "source": "reglas",
                "got": {"domain": routed.domain, "intents": routed.intents, "missing": routed.missing},
                "raw": "", "score": {**asdict(score), "correct": score.correct}, "model_score": None,
                "latency_ms": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "error": None,
            }
        messages = build_messages(message, context, BENCH_TODAY)
        async with slots:
            result = await call_model(client, messages, args)
        raw = parse_selection(result.raw) if not result.error else Selection(None, [], [], None, [result.error])
        # Modelo solo: sus intenciones y argumentos tal cual (dominio y faltantes deducidos).
        model_score = score_case(case["expected"], finalize(raw, normalize(message)))
        # Sistema: lo que hará el Server (reglas sobre la propuesta del modelo).
        selection = normalize_selection(message, context, raw)
        score = score_case(case["expected"], selection)
        return {
            "id": case["id"],
            "category": case["category"],
            "message": case["message"],
            "expected": case["expected"],
            "source": "modelo",
            "got": {"domain": selection.domain, "intents": selection.intents, "missing": selection.missing},
            "raw": result.raw[:2000],
            "score": {**asdict(score), "correct": score.correct},
            "model_score": {**asdict(model_score), "correct": model_score.correct},
            "latency_ms": round(result.latency_ms, 1),
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "error": result.error,
        }

    return list(await asyncio.gather(*(one(c) for c in cases)))


async def cache_probe(client: httpx.AsyncClient, args: argparse.Namespace) -> dict:
    """Dos llamadas con el mismo prefijo. Si Ollama reutiliza su caché, la segunda procesa muchos
    menos tokens de entrada (prompt_eval_count) y tarda menos."""
    first = await call_model(client, build_messages("¿cuántos pedidos hay hoy?", None, BENCH_TODAY), args)
    second = await call_model(client, build_messages("¿cuántos pedidos hubo ayer?", None, BENCH_TODAY), args)
    return {
        "first_prompt_tokens": first.prompt_tokens,
        "second_prompt_tokens": second.prompt_tokens,
        "first_ms": round(first.latency_ms, 1),
        "second_ms": round(second.latency_ms, 1),
        "prefix_cache_works": second.prompt_tokens < first.prompt_tokens * 0.5,
    }


async def cancellation_test(client: httpx.AsyncClient, args: argparse.Namespace) -> dict:
    """Abre una respuesta en streaming, la corta tras el primer fragmento y mide la siguiente llamada.

    Si Ollama libera la generación al cortarse la conexión, la siguiente llamada tarda lo normal.
    """
    messages = build_messages("Hola, ¿cómo estás?", None, BENCH_TODAY)
    long_body = request_body(messages, args, stream=True)
    long_body.pop("format", None)
    long_body["options"] = {**long_body["options"], "num_predict": 400}
    first_chunk_ms = None
    start = time.perf_counter()
    try:
        async with client.stream("POST", f"{args.base_url}/api/chat", json=long_body, timeout=args.timeout) as r:
            async for line in r.aiter_lines():
                if line.strip():
                    first_chunk_ms = (time.perf_counter() - start) * 1000
                    break
    except httpx.HTTPError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}
    after = await call_model(client, build_messages("pedidos por despachar", None, BENCH_TODAY), args)
    return {
        "first_chunk_ms": round(first_chunk_ms or 0, 1),
        "next_call_ms_after_cancel": round(after.latency_ms, 1),
        "next_call_error": after.error,
    }


def summarize(results: list[dict]) -> dict:
    total = len(results)
    ok = [r for r in results if r["score"]["correct"]]
    by_model = [r for r in results if r["source"] == "modelo"]
    latencies = [r["latency_ms"] for r in by_model if not r["error"]]
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)
    return {
        "cases": total,
        "correct": len(ok),
        "accuracy": round(len(ok) / total, 4) if total else 0,
        "intent_accuracy": round(sum(r["score"]["intent_ok"] for r in results) / total, 4) if total else 0,
        "domain_accuracy": round(sum(r["score"]["domain_ok"] for r in results) / total, 4) if total else 0,
        "json_valid_rate": round(sum(r["score"]["json_valid"] for r in results) / total, 4) if total else 0,
        "errors": sum(1 for r in results if r["error"]),
        "resolved_by_rules": sum(1 for r in results if r["source"] == "reglas"),
        "model_calls": len(by_model),
        "model_only_accuracy": round(sum(r["model_score"]["correct"] for r in by_model) / len(by_model), 4) if by_model else 0,
        "latency_p50_ms": round(percentile(latencies, 0.5), 1),
        "latency_p95_ms": round(percentile(latencies, 0.95), 1),
        "prompt_tokens_avg": round(statistics.mean(r["prompt_tokens"] for r in by_model), 1) if by_model else 0,
        "completion_tokens_avg": round(statistics.mean(r["completion_tokens"] for r in by_model), 1) if by_model else 0,
        "by_category": {
            cat: {"cases": len(rs), "correct": sum(r["score"]["correct"] for r in rs)} for cat, rs in sorted(by_cat.items())
        },
        "failure_kinds": dict(Counter(
            "json" if not r["score"]["json_valid"] else "intención" if not r["score"]["intent_ok"]
            else "argumentos" if not r["score"]["args_ok"] else "missing"
            for r in results if not r["score"]["correct"]
        )),
    }


def markdown_report(meta: dict, summary: dict, concurrency: dict | None, cancel: dict | None, results: list[dict], cache: dict | None = None) -> str:
    lines = [
        f"# Banco del selector — {meta['model']}",
        "",
        f"- Fecha: {meta['started_at']} · Ollama: {meta['base_url']} · num_ctx {meta['num_ctx']} · think {'no enviado' if meta['omit_think'] else 'false'}",
        f"- Casos: {summary['cases']} · **Acierto del sistema (reglas + modelo + normalización): {summary['accuracy']:.1%}** (meta ≥ 90 %)",
        f"- Resueltos por reglas sin modelo: {summary['resolved_by_rules']} · Llamadas al modelo: {summary['model_calls']} · Acierto del modelo solo (sin normalizar): {summary['model_only_accuracy']:.1%}",
        f"- Intención correcta: {summary['intent_accuracy']:.1%} · Dominio: {summary['domain_accuracy']:.1%} · JSON válido: {summary['json_valid_rate']:.1%} · Errores: {summary['errors']}",
        f"- Latencia de las llamadas al modelo p50 / p95: {summary['latency_p50_ms']:.0f} / {summary['latency_p95_ms']:.0f} ms",
        f"- Tokens medios: entrada {summary['prompt_tokens_avg']:.0f} · salida {summary['completion_tokens_avg']:.0f}",
        f"- Carga inicial del modelo: {meta['warmup_load_ms']:.0f} ms",
        "",
        "| Categoría | Casos | Correctos | % |",
        "|---|---|---|---|",
    ]
    for cat, v in summary["by_category"].items():
        lines.append(f"| {cat} | {v['cases']} | {v['correct']} | {v['correct'] / v['cases']:.0%} |")
    if concurrency:
        lines += ["", f"**Concurrencia {concurrency['level']}** ({concurrency['cases']} casos): p50 {concurrency['p50_ms']:.0f} ms · p95 {concurrency['p95_ms']:.0f} ms · tiempo total {concurrency['wall_ms']:.0f} ms (secuencial equivalente {concurrency['sequential_ms']:.0f} ms) · errores {concurrency['errors']}"]
    if cache:
        lines += ["", f"**Caché de prefijo**: {json.dumps(cache, ensure_ascii=False)}"]
    if cancel:
        lines += ["", f"**Cancelación**: {json.dumps(cancel, ensure_ascii=False)}"]
    fails = [r for r in results if not r["score"]["correct"]]
    if fails:
        lines += ["", "## Fallos", "", "| Caso | Mensaje | Problema |", "|---|---|---|"]
        lines[-2] = "| Caso | Origen | Mensaje | Problema |"
        lines[-1] = "|---|---|---|---|"
        for r in fails:
            note = "; ".join(r["score"]["notes"])[:200].replace("|", "/")
            lines.append(f"| {r['id']} | {r['source']} | {r['message'][:70].replace('|', '/')} | {note} |")
    return "\n".join(lines) + "\n"


async def main_async(args: argparse.Namespace) -> int:
    cases = load_cases()
    if args.category:
        cases = [c for c in cases if c["category"] == args.category]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("No hay casos que ejecutar.")
        return 1

    async with httpx.AsyncClient() as client:
        tags = (await client.get(f"{args.base_url}/api/tags", timeout=10)).json()
        names = [m["name"] for m in tags.get("models", [])]
        if args.model not in names:
            print(f"El modelo {args.model} no está instalado en {args.base_url}. Disponibles: {names}")
            return 2

        warm = await call_model(client, build_messages("hola", None, BENCH_TODAY), args)
        started = datetime.now().isoformat(timespec="seconds")
        print(f"Calentamiento: {warm.latency_ms:.0f} ms (carga {warm.load_ms:.0f} ms){' · ERROR ' + warm.error if warm.error else ''}")

        results = await run_cases(client, cases, args, concurrency=1)
        summary = summarize(results)
        print(
            f"Sistema: {summary['correct']}/{summary['cases']} correctos ({summary['accuracy']:.1%}) · "
            f"modelo solo {summary['model_only_accuracy']:.1%} · reglas {summary['resolved_by_rules']} · "
            f"p50 {summary['latency_p50_ms']:.0f} ms, p95 {summary['latency_p95_ms']:.0f} ms"
        )

        concurrency = None
        if args.concurrency > 1:
            subset = cases[: min(len(cases), args.concurrency * 5)]
            start = time.perf_counter()
            parallel = await run_cases(client, subset, args, concurrency=args.concurrency)
            wall = (time.perf_counter() - start) * 1000
            lat = [r["latency_ms"] for r in parallel if not r["error"]]
            seq = {r["id"]: r["latency_ms"] for r in results}
            concurrency = {
                "level": args.concurrency,
                "cases": len(subset),
                "p50_ms": percentile(lat, 0.5),
                "p95_ms": percentile(lat, 0.95),
                "wall_ms": wall,
                "sequential_ms": sum(seq[c["id"]] for c in subset),
                "errors": sum(1 for r in parallel if r["error"]),
            }
            print(f"Concurrencia {args.concurrency}: p50 {concurrency['p50_ms']:.0f} ms, total {wall:.0f} ms")

        cache = await cache_probe(client, args)
        print(f"Caché de prefijo: {cache}")
        cancel = None if args.skip_cancel else await cancellation_test(client, args)
        if cancel:
            print(f"Cancelación: {cancel}")

    meta = {
        "model": args.model,
        "base_url": args.base_url,
        "num_ctx": args.num_ctx,
        "num_predict": args.num_predict,
        "omit_think": args.omit_think,
        "started_at": started,
        "bench_today": BENCH_TODAY.isoformat(),
        "warmup_load_ms": warm.load_ms,
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    stem = f"{datetime.now():%Y%m%d-%H%M%S}-{args.model.replace(':', '_').replace('/', '_')}"
    (RESULTS_DIR / f"{stem}.json").write_text(
        json.dumps({"meta": meta, "summary": summary, "concurrency": concurrency, "cache": cache, "cancel": cancel, "results": results}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    (RESULTS_DIR / f"{stem}.md").write_text(markdown_report(meta, summary, concurrency, cancel, results, cache), encoding="utf-8")
    print(f"Informe: {RESULTS_DIR / (stem + '.md')}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Banco del selector de intenciones de Lumi")
    p.add_argument("--base-url", default="http://127.0.0.1:11434")
    p.add_argument("--model", default="qwen3.5:2b-q4_K_M")
    p.add_argument("--num-ctx", type=int, default=4096)
    p.add_argument("--num-predict", type=int, default=200)
    p.add_argument("--keep-alive", default="5m")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--category", default="")
    p.add_argument("--skip-cancel", action="store_true")
    p.add_argument("--omit-think", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
