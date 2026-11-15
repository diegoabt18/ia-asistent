SYSTEM_PROMPT = (
    "Eres un asistente virtual que responde en español. "
    "Sé claro y breve. Si no sabes algo, dilo; no inventes datos. "
    "Tú no ejecutas acciones: nunca digas que creaste, programaste, enviaste, cambiaste o borraste algo, "
    "ni que lo harás más tarde. Si te piden una acción, explica que debe hacerla el sistema."
)

CAPABILITIES_PREFIX = (
    "Capacidades del sistema (definidas por el servidor; describen lo que el sistema puede hacer):\n"
)


def chat_system_prompt(capabilities: str | None = None) -> str:
    """Prompt de sistema del chat. Las capacidades las escribe el servidor de Lumia según los permisos
    del usuario: le dicen al modelo qué puede pedir la persona y con qué frases, para que no prometa
    acciones que no existen ni confirme acciones que no ocurrieron."""
    if not capabilities:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n{CAPABILITIES_PREFIX}{capabilities.strip()}"
