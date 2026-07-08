from fastapi import FastAPI

from acestor_web.config import get_settings
from acestor_web.routes import health, presets

settings = get_settings()

app = FastAPI(title=settings.app_name, docs_url="/api/docs", redoc_url=None)

app.include_router(health.router)
app.include_router(presets.router)
