import uuid

from fastapi.testclient import TestClient

from acestor_web.auth.passwords import hash_password
from acestor_web.db import get_db
from acestor_web.main import app
from acestor_web.models import AuthProvider, LoginAttempt, User


def _client(db_session, provider="local"):
    def override():
        yield db_session

    app.dependency_overrides[get_db] = override

    from acestor_web.config import get_settings

    get_settings.cache_clear()
    import os

    os.environ["AUTH_PROVIDER"] = provider
    return TestClient(app)


def _make_local_user(db, email, password, is_admin=False):
    u = User(
        id=uuid.uuid4(),
        email=email.lower(),
        name=email.split("@")[0],
        auth_provider=AuthProvider.local,
        password_hash=hash_password(password),
        is_admin=is_admin,
    )
    db.add(u)
    db.flush()
    return u


def test_login_success(db_session):
    _make_local_user(db_session, "a@b.c", "GoodPassword!1234")
    client = _client(db_session)
    r = client.post(
        "/auth/local/login", json={"email": "a@b.c", "password": "GoodPassword!1234"}
    )
    assert r.status_code == 200
    assert r.json()["email"] == "a@b.c"
    assert "acestor_session" in r.cookies


def test_login_wrong_password(db_session):
    _make_local_user(db_session, "a@b.c", "GoodPassword!1234")
    client = _client(db_session)
    r = client.post(
        "/auth/local/login", json={"email": "a@b.c", "password": "wrongwrongwrong"}
    )
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid credentials"}


def test_login_missing_user_returns_same_message(db_session):
    client = _client(db_session)
    r = client.post(
        "/auth/local/login",
        json={"email": "nobody@example.com", "password": "whatever12!"},
    )
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid credentials"}


def test_login_disabled_user(db_session):
    u = _make_local_user(db_session, "a@b.c", "GoodPassword!1234")
    u.is_active = False
    db_session.flush()
    client = _client(db_session)
    r = client.post(
        "/auth/local/login", json={"email": "a@b.c", "password": "GoodPassword!1234"}
    )
    assert r.status_code == 401


def test_rate_limit_kicks_in(db_session):
    _make_local_user(db_session, "target@b.c", "GoodPassword!1234")
    client = _client(db_session)
    for _ in range(5):
        r = client.post(
            "/auth/local/login",
            json={"email": "target@b.c", "password": "wrong-a-lot!!"},
        )
        assert r.status_code == 401
    r = client.post(
        "/auth/local/login",
        json={"email": "target@b.c", "password": "GoodPassword!1234"},
    )
    assert r.status_code == 429


def test_change_password_flow(db_session):
    _make_local_user(db_session, "a@b.c", "InitialPass!1234")
    client = _client(db_session)
    r = client.post(
        "/auth/local/login", json={"email": "a@b.c", "password": "InitialPass!1234"}
    )
    assert r.status_code == 200
    r = client.post(
        "/auth/local/change-password",
        json={
            "current_password": "InitialPass!1234",
            "new_password": "NewShinyPass!7890",
        },
    )
    assert r.status_code == 204

    # Old password now rejected
    client.post("/auth/logout")
    r = client.post(
        "/auth/local/login", json={"email": "a@b.c", "password": "InitialPass!1234"}
    )
    assert r.status_code == 401

    r = client.post(
        "/auth/local/login", json={"email": "a@b.c", "password": "NewShinyPass!7890"}
    )
    assert r.status_code == 200


def test_change_password_rejects_weak(db_session):
    _make_local_user(db_session, "a@b.c", "InitialPass!1234")
    client = _client(db_session)
    client.post(
        "/auth/local/login", json={"email": "a@b.c", "password": "InitialPass!1234"}
    )
    r = client.post(
        "/auth/local/change-password",
        json={"current_password": "InitialPass!1234", "new_password": "short"},
    )
    assert r.status_code == 400


def test_logout_clears_cookie(db_session):
    _make_local_user(db_session, "a@b.c", "GoodPassword!1234")
    client = _client(db_session)
    client.post(
        "/auth/local/login", json={"email": "a@b.c", "password": "GoodPassword!1234"}
    )
    r = client.post("/auth/logout")
    assert r.status_code == 204
    r = client.get("/auth/me")
    assert r.status_code == 401


def test_me_authenticated(db_session):
    _make_local_user(db_session, "a@b.c", "GoodPassword!1234")
    client = _client(db_session)
    client.post(
        "/auth/local/login", json={"email": "a@b.c", "password": "GoodPassword!1234"}
    )
    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.json()["email"] == "a@b.c"


def test_login_records_attempt(db_session):
    _make_local_user(db_session, "a@b.c", "GoodPassword!1234")
    client = _client(db_session)
    client.post("/auth/local/login", json={"email": "a@b.c", "password": "wrong-o!!!!"})
    client.post(
        "/auth/local/login", json={"email": "a@b.c", "password": "GoodPassword!1234"}
    )
    attempts = db_session.query(LoginAttempt).order_by(LoginAttempt.at).all()
    assert [a.success for a in attempts] == [False, True]


def test_provider_gate(db_session):
    _make_local_user(db_session, "a@b.c", "GoodPassword!1234")
    client = _client(db_session, provider="google")
    r = client.post(
        "/auth/local/login", json={"email": "a@b.c", "password": "GoodPassword!1234"}
    )
    assert r.status_code == 404
