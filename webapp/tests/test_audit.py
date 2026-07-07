import uuid

from acestor_web.audit import write_audit
from acestor_web.models import AuditLog, AuthProvider, User


def test_write_audit_records_entry(db_session):
    user = User(
        id=uuid.uuid4(),
        email="admin@example.com",
        auth_provider=AuthProvider.local,
        password_hash="stub",
        is_admin=True,
    )
    db_session.add(user)
    db_session.flush()

    target_id = uuid.uuid4()
    write_audit(
        db_session,
        user_id=user.id,
        action="preset.create",
        target_type="preset",
        target_id=target_id,
        after={"name": "x"},
    )
    db_session.flush()

    entry = db_session.query(AuditLog).one()
    assert entry.user_id == user.id
    assert entry.action == "preset.create"
    assert entry.target_type == "preset"
    assert entry.target_id == target_id
    assert entry.after_json == {"name": "x"}
    assert entry.before_json is None


def test_write_audit_allows_null_user(db_session):
    write_audit(
        db_session,
        user_id=None,
        action="system.gc",
        target_type="system",
    )
    db_session.flush()
    entry = db_session.query(AuditLog).one()
    assert entry.user_id is None
    assert entry.action == "system.gc"
