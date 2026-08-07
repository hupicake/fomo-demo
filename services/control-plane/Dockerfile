# The default worker framework is MetaGPT, which supports Python < 3.12.
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv==0.8.15"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# MetaGPT is an optional project extra but mandatory for the default production
# framework. Keep the VCS-SHA lock as the single source of truth.
RUN uv sync --locked --no-dev --extra metagpt

FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:${PATH}

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin fomo

WORKDIR /app
COPY --from=builder --chown=fomo:fomo /app/.venv ./.venv
COPY --from=builder --chown=fomo:fomo /app/src ./src

USER fomo
EXPOSE 8000

CMD ["fomo-api"]
