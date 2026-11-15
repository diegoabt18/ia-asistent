"""Vuelve a puntuar un informe JSON del banco con las reglas actuales, sin llamar al modelo.

Reutiliza las respuestas crudas (`raw`) que el modelo dio en una medición. Sirve para saber cuánto
cambian las reglas el resultado sin repetir la medición en el ThinkCentre. No sustituye a una
medición nueva: si el prompt cambia, las respuestas del modelo también.

Uso:
    python -m bench.replay bench/results/<informe>.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from bench.rules import normalize_selection, route_rules
from bench.run import load_cases
from bench.selector import Selection, parse_selection, score_case


def replay(report: dict) -> list[dict]:
    cases = {case["id"]: case for case in load_cases()}
    rows = []
    for result in report["results"]:
        case = cases.get(result["id"])
        if case is None:
            continue
        message, context = case["message"], case.get("context")
        routed = route_rules(message, context)
        if routed is not None:
            selection, source = routed, "reglas"
        elif result.get("source") == "reglas" or (not result.get("raw") and not result.get("error")):
            # Antes lo resolvían las reglas de nivel 0 y ahora no: no hay respuesta del modelo.
            rows.append({"id": case["id"], "category": case["category"], "message": message, "source": "sin respuesta", "correct": False, "notes": ["sin respuesta del modelo en el informe"], "before": result["score"]["correct"]})
            continue
        else:
            raw = parse_selection(result["raw"]) if not result.get("error") else Selection(None, [], [], None, [result["error"]])
            selection, source = normalize_selection(message, context, raw), "modelo"
        score = score_case(case["expected"], selection)
        rows.append({
            "id": case["id"], "category": case["category"], "message": message, "source": source,
            "correct": score.correct, "notes": score.notes, "before": result["score"]["correct"],
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    rows = replay(json.loads(args.report.read_text(encoding="utf-8")))
    total = len(rows)
    now = sum(r["correct"] for r in rows)
    before = sum(r["before"] for r in rows)
    print(f"Antes: {before}/{total} ({before / total:.1%}) · Con las reglas actuales: {now}/{total} ({now / total:.1%})")
    by_category: Counter[str] = Counter(r["category"] for r in rows)
    ok_category: Counter[str] = Counter(r["category"] for r in rows if r["correct"])
    for category in sorted(by_category):
        print(f"  {category:18} {ok_category[category]:>2}/{by_category[category]}")
    for label, selected in (("Arreglados", [r for r in rows if r["correct"] and not r["before"]]), ("Empeorados", [r for r in rows if r["before"] and not r["correct"]]), ("Siguen fallando", [r for r in rows if not r["correct"] and not r["before"]])):
        if selected:
            print(f"\n{label}:")
            for r in selected:
                print(f"  {r['id']:8} [{r['source']}] {r['message'][:60]} · {'; '.join(r['notes'])[:160]}")


if __name__ == "__main__":
    main()
