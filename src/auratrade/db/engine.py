from __future__ import annotations

from sqlalchemy import Engine, create_engine

from auratrade.config import Settings


def make_engine(url: str) -> Engine:
    """Engine for a `postgresql+psycopg://` URL (local Docker or Neon over TLS).

    `pool_pre_ping` because Neon suspends idle compute and drops the connections."""
    return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2)


def engine_from_settings(settings: Settings) -> Engine:
    if settings.database_url is None:
        raise ValueError("DATABASE_URL is not set")
    return make_engine(settings.database_url.get_secret_value())
