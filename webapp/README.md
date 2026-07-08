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
