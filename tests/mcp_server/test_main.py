import sys
from typing import Any

import pytest
from pydantic import SecretStr

from auratrade import config
from auratrade.config import Settings
from auratrade.mcp_server import __main__ as entry
from auratrade.mcp_server.server import ServerDeps
from auratrade.mcp_server.sources import SourceError


class _Server:
    runs: list[str]

    def __init__(self, deps: ServerDeps) -> None:
        self.deps = deps
        self.runs = []
        _Server.last = self

    def run(self, transport: str) -> None:
        self.runs.append(transport)

    last: "_Server"


def _settings(**values: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url=SecretStr("postgresql+psycopg://u:pw@localhost:5434/db"),
        alpaca_api_key=SecretStr("key"),
        alpaca_secret_key=SecretStr("secret"),
        environment="paper",
        trading_mode="paper",
        **values,
    )


@pytest.fixture(autouse=True)
def _no_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entry, "build_server", _Server)


def test_public_reads_no_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> Settings:
        raise AssertionError("--public must not read settings or credentials")

    monkeypatch.setattr(config, "load_settings", refuse)

    assert entry.main(["--public", "--snapshot", "snap.json"]) == 0

    deps = _Server.last.deps
    assert deps.operator is None
    assert _Server.last.runs == ["stdio"]
    assert deps.now().tzinfo is not None


def test_operator_sources_from_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load_settings", _settings)

    assert entry.main(["--snapshot", "https://site.example/data/snapshot.json"]) == 0

    op = _Server.last.deps.operator
    assert op is not None
    try:
        assert op.environment == "paper"
        assert op.logs is not None
        assert op.logs.group("ingest") == "/aws/lambda/auratrade-paper-ingest"
        assert _Server.last.deps.redact("pw=secret") == "pw=***"
        # Built on first use; constructing them calls nothing.
        assert op.broker is not None
        assert type(op.broker()).__name__ == "AlpacaBroker"
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        assert op.logs._client().meta.service_model.service_name == "logs"
    finally:
        op.engine.dispose()


def test_logs_go_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []
    real = entry.configure_logging

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("stream"))
        return real(*args, **kwargs)

    monkeypatch.setattr(entry, "configure_logging", spy)
    monkeypatch.setattr(config, "load_settings", _settings)

    entry.main(["--public"])
    entry.main([])

    assert seen == [sys.stderr, sys.stderr]
    op = _Server.last.deps.operator
    assert op is not None
    op.engine.dispose()


def test_the_broker_needs_its_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config, "load_settings", lambda: _settings().model_copy(update={"alpaca_api_key": None})
    )
    entry.main([])
    op = _Server.last.deps.operator
    assert op is not None
    assert op.broker is not None
    try:
        with pytest.raises(SourceError, match="ALPACA_API_KEY"):
            op.broker()
    finally:
        op.engine.dispose()


def test_no_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config, "load_settings", lambda: _settings().model_copy(update={"database_url": None})
    )

    with pytest.raises(SystemExit, match="DATABASE_URL"):
        entry.main([])
