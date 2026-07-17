from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from acestor_web.auth.passwords import (
    WeakPasswordError,
    check_policy,
    hash_password,
    verify_password,
)
from acestor_web.auth.rate_limit import check_login_rate_limit, record_login_attempt
from acestor_web.auth.sessions import create_session_cookie
from acestor_web.config import get_settings
from acestor_web.db import get_db
from acestor_web.deps import get_current_user
from acestor_web.models import AuthProvider, User
from acestor_web.schemas.auth import ChangePasswordRequest, LoginRequest, MeOut

router = APIRouter(prefix="/auth/local", tags=["auth"])


def _provider_gate() -> None:
    if get_settings().auth_provider != "local":
        raise HTTPException(status_code=404, detail="not found")


def _client_ip(request: Request) -> str:
    import ipaddress

    if request.client is None:
        return "0.0.0.0"
    host = request.client.host
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        return "127.0.0.1"


@router.post("/login", response_model=MeOut)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_db),
) -> MeOut:
    _provider_gate()
    ip = _client_ip(request)
    email = payload.email.lower()

    check_login_rate_limit(session, ip=ip, email=email)

    user = session.query(User).filter_by(email=email).one_or_none()
    invalid = HTTPException(status_code=401, detail="invalid credentials")

    if (
        user is None
        or not user.is_active
        or user.auth_provider != AuthProvider.local
        or user.password_hash is None
        or not verify_password(payload.password, user.password_hash)
    ):
        record_login_attempt(session, ip=ip, email=email, success=False)
        session.commit()
        raise invalid

    record_login_attempt(session, ip=ip, email=email, success=True)
    session.commit()
    create_session_cookie(response, user.id)
    return MeOut(
        id=user.id,
        email=user.email,
        name=user.name,
        is_admin=user.is_admin,
        auth_provider=user.auth_provider.value,
    )


@router.post("/change-password", status_code=204)
def change_password(
    payload: ChangePasswordRequest,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    _provider_gate()
    if user.auth_provider != AuthProvider.local:
        raise HTTPException(status_code=400, detail="not applicable to this provider")
    if not verify_password(payload.current_password, user.password_hash or ""):
        raise HTTPException(status_code=400, detail="current password incorrect")
    try:
        check_policy(payload.new_password)
    except WeakPasswordError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    user.password_hash = hash_password(payload.new_password)
    user.password_changed_at = datetime.now(UTC)
    session.commit()
