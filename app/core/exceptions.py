class UpstreamServiceError(Exception):
    """Un servicio externo (Ollama, SearXNG, Redis) falló o no respondió correctamente."""

    def __init__(self, service: str, detail: str) -> None:
        self.service = service
        self.detail = detail
        super().__init__(f"{service}: {detail}")


class ConversationNotFoundError(Exception):
    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        super().__init__(f"Conversación no encontrada: {conversation_id}")
