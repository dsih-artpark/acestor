import uuid

from fastapi import Request, Response

from acestor_web.auth.sessions import (
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    create_session_cookie,
    read_session,
)


def _make_request(cookies: dict) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "headers": [
            (b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode())
        ],
    }
    return Request(scope)


def test_roundtrip_returns_uuid(monkeypatch):
    monkeypatch.setenv("AUTH_SESSION_SECRET", "test-secret")

    user_id = uuid.uuid4()
    resp = Response()
    create_session_cookie(resp, user_id)

    raw = resp.headers["set-cookie"]
    assert SESSION_COOKIE_NAME in raw
    assert "HttpOnly" in raw
    assert "SameSite=lax" in raw or "SameSite=Lax" in raw

    # Extract cookie value
    cookie_value = raw.split(SESSION_COOKIE_NAME + "=", 1)[1].split(";", 1)[0]

    req = _make_request({SESSION_COOKIE_NAME: cookie_value})
    assert read_session(req) == user_id


def test_read_session_missing_cookie(monkeypatch):
    monkeypatch.setenv("AUTH_SESSION_SECRET", "test-secret")
    req = _make_request({})
    assert read_session(req) is None


def test_read_session_tampered(monkeypatch):
    monkeypatch.setenv("AUTH_SESSION_SECRET", "test-secret")
    req = _make_request({SESSION_COOKIE_NAME: "totally-not-a-real-token"})
    assert read_session(req) is None


def test_read_session_expired(monkeypatch):
    monkeypatch.setenv("AUTH_SESSION_SECRET", "test-secret")

    user_id = uuid.uuid4()
    resp = Response()
    # Create cookie with negative TTL to simulate expiry
    from acestor_web.auth import sessions as sessmod

    monkeypatch.setattr(sessmod, "_max_age_seconds", lambda ttl_days: -1)
    create_session_cookie(resp, user_id, ttl_days=30)
    cookie_value = (
        resp.headers["set-cookie"]
        .split(SESSION_COOKIE_NAME + "=", 1)[1]
        .split(";", 1)[0]
    )

    req = _make_request({SESSION_COOKIE_NAME: cookie_value})
    assert read_session(req) is None


def test_clear_session_cookie():
    resp = Response()
    clear_session_cookie(resp)
    raw = resp.headers["set-cookie"]
    assert SESSION_COOKIE_NAME in raw
    assert "Max-Age=0" in raw
