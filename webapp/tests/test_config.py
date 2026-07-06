import os

from acestor_web.config import Settings


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("POSTGRES_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("APP_NAME", "acestor")
    monkeypatch.setenv("AUTH_PROVIDERS", "google,local")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "s")
    monkeypatch.setenv("S3_BUCKET", "b")
    monkeypatch.setenv("S3_REGION", "us-east-1")

    s = Settings()

    assert str(s.postgres_url) == "postgresql+psycopg://u:p@h/db"
    assert s.app_name == "acestor"
    assert s.auth_providers == ["google", "local"]
    assert s.s3_bucket == "b"


def test_auth_providers_defaults_to_local(monkeypatch):
    for k in list(os.environ):
        if k.startswith("AUTH_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AUTH_SESSION_SECRET", "s")
    monkeypatch.setenv("POSTGRES_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("S3_BUCKET", "b")
    monkeypatch.setenv("S3_REGION", "us-east-1")

    s = Settings()

    assert s.auth_providers == ["local"]
