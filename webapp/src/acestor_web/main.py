from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from acestor_web.auth import google as google_auth
from acestor_web.auth import local as local_auth
from acestor_web.config import get_settings
from acestor_web.routes import auth, health, presets

settings = get_settings()

app = FastAPI(title=settings.app_name, docs_url="/api/docs", redoc_url=None)

app.add_middleware(SessionMiddleware, secret_key=settings.auth_session_secret)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(local_auth.router)
app.include_router(google_auth.router)
app.include_router(presets.router)


@app.on_event("startup")
def _startup() -> None:
    from acestor_web.auth.bootstrap import bootstrap_admin_if_needed

    bootstrap_admin_if_needed()
