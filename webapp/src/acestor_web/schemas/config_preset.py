import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ConfigPresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    yaml_text: str = Field(min_length=1)


class ConfigPresetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    yaml_text: str | None = Field(default=None, min_length=1)
    archived: bool | None = None


class ConfigPresetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    yaml_text: str
    created_by: uuid.UUID
    archived: bool
    created_at: datetime
    updated_at: datetime
