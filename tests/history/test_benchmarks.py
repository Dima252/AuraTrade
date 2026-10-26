from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from auratrade.data.alpaca_data import AlpacaDataClient, alpaca_http
from auratrade.history.benchmarks import benchmark_history, load_benchmarks
from auratrade.storage import LocalStore
from tests.data_clients.fake import Server, fixture_json, ok

SESSIONS = [date(2026, 9, d) for d in (16, 17, 18, 21, 22)]


def client(server: Server) -> AlpacaDataClient:
    http = alpaca_http("PKTESTKEY", "test-secret", client=server.client(), sleep=lambda _: None)
    return AlpacaDataClient(http, now=lambda: datetime(2026, 10, 26, 21, 0, tzinfo=UTC))


def test_total_return_bars_are_fetched_and_stored_apart(tmp_path: Path) -> None:
    server = Server(ok(fixture_json("alpaca/bars_daily_all.json")))
    store = LocalStore(tmp_path)
    # 09-22 isn't a session here: the stage keeps calendar sessions only.
    sessions = set(SESSIONS[:4])
    counts = benchmark_history(store, client(server), SESSIONS[0], SESSIONS[-1], sessions)
    params = server.params(0)
    assert params["adjustment"] == "all"
    assert params["symbols"] == "MTUM,SPY"
    assert counts == {"SPY": 4, "MTUM": 4}
    loaded = load_benchmarks(store)
    assert loaded["MTUM"].dates == SESSIONS[:4]
    assert loaded["MTUM"].closes[:3] == [299.83, 306.12, 309.62]
    assert len(loaded["SPY"].closes) == 4


def test_a_missing_benchmark_fails_the_stage(tmp_path: Path) -> None:
    server = Server(ok(fixture_json("alpaca/bars_daily_all.json")))
    with pytest.raises(ValueError, match="QQQ"):
        benchmark_history(
            LocalStore(tmp_path),
            client(server),
            SESSIONS[0],
            SESSIONS[-1],
            set(SESSIONS),
            symbols=("SPY", "QQQ"),
        )


def test_loading_without_the_stage_says_how_to_run_it(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"backfill.py benchmarks"):
        load_benchmarks(LocalStore(tmp_path))
