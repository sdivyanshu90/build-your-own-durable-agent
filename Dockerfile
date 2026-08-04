FROM python:3.12.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml requirements.lock README.md LICENSE ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN python -m pip wheel --constraint requirements.lock --wheel-dir /wheels '.[postgres]'

FROM python:3.12.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DURABLE_AGENT_ARTIFACT_DIRECTORY=/data/artifacts
RUN groupadd --system agent && useradd --system --gid agent --home /app agent
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/* && rm -rf /wheels
COPY alembic.ini ./
COPY migrations ./migrations
RUN mkdir -p /data/artifacts && chown -R agent:agent /data /app
USER agent
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["uvicorn", "durable_agent.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
