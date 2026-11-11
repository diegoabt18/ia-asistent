FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

RUN useradd --create-home appuser
USER appuser

# Con network_mode: host (ver docker-compose.yml) API_PORT se abre directamente en el host.
CMD ["sh", "-c", "uvicorn app.main:app --host ${API_HOST:-127.0.0.1} --port ${API_PORT:-8000}"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD sh -c 'python -c "import urllib.request,os; urllib.request.urlopen(\"http://127.0.0.1:\" + os.environ.get(\"API_PORT\",\"8000\") + \"/api/v1/health\", timeout=4)"'
