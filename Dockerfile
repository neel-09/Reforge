# Reforge — single container: FastAPI backend + static frontend.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    REFORGE_USAGE_FILE=/data/usage.json
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt uv
COPY . .

RUN mkdir -p /data

# ONE worker only: runs share one workspace and the daily-limit counters.
CMD ["sh", "-c", "uvicorn src.backend.endpoints.web:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
