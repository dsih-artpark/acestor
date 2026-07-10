from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from acestor_web.models import LoginAttempt

WINDOW = timedelta(minutes=15)
PER_IP_LIMIT = 10
PER_EMAIL_LIMIT = 5


def _cutoff() -> datetime:
    return datetime.now(UTC) - WINDOW


def _count_failures(session: Session, column, value) -> int:
    stmt = select(LoginAttempt).where(
        and_(
            column == value,
            LoginAttempt.success.is_(False),
            LoginAttempt.at >= _cutoff(),
        )
    )
    return len(session.scalars(stmt).all())


def check_login_rate_limit(session: Session, *, ip: str, email: str) -> None:
    email_norm = email.strip().lower()
    ip_failures = _count_failures(session, LoginAttempt.ip, ip)
    if ip_failures >= PER_IP_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many login attempts",
            headers={"Retry-After": str(int(WINDOW.total_seconds()))},
        )
    email_failures = _count_failures(session, LoginAttempt.email, email_norm)
    if email_failures >= PER_EMAIL_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many login attempts",
            headers={"Retry-After": str(int(WINDOW.total_seconds()))},
        )


def record_login_attempt(
    session: Session, *, ip: str, email: str, success: bool
) -> None:
    email_norm = email.strip().lower()
    session.add(LoginAttempt(ip=ip, email=email_norm, success=success))
