class UpstreamServiceError(Exception):
    """Un servicio externo (Ollama, SearXNG, Redis) falló o no respondió correctamente."""

    def __init__(self, service: str, detail: str) -> None:
        self.service = service
        self.detail = detail
        super().__init__(f"{service}: {detail}")
