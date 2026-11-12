import asyncio
import json
import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura

from app.core.config import Settings
from app.services.text_analysis import clean_page_text
from app.services.url_guard import UnsafeUrlError, assert_public_http_url

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; LocalAIAssistant/0.3)"
MAX_REDIRECTS = 3
MIN_TEXT_CHARS = 200
# Archivos que nunca son HTML: no se descargan, para no gastar una plaza de fuente.
NON_HTML_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".gz",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp3", ".mp4", ".avi", ".mov",
)


def is_html_candidate(url: str) -> bool:
    """Descarta por la ruta de la URL los archivos que claramente no son páginas HTML."""
    return not urlsplit(url).path.lower().endswith(NON_HTML_EXTENSIONS)


@dataclass(frozen=True)
class PageDocument:
    url: str
    domain: str
    title: str
    published_date: str | None
    text: str


async def fetch_html(
    client: httpx.AsyncClient, url: str, *, timeout: float, max_bytes: int
) -> str | None:
    """Descarga HTML validando cada URL (incluidas las redirecciones) contra SSRF."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        await asyncio.to_thread(assert_public_http_url, current)
        async with client.stream(
            "GET",
            current,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            timeout=timeout,
            follow_redirects=False,
        ) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    return None
                current = urljoin(current, location)
                continue
            if response.status_code != 200:
                return None
            if "html" not in response.headers.get("content-type", "").lower():
                return None
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) >= max_bytes:
                    break
            encoding = response.encoding or "utf-8"
            return bytes(buffer[:max_bytes]).decode(encoding, errors="replace")
    return None


def extract_document(html: str, url: str, max_chars: int) -> PageDocument | None:
    """Extrae el contenido principal con Trafilatura. Devuelve None si hay muy poco texto útil."""
    raw = trafilatura.extract(
        html,
        url=url,
        output_format="json",
        with_metadata=True,
        include_comments=False,
        include_tables=False,
    )
    if not raw:
        return None
    data = json.loads(raw)
    text = clean_page_text(data.get("text") or "")
    if len(text) < MIN_TEXT_CHARS:
        return None
    domain = urlsplit(url).hostname or url
    return PageDocument(
        url=url,
        domain=domain,
        title=(data.get("title") or domain).strip(),
        published_date=data.get("date") or None,
        text=text[:max_chars],
    )


async def fetch_document(
    client: httpx.AsyncClient, url: str, settings: Settings
) -> PageDocument | None:
    """Descarga y extrae una página. Cualquier fallo se registra y devuelve None."""
    try:
        html = await fetch_html(
            client,
            url,
            timeout=settings.research_fetch_timeout_seconds,
            max_bytes=settings.research_max_page_bytes,
        )
    except (UnsafeUrlError, httpx.HTTPError, OSError) as exc:
        logger.info("Página omitida (%s): %s", exc.__class__.__name__, url)
        return None
    if not html:
        return None
    try:
        return await asyncio.to_thread(
            extract_document, html, url, settings.research_page_max_chars
        )
    except Exception:  # Trafilatura/lxml pueden fallar con HTML malformado; no debe tumbar la respuesta
        logger.warning("No se pudo extraer contenido de %s", url)
        return None
