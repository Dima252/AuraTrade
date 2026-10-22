"""scripts/put_parameters.py: the refusals, the shared-key fallback, the plan output (names only)
and the writes, against a fake SSM client; nothing reaches AWS."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts import put_parameters as pp

ACCOUNT = {
    "ALPACA_API_KEY": "PKPAPER123",
    "ALPACA_SECRET_KEY": "paper-secret-456",
    "DATABASE_URL": "postgresql+psycopg://user:dbpassword@ep-x.neon.tech/app",
    "HEALTHCHECKS_PING_URL": "https://hc-ping.com/secret-uuid",
}
SHARED = {
    "ANTHROPIC_API_KEY": "sk-ant-secret-789",
    "FRED_API_KEY": "fredkey0123456789",
    "FINNHUB_API_KEY": "finnhubkey012",
    "EDGAR_USER_AGENT": "AuraTrade research owner@realmail.net",
    "TELEGRAM_BOT_TOKEN": "123456:telegramtoken",
    "TELEGRAM_CHAT_ID": "42",
}
VALUES = {**ACCOUNT, **SHARED}
DEV = {"ALPACA_API_KEY": "PKDEV", "ANTHROPIC_API_KEY": "sk-ant-dev", "TELEGRAM_CHAT_ID": "7"}


class FakeSSM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_parameter(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def write_env(path: Path, values: dict[str, str]) -> Path:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return path


def test_the_plan_takes_every_required_key_and_leaves_the_rest_out() -> None:
    plan = pp.make_plan({**VALUES, "TRADING_MODE": "paper", "ANALYST_MODEL": "x"}, DEV)
    assert plan.values == {k: VALUES[k] for k in pp.REQUIRED}
    assert plan.shared_from_dev == []
    assert plan.left_out == ["ANALYST_MODEL", "TRADING_MODE"]


def test_empty_shared_keys_fall_back_to_dev() -> None:
    plan = pp.make_plan({**VALUES, "ANTHROPIC_API_KEY": "", "TELEGRAM_CHAT_ID": ""}, DEV)
    assert plan.shared_from_dev == ["ANTHROPIC_API_KEY", "TELEGRAM_CHAT_ID"]
    assert plan.values["ANTHROPIC_API_KEY"] == "sk-ant-dev"
    assert plan.values["TELEGRAM_CHAT_ID"] == "7"
    assert plan.values["FRED_API_KEY"] == SHARED["FRED_API_KEY"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"ALPACA_SECRET_KEY": ""}, "ALPACA_SECRET_KEY is missing or empty"),
        ({"FRED_API_KEY": ""}, "FRED_API_KEY is missing or empty"),
        (
            {"DATABASE_URL": "postgresql+psycopg://auratrade:auratrade@localhost:5432/auratrade"},
            "points at this machine",
        ),
        ({"ALPACA_API_KEY": "PKDEV"}, "the dev account's key"),
    ],
)
def test_refusals(change: dict[str, str], message: str) -> None:
    with pytest.raises(pp.RefusedError, match=message):
        pp.make_plan({**VALUES, **change}, DEV)


def test_account_keys_never_fall_back_to_dev() -> None:
    values = {k: v for k, v in VALUES.items() if k != "HEALTHCHECKS_PING_URL"}
    with pytest.raises(pp.RefusedError, match="HEALTHCHECKS_PING_URL is missing"):
        pp.make_plan(values, {**DEV, "HEALTHCHECKS_PING_URL": "https://hc-ping.com/dev"})


def test_put_writes_secure_strings_under_the_path() -> None:
    fake = FakeSSM()
    pp.put(fake, "paper", {"ALPACA_API_KEY": "PKPAPER123"})
    assert fake.calls == [
        {
            "Name": "/auratrade/paper/ALPACA_API_KEY",
            "Value": "PKPAPER123",
            "Type": "SecureString",
            "Overwrite": True,
            "Tier": "Standard",
        }
    ]


def test_main_plans_without_aws_and_prints_names_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pp, "ssm_client", lambda _p: pytest.fail("no AWS call without --apply"))
    env = write_env(
        tmp_path / ".env.paper", {**VALUES, "ANTHROPIC_API_KEY": "", "TRADING_MODE": "paper"}
    )
    dev = write_env(tmp_path / ".env", DEV)
    code = pp.main(["--env", "paper", "--env-file", str(env), "--dev-env-file", str(dev)])
    out = capsys.readouterr().out
    assert code == 0
    assert "would put /auratrade/paper/ALPACA_API_KEY\n" in out
    assert f"would put /auratrade/paper/ANTHROPIC_API_KEY (from {dev})" in out
    assert "left out: TRADING_MODE (set by the stack)" in out
    assert "plan only" in out
    for value in [*VALUES.values(), *DEV.values()]:
        assert value not in out


def test_main_applies_with_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeSSM()
    profiles: list[str | None] = []

    def client(profile: str | None) -> FakeSSM:
        profiles.append(profile)
        return fake

    monkeypatch.setattr(pp, "ssm_client", client)
    env = write_env(tmp_path / ".env.paper", {**VALUES, "EXTRA": "x"})
    argv = ["--env", "paper", "--env-file", str(env), "--dev-env-file", str(tmp_path / "none")]
    assert pp.main([*argv, "--apply", "--profile", "auratrade"]) == 0
    assert profiles == ["auratrade"]
    assert {c["Name"].rsplit("/", 1)[1] for c in fake.calls} == set(pp.REQUIRED)
    out = capsys.readouterr().out
    assert "left out: EXTRA (not used in AWS)" in out
    assert "done: 10 SecureString parameters under /auratrade/paper/" in out
    for value in VALUES.values():
        assert value not in out


def test_main_refuses_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pp, "ssm_client", lambda _p: pytest.fail("must not write"))
    env = write_env(tmp_path / ".env.paper", {**VALUES, "DATABASE_URL": "postgresql://localhost/x"})
    argv = ["--env", "paper", "--env-file", str(env), "--dev-env-file", str(tmp_path / "none")]
    assert pp.main([*argv, "--apply"]) == 1
    assert "refused: DATABASE_URL points at this machine" in capsys.readouterr().err


def test_main_needs_the_file(tmp_path: Path) -> None:
    assert pp.main(["--env", "paper", "--env-file", str(tmp_path / "missing")]) == 2


def test_the_client_is_ssm_in_us_east_1(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    seen: dict[str, Any] = {}

    class Session:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

        def client(self, service: str) -> str:
            seen["service"] = service
            return "client"

    monkeypatch.setattr(boto3, "Session", Session)
    assert pp.ssm_client("auratrade") == "client"
    assert seen == {"profile_name": "auratrade", "region_name": "us-east-1", "service": "ssm"}
