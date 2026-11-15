"""Catálogo de intenciones del banco y esquema de salida del selector.

Refleja las herramientas que existen en el Server (catálogo de la Fase 3) y las que propone la
auditoría de viabilidad para las Fases 2 a 4. En la Fase 2 este catálogo lo enviará el Server,
ya filtrado por los permisos del usuario. Aquí es fijo para que las mediciones sean comparables.

Cada argumento se describe con un tipo simple (`str`, `int`, `bool`) o una lista de valores
permitidos. Los periodos y estados son enumeraciones cerradas: el modelo nunca calcula fechas.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.prompts.selector_prompt import output_schema as app_output_schema

DOMAINS = ("chat", "external", "lumia_op", "lumia_action", "analysis", "automation", "mixed")
MAX_INTENTS = 2

PERIODS = (
    "today",
    "yesterday",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "last_7_days",
    "last_30_days",
    "last_90_days",
)
ORDER_STATUSES = ("pending", "paid", "shipped", "delivered", "cancelled", "expired", "open")
SEARCH_MODES = ("contains", "exact")
REPEATS = ("none", "daily", "weekly", "monthly")


@dataclass(frozen=True)
class Arg:
    """Argumento de una intención. `kind` es str, int, bool o una tupla de valores permitidos."""

    kind: str | tuple[str, ...]
    required: bool = False
    description: str = ""


@dataclass(frozen=True)
class Intent:
    name: str
    description: str
    args: dict[str, Arg] = field(default_factory=dict)
    # Lectura, escritura (siempre con confirmación) o externa. Solo informativo en el banco.
    effect: str = "read"


INTENTS: tuple[Intent, ...] = (
    Intent("products.search", "Buscar productos por nombre", {
        "query": Arg("str", True, "texto a buscar, sin palabras como 'busca' o 'producto'"),
        "mode": Arg(SEARCH_MODES, False, "exact si el usuario da el nombre completo entre comillas o dice 'exactamente'"),
    }),
    Intent("products.stock", "Existencias de un producto concreto", {
        "query": Arg("str", True, "nombre del producto; si dice 'ese' o 'este', el del contexto"),
    }),
    Intent("inventory.low", "Productos agotados, sin stock, con poco stock o por reponer"),
    Intent("orders.lookup", "Estado de un pedido por su referencia (ORD-...)", {
        "reference": Arg("str", True, "referencia tal como la escribe el usuario"),
    }),
    Intent("orders.count", "Contar pedidos por estado o periodo", {
        "status": Arg(ORDER_STATUSES),
        "period": Arg(PERIODS),
    }),
    Intent("orders.recent", "Listar los últimos pedidos", {"limit": Arg("int")}),
    Intent("orders.payments", "Pagos registrados de un pedido", {"reference": Arg("str", True)}),
    Intent("payments.pending", "Pedidos con pagos pendientes de confirmar"),
    Intent("sales.summary", "Ventas, ticket promedio y más vendidos de un periodo", {
        "period": Arg(PERIODS, True),
        "compare_to": Arg(("previous_period",), False, "si pide comparar con el periodo anterior"),
    }),
    Intent("dashboard.today", "Resumen del día: ventas, pedidos por despachar, iniciativas urgentes"),
    Intent("production.approvals.pending", "Precios o recetas pendientes de aprobación"),
    Intent("initiatives.list", "Iniciativas abiertas de Lumi", {"urgent_only": Arg("bool")}),
    Intent("notifications.unread", "Notificaciones sin leer"),
    # Una sola intención para recordatorios puntuales y repetidos: con dos intenciones parecidas el
    # modelo pequeño elegía la puntual casi siempre (Fase 1, primera medición). `repeat` lo fijan
    # las reglas ("todos los días", "cada lunes").
    Intent("reminders.create", "Crear un recordatorio o una tarea, puntual o que se repite", {
        "text": Arg("str", True, "qué recordar"),
        "when": Arg("str", True, "fecha u hora tal como las dice el usuario"),
        "repeat": Arg(REPEATS),
    }, effect="write"),
    Intent("time.now", "Fecha y hora actuales"),
    Intent("weather.get", "Clima de un lugar", {
        "place": Arg("str"),
        "day": Arg(("today", "tomorrow")),
    }, effect="external"),
    Intent("web.research", "Buscar información actual en internet", {"query": Arg("str", True)}, effect="external"),
    Intent("orders.mark_shipped", "Marcar un pedido como enviado", {"reference": Arg("str", True)}, effect="write"),
    Intent("promotions.set_active", "Pausar o reanudar una promoción", {
        "name": Arg("str", True),
        "active": Arg("bool", True),
    }, effect="write"),
    Intent("products.set_active", "Activar o desactivar un producto", {
        "name": Arg("str", True),
        "active": Arg("bool", True),
    }, effect="write"),
)

INTENTS_BY_NAME = {intent.name: intent for intent in INTENTS}


# Módulos de la tienda: el modelo indica a cuál se refiere una petición (sobre todo cuando falta la
# herramienta: así el Server registra la capacidad en su módulo).
MODULES = (
    "productos", "inventario", "pedidos", "pagos", "ventas", "clientes", "promociones", "banners",
    "categorias", "resenas", "produccion", "usuarios", "configuracion", "recordatorios",
    "notificaciones", "iniciativas",
)


def tool_specs() -> list[dict]:
    """El catálogo en el formato que envía el Server a /select (app/schemas/select.py)."""
    return [
        {
            "name": intent.name,
            "description": intent.description,
            "args": [
                {"name": name, "kind": list(arg.kind) if isinstance(arg.kind, tuple) else arg.kind,
                 "required": arg.required, "description": arg.description}
                for name, arg in intent.args.items()
            ],
        }
        for intent in INTENTS
    ]


def output_schema() -> dict:
    """Esquema de salida de producción (app/prompts/selector_prompt.py) para este catálogo."""
    return app_output_schema([i.name for i in INTENTS], MODULES)


def validate_args(tool: str, args: object) -> list[str]:
    """Errores de los argumentos frente al catálogo (lo que haría el Server antes de ejecutar)."""
    intent = INTENTS_BY_NAME.get(tool)
    if intent is None:
        return [f"intención desconocida: {tool}"]
    if not isinstance(args, dict):
        return ["args no es un objeto"]
    errors = []
    for name, arg in intent.args.items():
        value = args.get(name)
        if value is None:
            if arg.required:
                errors.append(f"falta {name}")
            continue
        if isinstance(arg.kind, tuple):
            if value not in arg.kind:
                errors.append(f"{name}={value!r} fuera de {arg.kind}")
        elif arg.kind == "int" and not (isinstance(value, int) and not isinstance(value, bool)):
            errors.append(f"{name} no es entero")
        elif arg.kind == "bool" and not isinstance(value, bool):
            errors.append(f"{name} no es booleano")
        elif arg.kind == "str" and not (isinstance(value, str) and value.strip()):
            errors.append(f"{name} vacío")
    for name in args:
        if name not in intent.args:
            errors.append(f"argumento no permitido: {name}")
    return errors
