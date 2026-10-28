"""The single-writer lock (README §5.6.5): one account-wide Postgres advisory lock around every
section of code that changes orders.

- Taken on its own direct (unpooled) connection: session-level advisory locks don't work through
  Neon's transaction-mode pooler, so a `-pooler` host is swapped for the direct one.
- Released in `finally`. A Lambda that times out or is frozen may keep its connection open, so
  the connection also sets `idle_session_timeout`: Postgres ends it, and frees the lock, after
  that long without a statement. Order sections are far shorter.
- Order jobs wait for it (alerting after `lock_wait_alert_seconds`, failing after
  `lock_wait_max_seconds`). `stop_guard` doesn't wait: a held lock may mean a sell sequence is
  legitimately between "reduce the stop" and "sell", so it skips to its next run.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

import psycopg
import structlog
from sqlalchemy.engine import make_url

from auratrade.config import ExecutionConfig

log = structlog.get_logger(__name__)

# The lock's key: one per account, and each environment has its own database branch.
LOCK_KEY = 0x4155_5241_0001  # "AURA" + 1
_CLASS_ID, _OBJ_ID = LOCK_KEY >> 32, LOCK_KEY & 0xFFFF_FFFF
APPLICATION_PREFIX = "auratrade-lock"


class LockTimeoutError(RuntimeError):
    pass


def direct_conninfo(url: str) -> str:
    """A libpq connection string for `url`, on Neon's direct host rather than its pooler."""
    parsed = make_url(url).set(drivername="postgresql")
    host = parsed.host
    if host is not None:
        first, _, rest = host.partition(".")
        if first.endswith("-pooler"):
            parsed = parsed.set(host=f"{first.removesuffix('-pooler')}.{rest}")
    return parsed.render_as_string(hide_password=False)


@dataclass(frozen=True)
class Holder:
    application_name: str
    held_for: timedelta
    """Measured by the server: the holder's connection is opened just to take the lock."""


@dataclass
class OrderLock:
    url: str
    cfg: ExecutionConfig
    alert: Callable[[str], None]
    sleep: Callable[[float], None] = time.sleep
    poll_seconds: float = 1.0

    def _connect(self, job: str) -> psycopg.Connection[tuple[object, ...]]:
        conn = psycopg.connect(
            direct_conninfo(self.url),
            autocommit=True,
            connect_timeout=10,
            application_name=f"{APPLICATION_PREFIX}:{job}",
        )
        minutes = int(self.cfg.lock_idle_timeout_minutes)
        conn.execute(f"SET idle_session_timeout = '{minutes}min'")
        return conn

    @staticmethod
    def _try(conn: psycopg.Connection[tuple[object, ...]]) -> bool:
        row = conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()
        return bool(row and row[0])

    @contextmanager
    def hold(self, job: str, *, wait: bool = True) -> Iterator[bool]:
        """Yield True while holding the lock. Without `wait`, yield False at once if another
        job holds it."""
        conn = self._connect(job)
        acquired = False
        try:
            acquired = self._try(conn)
            if not acquired and not wait:
                yield False
                return
            waited = 0.0
            alerted = False
            while not acquired:
                if waited >= self.cfg.lock_wait_max_seconds:
                    raise LockTimeoutError(
                        f"{job} waited {waited:.0f} s for the order lock; held by {self.holder()}"
                    )
                if waited >= self.cfg.lock_wait_alert_seconds and not alerted:
                    self.alert(f"{job} has waited {waited:.0f} s for the order lock")
                    alerted = True
                self.sleep(self.poll_seconds)
                waited += self.poll_seconds
                acquired = self._try(conn)
            log.info("order_lock_taken", job=job)
            yield True
        finally:
            try:
                if acquired and not conn.closed:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
                    log.info("order_lock_released", job=job)
            finally:
                conn.close()

    def holder(self) -> Holder | None:
        """Who holds the lock, and for how long."""
        with psycopg.connect(direct_conninfo(self.url), autocommit=True, connect_timeout=10) as c:
            row = c.execute(
                "SELECT a.application_name, now() - a.backend_start FROM pg_locks l "
                "JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.locktype = 'advisory' AND l.granted "
                "AND l.classid = %s AND l.objid = %s AND l.objsubid = 1",
                (_CLASS_ID, _OBJ_ID),
            ).fetchone()
        if row is None:
            return None
        name, held_for = row
        return Holder(str(name), held_for if isinstance(held_for, timedelta) else timedelta(0))
