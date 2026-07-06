import os

from acestor_web.config import Settings


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("POSTGRES_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("APP_NAME", "acestor")
    monkeypatch.setenv("AUTH_PROVIDER", "google")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "s")
    monkeypatch.setenv("AUTH_ALLOWED_DOMAINS", "artpark.in,partner.org")
    monkeypatch.setenv("S3_BUCKET", "b")
    monkeypatch.setenv("S3_REGION", "us-east-1")

    s = Settings()

    assert s.postgres_url == "postgresql+psycopg://u:p@h/db"
    assert s.app_name == "acestor"
    assert s.auth_provider == "google"
    assert s.auth_allowed_domains == ["artpark.in", "partner.org"]
    assert s.s3_bucket == "b"


def test_auth_provider_defaults_to_local(monkeypatch):
    for k in list(os.environ):
        if k.startswith("AUTH_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AUTH_SESSION_SECRET", "s")
    monkeypatch.setenv("POSTGRES_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("S3_BUCKET", "b")
    monkeypatch.setenv("S3_REGION", "us-east-1")

    s = Settings()

    assert s.auth_provider == "local"
    assert s.auth_allowed_domains == []


def test_allowed_domains_single_value(monkeypatch):
    monkeypatch.setenv("POSTGRES_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "s")
    monkeypatch.setenv("AUTH_ALLOWED_DOMAINS", "artpark.in")
    monkeypatch.setenv("S3_BUCKET", "b")
    monkeypatch.setenv("S3_REGION", "us-east-1")

    s = Settings()

    assert s.auth_allowed_domains == ["artpark.in"]
