"""Reglas deterministas alrededor del selector: lo que hará el Server en la Fase 2.

1. `route_rules`: nivel 0. Frases inequívocas (hora, clima, buscar en internet) se resuelven sin
   llamar al modelo.
2. `normalize_selection`: después del modelo. El modelo solo propone QUÉ herramienta usar; el
   Server fija con reglas los valores cerrados (periodos, estados, comparación, repetición,
   referencias), descarta argumentos que no existen, exige el verbo de las escrituras y calcula
   qué datos obligatorios faltan y el dominio.

Primera medición del Qwen 2B (125 casos): 64,8 %. Los fallos eran casi todos de este tipo
(enumeraciones, `missing`, rutinas, escrituras inventadas, hora y clima), no de elegir la
herramienta. Estas reglas son generales del español del panel, no ajustes a frases concretas.
"""

from __future__ import annotations

import re

from bench.catalog import INTENTS_BY_NAME, validate_args
from bench.selector import Selection, normalize

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

REPEAT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bcada mes\b|\bmensual\w*\b|\bde cada mes\b"), "monthly"),
    (re.compile(r"\bcada semana\b|\bsemanal\w*\b|\bcada (lunes|martes|miercoles|jueves|viernes|sabado|domingo)\b|\blos (lunes|martes|miercoles|jueves|viernes|sabados|domingos)\b"), "weekly"),
    (re.compile(r"\btodos los dias\b|\bcada dia\b|\bdiari\w*\b"), "daily"),
]

ORDER_REF = re.compile(r"\b(ord-[0-9a-f]{8}(?:-[0-9a-f]{0,8})?|ord-\d{8}-\d{6}-[0-9a-f]{8})\b", re.I)

# Una escritura o un recordatorio solo se aceptan si el mensaje contiene su verbo: el modelo no
# puede convertir "borra todos los productos" en "desactivar producto".
WRITE_VERBS = {
    "products.set_active": re.compile(r"\b(des)?activ\w*|\bapag\w*|\bocult\w*|\bretir\w*"),
    "promotions.set_active": re.compile(r"\bpaus\w*|\breanud\w*|\b(des)?activ\w*|\bdeten\w*"),
    "orders.mark_shipped": re.compile(r"\benvi\w*|\bdespach\w*|\bsalio\b"),
    "reminders.create": re.compile(
        r"\brecuerd\w*|\bavis\w*|\balarma\b|\brecordatorio\b|\bagenda\w*|\bprogram\w*|\btarea\b|\bcada\b|\btodos los dias\b|\bdiari\w*|\bsemanal\w*|\bmensual\w*"
    ),
}
DEACTIVATE = re.compile(r"\bdesactiv\w*|\bpaus\w*|\bapag\w*|\bocult\w*|\bdeten\w*|\bretir\w*")


COMPARED_PERIOD = re.compile(
    r"\b(con|que|vs|frente a|contra) (el |la |lo de la |lo del )?(mes |semana |periodo )?(anterior|pasad[oa])\b"
)


def first_match(rules: list[tuple[re.Pattern[str], str]], text: str) -> str | None:
    return next((value for pattern, value in rules if pattern.search(text)), None)


# ---------------------------------------------------------------- nivel 0

TIME_RE = re.compile(r"\b(que hora es|me dices la hora|dime la hora|que dia es hoy|que fecha es hoy|fecha de hoy)\b")
WEATHER_RE = re.compile(r"\b(clima|llover|llueve|lluvia|temperatura|pronostico|cuantos grados)\b")
WEB_RE = re.compile(r"\b(busca informacion|buscar informacion|investiga\w*|noticias|averigua\w*|en internet)\b")
PLACE_RE = re.compile(r"\ben ([a-z][a-z ]{2,30}?)(?:\s+(?:hoy|manana)\b|[?.!,]|$)")


def route_rules(message: str, context: dict | None = None) -> Selection | None:
    """Hora, clima e internet sin modelo. No se aplica si la frase también habla de la tienda
    (petición compuesta): esa la decide el selector."""
    text = normalize(message)
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
    return finalize(Selection(None, intents[:2], [], None, []), text)


# ---------------------------------------------------------------- después del modelo

def normalize_selection(message: str, context: dict | None, selection: Selection) -> Selection:
    """Aplica las reglas del Server a la propuesta del modelo. No inventa intenciones nuevas:
    solo corrige argumentos cerrados, descarta lo que no cumple y deduce dominio y faltantes."""
    text = normalize(message)
    entity = (context or {}).get("last_entity") or {}
    kept: list[dict] = []
    for intent in selection.intents:
        tool = intent["tool"]
        spec = INTENTS_BY_NAME[tool]
        verb = WRITE_VERBS.get(tool)
        if verb and not verb.search(text):
            continue
        # Solo argumentos del catálogo y con valor (las cadenas vacías cuentan como ausentes).
        args = {
            k: v for k, v in intent["args"].items()
            if k in spec.args and v is not None and not (isinstance(v, str) and not v.strip())
        }
        if "period" in spec.args:
            # "este mes con el mes anterior": el periodo de referencia de la comparación no es el pedido.
            period_text = COMPARED_PERIOD.sub(" ", text)
            period = first_match(PERIOD_RULES, period_text)
            if period:
                args["period"] = period
            elif args.get("period") not in spec.args["period"].kind:
                args.pop("period", None)
                if spec.args["period"].required:
                    args["period"] = "today"
        if "status" in spec.args:
            status = first_match(STATUS_RULES, text)
            if status:
                args["status"] = status
            elif args.get("status") not in spec.args["status"].kind:
                args.pop("status", None)
        if "compare_to" in spec.args:
            if EXPLAIN_WORDS.search(text) and re.search(r"\bcompar\w*|\banterior\b|\bmas o menos\b", text):
                args["compare_to"] = "previous_period"
            else:
                args.pop("compare_to", None)
        if "repeat" in spec.args:
            args["repeat"] = first_match(REPEAT_RULES, text) or "none"
        if "reference" in spec.args:
            ref = ORDER_REF.search(message)
            if ref:
                args["reference"] = ref.group(1).upper()
            elif entity.get("type") == "pedido" and not args.get("reference"):
                args["reference"] = entity["name"]
        if "active" in spec.args:
            args["active"] = not bool(DEACTIVATE.search(text))
        if tool == "orders.recent":
            count = re.search(r"\b(\d{1,2})\b", text)
            if count:
                args["limit"] = max(1, min(int(count.group(1)), 50))
        kept.append({"tool": tool, "args": args})
    return finalize(Selection(None, kept, [], None, list(selection.errors)), text)


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
    return Selection(domain, selection.intents, missing, None, selection.errors)


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
