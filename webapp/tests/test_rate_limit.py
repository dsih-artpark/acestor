import pytest
from fastapi import HTTPException

from acestor_web.auth.rate_limit import (
    PER_EMAIL_LIMIT,
    PER_IP_LIMIT,
    check_login_rate_limit,
    record_login_attempt,
)


def test_no_attempts_allows(db_session):
    check_login_rate_limit(db_session, ip="1.2.3.4", email="a@b.c")


def test_ip_limit_triggers(db_session):
    for _ in range(PER_IP_LIMIT):
        record_login_attempt(
            db_session, ip="1.2.3.4", email=f"unique-{_}@b.c", success=False
        )
    db_session.flush()
    with pytest.raises(HTTPException) as exc:
        check_login_rate_limit(db_session, ip="1.2.3.4", email="x@y.z")
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers


def test_email_limit_triggers(db_session):
    for i in range(PER_EMAIL_LIMIT):
        record_login_attempt(
            db_session, ip=f"1.2.3.{i}", email="target@b.c", success=False
        )
    db_session.flush()
    with pytest.raises(HTTPException) as exc:
        check_login_rate_limit(db_session, ip="9.9.9.9", email="target@b.c")
    assert exc.value.status_code == 429


def test_successful_attempts_do_not_count(db_session):
    for i in range(PER_EMAIL_LIMIT + 3):
        record_login_attempt(
            db_session, ip=f"1.2.3.{i}", email="target@b.c", success=True
        )
    db_session.flush()
    check_login_rate_limit(db_session, ip="9.9.9.9", email="target@b.c")


def test_email_is_case_insensitive(db_session):
    for i in range(PER_EMAIL_LIMIT):
        record_login_attempt(
            db_session, ip=f"1.2.3.{i}", email="Target@B.C", success=False
        )
    db_session.flush()
    with pytest.raises(HTTPException):
        check_login_rate_limit(db_session, ip="9.9.9.9", email="target@b.c")
