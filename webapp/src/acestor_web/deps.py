import uuid

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from acestor_web.db import get_db
from acestor_web.models import AuthProvider, User


def get_current_user(
    x_dev_user: str | None = Header(default=None),
    session: Session = Depends(get_db),
) -> User:
    """DEV STUB — replaced in Plan B with real session auth."""
    if not x_dev_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="auth required"
        )
    user = session.query(User).filter_by(email=x_dev_user).one_or_none()
    if user is None:
        user = User(
            id=uuid.uuid4(),
            email=x_dev_user,
            name=x_dev_user.split("@")[0],
            auth_provider=AuthProvider.local,
            password_hash="dev-stub",
            is_admin=True,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin required"
        )
    return user
