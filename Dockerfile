FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# FFmpeg para el módulo de música (transcodificación a MP3/PCM). Sin paquetes recomendados.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# UID fijo para que coincida con el dueño de ./data en el host (ver README).
RUN useradd --uid 1000 --create-home appuser
USER appuser

# Sin puertos publicados: solo es accesible desde las redes Docker del compose (ver docker-compose.yml).
# El ESP32 de música necesita docker-compose.music.yml, que publica el puerto solo en la IP de la LAN.
# Un único proceso de uvicorn: el estado de la música vive en memoria del proceso.
CMD ["sh", "-c", "uvicorn app.main:app --host ${API_HOST:-127.0.0.1} --port ${API_PORT:-8000}"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD sh -c 'python -c "import urllib.request,os; urllib.request.urlopen(\"http://127.0.0.1:\" + os.environ.get(\"API_PORT\",\"8000\") + \"/api/v1/health\", timeout=4)"'
