# Build from the folder that contains pipelines/ (the repo root).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

COPY pipelines/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY pipelines ./pipelines

RUN useradd --system --uid 10001 --no-create-home app
USER app

EXPOSE 8080

# Cloud Run sets $PORT. Access log has method, path and status only (no headers or bodies).
CMD ["sh", "-c", "exec uvicorn pipelines.api:app --host 0.0.0.0 --port ${PORT:-8080}"]
