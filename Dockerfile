FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TAU_NO_UPDATE_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN python -m pip install --upgrade pip \
    && pip install ".[dataquery]" \
    && tau --help >/dev/null

ENTRYPOINT ["tau"]
CMD ["--help"]
