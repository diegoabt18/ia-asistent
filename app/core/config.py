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
    ollama_model: str = "qwen2.5:1.5b-instruct-q4_K_M"
    ollama_timeout_seconds: float = 120.0

    # Historial enviado al modelo: pocos mensajes y límite de caracteres, para no exceder
    # el contexto de un modelo pequeño (4096 tokens por defecto en Ollama).
    chat_history_messages: int = 12
    chat_max_context_chars: int = 6000

    # Investigación web: cuántas páginas leer, cuánto texto de cada una y límites de descarga.
    research_max_sources: int = 4
    research_page_max_chars: int = 1500
    research_fetch_concurrency: int = 3
    research_fetch_timeout_seconds: float = 10.0
    research_max_page_bytes: int = 2_000_000

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
