import re

CITATION_PATTERN = re.compile(r"[ \t]*\[(\d{1,2})\]")


def validate_citations(text: str, source_count: int) -> tuple[str, list[int]]:
    """Valida las citas [n] de la respuesta.

    Las citas que apuntan a fuentes que no existen se eliminan del texto (con el espacio
    que las precede). Devuelve el texto limpio y la lista ordenada de fuentes citadas.
    """
    cited: set[int] = set()

    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if 1 <= index <= source_count:
            cited.add(index)
            return match.group(0)
        return ""

    return CITATION_PATTERN.sub(replace, text).strip(), sorted(cited)
