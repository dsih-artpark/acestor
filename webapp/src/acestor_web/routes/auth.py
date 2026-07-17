from fastapi import APIRouter, Depends, Response

from acestor_web.auth.sessions import clear_session_cookie
from acestor_web.deps import get_current_user
from acestor_web.models import User
from acestor_web.schemas.auth import MeOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/logout", status_code=204)
def logout(response: Response) -> None:
    clear_session_cookie(response)


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)) -> MeOut:
    return MeOut(
        id=user.id,
        email=user.email,
        name=user.name,
        is_admin=user.is_admin,
        auth_provider=user.auth_provider.value,
    )
