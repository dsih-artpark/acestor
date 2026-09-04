import uuid
from typing import Any

from sqlalchemy.orm import Session

from acestor_web.models import AuditLog


def write_audit(
    session: Session,
    *,
    user_id: uuid.UUID | None,
    action: str,
    target_type: str,
    target_id: uuid.UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Append one audit entry to the session. Caller is responsible for commit."""
    entry = AuditLog(
        user_id=user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before_json=before,
        after_json=after,
    )
    session.add(entry)
