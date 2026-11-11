# Local AI Assistant — Fases 1 y 2

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

**Permisos de la carpeta `data/`** (obligatorio con Docker): el contenedor corre con UID 1000 y la base de datos se guarda en `./data`.

```bash
mkdir -p data
sudo chown 1000:1000 data
```

## Ejecución con Docker (recomendada en el servidor)

```bash
docker compose up -d --build
docker compose logs -f api
```

Tras cambiar código: `git pull` y después `docker compose up -d --build`.

## Ejecución directa con Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Endpoints

Swagger: http://127.0.0.1:8000/docs

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| GET | `/api/v1/health` | No | Estado de la API |
| GET | `/api/v1/health/dependencies` | Sí | Estado de Redis, Ollama y SearXNG |
| GET | `/api/v1/models` | Sí | Modelos de Ollama y disponibilidad del configurado |
| POST | `/api/v1/chat` | Sí | Responde y guarda el turno. Crea conversación si no envías `conversation_id` |
| POST | `/api/v1/chat/stream` | Sí | Igual que `/chat`, pero en streaming SSE |
| GET | `/api/v1/conversations` | Sí | Lista conversaciones (`limit`, `offset`) |
| GET | `/api/v1/conversations/{id}` | Sí | Conversación con todos sus mensajes |
| DELETE | `/api/v1/conversations/{id}` | Sí | Elimina la conversación y sus mensajes |
| GET | `/api/v1/search?q=...` | Sí | Búsqueda directa en SearXNG |

Autenticación: cabecera `X-API-Key: <tu clave>`. Si `API_KEY` está vacía, los endpoints protegidos responden 503.

### Ejemplos

Nueva conversación:

```bash
curl -X POST -H "X-API-Key: TU_CLAVE" -H "Content-Type: application/json" \
  -d '{"message": "Hola, me llamo Ana"}' http://127.0.0.1:8000/api/v1/chat
```

Continuar la conversación: envía el `conversation_id` que devolvió la respuesta anterior.

```bash
curl -X POST -H "X-API-Key: TU_CLAVE" -H "Content-Type: application/json" \
  -d '{"message": "¿Cómo me llamo?", "conversation_id": "ID_DEVUELTO"}' \
  http://127.0.0.1:8000/api/v1/chat
```

Streaming: el cuerpo llega como eventos `start`, `token`, `done` o `error`.

```bash
curl -N -X POST -H "X-API-Key: TU_CLAVE" -H "Content-Type: application/json" \
  -d '{"message": "Cuenta hasta cinco"}' http://127.0.0.1:8000/api/v1/chat/stream
```

## Cómo funciona la memoria de la Fase 2

- Cada turno (pregunta + respuesta) se guarda en SQLite (`data/assistant.db`) en una sola transacción, y solo cuando el modelo respondió. Si Ollama falla, no queda una conversación vacía ni una respuesta a medias.
- Al modelo se le envían como máximo `CHAT_HISTORY_MESSAGES` mensajes recientes y `CHAT_MAX_CONTEXT_CHARS` caracteres. El historial más antiguo se descarta primero. Así no se supera el contexto de un modelo pequeño.
- En streaming, si el cliente se desconecta antes de terminar, el turno no se guarda.
- Las tablas se crean al arrancar (`create_all`). Cuando cambie el esquema, se añadirá Alembic.

## Tests

```bash
pytest
```

## Limitaciones actuales

- Sin investigación web integrada en `/chat` (`use_web`): llega en la Fase 3.
- Sin memoria a largo plazo ni resúmenes de conversaciones: Fase 4.
- Sin migraciones Alembic todavía; las tablas se crean con `create_all`.
- `network_mode: host` solo funciona en Linux.
- Redis publicado en `0.0.0.0:6379` en el servidor: conviene limitarlo a `127.0.0.1`, porque afecta a otros servicios.
