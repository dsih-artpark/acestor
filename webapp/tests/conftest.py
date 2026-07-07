import os
import subprocess

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


@pytest.fixture(scope="session")
def _db_url() -> str:
    url = os.environ.get(
        "TEST_POSTGRES_URL",
        "postgresql+psycopg://acestor:acestor@localhost:5432/acestor_test",
    )
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    dbname = url.rsplit("/", 1)[1]
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {dbname}"))
        conn.execute(text(f"CREATE DATABASE {dbname}"))
    admin.dispose()
    return url


@pytest.fixture(scope="session")
def _migrated_engine(_db_url):
    env = os.environ.copy()
    env["POSTGRES_URL"] = _db_url
    subprocess.check_call(
        ["uv", "run", "alembic", "upgrade", "head"],
        env=env,
    )
    engine = create_engine(_db_url, future=True)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(_migrated_engine):
    connection = _migrated_engine.connect()
    trans = connection.begin()
    Session = sessionmaker(bind=connection, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()
