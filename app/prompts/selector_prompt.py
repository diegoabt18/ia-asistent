"""Prompt y esquema del selector de herramientas de Lumi (etapa 2).

El Server de Lumia envía el catálogo (ya filtrado por permisos) y los módulos; aquí se construye
el prompt y el esquema JSON de salida. El banco (`bench/`) usa estas mismas funciones, así que lo
que se mide es exactamente lo que usa producción.

El modelo solo PROPONE: qué herramienta, o que es conversación, o que falta una capacidad. El
Server valida, completa los argumentos y decide el estado final.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any, TypedDict

# tool: hay herramienta · chat: conversación sin datos · missing: datos o acciones de la tienda
# que ninguna herramienta hace · ambiguous: no se entiende qué quiere.
STATUSES = ("tool", "chat", "missing", "ambiguous")
NO_MODULE = "none"
MAX_INTENTS = 2
NEED_MAX_CHARS = 120


class ArgSpec(TypedDict, total=False):
    name: str
    kind: str | list[str]
    required: bool
    description: str


class ToolSpec(TypedDict, total=False):
    name: str
    description: str
    args: list[ArgSpec]


TEMPLATE = """Eres el clasificador de Lumi, el asistente del panel de administración de una tienda.
No respondas al usuario. Devuelve SOLO un JSON con:
- status: "tool" si una herramienta del catálogo hace exactamente lo que pide; "chat" si es conversación, redactar textos, dar ideas o una pregunta general que no necesita datos de la tienda; "missing" si pide datos o acciones de la tienda que ninguna herramienta del catálogo hace; "ambiguous" si no se entiende qué quiere.
- intents: 0 a 2 herramientas del catálogo, solo con status "tool".
- need: con status "missing", qué pide en pocas palabras; en los demás casos "".
- module: el módulo de la tienda al que se refiere, o "none".

Reglas:
- Usa una herramienta solo si hace lo que pide. Si solo se parece (filtrar por un dato que la herramienta no tiene, exportar o descargar, datos de clientes o usuarios), es "missing". Nunca elijas una herramienta parecida.
{rules}- args: solo los del catálogo, con los valores permitidos. Si falta un dato, omite el argumento.
- "ese", "este", "lo", "la": usa la entidad del contexto.

Catálogo:
{catalog}

Módulos: {modules}

Ejemplos:
{examples}"""

# Reglas que solo tienen sentido si la herramienta está en el catálogo enviado.
TOOL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        'Si el mensaje nombra un producto, un aroma o un tipo de producto (vela, difusor, jabón, lavanda, coco...), '
        'aunque sea una sola palabra o una pregunta como "¿tenemos...?", es products.search.',
        ("products.search",),
    ),
    ("Si pregunta cuánto queda o cuántas unidades hay de un producto, es products.stock.", ("products.stock",)),
    ("Si pregunta por qué cambian sus ventas o pide compararlas, usa sales.summary igualmente.", ("sales.summary",)),
)

# (mensaje, salida). Solo se incluyen si sus herramientas y su módulo están en el catálogo enviado.
EXAMPLES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("hola, buenos días", {"status": "chat", "intents": [], "need": "", "module": NO_MODULE}),
    ("¿tienen velas de coco?", {"status": "tool", "intents": [{"tool": "products.search", "args": {"query": "velas de coco"}}], "need": "", "module": "productos"}),
    ("eucalipto", {"status": "tool", "intents": [{"tool": "products.search", "args": {"query": "eucalipto"}}], "need": "", "module": "productos"}),
    ("cuánto queda del jabón de miel", {"status": "tool", "intents": [{"tool": "products.stock", "args": {"query": "jabón de miel"}}], "need": "", "module": "inventario"}),
    ("recuérdame el martes a las 4 de la tarde pagar la luz", {"status": "tool", "intents": [{"tool": "reminders.create", "args": {"text": "pagar la luz", "when": "martes a las 4 de la tarde"}}], "need": "", "module": "recordatorios"}),
    ("¿cuántos pedidos están por despachar?", {"status": "tool", "intents": [{"tool": "orders.count", "args": {"status": "paid"}}], "need": "", "module": "pedidos"}),
    ("en qué estado está la orden", {"status": "tool", "intents": [{"tool": "orders.lookup", "args": {}}], "need": "", "module": "pedidos"}),
    ("¿qué clientes compraron más este año?", {"status": "missing", "intents": [], "need": "clientes que más compran", "module": "clientes"}),
    ("exporta los pedidos de octubre a Excel", {"status": "missing", "intents": [], "need": "exportar pedidos a Excel", "module": "pedidos"}),
    ("¿qué significa margen bruto?", {"status": "chat", "intents": [], "need": "", "module": NO_MODULE}),
)


def _compact(output: dict[str, Any]) -> str:
    return json.dumps(output, ensure_ascii=False, separators=(",", ":"))


def describe_arg(arg: ArgSpec) -> str:
    kind = arg.get("kind", "str")
    kind_text = "|".join(kind) if isinstance(kind, list) else kind
    req = "" if arg.get("required") else "?"
    note = f" ({arg['description']})" if arg.get("description") else ""
    return f"{arg['name']}{req}:{kind_text}{note}"


def catalog_lines(tools: Iterable[ToolSpec]) -> list[str]:
    lines = []
    for tool in tools:
        args = ", ".join(describe_arg(a) for a in tool.get("args", []))
        lines.append(f"- {tool['name']}: {tool['description']}" + (f". args: {args}" if args else ""))
    return lines


def build_system_prompt(tools: Sequence[ToolSpec], modules: Sequence[str]) -> str:
    """Prompt de sistema. Para el mismo catálogo y los mismos módulos es idéntico en cada llamada."""
    names = {t["name"] for t in tools}
    module_set = set(modules)
    rules = "".join(f"- {text}\n" for text, needs in TOOL_RULES if set(needs) <= names)
    examples = []
    for message, output in EXAMPLES:
        if not {i["tool"] for i in output["intents"]} <= names:
            continue
        if output["module"] != NO_MODULE and output["module"] not in module_set:
            continue
        examples.append(f'Mensaje: "{message}"\n{_compact(output)}')
    return (
        TEMPLATE.replace("{rules}", rules)
        .replace("{catalog}", "\n".join(catalog_lines(tools)))
        .replace("{modules}", ", ".join([*modules, NO_MODULE]))
        .replace("{examples}", "\n".join(examples))
    )


def build_user_prompt(message: str, context: str | None = None, examples: Sequence[dict[str, Any]] = ()) -> str:
    """Mensaje del usuario con el contexto mínimo y, si los hay, ejemplos que el usuario ya confirmó."""
    parts = []
    if examples:
        parts.append("Frases que este usuario ya confirmó:")
        for ex in examples:
            parts.append(f'- "{ex["message"]}" → {ex["tool"]} {_compact(ex.get("args") or {})}')
    if context:
        parts.append(f"Contexto: {context}")
    parts.append(f'Mensaje: "{message}"')
    return "\n".join(parts)


def output_schema(tool_names: Sequence[str], modules: Sequence[str]) -> dict[str, Any]:
    """Esquema que se pasa a Ollama en `format`. El orden de las propiedades es el de generación."""
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(STATUSES)},
            "intents": {
                "type": "array",
                "maxItems": MAX_INTENTS,
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "enum": list(tool_names)},
                        "args": {"type": "object"},
                    },
                    "required": ["tool", "args"],
                },
            },
            "need": {"type": "string", "maxLength": NEED_MAX_CHARS},
            "module": {"type": "string", "enum": [*modules, NO_MODULE]},
        },
        "required": ["status", "intents", "need", "module"],
    }


def parse_output(raw: str, tool_names: Iterable[str], modules: Iterable[str]) -> tuple[dict[str, Any], list[str]]:
    """Lee la salida del modelo sin lanzar nunca. Devuelve (selección, errores)."""
    names = set(tool_names)
    module_set = set(modules)
    empty = {"status": "chat", "intents": [], "need": "", "module": NO_MODULE}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return empty, ["JSON inválido"]
    if not isinstance(data, dict):
        return empty, ["la raíz no es un objeto"]
    errors: list[str] = []
    status = data.get("status") if data.get("status") in STATUSES else None
    if status is None:
        errors.append("status inválido")
    intents: list[dict[str, Any]] = []
    raw_intents = data.get("intents")
    if not isinstance(raw_intents, list):
        errors.append("intents no es una lista")
        raw_intents = []
    if len(raw_intents) > MAX_INTENTS:
        errors.append(f"más de {MAX_INTENTS} intenciones")
    for item in raw_intents[:MAX_INTENTS]:
        if not isinstance(item, dict) or item.get("tool") not in names:
            errors.append(f"intención desconocida: {item!r}"[:120])
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        intents.append({"tool": item["tool"], "args": args})
    need = data.get("need") if isinstance(data.get("need"), str) else ""
    module = data.get("module") if data.get("module") in module_set else NO_MODULE
    if status is None:
        status = "tool" if intents else "chat"
    return {"status": status, "intents": intents, "need": need.strip()[:NEED_MAX_CHARS], "module": module}, errors
