from functools import lru_cache
from typing import Literal

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
    # Llamadas simultáneas al modelo en este proceso. Con recursos limitados, más peticiones a la vez
    # solo reparten la misma CPU/RAM y acaban todas en timeout: el resto espera su turno.
    ollama_max_concurrent: int = 2
    # Espera máxima por una plaza libre antes de responder 503 (modelo ocupado).
    ollama_queue_timeout_seconds: float = 60.0
    # Selector de herramientas (/select): contexto y tokens de salida acotados.
    selector_num_ctx: int = 4096
    selector_max_tokens: int = 200

    # Días que se conservan los registros de consumo (usage_events). 0 = no borrar.
    usage_retention_days: int = 90

    # Historial enviado al modelo: pocos mensajes y límite de caracteres, para no exceder
    # el contexto de un modelo pequeño (4096 tokens por defecto en Ollama).
    chat_history_messages: int = 12
    chat_max_context_chars: int = 6000
    # En modo web, el historial reciente solo sirve para entender la pregunta (referencias como
    # "esa" o "eso"). Es más corto que el historial normal.
    web_history_messages: int = 4
    web_history_chars: int = 1500

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

    # Módulo de música (búsqueda con yt-dlp, transcodificación con FFmpeg y stream HTTP a un ESP32).
    # Desactivado por defecto: con false, todas las rutas /music responden 404.
    music_enabled: bool = False
    # Token del ESP32 (cabecera Authorization: Bearer). Distinto de API_KEY: el dispositivo solo
    # controla la música, nunca el modelo. Vacío = las rutas del dispositivo responden 503.
    music_device_token: str = ""
    # Usuarios (X-User-Ref) que pueden controlar la reproducción. Vacío = cualquiera que envíe el
    # servidor de Lumia, que ya comprueba sus permisos. Defensa adicional, no sustituye al servidor.
    music_control_user_refs: list[str] = []
    music_ffmpeg_path: str = "ffmpeg"
    # Formato hacia el ESP32: "mp3" (poco ancho de banda) o "pcm" (s16le estéreo, sin decodificar).
    music_output_format: Literal["mp3", "pcm"] = "mp3"
    music_bitrate_kbps: int = 128
    music_sample_rate: int = 44100
    # Bytes por lectura de FFmpeg: es también el tamaño de cada trozo enviado al ESP32.
    music_chunk_bytes: int = 4096
    music_search_limit: int = 5
    music_search_timeout_seconds: float = 20.0
    music_extract_timeout_seconds: float = 30.0
    # Pistas más largas se descartan (evita directos de horas y mezclas que ocupan la cola).
    music_max_track_seconds: int = 900
    music_queue_max_items: int = 50
    # Sin latido durante este tiempo, el ESP32 se considera desconectado.
    music_device_timeout_seconds: float = 15.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
