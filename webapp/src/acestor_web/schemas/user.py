import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class UserCreate(BaseModel):
    email: EmailStr
    name: str = ""
    auth_provider: str
    is_admin: bool = False
    password: str | None = None


class UserUpdate(BaseModel):
    name: str | None = None
    is_admin: bool | None = None
    is_active: bool | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    auth_provider: str
    is_admin: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ResetPasswordRequest(BaseModel):
    new_password: str
