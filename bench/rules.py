"""Reglas deterministas alrededor del selector: lo que hará el Server en la Fase 2.

1. `route_rules`: nivel 0. Frases inequívocas (hora, clima, buscar en internet, atajos de una
   palabra, resumen del día) se resuelven sin llamar al modelo.
2. `normalize_selection`: después del modelo. El modelo propone QUÉ herramienta usar; el Server:
   - corrige la herramienta cuando el mensaje contiene una señal inequívoca (verbo de recordatorio,
     "sin stock", "los pagos del pedido", pregunta de definición);
   - fija con reglas los valores cerrados (periodos, estados, comparación, modo de búsqueda,
     repetición, referencias, fecha del recordatorio): nunca los toma del modelo;
   - descarta los textos que el modelo inventa (deben aparecer en el mensaje o en el contexto);
   - exige el verbo de las escrituras y calcula los datos que faltan y el dominio.

Mediciones del Qwen 2B (125 casos): 64,8 % (v1) y 80,8 % (v2). Los fallos que quedaban eran casi
todos de argumentos inventados, de valores cerrados o de recordatorios, no de elegir la
herramienta. Estas reglas son generales del español del panel, no ajustes a frases concretas.
"""

from __future__ import annotations

import re

from bench.catalog import INTENTS_BY_NAME, MAX_INTENTS, validate_args
from bench.selector import Selection, normalize, same_word

# ---------------------------------------------------------------- vocabulario

STORE_WORDS = re.compile(r"\b(ventas?|vend\w*|pedidos?|ordenes?|productos?|stock|inventario|mis|nuestr\w*|tienda|catalogo)\b")
EXPLAIN_WORDS = re.compile(r"\b(por que|porque|compar\w*|analiz\w*|explica\w*|mas o menos|anterior)\b")

PERIOD_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bultimos? 90 dias\b|\bultimos noventa dias\b|\bultimo trimestre\b"), "last_90_days"),
    (re.compile(r"\bultimos? 30 dias\b|\bultimos treinta dias\b|\bultimo mes\b"), "last_30_days"),
    (re.compile(r"\bultimos? 7 dias\b|\bultimos siete dias\b"), "last_7_days"),
    (re.compile(r"\bsemana pasada\b|\bla semana anterior\b"), "last_week"),
    (re.compile(r"\bmes pasado\b|\bel mes anterior\b"), "last_month"),
    (re.compile(r"\besta semana\b|\ben la semana\b"), "this_week"),
    (re.compile(r"\beste mes\b|\bdel mes\b|\ben el mes\b"), "this_month"),
    (re.compile(r"\bayer\b"), "yesterday"),
    (re.compile(r"\bhoy\b|\bdel dia\b"), "today"),
]

STATUS_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bpor despachar\b|\bsin despachar\b|\bpor enviar\b|\bpagados?\b"), "paid"),
    (re.compile(r"\bpor pagar\b|\bsin pagar\b|\bpendientes? de pago\b"), "pending"),
    (re.compile(r"\ben camino\b|\benviados?\b"), "shipped"),
    (re.compile(r"\bentreg\w*\b"), "delivered"),
    (re.compile(r"\bcancel\w*\b"), "cancelled"),
    (re.compile(r"\bvencidos?\b|\bexpirados?\b"), "expired"),
    (re.compile(r"\bpendientes?\b|\babiertos?\b|\ben curso\b"), "open"),
]

WEEKDAYS = r"(?:lunes|martes|miercoles|jueves|viernes|sabados?|domingos?)"
REPEAT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:de )?cada mes\b|\bmensual\w*\b"), "monthly"),
    (re.compile(rf"\bcada semana\b|\bsemanal\w*\b|\bcada {WEEKDAYS}\b|\blos {WEEKDAYS}\b"), "weekly"),
    (re.compile(r"\btodos los dias\b|\bcada dia\b|\bdiari\w*\b"), "daily"),
]

SEARCH_MODE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bexact\w*|\btal cual\b|\bliteral\w*"), "exact"),
    (re.compile(r"\bconteng\w*|\bcontien\w*|\bcon la palabra\b|\bla palabra\b|\bque diga\w*|\bincluy\w*"), "contains"),
]
QUOTED = re.compile(r"[\"«“”»]")

ORDER_REF = re.compile(r"\b(ord-[0-9a-f]{8}(?:-[0-9a-f]{0,8})?|ord-\d{8}-\d{6}-[0-9a-f]{8})\b", re.I)
SINGLE_ORDER = re.compile(r"\b(pedido|orden)\b")

# Una escritura o un recordatorio solo se aceptan si el mensaje contiene su verbo: el modelo no
# puede convertir "borra todos los productos" en "desactivar producto".
WRITE_VERBS = {
    "products.set_active": re.compile(r"\b(des|re)?activ\w*|\bapag\w*|\bocult\w*|\bretir\w*"),
    "promotions.set_active": re.compile(r"\bpaus\w*|\breanud\w*|\b(des|re)?activ\w*|\bdeten\w*"),
    "orders.mark_shipped": re.compile(r"\benvi\w*|\bdespach\w*|\bsalio\b"),
    "reminders.create": re.compile(
        r"\brecuerd\w*|\brecordar\w*|\bavis(?:ame|eme|arme|anos|ale|ales)\b|\balarma\b|\brecordatorio\b|\bagenda\w*|\bprogram\w*|\btarea\b|\bcada\b|\btodos los dias\b|\bdiari\w*|\bsemanal\w*|\bmensual\w*"
    ),
}
DEACTIVATE = re.compile(r"\bdesactiv\w*|\bpaus\w*|\bapag\w*|\bocult\w*|\bdeten\w*|\bretir\w*")

# Verbos que piden un recordatorio sin ambigüedad ("recuérdame", "avísame", "ponme una alarma").
# "avísame" sí; "avisos" (las notificaciones) no.
REMINDER_VERB = re.compile(r"\brecuerd\w*|\brecordar\w*|\bavis(?:ame|eme|arme|anos|ale|ales)\b|\balarma\b|\brecordatorio\b")
LOW_STOCK = re.compile(r"\bsin stock\b|\bagot\w*|\bpoco stock\b|\bbajo stock\b|\bse (?:esta|estan) acabando\b|\bpor acabarse\b|\breponer\b")
PAYMENTS = re.compile(r"\bpagos?\b")
MARKET = re.compile(r"\b(mercado|competencia|tendencias?)\b")
SALES = re.compile(r"\bventas?\b|\bvend\w*|\bfactur\w*")
# "¿Qué es un ticket promedio?": pide una definición, no datos de la tienda.
DEFINITION = re.compile(r"^(?:que es(?! lo)|que son|que significa|que quiere decir|define|definicion de)\b")

COMPARED_PERIOD = re.compile(
    r"\b(con|que|vs|frente a|contra) (el |la |lo de la |lo del )?(mes |semana |periodo )?(anterior|pasad[oa])\b"
)

# Fecha y hora de un recordatorio, tal como las dice el usuario.
NUM_WORDS = r"(?:\d+|un|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|quince|veinte|treinta|media)"
HOUR_RE = re.compile(
    r"\ba (?:eso de )?las? \d{1,2}(?::\d{2})?(?: (?:de la )?(?:manana|tarde|noche)| ?(?:am|pm))?\b|\bal mediodia\b|\ba medianoche\b"
)
DAY_RE = re.compile(
    rf"\b(?:pasado manana|manana|hoy|esta (?:noche|tarde)|el (?:proximo )?{WEEKDAYS}|(?:en|dentro de) {NUM_WORDS} (?:minutos?|horas?|dias?|semanas?)"
    r"|el (?:primer|ultimo) dia|el dia \d{1,2}|el \d{1,2} de [a-z]+)\b"
)
TIME_WORDS = re.compile(r"\d|\b(?:manana|hoy|tarde|noche|mediodia|medianoche|hora|horas|minutos?|dias?|semana|mes|lunes|martes|miercoles|jueves|viernes|sabado|domingo)\b")
REMINDER_PHRASE = re.compile(
    r"\b(?:recuerdame|recordarme|recuerdeme|avisame|aviseme|(?:ponme|pon|crea|creame) una alarma"
    r"|agenda\w*(?: un recordatorio| una tarea)?|crea\w* (?:un recordatorio|una tarea)|program\w*|anota\w*)\b"
)
LEADING_FILLER = re.compile(r"^(?:(?:que tengo que|que debo|que hay que|que|para|de|a|el|la|y)(?:\s+|$))+")

# Palabras que no identifican nada por sí solas: si el modelo solo pone esto, el dato falta.
GENERIC_WORDS = frozenset(
    "producto productos promocion promociones promo pedido pedidos orden ordenes stock inventario articulo "
    "articulos item cosa algo eso esto este ese esta esa mi mis su sus el la los las lo un una unos unas de "
    "del al a y o que cuanto cuanta cuantos cuantas queda quedan hay tiene tengo para por con en sin me te se le les nos".split()
)

ENTITY_TOOLS = {
    "products.stock": ("producto", "query"),
    "products.set_active": ("producto", "name"),
    "promotions.set_active": ("promocion", "name"),
}


def first_match(rules: list[tuple[re.Pattern[str], str]], text: str) -> str | None:
    return next((value for pattern, value in rules if pattern.search(text)), None)


def strip_repeat(text: str) -> str:
    for pattern, _ in REPEAT_RULES:
        text = pattern.sub(" ", text)
    return " ".join(text.split())


def extract_when(text: str) -> str | None:
    """Fecha y hora del recordatorio sacadas del mensaje ("mañana a las 9 de la mañana")."""
    rest = strip_repeat(text)
    hour = HOUR_RE.search(rest)
    if hour:
        rest = rest[: hour.start()] + " " + rest[hour.end():]
    day = DAY_RE.search(rest)
    parts = [m.group(0) for m in (day, hour) if m]
    return " ".join(parts) or None


def reminder_clause(text: str) -> str:
    """En una petición compuesta, la parte que habla del recordatorio."""
    clauses = [c for c in re.split(r"\s+y\s+", text) if c.strip()]
    for clause in clauses:
        if REMINDER_VERB.search(clause) or REMINDER_PHRASE.search(clause) or first_match(REPEAT_RULES, clause):
            return clause
    return text


def derive_reminder_text(text: str) -> str | None:
    """Qué recordar, con las palabras del usuario: el mensaje sin el verbo, la repetición ni la fecha."""
    clause = strip_repeat(reminder_clause(text))
    clause = HOUR_RE.sub(" ", clause)
    clause = DAY_RE.sub(" ", clause)
    clause = REMINDER_PHRASE.sub(" ", clause)
    clause = LEADING_FILLER.sub("", " ".join(clause.split()))
    return clause or None


def content_words(value: str) -> list[str]:
    return [w for w in normalize(value).split() if w not in GENERIC_WORDS]


def grounded(value: object, text: str, entity_name: str = "", generic_ok: bool = False) -> bool:
    """Un texto del modelo solo vale si alguna de sus palabras significativas aparece en el
    mensaje o en la entidad del contexto. Así se descartan "mi pedido", "producto" o una hora
    que el usuario no dijo. `generic_ok`: para el texto de un recordatorio, "pedidos" sí dice algo."""
    if not isinstance(value, str):
        return False
    words = [w for w in normalize(value).split() if len(w) >= 4] if generic_ok else content_words(value)
    source = text.split() + normalize(entity_name).split()
    return any(same_word(word, other) for word in words for other in source)


# ---------------------------------------------------------------- nivel 0

TIME_RE = re.compile(r"\b(que hora es|me dices la hora|dime la hora|que dia es hoy|que fecha es hoy|fecha de hoy)\b")
WEATHER_RE = re.compile(r"\b(clima|llover|llueve|lluvia|temperatura|pronostico|cuantos grados)\b")
WEB_RE = re.compile(r"\b(busca informacion|buscar informacion|investiga\w*|noticias|averigua\w*|en internet)\b")
PLACE_RE = re.compile(r"\ben ([a-z][a-z ]{2,30}?)(?:\s+(?:hoy|manana)\b|[?.!,]|$)")
DASHBOARD_RE = re.compile(r"(?:como vamos|como va el dia|como va la tienda|como estamos|resumen del dia)(?: hoy)?")

# Atajos de una palabra: abren la vista por defecto de cada módulo.
SHORTCUTS: dict[str, dict] = {
    "pedidos": {"tool": "orders.recent", "args": {}},
    "ordenes": {"tool": "orders.recent", "args": {}},
    "ventas": {"tool": "sales.summary", "args": {"period": "today"}},
    "stock": {"tool": "inventory.low", "args": {}},
    "inventario": {"tool": "inventory.low", "args": {}},
    "notificaciones": {"tool": "notifications.unread", "args": {}},
    "iniciativas": {"tool": "initiatives.list", "args": {}},
}


def route_rules(message: str, context: dict | None = None) -> Selection | None:
    """Hora, clima, internet, atajos y resumen del día sin modelo. Hora, clima e internet no se
    aplican si la frase también habla de la tienda (petición compuesta): esa la decide el selector."""
    text = normalize(message)
    if text in SHORTCUTS:
        return finalize(Selection(None, [dict(SHORTCUTS[text], args=dict(SHORTCUTS[text]["args"]))], [], None, []), text)
    if DASHBOARD_RE.fullmatch(text):
        return finalize(Selection(None, [{"tool": "dashboard.today", "args": {}}], [], None, []), text)
    if STORE_WORDS.search(text):
        return None
    intents: list[dict] = []
    if TIME_RE.search(text):
        intents.append({"tool": "time.now", "args": {}})
    if WEATHER_RE.search(text):
        args: dict = {"day": "tomorrow" if re.search(r"\bmanana\b", text) else "today"}
        place = PLACE_RE.search(text)
        if place:
            args["place"] = place.group(1).strip()
        intents.append({"tool": "weather.get", "args": args})
    if WEB_RE.search(text) and not intents:
        query = WEB_RE.sub(" ", text)
        query = re.sub(r"\b(sobre|de|la|el|las|los|del|en|hoy)\b", " ", query)
        intents.append({"tool": "web.research", "args": {"query": " ".join(query.split()) or text}})
    if not intents:
        return None
    return finalize(Selection(None, intents[:MAX_INTENTS], [], None, []), text)


# ---------------------------------------------------------------- después del modelo

def correct_tools(text: str, message: str, entity: dict, intents: list[dict]) -> list[dict]:
    """Corrige la elección de herramientas cuando el mensaje tiene una señal inequívoca."""
    if DEFINITION.search(text) and not ORDER_REF.search(message):
        return []
    tools = [i["tool"] for i in intents]
    has_ref = bool(ORDER_REF.search(message)) or entity.get("type") == "pedido"

    # "los pagos del pedido X": pagos de un pedido, no la lista global de pagos pendientes.
    if "payments.pending" in tools and (ORDER_REF.search(message) or SINGLE_ORDER.search(text)):
        intents = [{"tool": "orders.payments", "args": {}} if i["tool"] == "payments.pending" else i for i in intents]

    # "productos sin stock" sin un producto concreto: inventario bajo.
    intents = [
        {"tool": "inventory.low", "args": {}}
        if i["tool"] == "products.stock" and LOW_STOCK.search(text) and not grounded(i["args"].get("query"), text)
        else i
        for i in intents
    ]

    # El stock de un producto ya incluye su búsqueda.
    tools = [i["tool"] for i in intents]
    if "products.stock" in tools and "products.search" in tools:
        search = next(i for i in intents if i["tool"] == "products.search")
        intents = [i for i in intents if i["tool"] != "products.search"]
        stock = next(i for i in intents if i["tool"] == "products.stock")
        if not grounded(stock["args"].get("query"), text):
            stock["args"] = {**stock["args"], "query": search["args"].get("query")}

    # Recordatorios: "recuérdame…", o una tarea que se repite ("cada lunes mándame el resumen").
    compound = re.search(r"\s+y\s+", text) is not None
    repeat_only = first_match(REPEAT_RULES, text) and not first_match(PERIOD_RULES, text)
    explicit = REMINDER_VERB.search(text)
    if explicit or repeat_only:
        reminder = next((i for i in intents if i["tool"] == "reminders.create"), {"tool": "reminders.create", "args": {}})
        if compound and explicit:
            # "cuántos pedidos hay por despachar y recuérdame despacharlos": las dos cosas.
            others = [i for i in intents if i["tool"] != "reminders.create"]
            intents = others[: MAX_INTENTS - 1] + [reminder]
        else:
            # Lo que se lee es lo que hará el recordatorio, no algo que haya que ejecutar ahora.
            intents = [reminder]

    # Peticiones compuestas: la segunda parte que el modelo pequeño suele olvidar.
    if compound and len(intents) < MAX_INTENTS:
        tools = {i["tool"] for i in intents}
        additions: list[dict] = []
        if LOW_STOCK.search(text) and "inventory.low" not in tools:
            additions.append({"tool": "inventory.low", "args": {}})
        if PAYMENTS.search(text) and has_ref and not tools & {"orders.payments", "payments.pending"}:
            additions.append({"tool": "orders.payments", "args": {}})
        market = MARKET.search(text)
        if market and "web.research" not in tools:
            additions.append({"tool": "web.research", "args": {"query": market.group(1)}})
        if SALES.search(text) and not tools & {"sales.summary", "dashboard.today"}:
            additions.append({"tool": "sales.summary", "args": {}})
        intents = intents + additions[: MAX_INTENTS - len(intents)]
    return intents


def normalize_selection(message: str, context: dict | None, selection: Selection) -> Selection:
    """Aplica las reglas del Server a la propuesta del modelo: corrige la herramienta cuando hay
    una señal inequívoca, fija los valores cerrados, descarta lo inventado y deduce dominio y
    faltantes."""
    text = normalize(message)
    entity = (context or {}).get("last_entity") or {}
    entity_name = entity.get("name", "")
    entity_type = normalize(entity.get("type", ""))
    intents = [{"tool": i["tool"], "args": dict(i["args"])} for i in selection.intents]
    intents = correct_tools(text, message, entity, intents)

    kept: list[dict] = []
    dropped_write = False
    for intent in intents:
        tool = intent["tool"]
        if any(k["tool"] == tool for k in kept):
            continue
        spec = INTENTS_BY_NAME[tool]
        verb = WRITE_VERBS.get(tool)
        if verb and not verb.search(text):
            dropped_write = True
            continue
        # Solo argumentos del catálogo y con valor (las cadenas vacías cuentan como ausentes).
        args = {
            k: v for k, v in intent["args"].items()
            if k in spec.args and v is not None and not (isinstance(v, str) and not v.strip())
        }
        # Textos: deben salir del mensaje o del contexto. Las enumeraciones se fijan abajo.
        for name, arg in spec.args.items():
            if arg.kind == "str" and name in args and name not in ("reference", "when", "text"):
                if not grounded(args[name], text, entity_name):
                    args.pop(name)
        if tool in ENTITY_TOOLS:
            kind, name = ENTITY_TOOLS[tool]
            if name not in args and entity_type == kind and entity_name:
                args[name] = entity_name
        if "period" in spec.args:
            # "este mes con el mes anterior": el periodo de referencia de la comparación no es el pedido.
            period = first_match(PERIOD_RULES, COMPARED_PERIOD.sub(" ", text))
            args.pop("period", None)
            if period:
                args["period"] = period
            elif spec.args["period"].required:
                args["period"] = "today"
        if "status" in spec.args:
            status = first_match(STATUS_RULES, text)
            args.pop("status", None)
            if status:
                args["status"] = status
        if "mode" in spec.args:
            mode = "exact" if QUOTED.search(message) else first_match(SEARCH_MODE_RULES, text)
            args.pop("mode", None)
            if mode:
                args["mode"] = mode
        if "compare_to" in spec.args:
            if EXPLAIN_WORDS.search(text) and re.search(r"\bcompar\w*|\banterior\b|\bmas o menos\b", text):
                args["compare_to"] = "previous_period"
            else:
                args.pop("compare_to", None)
        if "repeat" in spec.args:
            args["repeat"] = first_match(REPEAT_RULES, text) or "none"
        if tool == "reminders.create":
            when = extract_when(text)
            model_when = args.pop("when", None)
            if when:
                args["when"] = when
            elif isinstance(model_when, str) and TIME_WORDS.search(normalize(model_when)) and grounded(model_when, strip_repeat(text)):
                args["when"] = model_when
            reminder_text = derive_reminder_text(text)
            model_text = args.pop("text", None)
            if reminder_text and content_words(reminder_text):
                args["text"] = reminder_text
            elif grounded(model_text, text, generic_ok=True):
                args["text"] = model_text
        if "reference" in spec.args:
            ref = ORDER_REF.search(message)
            model_ref = args.pop("reference", None)
            if ref:
                args["reference"] = ref.group(1).upper()
            elif entity.get("type") == "pedido" and entity_name:
                args["reference"] = entity_name
            elif isinstance(model_ref, str) and re.search(r"\d", model_ref) and grounded(model_ref, text):
                args["reference"] = model_ref.strip()
        if "active" in spec.args:
            args["active"] = not bool(DEACTIVATE.search(text))
        if tool == "orders.recent":
            args.pop("limit", None)
            count = re.search(r"\b(\d{1,2})\b", text)
            if count:
                args["limit"] = max(1, min(int(count.group(1)), 50))
        kept.append({"tool": tool, "args": args})
    status = final_status(text, selection, kept, dropped_write)
    need = (selection.need or message.strip())[:120] if status == "missing" else ""
    module = selection.module if status != "chat" else "none"
    return finalize(Selection(None, kept, [], None, list(selection.errors), status, need, module), text)


def final_status(text: str, model: Selection, kept: list[dict], dropped_write: bool) -> str:
    """Estado definitivo (lo decide el Server, no el modelo):
    tool: hay herramienta válida · chat: conversación · missing: la tienda no tiene esa capacidad ·
    ambiguous: hay que preguntar. Los datos que faltan (`missing_information`) salen de `finalize`."""
    if kept:
        return "tool"
    if DEFINITION.search(text):
        return "chat"
    # "borra todos los productos": una escritura sin su verbo no se ejecuta; es una capacidad que no existe.
    if dropped_write and STORE_WORDS.search(text):
        return "missing"
    if model.status == "missing":
        return "missing" if model.module != "none" or STORE_WORDS.search(text) else "chat"
    if model.status == "ambiguous":
        return "ambiguous"
    return "chat"


def finalize(selection: Selection, text: str) -> Selection:
    """Datos obligatorios que faltan (los calcula el Server, no el modelo) y dominio."""
    missing: list[str] = []
    for intent in selection.intents:
        for error in validate_args(intent["tool"], intent["args"]):
            if error.startswith("falta "):
                missing.append(error[6:])
    domains = {domain_of(i["tool"], text) for i in selection.intents}
    if not domains:
        domain = "chat"
    elif len(domains) == 1:
        domain = domains.pop()
    else:
        domain = "mixed"
    status = "tool" if selection.intents else selection.status
    return Selection(domain, selection.intents, missing, None, selection.errors, status, selection.need, selection.module)


def domain_of(tool: str, text: str) -> str:
    effect = INTENTS_BY_NAME[tool].effect
    if tool == "reminders.create":
        return "automation"
    if effect == "write":
        return "lumia_action"
    if effect == "external" or tool == "time.now":
        return "external"
    if EXPLAIN_WORDS.search(text):
        return "analysis"
    return "lumia_op"
