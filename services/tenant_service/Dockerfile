# syntax=docker/dockerfile:1.7
# NexusCall Tenant & Authorization Service. Build context: repository root.
ARG PYTHON_IMAGE=python:3.12.8-slim-bookworm

FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_COMPILE_BYTECODE_TIMEOUT=600 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services/tenant_service ./services/tenant_service
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package nexus-tenant-service

FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
ENV PATH="/src/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY --from=build --chown=root:root /src/.venv /src/.venv
COPY --chown=root:root services/tenant_service/alembic.ini services/tenant_service/alembic.ini
COPY --chown=root:root services/tenant_service/migrations services/tenant_service/migrations
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=10s --start-period=120s --retries=5 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]
STOPSIGNAL SIGTERM
CMD ["uvicorn", "tenant_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*", "--no-server-header"]
