import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: uuid.UUID | None
    action: str
    target_type: str
    target_id: uuid.UUID | None
    before_json: dict[str, Any] | None
    after_json: dict[str, Any] | None
    at: datetime
