# acestor-web

Web interface for the acestor forecasting pipeline.

## Local dev

    uv sync --all-extras
    cp .env.example .env
    docker compose -f docker-compose.dev.yml up -d postgres minio
    uv run alembic upgrade head
    uv run uvicorn acestor_web.main:app --reload

## Tests

    ./scripts/test.sh

## Smoke tests

    ./scripts/smoke-local-auth.sh   # local password provider end-to-end

## Prod run

    cp .env.example .env  # edit values
    docker compose up -d --build
