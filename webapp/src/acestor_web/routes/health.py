from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from acestor_web.db import get_db

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
def readyz(session: Session = Depends(get_db)) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ready"}
