# Local AI Assistant — Fases 1, 2 y 3

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
| POST | `/api/v1/chat` | Sí | Responde y guarda el turno. Con `use_web: true` busca en la web antes |
| POST | `/api/v1/chat/stream` | Sí | Igual que `/chat`, en streaming SSE |
| POST | `/api/v1/research` | Sí | Investiga una pregunta y devuelve respuesta con fuentes |
| GET | `/api/v1/conversations` | Sí | Lista conversaciones (`limit`, `offset`) |
| GET | `/api/v1/conversations/{id}` | Sí | Conversación con todos sus mensajes |
| DELETE | `/api/v1/conversations/{id}` | Sí | Elimina la conversación y sus mensajes |
| GET | `/api/v1/search?q=...` | Sí | Búsqueda directa en SearXNG (sin leer las páginas) |

Autenticación: cabecera `X-API-Key: <tu clave>`. Si `API_KEY` está vacía, los endpoints protegidos responden 503.

### Ejemplos

Investigación con fuentes:

```bash
curl -X POST -H "X-API-Key: TU_CLAVE" -H "Content-Type: application/json" \
  -d '{"question": "¿quién es el presidente de Colombia?"}' http://127.0.0.1:8000/api/v1/research
```

Chat con investigación web:

```bash
curl -X POST -H "X-API-Key: TU_CLAVE" -H "Content-Type: application/json" \
  -d '{"message": "¿Quién es el presidente de Colombia?", "use_web": true}' \
  http://127.0.0.1:8000/api/v1/chat
```

## Cómo funciona la investigación (Fase 3)

1. SearXNG devuelve hasta 10 resultados para la pregunta.
2. Se descartan redes sociales y URLs duplicadas (con o sin `www`, con parámetros de rastreo, etc.).
3. Se priorizan fuentes oficiales (`.gov`, `.gob`, `.edu`, `.mil`, `.int`) y de referencia (Wikipedia). Máximo 2 páginas por dominio.
4. Se descargan las 4 mejores páginas (configurable) con timeouts y límite de tamaño, y se extrae el contenido principal con Trafilatura.
5. El modelo responde solo con esas fuentes, cita con `[n]` y el backend valida las citas: las que apuntan a fuentes inexistentes se eliminan.

Cada fuente devuelve `title`, `url`, `domain`, `category` (`oficial`, `referencia` u `otro`), `published_date` cuando la página la indica, y `cited`.

### Seguridad de la descarga de páginas

- Solo `http` y `https`, sin credenciales en la URL y solo puertos 80 y 443.
- Se bloquean destinos no públicos (loopback, redes privadas, link-local como `169.254.169.254`). Así el asistente no puede leer Redis, Ollama ni otros servicios internos.
- Cada redirección se valida de nuevo; máximo 3 redirecciones.
- Se descargan como máximo 2 MB por página, con timeout de 10 s.

### Límites conocidos

- El modelo de 1.5B no siempre elige bien ni contrasta las fuentes: el backend solo verifica que las citas existan, no que el contenido sea correcto. Revisa las URLs antes de confiar en un dato importante.
- No hay generación de consultas alternativas: se busca la pregunta tal cual.
- Páginas que cargan su contenido con JavaScript no tienen texto extraíble y se omiten.
- En el streaming los tokens llegan tal cual los genera el modelo. Las citas inválidas se eliminan solo del texto guardado y del evento `done`.
- La protección contra redirecciones a destinos internos depende de la resolución DNS en el momento de la petición; un atacante con control de su DNS podría intentar un rebinding. Para este uso personal el riesgo es bajo, pero no es una defensa perfecta.

## Cómo funciona la memoria (Fase 2)

- Cada turno (pregunta + respuesta) se guarda en SQLite (`data/assistant.db`) en una sola transacción, y solo cuando el modelo respondió.
- Al modelo se le envían como máximo `CHAT_HISTORY_MESSAGES` mensajes recientes y `CHAT_MAX_CONTEXT_CHARS` caracteres. El historial más antiguo se descarta primero.
- En streaming, si el cliente se desconecta antes de terminar, el turno no se guarda.
- Las tablas se crean al arrancar (`create_all`). Cuando cambie el esquema, se añadirá Alembic.

## Tests

```bash
pytest
```

## Limitaciones actuales

- Sin memoria a largo plazo ni resúmenes de conversaciones: Fase 4.
- Sin migraciones Alembic todavía; las tablas se crean con `create_all`.
- `network_mode: host` solo funciona en Linux.
- Redis publicado en `0.0.0.0:6379` en el servidor: conviene limitarlo a `127.0.0.1`, porque afecta a otros servicios.
