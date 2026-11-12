import re
import unicodedata

# Palabras muy frecuentes que no aportan a la comparación de fragmentos.
STOPWORDS = frozenset(
    {
        "the", "and", "for", "that", "with", "from", "this", "are", "was", "were",
        "los", "las", "del", "que", "con", "por", "para", "una", "uno", "como", "sus",
        "son", "fue", "fueron", "esta", "este", "esto", "entre", "sobre", "pero", "tambien",
        "muy", "mas", "hay", "ser", "esta", "estan", "desde", "hasta", "cuando", "donde",
        "solo", "todo", "toda", "todos", "todas", "segun", "otro", "otra", "tras", "ante",
    }
)

URL_PATTERN = re.compile(r"https?://[^\s)\]>\"']+")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
NUMBER_PATTERN = re.compile(r"\d+")


def normalize_text(text: str) -> str:
    """Minúsculas, sin acentos y solo letras y números separados por espacios."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", without_accents).strip()


def content_tokens(text: str) -> set[str]:
    return {t for t in normalize_text(text).split() if len(t) >= 3 and t not in STOPWORDS}


def numbers(text: str) -> set[str]:
    return set(NUMBER_PATTERN.findall(text))


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in SENTENCE_SPLIT.split(text) if part.strip()]


# Líneas más cortas que esto y sin puntuación final suelen ser menús, botones o etiquetas.
SHORT_LINE_CHARS = 25
_ENDS_SENTENCE = re.compile(r"[.!?:;]$")


def clean_page_text(raw: str) -> str:
    """Normaliza espacios, descarta líneas de menú y líneas repetidas. Conserva saltos de línea."""
    seen: set[str] = set()
    kept: list[str] = []
    for line in raw.splitlines():
        line = " ".join(line.split())
        key = normalize_text(line)
        if not key or key in seen:
            continue
        if len(line) < SHORT_LINE_CHARS and not _ENDS_SENTENCE.search(line):
            continue
        seen.add(key)
        kept.append(line)
    return "\n".join(kept)
