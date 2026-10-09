"""The evening chain and its fallback start times (README §2, §3), all America/New_York.

The chain is the single source for the runner and, from task 1.8, the scheduler stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class ChainLink:
    job: str
    # The scheduled start. For every job after the first it is a fallback: the job normally
    # starts when its predecessor succeeds and invokes it.
    fallback_start: time


EVENING_CHAIN: tuple[ChainLink, ...] = (
    ChainLink("post_market", time(16, 20)),
    ChainLink("ingest", time(17, 0)),
    ChainLink("simulate", time(17, 15)),
    ChainLink("regime", time(17, 25)),
    ChainLink("scan", time(17, 35)),
    ChainLink("analyst_submit", time(17, 50)),
)

_INDEX = {link.job: i for i, link in enumerate(EVENING_CHAIN)}


def chain_link(job: str) -> ChainLink | None:
    i = _INDEX.get(job)
    return None if i is None else EVENING_CHAIN[i]


def predecessor(job: str) -> str | None:
    i = _INDEX.get(job)
    return None if i is None or i == 0 else EVENING_CHAIN[i - 1].job


def successor(job: str) -> str | None:
    i = _INDEX.get(job)
    return None if i is None or i == len(EVENING_CHAIN) - 1 else EVENING_CHAIN[i + 1].job
