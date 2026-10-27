"""The `simulate` job on the test database: the books, and nothing on incomplete data (the
books themselves are tested through `journal.portfolio` in `tests/journal`)."""

from __future__ import annotations

from datetime import date

from auratrade.jobs import simulate
from tests.execution.conftest import World

T = date(2026, 10, 22)


class NoFunds:
    def daily_bars(self, *_args: object, **_kwargs: object) -> dict[str, list[object]]:
        raise AssertionError("no bars are fetched before the trial starts")


def test_nothing_before_the_trial_starts(world: World) -> None:
    spec = simulate.make_job(
        engine_factory=lambda _s: world.engine, funds_factory=lambda _s: NoFunds()
    )
    out = dict(spec.run(world.context("simulate", session=T)) or {})
    assert out["books"] == {"skipped": "the trial hasn't started"}


def test_the_job_skips_incomplete_data(world: World) -> None:
    world.set_session(T, data_status="INCOMPLETE")
    spec = simulate.make_job(engine_factory=lambda _s: world.engine)
    assert spec.run(world.context("simulate", session=T)) == {"skipped": "data status INCOMPLETE"}
