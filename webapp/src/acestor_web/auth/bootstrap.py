import logging
import uuid
from collections.abc import Callable

from acestor_web.auth.passwords import WeakPasswordError, check_policy, hash_password
from acestor_web.config import get_settings
from acestor_web.db import SessionLocal
from acestor_web.models import AuthProvider, User

log = logging.getLogger(__name__)


def bootstrap_admin_if_needed(session_factory: Callable = SessionLocal) -> None:
    s = get_settings()
    if s.auth_provider != "local":
        return

    session = session_factory()
    try:
        if session.query(User).count() > 0:
            return

        if not s.initial_admin_email or not s.initial_admin_password:
            log.warning(
                "No users exist and INITIAL_ADMIN_EMAIL/INITIAL_ADMIN_PASSWORD are unset. "
                "Set them or create an admin via `python -m acestor_web.cli users create`."
            )
            return

        try:
            check_policy(s.initial_admin_password)
        except WeakPasswordError as e:
            log.error("INITIAL_ADMIN_PASSWORD fails password policy: %s. Skipping.", e)
            return

        u = User(
            id=uuid.uuid4(),
            email=s.initial_admin_email.lower(),
            name=s.initial_admin_email.split("@")[0],
            auth_provider=AuthProvider.local,
            password_hash=hash_password(s.initial_admin_password),
            is_admin=True,
        )
        session.add(u)
        session.commit()
        log.info("Bootstrapped initial admin user: %s", u.email)
    finally:
        if session_factory is SessionLocal:
            session.close()
