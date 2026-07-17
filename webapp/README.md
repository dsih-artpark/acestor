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

Google OAuth cannot be reliably auto-smoked without either a real Google
project or a mock OIDC server. See `docs/google-oauth-manual-verification.md`
for the manual checklist to run before enabling `AUTH_PROVIDER=google` in a
deployment.

## Prod run

    cp .env.example .env  # edit values
    docker compose up -d --build
