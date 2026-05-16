FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md alembic.ini ./
COPY check_price_write_config.py ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
COPY tests ./tests

RUN pip install --no-cache-dir ".[dev]"

CMD ["python", "-m", "app.worker_main"]
