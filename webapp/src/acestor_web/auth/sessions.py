import uuid

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from acestor_web.config import get_settings

SESSION_COOKIE_NAME = "acestor_session"
_SALT = "acestor-session-v1"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().auth_session_secret, salt=_SALT)


def _max_age_seconds(ttl_days: int) -> int:
    return ttl_days * 24 * 3600


def create_session_cookie(
    response: Response, user_id: uuid.UUID, ttl_days: int = 30
) -> None:
    token = _serializer().dumps({"uid": str(user_id)})
    settings = get_settings()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=_max_age_seconds(ttl_days),
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )


def read_session(request: Request) -> uuid.UUID | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=_max_age_seconds(30))
    except (BadSignature, SignatureExpired):
        return None
    uid = data.get("uid")
    if not isinstance(uid, str):
        return None
    try:
        return uuid.UUID(uid)
    except ValueError:
        return None


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
