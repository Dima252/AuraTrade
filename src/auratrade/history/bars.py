"""One ticker's vendor bars → checked, outlier-corrected pieces, one per security (pure).

The same rules as the evening ingest (`ingest.bars`), so the history matches what Postgres
holds: the raw close attached (a session without one is dropped: it can't pass the
point-in-time price filter), the sanity check (zero-volume zombie bars after a delisting go),
then the split at gaps and level jumps (`securities.segments`), then the outlier filter within
each piece. The jump split comes before the outlier filter, which looks only at highs and lows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from auratrade.config import OutlierConfig
from auratrade.history.securities import segments
from auratrade.ingest.bars import (
    Correction,
    IngestBar,
    apply_corrections,
    combine,
    find_outliers,
    sanity_problem,
)

NO_RAW = "no raw bar"


@dataclass(frozen=True, slots=True)
class Piece:
    bars: list[IngestBar]
    corrections: list[Correction]

    @property
    def first(self) -> date:
        return self.bars[0].session

    @property
    def last(self) -> date:
        return self.bars[-1].session


@dataclass
class Prepared:
    pieces: list[Piece]
    dropped: dict[str, int] = field(default_factory=dict)


def prepare(
    adjusted: Sequence[IngestBar],
    raw_closes: Mapping[date, float],
    position: Mapping[date, int],
    *,
    max_gap: int,
    max_jump: float,
    outliers: OutlierConfig,
) -> Prepared:
    """`adjusted`: split-adjusted bars, oldest first (`raw_close` not yet attached);
    `position`: each session's index in the calendar."""
    dropped: dict[str, int] = {}
    combined, no_raw = combine(adjusted, raw_closes)
    if no_raw:
        dropped[NO_RAW] = len(no_raw)
    kept = []
    for bar in combined:
        problem = sanity_problem(bar)
        if problem is None:
            kept.append(bar)
        else:
            dropped[problem] = dropped.get(problem, 0) + 1
    pieces = []
    found_pieces = segments(
        [b.session for b in kept],
        position,
        max_gap,
        closes=[b.close for b in kept],
        max_jump=max_jump,
    )
    for first, last in found_pieces:
        bars = kept[first : last + 1]
        found = find_outliers(bars, outliers)
        pieces.append(Piece(apply_corrections(bars, found), found))
    return Prepared(pieces, dropped)
