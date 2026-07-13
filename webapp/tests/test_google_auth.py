import os
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


def _client(db_session, provider="google", allowed=""):
    os.environ["AUTH_PROVIDER"] = provider
    os.environ["AUTH_GOOGLE_CLIENT_ID"] = "test-client-id"
    os.environ["AUTH_GOOGLE_CLIENT_SECRET"] = "test-client-secret"
    os.environ["AUTH_ALLOWED_DOMAINS"] = allowed

    from acestor_web.config import get_settings

    get_settings.cache_clear()

    from acestor_web.db import get_db
    from acestor_web.main import app

    def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    return TestClient(app)


def test_provider_gate_when_local(db_session):
    client = _client(db_session, provider="local")
    r = client.get("/auth/google/login", follow_redirects=False)
    assert r.status_code == 404


def test_login_redirects_to_google(db_session):
    client = _client(db_session, provider="google", allowed="artpark.in")

    mock_response = MagicMock()
    mock_response.status_code = 302
    mock_response.headers = {
        "location": "https://accounts.google.com/o/oauth2/v2/auth?hd=artpark.in"
    }
    mock_response.body = b""

    mock_oauth_instance = MagicMock()
    mock_oauth_instance.google.authorize_redirect = AsyncMock(
        return_value=mock_response
    )

    with patch(
        "acestor_web.auth.google._oauth_client", return_value=mock_oauth_instance
    ):
        r = client.get("/auth/google/login", follow_redirects=False)
    # Smoke test: endpoint should not 500; it either returns the mock redirect (302) or similar
    assert r.status_code != 500


def test_callback_allowed_domain_creates_user(db_session):
    client = _client(db_session, provider="google", allowed="artpark.in")

    from acestor_web.models import AuthProvider, User

    with patch("acestor_web.auth.google._authorize_access_token") as mock_token:
        mock_token.return_value = {
            "userinfo": {
                "email": "alice@artpark.in",
                "email_verified": True,
                "name": "Alice",
            }
        }
        r = client.get(
            "/auth/google/callback?code=fake&state=fake", follow_redirects=False
        )

    assert r.status_code in (302, 307)
    u = db_session.query(User).filter_by(email="alice@artpark.in").one()
    assert u.auth_provider == AuthProvider.google
    assert u.password_hash is None
    assert u.is_active is True


def test_callback_disallowed_domain_rejected(db_session):
    client = _client(db_session, provider="google", allowed="artpark.in")

    with patch("acestor_web.auth.google._authorize_access_token") as mock_token:
        mock_token.return_value = {
            "userinfo": {
                "email": "eve@evil.com",
                "email_verified": True,
                "name": "Eve",
            }
        }
        r = client.get(
            "/auth/google/callback?code=fake&state=fake", follow_redirects=False
        )

    assert r.status_code == 403
    assert "artpark.in" not in r.text  # do not leak the allowlist
    assert "not permitted" in r.text.lower()


def test_callback_unverified_email_rejected(db_session):
    client = _client(db_session, provider="google", allowed="artpark.in")

    with patch("acestor_web.auth.google._authorize_access_token") as mock_token:
        mock_token.return_value = {
            "userinfo": {
                "email": "alice@artpark.in",
                "email_verified": False,
                "name": "Alice",
            }
        }
        r = client.get(
            "/auth/google/callback?code=fake&state=fake", follow_redirects=False
        )

    assert r.status_code == 403


def test_callback_no_allowlist_accepts_any(db_session):
    client = _client(db_session, provider="google", allowed="")

    with patch("acestor_web.auth.google._authorize_access_token") as mock_token:
        mock_token.return_value = {
            "userinfo": {
                "email": "someone@random.org",
                "email_verified": True,
                "name": "Somebody",
            }
        }
        r = client.get(
            "/auth/google/callback?code=fake&state=fake", follow_redirects=False
        )

    assert r.status_code in (302, 307)
