import logging
import uuid

from acestor_web.auth.bootstrap import bootstrap_admin_if_needed
from acestor_web.auth.passwords import verify_password
from acestor_web.models import AuthProvider, User


def test_seeds_admin_when_empty(db_session, monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "local")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "StrongPass!1234")
    from acestor_web.config import get_settings

    get_settings.cache_clear()

    bootstrap_admin_if_needed(session_factory=lambda: db_session)
    db_session.flush()

    u = db_session.query(User).filter_by(email="admin@example.com").one()
    assert u.is_admin is True
    assert verify_password("StrongPass!1234", u.password_hash)


def test_noop_when_users_exist(db_session, monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "local")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "StrongPass!1234")
    from acestor_web.config import get_settings

    get_settings.cache_clear()

    db_session.add(
        User(
            id=uuid.uuid4(),
            email="existing@x.com",
            auth_provider=AuthProvider.local,
            password_hash="x",
        )
    )
    db_session.flush()

    bootstrap_admin_if_needed(session_factory=lambda: db_session)
    assert (
        db_session.query(User).filter_by(email="admin@example.com").one_or_none()
        is None
    )


def test_skips_on_weak_password(db_session, monkeypatch, caplog):
    monkeypatch.setenv("AUTH_PROVIDER", "local")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "weak")
    from acestor_web.config import get_settings

    get_settings.cache_clear()

    with caplog.at_level(logging.ERROR):
        bootstrap_admin_if_needed(session_factory=lambda: db_session)

    assert db_session.query(User).count() == 0
    assert any(
        "policy" in r.message.lower() or "weak" in r.message.lower()
        for r in caplog.records
    )
