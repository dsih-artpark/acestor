"""CLI tests for acestor_web.cli users subcommand."""

import os
import subprocess
import sys

STRONG_PASSWORD = "S3cur3P@ssw0rd!"

_ENV_VARS = {
    "POSTGRES_URL": os.environ.get(
        "POSTGRES_URL",
        "postgresql+psycopg://acestor:acestor@localhost:5432/acestor_test",
    ),
    "AUTH_SESSION_SECRET": os.environ.get("AUTH_SESSION_SECRET", "test"),
    "AUTH_PROVIDER": "devstub",
    "S3_BUCKET": os.environ.get("S3_BUCKET", "acestor-test"),
    "S3_REGION": os.environ.get("S3_REGION", "us-east-1"),
}


def _run(*args, input_text: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(_ENV_VARS)
    return subprocess.run(
        [sys.executable, "-m", "acestor_web.cli", *args],
        capture_output=True,
        text=True,
        input=input_text,
        env=env,
    )


# ---------------------------------------------------------------------------
# create + list
# ---------------------------------------------------------------------------


def test_create_and_list(db_session):
    email = "cliuser@example.com"
    result = _run(
        "users", "create", email, "--password", STRONG_PASSWORD, "--name", "CLI User"
    )
    assert result.returncode == 0, result.stderr
    user_id = result.stdout.strip()
    # should be a valid UUID
    import uuid

    uuid.UUID(user_id)

    result2 = _run("users", "list")
    assert result2.returncode == 0, result2.stderr
    assert email in result2.stdout


def test_create_admin_flag(db_session):
    email = "cliadmin@example.com"
    result = _run("users", "create", email, "--password", STRONG_PASSWORD, "--admin")
    assert result.returncode == 0, result.stderr

    list_result = _run("users", "list")
    assert email in list_result.stdout


def test_create_duplicate_fails(db_session):
    email = "clidup@example.com"
    _run("users", "create", email, "--password", STRONG_PASSWORD)
    result = _run("users", "create", email, "--password", STRONG_PASSWORD)
    assert result.returncode != 0


def test_create_weak_password_fails(db_session):
    result = _run("users", "create", "weakpw@example.com", "--password", "short")
    assert result.returncode != 0
    assert "error" in result.stderr.lower()


# ---------------------------------------------------------------------------
# disable + enable
# ---------------------------------------------------------------------------


def test_disable_and_enable(db_session):
    email = "toenable@example.com"
    _run("users", "create", email, "--password", STRONG_PASSWORD)

    disable_result = _run("users", "disable", email)
    assert disable_result.returncode == 0, disable_result.stderr
    assert "disabled" in disable_result.stdout

    enable_result = _run("users", "enable", email)
    assert enable_result.returncode == 0, enable_result.stderr
    assert "enabled" in enable_result.stdout


def test_disable_nonexistent_fails(db_session):
    result = _run("users", "disable", "ghost@example.com")
    assert result.returncode != 0


def test_disable_idempotent(db_session):
    email = "idempotent@example.com"
    _run("users", "create", email, "--password", STRONG_PASSWORD)
    _run("users", "disable", email)
    result = _run("users", "disable", email)
    assert result.returncode == 0  # idempotent — no error


# ---------------------------------------------------------------------------
# set-password
# ---------------------------------------------------------------------------


def test_set_password(db_session):
    email = "setpw@example.com"
    _run("users", "create", email, "--password", STRONG_PASSWORD)

    result = _run("users", "set-password", email, "--password", "NewStr0ng!pass99")
    assert result.returncode == 0, result.stderr
    assert "updated" in result.stdout


def test_set_password_weak_fails(db_session):
    email = "weaksetpw@example.com"
    _run("users", "create", email, "--password", STRONG_PASSWORD)
    result = _run("users", "set-password", email, "--password", "weak")
    assert result.returncode != 0


def test_set_password_nonexistent_fails(db_session):
    result = _run(
        "users", "set-password", "ghost2@example.com", "--password", STRONG_PASSWORD
    )
    assert result.returncode != 0
