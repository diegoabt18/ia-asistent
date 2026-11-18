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

# Contrato con el Server (no cambia): tool: hay herramienta · chat: conversación sin datos ·
# missing: datos o acciones de la tienda que ninguna herramienta hace · ambiguous: no se entiende.
STATUSES = ("tool", "chat", "missing", "ambiguous")
# Lo que ve el modelo (v5). Con "missing", el 2B lo usaba también cuando solo faltaba un dato
# ("¿cómo va mi pedido?") y devolvía cero herramientas. "no_tool" solo puede significar que la
# herramienta no existe. parse_output lo traduce a "missing".
MODEL_STATUSES = ("tool", "chat", "no_tool", "ambiguous")
MODEL_TO_STATUS = {"no_tool": "missing"}
NO_MODULE = "none"
MAX_INTENTS = 2
NEED_MAX_CHARS = 60


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
- module: el módulo de la tienda al que se refiere, o "none".
- status:
  "tool": una herramienta del catálogo da lo que pide, aunque lo diga con otras palabras, de forma coloquial o con faltas de ortografía.
  "chat": conversación, redactar textos, dar ideas o consejos, o una pregunta general que no necesita datos de la tienda.
  "no_tool": pide datos o acciones de la tienda que ninguna herramienta del catálogo puede dar.
  "ambiguous": no se entiende qué quiere.
- intents: 1 o 2 herramientas con "tool"; vacío en los demás casos.
- need: con "no_tool", lo que pide en 3 a 6 palabras; en los demás casos "".

Reglas:
- Si una herramienta hace lo que pide pero falta un dato (qué pedido, qué producto), es "tool" con esa herramienta y sin ese argumento. El sistema le preguntará el dato. Nunca uses "no_tool" porque falte un dato.
- "no_tool" es para lo que ninguna herramienta tiene: exportar o descargar, datos de clientes o usuarios, filtrar por un dato que la herramienta no tiene, modificar datos sin una herramienta que lo haga. No elijas una herramienta que solo se parece.
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
    # Módulo de música: solo aparece si el Server envía sus herramientas (ver app/services/music/commands.py).
    (
        "Si pide poner, escuchar o reproducir música, una canción o un artista, es music.play; "
        "si solo pide buscarla sin reproducir, es music.search.",
        ("music.play", "music.search"),
    ),
)

def _out(module: str, status: str, intents: list[dict[str, Any]] | None = None, need: str = "") -> dict[str, Any]:
    """Salida de ejemplo con las claves en el orden de generación del esquema."""
    return {"module": module, "status": status, "intents": intents or [], "need": need}


# (mensaje, salida). Solo se incluyen si sus herramientas y su módulo están en el catálogo enviado.
# Ninguno copia frases de los bancos de validación ni de control.
EXAMPLES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("hola, buenos días", _out(NO_MODULE, "chat")),
    ("¿tienen velas de coco?", _out("productos", "tool", [{"tool": "products.search", "args": {"query": "velas de coco"}}])),
    ("eucalipto", _out("productos", "tool", [{"tool": "products.search", "args": {"query": "eucalipto"}}])),
    ("cuánto queda del jabón de miel", _out("inventario", "tool", [{"tool": "products.stock", "args": {"query": "jabón de miel"}}])),
    ("¿qué productos se quedaron sin existencias?", _out("inventario", "tool", [{"tool": "inventory.low", "args": {}}])),
    ("recuérdame el martes a las 4 de la tarde pagar la luz", _out("recordatorios", "tool", [{"tool": "reminders.create", "args": {"text": "pagar la luz", "when": "martes a las 4 de la tarde"}}])),
    ("¿cuántos pedidos están por despachar?", _out("pedidos", "tool", [{"tool": "orders.count", "args": {"status": "paid"}}])),
    # Falta un dato: herramienta sin el argumento, nunca "no_tool".
    ("en qué estado está la orden", _out("pedidos", "tool", [{"tool": "orders.lookup", "args": {}}])),
    ("¿qué precio tiene?", _out("productos", "tool", [{"tool": "products.search", "args": {}}])),
    ("¿qué clientes compraron más este año?", _out("clientes", "no_tool", need="clientes que más compran")),
    ("exporta los pedidos de octubre a Excel", _out("pedidos", "no_tool", need="exportar pedidos a Excel")),
    ("¿qué significa margen bruto?", _out(NO_MODULE, "chat")),
    ("ponme algo de Soda Stereo", _out("musica", "tool", [{"tool": "music.play", "args": {"query": "Soda Stereo"}}])),
    ("para la música un momento", _out("musica", "tool", [{"tool": "music.pause", "args": {}}])),
    ("¿qué canción suena?", _out("musica", "tool", [{"tool": "music.current", "args": {}}])),
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
            # El módulo primero: ubicar el área de la tienda antes de decidir si hay herramienta.
            "module": {"type": "string", "enum": [*modules, NO_MODULE]},
            "status": {"type": "string", "enum": list(MODEL_STATUSES)},
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
        },
        "required": ["module", "status", "intents", "need"],
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
    # Acepta la etiqueta del modelo (v5) y la del contrato (informes anteriores).
    label = data.get("status")
    label = MODEL_TO_STATUS.get(label, label) if isinstance(label, str) else None
    status = label if label in STATUSES else None
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
