import logging
import uuid

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from acestor_web.auth.sessions import create_session_cookie
from acestor_web.config import get_settings
from acestor_web.db import get_db
from acestor_web.models import AuthProvider, User

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/google", tags=["auth"])


def _provider_gate() -> None:
    if get_settings().auth_provider != "google":
        raise HTTPException(status_code=404, detail="not found")


def _oauth_client() -> OAuth:
    s = get_settings()
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=s.auth_google_client_id,
        client_secret=s.auth_google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


async def _authorize_access_token(request: Request) -> dict:
    """Thin async wrapper so tests can patch this instead of hitting Google."""
    oauth = _oauth_client()
    return await oauth.google.authorize_access_token(request)


@router.get("/login")
async def login(request: Request):
    _provider_gate()
    s = get_settings()
    oauth = _oauth_client()
    redirect_uri = request.url_for("google_callback")
    kwargs = {}
    if len(s.auth_allowed_domains) == 1:
        kwargs["hd"] = s.auth_allowed_domains[0]
    return await oauth.google.authorize_redirect(request, str(redirect_uri), **kwargs)


REJECTION_PAGE = """\
<!doctype html>
<html><head><title>Access denied</title></head>
<body style="font-family:system-ui;padding:2rem;max-width:36rem;">
<h1 style="font-weight:600;font-size:1.25rem;">Access denied</h1>
<p>Access is not permitted for accounts on domain
<code>{domain}</code>. Contact your administrator.</p>
</body></html>"""


@router.get("/callback", name="google_callback")
async def callback(request: Request, session: Session = Depends(get_db)):
    _provider_gate()
    s = get_settings()

    token = await _authorize_access_token(request)
    info = token.get("userinfo") or {}

    email = (info.get("email") or "").lower()
    email_verified = bool(info.get("email_verified"))
    name = info.get("name") or (email.split("@")[0] if email else "user")

    if not email or not email_verified:
        domain = email.split("@")[-1] if "@" in email else "unknown"
        return HTMLResponse(REJECTION_PAGE.format(domain=domain), status_code=403)

    if s.auth_allowed_domains:
        domain = email.split("@")[-1]
        if domain not in s.auth_allowed_domains:
            return HTMLResponse(REJECTION_PAGE.format(domain=domain), status_code=403)

    user = session.query(User).filter_by(email=email).one_or_none()
    if user is None:
        user = User(
            id=uuid.uuid4(),
            email=email,
            name=name,
            auth_provider=AuthProvider.google,
            password_hash=None,
            is_admin=False,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
    elif user.auth_provider != AuthProvider.google or not user.is_active:
        return HTMLResponse(
            REJECTION_PAGE.format(domain=email.split("@")[-1]), status_code=403
        )

    resp = RedirectResponse(url="/", status_code=302)
    create_session_cookie(resp, user.id)
    return resp
