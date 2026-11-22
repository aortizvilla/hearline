# Hearline backend: one image, three uses (see hearlineCDK/README.md)
#   API:      default command (uvicorn on :8000)
#   worker:   python -m app.jobs.worker      (runs the hourly daily plan + all pipeline jobs)
#   migrate:  python -m app.cli migrate      (once per deploy, before API/worker start)
FROM python:3.13-slim-bookworm

# ffmpeg: MP3 export and loudness levelling of episodes
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/app/.cache/fastembed
WORKDIR /app

# Editable install keeps the code in /app, where it finds config/ and prompts/
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir -e ".[openai,postgres,embeddings-onnx]"

# The embedding model ships inside the image, so containers start without downloading it
RUN python -c "from fastembed import TextEmbedding; \
TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')"

COPY config ./config
COPY prompts ./prompts
COPY sounds ./sounds

# Unprivileged user; storage/ is the local audio cache (S3 holds the real copies)
RUN useradd --create-home --uid 10001 hearline \
    && mkdir -p /app/storage/audio \
    && chown -R hearline /app/storage /app/.cache
USER hearline

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
