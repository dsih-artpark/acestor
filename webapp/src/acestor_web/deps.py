import uuid

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from acestor_web.auth.sessions import read_session
from acestor_web.config import get_settings
from acestor_web.db import get_db
from acestor_web.models import AuthProvider, User


def get_current_user(
    request: Request,
    x_dev_user: str | None = Header(default=None),
    session: Session = Depends(get_db),
) -> User:
    provider = get_settings().auth_provider

    if provider == "devstub":
        if not x_dev_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
            )
        u = session.query(User).filter_by(email=x_dev_user.lower()).one_or_none()
        if u is None:
            u = User(
                id=uuid.uuid4(),
                email=x_dev_user.lower(),
                name=x_dev_user.split("@")[0],
                auth_provider=AuthProvider.local,
                password_hash="dev-stub",
                is_admin=True,
            )
            session.add(u)
            session.commit()
            session.refresh(u)
        return u

    user_id = read_session(request)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    u = session.query(User).filter_by(id=user_id).one_or_none()
    if u is None or not u.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    return u


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin required"
        )
    return user
