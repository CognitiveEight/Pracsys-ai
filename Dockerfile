# SimplerToday AI service (Socket.IO client) — production image.
#
# Runtime configuration comes from the environment (see env.example); nothing
# secret is baked in. Mount the GCP service-account file read-only and point
# GOOGLE_APPLICATION_CREDENTIALS (and SPEECH_GOOGLE_APPLICATION_CREDENTIALS, if
# separate) at it, e.g.:
#   docker run --env-file .env \
#     -v /secure/sa.json:/run/secrets/gcp-sa.json:ro \
#     -e GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp-sa.json \
#     -v simplertoday-logs:/app/logs -v simplertoday-data:/app/data simplertoday-ai

# ---- build stage: compile wheels with the toolchain, keep it out of the runtime
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Exact, tested versions (requirements.lock.txt is resolved from requirements.txt).
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt

# ---- runtime stage
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# System tools the code shells out to:
#   ffmpeg        - audio evidence transcription (app/processing/audio_transcription.py)
#   poppler-utils - pdf2image, used to OCR scanned PDFs (app/processing/file_handlers.py)
#   antiword      - legacy .doc extraction fallback (app/processing/file_handlers.py)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg poppler-utils antiword \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv

# Run as an unprivileged user; it owns only the directories the service writes.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY --chown=app:app app/ ./app/
RUN mkdir -p logs/audit data/workspaces && chown -R app:app logs data

USER app

# No HEALTHCHECK: the service is an outbound Socket.IO client with no port to
# probe, and the process exits on a fatal error (restart it with the
# orchestrator's restart policy). Liveness is the ai_heartbeat_ping round trip
# with the backend, and every disconnect is logged.

CMD ["python", "-m", "app.main"]
