import uuid

from acestor_web.models import AuthProvider, User


def test_can_insert_and_query_user(db_session):
    u = User(
        id=uuid.uuid4(),
        email="dev@example.com",
        name="Dev",
        auth_provider=AuthProvider.local,
        password_hash="argon2:stub",
        is_admin=True,
    )
    db_session.add(u)
    db_session.flush()

    found = db_session.query(User).filter_by(email="dev@example.com").one()
    assert found.name == "Dev"
    assert found.auth_provider == AuthProvider.local
    assert found.is_admin is True
    assert found.is_active is True
