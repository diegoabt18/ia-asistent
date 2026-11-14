from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Local AI Assistant"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # Si está vacía, los endpoints protegidos responden 503 (no se permite acceso sin clave).
    api_key: str = ""
    cors_origins: list[str] = []

    database_url: str = "sqlite:///./data/assistant.db"

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.5:2b-q4_K_M"
    ollama_timeout_seconds: float = 120.0

    # Días que se conservan los registros de consumo (usage_events). 0 = no borrar.
    usage_retention_days: int = 90

    # Historial enviado al modelo: pocos mensajes y límite de caracteres, para no exceder
    # el contexto de un modelo pequeño (4096 tokens por defecto en Ollama).
    chat_history_messages: int = 12
    chat_max_context_chars: int = 6000

    # Investigación web: cuántas páginas leer, cuánto texto guardar de cada una y límites de descarga.
    research_max_sources: int = 4
    # Texto extraído máximo por página (memoria). La verificación recorre todo este texto por fragmentos.
    research_page_max_chars: int = 20000
    # Texto por fuente que se envía al modelo (solo los fragmentos más relevantes para la pregunta).
    research_context_max_chars: int = 6000
    research_fetch_concurrency: int = 3
    research_fetch_timeout_seconds: float = 10.0
    research_max_page_bytes: int = 2_000_000

    # Consultas: la pregunta original más alternativas por plantilla, hasta este total.
    research_max_queries: int = 4

    # Fragmentación de evidencia (sin modelos).
    evidence_fragment_chars: int = 800
    evidence_fragment_overlap_chars: int = 120
    evidence_max_fragments_per_source: int = 40
    # Fragmentos candidatos por afirmación.
    evidence_max_fragments_per_claim: int = 3

    # Verificación determinística (sin usar el modelo para decidir).
    verification_support_threshold: float = 0.6
    verification_contradiction_threshold: float = 0.6
    verification_min_independent_sources: int = 1
    verification_detect_contradictions: bool = True
    # Tiempo máximo de contraste por respuesta. Lo que no da tiempo a verificar queda sin respaldo.
    verification_max_seconds: float = 20.0
    # Verificaciones que pueden ejecutarse a la vez en el proceso (CPU). Evita saturar el servidor.
    verification_max_concurrent: int = 2

    searxng_base_url: str = "http://127.0.0.1:7080"
    searxng_timeout_seconds: float = 20.0

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_password: str | None = None
    redis_db: int = 0
    redis_ssl: bool = False
    redis_key_prefix: str = "local-ai-assistant:"


@lru_cache
def get_settings() -> Settings:
    return Settings()
