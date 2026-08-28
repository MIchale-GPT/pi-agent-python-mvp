FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    TAU_NO_UPDATE_CHECK=1

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN uv sync --frozen --no-dev --extra dataquery --no-editable \
    && uv run --frozen --no-sync tau --help >/dev/null

ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["tau"]
CMD ["--help"]
