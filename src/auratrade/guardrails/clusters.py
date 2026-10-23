"""Correlated clusters (README §8, #53).

A cluster is the same industry group **or** a daily-return correlation >= 0.7 over the last 60
sessions; at most 2 positions per cluster. A new entry is rejected when it is linked to
``max_per_cluster`` positions already held, pending or approved. A correlation that can't be
computed (too little history, a flat series) counts as linked: fail closed.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from auratrade.config import GuardrailsConfig
from auratrade.guardrails.model import Finding, GuardrailRule, reject


def daily_returns(closes: Sequence[float]) -> list[float]:
    return [b / a - 1 for a, b in pairwise(closes)]


def return_correlation(a: Sequence[float], b: Sequence[float], sessions: int) -> float | None:
    """Pearson correlation of the last ``sessions`` daily returns of two close series aligned
    on the same sessions; None without enough finite, positive data or with a flat series."""
    if len(a) <= sessions or len(b) <= sessions:
        return None
    a, b = a[-sessions - 1 :], b[-sessions - 1 :]
    if not all(math.isfinite(v) and v > 0 for v in (*a, *b)):
        return None
    ra, rb = daily_returns(a), daily_returns(b)
    mean_a, mean_b = sum(ra) / sessions, sum(rb) / sessions
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb, strict=True))
    var_a = sum((x - mean_a) ** 2 for x in ra)
    var_b = sum((y - mean_b) ** 2 for y in rb)
    if var_a <= 0 or var_b <= 0:
        return None
    return cov / math.sqrt(var_a * var_b)


@dataclass(frozen=True, slots=True)
class ClusterMember:
    symbol: str
    industry: str | None
    """The industry group (SIC); None when unknown."""
    closes: Sequence[float]
    """Closes on the same sessions as every other member's, oldest first."""


def linked(a: ClusterMember, b: ClusterMember, config: GuardrailsConfig) -> bool:
    if a.industry is not None and a.industry == b.industry:
        return True
    corr = return_correlation(a.closes, b.closes, config.cluster_correlation_sessions)
    return corr is None or corr >= config.cluster_correlation


def cluster_check(
    candidate: ClusterMember, members: Sequence[ClusterMember], config: GuardrailsConfig
) -> Finding | None:
    peers = [m.symbol for m in members if linked(candidate, m, config)]
    if len(peers) >= config.max_per_cluster:
        return reject(
            GuardrailRule.CLUSTER,
            f"{candidate.symbol} is in a cluster with {', '.join(sorted(peers))}",
            len(peers) + 1,
        )
    return None
