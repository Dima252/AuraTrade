"""Throwaway Postgres databases for tests (pytest plugin, loaded by tests/conftest.py).

The Docker Postgres from docker-compose.yml is shared with other work, so each test
session creates its own uniquely named database and drops it afterwards. Without a
reachable server the database tests are skipped, unless AURATRADE_REQUIRE_DB=1 (CI).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, delete, insert
from sqlalchemy.engine import make_url

from auratrade.db.engine import make_engine
from auratrade.db.flags import SEED
from auratrade.db.models import Base, SystemFlag

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "postgresql+psycopg://auratrade:auratrade@localhost:5432/auratrade"


def server_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_URL)


def _admin_conninfo() -> str:
    url = make_url(server_url()).set(drivername="postgresql", database="postgres")
    return url.render_as_string(hide_password=False)


@contextmanager
def temporary_database(prefix: str = "auratrade_test") -> Iterator[str]:
    """Create an empty database; yield its SQLAlchemy URL; drop it."""
    name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(_admin_conninfo(), autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("AURATRADE_REQUIRE_DB") == "1":
            raise
        pytest.skip(f"Postgres not reachable ({type(exc).__name__}); run `docker compose up -d`")
    with admin:
        admin.execute(f'CREATE DATABASE "{name}"')
        try:
            yield make_url(server_url()).set(database=name).render_as_string(hide_password=False)
        finally:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def alembic_config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    return config


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


@pytest.fixture(scope="session")
def migrated_url() -> Iterator[str]:
    with temporary_database() as url:
        upgrade(url)
        yield url


@pytest.fixture(scope="session")
def _session_engine(migrated_url: str) -> Iterator[Engine]:
    engine = make_engine(migrated_url)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_engine(_session_engine: Engine) -> Iterator[Engine]:
    """The migrated test database, emptied (and flags re-seeded) after each test."""
    yield _session_engine
    with _session_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(delete(table))
        conn.execute(insert(SystemFlag), [{"name": n, "value": v} for n, v in SEED.items()])
