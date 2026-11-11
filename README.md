# Local AI Assistant — Fase 1

API FastAPI que reutiliza la infraestructura existente del servidor: Ollama, SearXNG y Redis. No crea contenedores para ninguno de ellos.

## Direcciones de conexión

Con `network_mode: host`, la API corre en el mismo espacio de red que el servidor, así que los servicios existentes se usan por `127.0.0.1`:

| Servicio | Dirección | Variable |
|---|---|---|
| Ollama | http://127.0.0.1:11434 | `OLLAMA_BASE_URL` |
| SearXNG | http://127.0.0.1:7080 | `SEARXNG_BASE_URL` |
| Redis | 127.0.0.1:6379 | `REDIS_HOST`, `REDIS_PORT` |

Requisitos del servidor:
- SearXNG debe tener `json` en `search.formats` de `settings.yml`.
- Redis debe tener contraseña configurada en `REDIS_PASSWORD`.

## Configuración

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Pega el resultado en `API_KEY` dentro de `.env`.

## Ejecución con Docker (recomendada en el servidor)

```bash
docker compose up -d --build
docker compose logs -f api
```

## Ejecución directa con Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Endpoints de Fase 1

Swagger: http://127.0.0.1:8000/docs

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| GET | `/api/v1/health` | No | Estado de la API |
| GET | `/api/v1/health/dependencies` | Sí | Estado de Redis, Ollama y SearXNG |
| GET | `/api/v1/models` | Sí | Modelos de Ollama y disponibilidad del configurado |
| POST | `/api/v1/chat` | Sí | Pregunta a Ollama (sin historial en esta fase) |
| GET | `/api/v1/search?q=...` | Sí | Búsqueda directa en SearXNG |

Autenticación: cabecera `X-API-Key: <tu clave>`. Si `API_KEY` está vacía, los endpoints protegidos responden 503.

Ejemplo:

```bash
curl -H "X-API-Key: TU_CLAVE" "http://127.0.0.1:8000/api/v1/health/dependencies"
```

## Tests

```bash
pytest
```

## Limitaciones de la Fase 1

- Sin historial de conversación ni base de datos: SQLite, SQLAlchemy y Alembic llegan en la Fase 2.
- Sin extracción de páginas (Trafilatura) ni síntesis de fuentes: llegan en la Fase 3.
- `network_mode: host` solo funciona en Linux.
- Redis publicado en `0.0.0.0:6379` en el servidor: conviene limitarlo a `127.0.0.1` por separado, porque afecta a otros servicios.
