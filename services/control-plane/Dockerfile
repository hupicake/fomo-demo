# Direct Pi control plane.
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir "uv==0.8.15"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src

# Direct Pi production installs only the control-plane dependency closure.
RUN uv sync --locked --no-dev

FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:${PATH}

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin fomo

WORKDIR /app
COPY --from=builder --chown=fomo:fomo /app/.venv ./.venv
COPY --from=builder --chown=fomo:fomo /app/alembic.ini ./alembic.ini
COPY --from=builder --chown=fomo:fomo /app/migrations ./migrations
COPY --from=builder --chown=fomo:fomo /app/src ./src

USER fomo
EXPOSE 8000

CMD ["fomo-api"]
