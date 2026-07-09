from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_name: str = "acestor"
    app_logo_text: str = "acestor"
    app_support_url: str = ""

    # Postgres
    postgres_url: str

    # Auth
    auth_provider: str = "local"
    auth_session_secret: str
    session_cookie_secure: bool = False
    auth_google_client_id: str = ""
    auth_google_client_secret: str = ""
    auth_allowed_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)
    initial_admin_email: str = ""
    initial_admin_password: str = ""

    # S3
    s3_endpoint_url: str = ""
    s3_region: str
    s3_bucket: str
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_use_path_style: bool = False

    # Locale
    default_timezone: str = "UTC"

    @field_validator("auth_allowed_domains", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
