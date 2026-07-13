"""HTTP API tests for admin user management (/api/users/...)."""

import uuid

from fastapi.testclient import TestClient

from acestor_web.db import get_db
from acestor_web.main import app
from acestor_web.models import AuditLog, User
from acestor_web.models.user import AuthProvider

ADMIN_HEADERS = {"X-Dev-User": "admin@example.com"}
STRONG_PASSWORD = "S3cur3P@ssw0rd!"


def _client(db_session):
    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_local_user(
    client, email: str, *, password: str = STRONG_PASSWORD, is_admin: bool = False
) -> dict:
    r = client.post(
        "/api/users",
        json={
            "email": email,
            "auth_provider": "local",
            "password": password,
            "is_admin": is_admin,
        },
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _non_admin_headers(db_session) -> dict:
    """Return devstub headers for a non-admin user, creating the DB row manually."""
    u = User(
        id=uuid.uuid4(),
        email="nonadmin@example.com",
        name="nonadmin",
        auth_provider=AuthProvider.local,
        password_hash="dev-stub",
        is_admin=False,
    )
    db_session.add(u)
    db_session.commit()
    return {"X-Dev-User": "nonadmin@example.com"}


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_list_returns_active_users_only(db_session):
    client = _client(db_session)
    _create_local_user(client, "a@example.com")
    r = client.get("/api/users", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    emails = [u["email"] for u in r.json()]
    assert "a@example.com" in emails
    assert "admin@example.com" in emails


def test_list_include_inactive(db_session):
    client = _client(db_session)
    u = _create_local_user(client, "inactive@example.com")
    # disable user via API
    client.delete(f"/api/users/{u['id']}", headers=ADMIN_HEADERS)

    r = client.get("/api/users", headers=ADMIN_HEADERS)
    active_emails = [x["email"] for x in r.json()]
    assert "inactive@example.com" not in active_emails

    r = client.get("/api/users?include_inactive=true", headers=ADMIN_HEADERS)
    all_emails = [x["email"] for x in r.json()]
    assert "inactive@example.com" in all_emails


def test_list_requires_admin(db_session):
    client = _client(db_session)
    headers = _non_admin_headers(db_session)
    r = client.get("/api/users", headers=headers)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Create — local
# ---------------------------------------------------------------------------


def test_create_local_user(db_session):
    client = _client(db_session)
    r = client.post(
        "/api/users",
        json={
            "email": "new@example.com",
            "name": "New User",
            "auth_provider": "local",
            "password": STRONG_PASSWORD,
        },
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "new@example.com"
    assert body["name"] == "New User"
    assert body["auth_provider"] == "local"
    assert body["is_active"] is True
    assert "password_hash" not in body


def test_create_local_user_without_password_returns_400(db_session):
    client = _client(db_session)
    r = client.post(
        "/api/users",
        json={"email": "nopw@example.com", "auth_provider": "local"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 400


def test_create_local_user_with_weak_password_returns_400(db_session):
    client = _client(db_session)
    r = client.post(
        "/api/users",
        json={
            "email": "weak@example.com",
            "auth_provider": "local",
            "password": "short",
        },
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Create — google
# ---------------------------------------------------------------------------


def test_create_google_user(db_session):
    client = _client(db_session)
    r = client.post(
        "/api/users",
        json={"email": "g@example.com", "auth_provider": "google"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["auth_provider"] == "google"
    assert "password_hash" not in body


def test_create_google_user_with_password_returns_400(db_session):
    client = _client(db_session)
    r = client.post(
        "/api/users",
        json={
            "email": "g2@example.com",
            "auth_provider": "google",
            "password": STRONG_PASSWORD,
        },
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 400


def test_create_invalid_provider_returns_400(db_session):
    client = _client(db_session)
    r = client.post(
        "/api/users",
        json={
            "email": "bad@example.com",
            "auth_provider": "twitter",
            "password": STRONG_PASSWORD,
        },
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Duplicate email
# ---------------------------------------------------------------------------


def test_duplicate_email_returns_409(db_session):
    client = _client(db_session)
    _create_local_user(client, "dup@example.com")
    r = client.post(
        "/api/users",
        json={
            "email": "dup@example.com",
            "auth_provider": "local",
            "password": STRONG_PASSWORD,
        },
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 409


def test_create_writes_audit(db_session):
    client = _client(db_session)
    _create_local_user(client, "audit@example.com")
    entries = db_session.query(AuditLog).filter_by(action="user.create").all()
    assert any(e.after_json["email"] == "audit@example.com" for e in entries)


# ---------------------------------------------------------------------------
# Get single user
# ---------------------------------------------------------------------------


def test_get_user(db_session):
    client = _client(db_session)
    u = _create_local_user(client, "get@example.com")
    r = client.get(f"/api/users/{u['id']}", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["email"] == "get@example.com"


def test_get_user_404(db_session):
    client = _client(db_session)
    r = client.get(
        "/api/users/00000000-0000-0000-0000-000000000000", headers=ADMIN_HEADERS
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


def test_patch_user(db_session):
    client = _client(db_session)
    u = _create_local_user(client, "patch@example.com")
    r = client.patch(
        f"/api/users/{u['id']}",
        json={"name": "Updated Name", "is_admin": True},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Updated Name"
    assert body["is_admin"] is True


def test_patch_writes_audit(db_session):
    client = _client(db_session)
    u = _create_local_user(client, "patchaudit@example.com")
    uid = uuid.UUID(u["id"])
    client.patch(
        f"/api/users/{u['id']}", json={"name": "Changed"}, headers=ADMIN_HEADERS
    )
    entries = (
        db_session.query(AuditLog).filter_by(action="user.update", target_id=uid).all()
    )
    assert len(entries) == 1
    assert entries[0].before_json["name"] == ""
    assert entries[0].after_json["name"] == "Changed"


def test_patch_requires_admin(db_session):
    client = _client(db_session)
    u = _create_local_user(client, "patchnonadmin@example.com")
    headers = _non_admin_headers(db_session)
    r = client.patch(f"/api/users/{u['id']}", json={"name": "X"}, headers=headers)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# DELETE (soft-delete)
# ---------------------------------------------------------------------------


def test_delete_disables_user(db_session):
    client = _client(db_session)
    u = _create_local_user(client, "del@example.com")
    r = client.delete(f"/api/users/{u['id']}", headers=ADMIN_HEADERS)
    assert r.status_code == 204

    r2 = client.get(f"/api/users/{u['id']}", headers=ADMIN_HEADERS)
    assert r2.json()["is_active"] is False


def test_delete_self_returns_403(db_session):
    client = _client(db_session)
    # The devstub auto-creates admin@example.com — retrieve their id
    r = client.get("/api/users", headers=ADMIN_HEADERS)
    admin = next(u for u in r.json() if u["email"] == "admin@example.com")
    r2 = client.delete(f"/api/users/{admin['id']}", headers=ADMIN_HEADERS)
    assert r2.status_code == 403


def test_delete_idempotent(db_session):
    client = _client(db_session)
    u = _create_local_user(client, "idem@example.com")
    uid = uuid.UUID(u["id"])
    client.delete(f"/api/users/{u['id']}", headers=ADMIN_HEADERS)
    before_count = (
        db_session.query(AuditLog)
        .filter_by(action="user.disable", target_id=uid)
        .count()
    )
    # second delete should succeed (204) without adding a new audit row
    r = client.delete(f"/api/users/{u['id']}", headers=ADMIN_HEADERS)
    assert r.status_code == 204
    after_count = (
        db_session.query(AuditLog)
        .filter_by(action="user.disable", target_id=uid)
        .count()
    )
    assert before_count == 1
    assert after_count == 1  # no second row


def test_delete_requires_admin(db_session):
    client = _client(db_session)
    u = _create_local_user(client, "delnonadmin@example.com")
    headers = _non_admin_headers(db_session)
    r = client.delete(f"/api/users/{u['id']}", headers=headers)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Reset password
# ---------------------------------------------------------------------------


def test_reset_password_local(db_session):
    client = _client(db_session)
    u = _create_local_user(client, "reset@example.com")
    r = client.post(
        f"/api/users/{u['id']}/reset-password",
        json={"new_password": "NewStr0ng!pass"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 204


def test_reset_password_google_returns_400(db_session):
    client = _client(db_session)
    r = client.post(
        "/api/users",
        json={"email": "gresettest@example.com", "auth_provider": "google"},
        headers=ADMIN_HEADERS,
    )
    gu = r.json()
    r2 = client.post(
        f"/api/users/{gu['id']}/reset-password",
        json={"new_password": STRONG_PASSWORD},
        headers=ADMIN_HEADERS,
    )
    assert r2.status_code == 400


def test_reset_password_weak_returns_400(db_session):
    client = _client(db_session)
    u = _create_local_user(client, "weakreset@example.com")
    r = client.post(
        f"/api/users/{u['id']}/reset-password",
        json={"new_password": "short"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 400


def test_reset_password_writes_audit(db_session):
    client = _client(db_session)
    u = _create_local_user(client, "resetaudit@example.com")
    uid = uuid.UUID(u["id"])
    client.post(
        f"/api/users/{u['id']}/reset-password",
        json={"new_password": "NewStr0ng!pass"},
        headers=ADMIN_HEADERS,
    )
    entries = (
        db_session.query(AuditLog)
        .filter_by(action="user.reset_password", target_id=uid)
        .all()
    )
    assert len(entries) == 1
    # password must NOT appear in audit
    assert entries[0].before_json == {}
    assert entries[0].after_json == {}


def test_reset_password_requires_admin(db_session):
    client = _client(db_session)
    u = _create_local_user(client, "resetnonadmin@example.com")
    headers = _non_admin_headers(db_session)
    r = client.post(
        f"/api/users/{u['id']}/reset-password",
        json={"new_password": STRONG_PASSWORD},
        headers=headers,
    )
    assert r.status_code == 403
