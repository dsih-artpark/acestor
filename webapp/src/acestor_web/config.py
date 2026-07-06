from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource


class _CsvEnvSettingsSource(EnvSettingsSource):
    """Custom env source that treats list fields with CSV strings as comma-separated values."""

    CSV_FIELDS = {"auth_providers", "auth_allowed_domains"}

    def prepare_field_value(self, field_name, field, value, value_is_complex):
        if field_name in self.CSV_FIELDS and isinstance(value, str):
            items = [item.strip() for item in value.split(",") if item.strip()]
            return items
        return super().prepare_field_value(field_name, field, value, value_is_complex)


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
    auth_providers: list[str] = Field(default_factory=lambda: ["local"])
    auth_session_secret: str
    auth_google_client_id: str = ""
    auth_google_client_secret: str = ""
    auth_allowed_domains: list[str] = Field(default_factory=list)
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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (
            init_settings,
            _CsvEnvSettingsSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
