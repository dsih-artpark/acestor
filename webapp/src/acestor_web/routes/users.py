import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from acestor_web.audit import write_audit
from acestor_web.auth.passwords import WeakPasswordError, check_policy, hash_password
from acestor_web.db import get_db
from acestor_web.deps import require_admin
from acestor_web.models import User
from acestor_web.schemas import ResetPasswordRequest, UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/api/users", tags=["users"])


def _snapshot(u: User) -> dict:
    return {
        "email": u.email,
        "name": u.name,
        "auth_provider": (
            str(u.auth_provider.value)
            if hasattr(u.auth_provider, "value")
            else str(u.auth_provider)
        ),
        "is_admin": u.is_admin,
        "is_active": u.is_active,
    }


@router.get("", response_model=list[UserOut])
def list_users(
    include_inactive: bool = Query(False),
    session: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> list[User]:
    q = session.query(User)
    if not include_inactive:
        q = q.filter(User.is_active.is_(True))
    return q.order_by(User.email).all()


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> User:
    provider = payload.auth_provider.lower()
    if provider not in ("local", "google"):
        raise HTTPException(status_code=400, detail="invalid auth_provider")

    if provider == "local":
        if not payload.password:
            raise HTTPException(
                status_code=400, detail="password is required for local auth"
            )
        try:
            check_policy(payload.password)
        except WeakPasswordError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        pw_hash: str | None = hash_password(payload.password)
    else:
        if payload.password:
            raise HTTPException(
                status_code=400, detail="password must be null for google auth"
            )
        pw_hash = None

    from acestor_web.models.user import AuthProvider

    user = User(
        id=uuid.uuid4(),
        email=payload.email.lower(),
        name=payload.name,
        auth_provider=AuthProvider(provider),
        password_hash=pw_hash,
        is_admin=payload.is_admin,
        is_active=True,
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="email already exists") from None

    write_audit(
        session,
        user_id=admin.id,
        action="user.create",
        target_type="user",
        target_id=user.id,
        after=_snapshot(user),
    )
    session.commit()
    session.refresh(user)
    return user


@router.get("/{user_id}", response_model=UserOut)
def get_user(
    user_id: uuid.UUID,
    session: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    before = _snapshot(user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    session.flush()
    write_audit(
        session,
        user_id=admin.id,
        action="user.update",
        target_type="user",
        target_id=user.id,
        before=before,
        after=_snapshot(user),
    )
    session.commit()
    session.refresh(user)
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def disable_user(
    user_id: uuid.UUID,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> None:
    if user_id == admin.id:
        raise HTTPException(status_code=403, detail="cannot disable yourself")
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if not user.is_active:
        return  # idempotent — no audit row
    user.is_active = False
    session.flush()
    write_audit(
        session,
        user_id=admin.id,
        action="user.disable",
        target_type="user",
        target_id=user.id,
        before={"is_active": True},
        after={"is_active": False},
    )
    session.commit()


@router.post("/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(
    user_id: uuid.UUID,
    payload: ResetPasswordRequest,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> None:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")

    provider = (
        user.auth_provider.value
        if hasattr(user.auth_provider, "value")
        else str(user.auth_provider)
    )
    if provider != "local":
        raise HTTPException(status_code=400, detail="not applicable to this provider")

    try:
        check_policy(payload.new_password)
    except WeakPasswordError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    user.password_hash = hash_password(payload.new_password)
    user.password_changed_at = datetime.now(tz=UTC)
    session.flush()
    write_audit(
        session,
        user_id=admin.id,
        action="user.reset_password",
        target_type="user",
        target_id=user.id,
        before={},
        after={},
    )
    session.commit()
