from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from acestor_web.db import get_db
from acestor_web.deps import get_current_user, require_admin
from acestor_web.models import User


def _make_app(db_session):
    app = FastAPI()

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db

    @app.get("/whoami")
    def whoami(user: User = Depends(get_current_user)):
        return {"email": user.email, "is_admin": user.is_admin}

    @app.get("/admin-only")
    def admin_only(user: User = Depends(require_admin)):
        return {"ok": True}

    return app


def test_dev_stub_rejects_missing_header(db_session):
    with TestClient(_make_app(db_session)) as client:
        r = client.get("/whoami")
        assert r.status_code == 401


def test_dev_stub_creates_user_on_first_call(db_session):
    with TestClient(_make_app(db_session)) as client:
        r = client.get("/whoami", headers={"X-Dev-User": "test@example.com"})
        assert r.status_code == 200
        assert r.json() == {"email": "test@example.com", "is_admin": True}


def test_require_admin_allows_admin(db_session):
    with TestClient(_make_app(db_session)) as client:
        r = client.get("/admin-only", headers={"X-Dev-User": "test@example.com"})
        assert r.status_code == 200
