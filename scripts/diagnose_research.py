"""Diagnóstico de la fase de descarga de /research.

Repite las consultas reales, aplica el mismo ranking y, para cada página, compara la extracción
con y sin clean_page_text. Solo lectura: no guarda nada ni modifica la base de datos.

Uso (dentro del contenedor, con el código actual):
    python diagnose_research.py "¿pregunta a probar?"
"""

import asyncio
import json
import sys

import httpx
import trafilatura

from app.core.config import get_settings
from app.services.query_generator import QueryGenerator
from app.services.search_service import SearchService
from app.services.source_ranking import rank_and_dedupe
from app.services.text_analysis import clean_page_text
from app.services.web_extraction_service import MIN_TEXT_CHARS, fetch_html


def extract_raw(html: str, url: str) -> str:
    raw = trafilatura.extract(
        html, url=url, output_format="json", with_metadata=True,
        include_comments=False, include_tables=False,
    )
    if not raw:
        return ""
    return json.loads(raw).get("text") or ""


async def main(question: str) -> None:
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        search = SearchService(settings, client)
        queries = QueryGenerator(settings.research_max_queries).generate(question)
        print(f"Consultas ({len(queries)}): {queries}\n")

        found = []
        for q in queries:
            try:
                found.extend(await search.search(q, limit=10))
            except Exception as exc:  # noqa: BLE001 - diagnóstico
                print(f"[búsqueda fallida] {q!r}: {exc!r}")
        ranked = rank_and_dedupe(found, max_total=settings.research_max_sources)
        print(f"Candidatas tras ranking ({len(ranked)}):\n")

        for result in ranked:
            url = result.url
            print(f"URL: {url}")
            try:
                html = await fetch_html(
                    client, url,
                    timeout=settings.research_fetch_timeout_seconds,
                    max_bytes=settings.research_max_page_bytes,
                )
            except Exception as exc:  # noqa: BLE001 - diagnóstico
                print(f"  descarga: ERROR {exc.__class__.__name__}: {exc}\n")
                continue
            if not html:
                print("  descarga: sin HTML (no 200, no text/html, redirección o vacía)\n")
                continue
            raw_text = await asyncio.to_thread(extract_raw, html, url)
            cleaned = clean_page_text(raw_text)
            print(f"  html: {len(html)} bytes")
            print(f"  texto sin limpiar: {len(raw_text)} caracteres -> pasa MIN ({MIN_TEXT_CHARS}): {len(raw_text) >= MIN_TEXT_CHARS}")
            print(f"  texto con clean_page_text: {len(cleaned)} caracteres -> pasa MIN: {len(cleaned) >= MIN_TEXT_CHARS}")
            if len(raw_text) >= MIN_TEXT_CHARS and len(cleaned) < MIN_TEXT_CHARS:
                print("  >>> clean_page_text descarta esta página que sí tenía texto")
            print()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "¿cuantos grados farenheit se necesita para derretir una barrilla de acero?"))
