# syntax=docker/dockerfile:1.7
# One image for every Python service; docker-compose selects the entry point per service.
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

FROM python:3.12-slim AS runtime
ARG PRELOAD_MODELS=true
ARG AGENT_VERSION=1.0.0
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/app \
    FASTEMBED_CACHE_DIR=/app/.cache/fastembed \
    HF_HUB_DISABLE_TELEMETRY=1 \
    AGENT_VERSION=${AGENT_VERSION}
# Fixed UID/GID so Kubernetes securityContext (runAsUser/fsGroup 10001) matches file ownership.
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home /app app
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
# Bake the local embedding + reranker ONNX models so containers start without internet access.
RUN mkdir -p /app/.cache/fastembed && chown -R app:app /app/.cache && \
    if [ "$PRELOAD_MODELS" = "true" ]; then \
      su app -s /bin/sh -c "python -c \"from fastembed import TextEmbedding; from fastembed.rerank.cross_encoder import TextCrossEncoder; \
TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/app/.cache/fastembed'); \
TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2', cache_dir='/app/.cache/fastembed')\""; \
    fi
COPY --chown=app:app pyproject.toml alembic.ini ./
COPY --chown=app:app src ./src
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app data ./data
COPY --chown=app:app governance ./governance
COPY --chown=app:app evals ./evals
USER app
EXPOSE 8000 8010 8020 9001 9002 9003 9004
CMD ["python", "-c", "print('select a service command in docker-compose.yml')"]
