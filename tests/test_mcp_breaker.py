"""The remote MCP server's circuit breaker (#38)."""

from typing import Any

import pytest

from auratrade import mcp_breaker
from auratrade.notify.alerts import LogAlerter
from auratrade.notify.telegram import TelegramNotifier


class _Lambda:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.concurrency: dict[str, int] = {}

    def put_function_concurrency(
        self,
        *,
        FunctionName: str,  # noqa: N803
        ReservedConcurrentExecutions: int,  # noqa: N803
    ) -> None:
        self.calls.append("off")
        self.concurrency[FunctionName] = ReservedConcurrentExecutions


class _Alerter:
    def __init__(self, calls: list[str], *, fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail
        self.texts: list[str] = []

    def alert(self, text: str) -> None:
        self.calls.append("alert")
        self.texts.append(text)
        if self.fail:
            raise RuntimeError("telegram down")


class _Ssm:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.asked: list[str] = []

    def get_parameters(self, *, Names: list[str], WithDecryption: bool) -> dict[str, Any]:  # noqa: N803
        assert WithDecryption
        self.asked = Names
        return {
            "Parameters": [{"Name": n, "Value": self.values[n]} for n in Names if n in self.values]
        }


def test_off_first_then_the_alert() -> None:
    calls: list[str] = []
    client, alerter = _Lambda(calls), _Alerter(calls)

    mcp_breaker.trip("auratrade-mcp", client, alerter, "Threshold Crossed")

    assert calls == ["off", "alert"]
    assert client.concurrency == {"auratrade-mcp": 0}
    assert "Threshold Crossed" in alerter.texts[0]
    assert "--reserved-concurrent-executions 1" in alerter.texts[0]


def test_a_failed_alert_leaves_it_off() -> None:
    calls: list[str] = []
    client = _Lambda(calls)

    with pytest.raises(RuntimeError):
        mcp_breaker.trip("auratrade-mcp", client, _Alerter(calls, fail=True), "flood")

    assert client.concurrency == {"auratrade-mcp": 0}


def test_the_alerter_reads_the_two_parameters_alone() -> None:
    path = "/auratrade/paper/"
    ssm = _Ssm({path + "TELEGRAM_BOT_TOKEN": "t", path + "TELEGRAM_CHAT_ID": "1"})

    assert isinstance(mcp_breaker._alerter(ssm, path), TelegramNotifier)
    assert ssm.asked == [path + "TELEGRAM_BOT_TOKEN", path + "TELEGRAM_CHAT_ID"]
    assert isinstance(mcp_breaker._alerter(_Ssm({}), path), LogAlerter)
    assert isinstance(mcp_breaker._alerter(ssm, None), LogAlerter)


def _alarm(value: str) -> dict[str, Any]:
    return {"alarmData": {"state": {"value": value, "reason": "Threshold Crossed: 700 > 600"}}}


@pytest.mark.parametrize(("state", "tripped"), [("ALARM", True), ("OK", False)])
def test_the_handler(monkeypatch: pytest.MonkeyPatch, state: str, tripped: bool) -> None:
    import boto3

    calls: list[str] = []
    client = _Lambda(calls)
    clients = {"lambda": client, "ssm": _Ssm({})}
    monkeypatch.setattr(boto3, "client", lambda name: clients[name])
    monkeypatch.setenv(mcp_breaker.FUNCTION, "auratrade-mcp")
    monkeypatch.setenv(mcp_breaker.PARAMETER_PATH, "/auratrade/paper/")

    mcp_breaker.handler(_alarm(state), None)

    assert client.concurrency == ({"auratrade-mcp": 0} if tripped else {})
