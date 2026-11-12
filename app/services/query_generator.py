import re

from app.services.text_analysis import normalize_text

# Plantillas deterministas. No se usa el modelo para generar consultas.
TEMPLATES = (
    "{topic}",
    "{topic} estadísticas",
    "{topic} datos recientes",
    "{topic} fuentes oficiales",
)

MIN_TOPIC_WORDS = 2
MAX_TOPIC_WORDS = 10

_LEADING_QUESTION = re.compile(
    r"^(cu[aá]les?\s+(son|fueron|es|fue)|cu[aá]l\s+(es|fue)|qu[eé]\s+(es|son|fue)|"
    r"qui[eé]n(es)?\s+(es|son|fue|fueron)|c[oó]mo\s+(es|son|est[aá])|"
    r"cu[aá]ndo\s+(es|fue|ser[aá])|d[oó]nde\s+(es|est[aá]|se)|por\s+qu[eé]\s+(es|son)|"
    r"cu[aá]nt[oa]s?\s+(son|es|fue|fueron)|hay|qu[eé])\s+",
    re.IGNORECASE,
)
_LEADING_ARTICLE = re.compile(r"^(el|la|los|las|un|una|unos|unas)\s+", re.IGNORECASE)


def _collapse(text: str) -> str:
    """Quita caracteres de control y colapsa espacios, sin alterar el contenido."""
    without_control = "".join(ch for ch in text if ch.isprintable())
    return " ".join(without_control.split())


def _topic(question: str) -> str:
    """Tema de la pregunta: sin signos de pregunta, sin palabra interrogativa ni artículo inicial."""
    topic = _LEADING_QUESTION.sub("", _collapse(question).strip("¿? "), count=1)
    topic = _LEADING_ARTICLE.sub("", topic, count=1)
    words = topic.split()
    return " ".join(words[:MAX_TOPIC_WORDS])


class QueryGenerator:
    """Genera la pregunta original y consultas alternativas por plantilla, sin duplicados."""

    def __init__(self, max_queries: int) -> None:
        self._max_queries = max(1, max_queries)

    def generate(self, question: str) -> list[str]:
        original = _collapse(question)
        candidates = [original]

        topic = _topic(question)
        if len(topic.split()) >= MIN_TOPIC_WORDS:
            display = topic[:1].upper() + topic[1:]
            candidates.extend(template.format(topic=display) for template in TEMPLATES)

        queries: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = normalize_text(candidate)
            if not key or key in seen:
                continue
            seen.add(key)
            queries.append(candidate)
            if len(queries) >= self._max_queries:
                break
        return queries
