# syntax=docker/dockerfile:1.7
# One-off operational tooling image: migrations (run as *_owner roles), broker bootstrap, dev seed.
# Never runs as a long-lived service.
ARG PYTHON_IMAGE=python:3.12.8-slim-bookworm
FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_COMPILE_BYTECODE_TIMEOUT=600 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services ./services
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable --all-packages

FROM ${PYTHON_IMAGE}
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
ENV PATH="/src/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY --from=build /src/.venv /src/.venv
COPY services ./services
COPY scripts ./scripts
USER 10001:10001
ENTRYPOINT ["python"]
CMD ["scripts/migrate_all.py"]
