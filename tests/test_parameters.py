"""Secrets from SSM Parameter Store (`auratrade.parameters`), against a fake SSM client."""

from __future__ import annotations

from typing import Any, cast

import pytest

from auratrade import parameters

PATH = "/auratrade/paper/"


class FakeSSM:
    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def get_paginator(self, name: str) -> FakeSSM:
        assert name == "get_parameters_by_path"
        return self

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return [{"Parameters": page} for page in self.pages]


def client(*pages: list[dict[str, Any]]) -> Any:
    return FakeSSM(list(pages))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parameters, "_loaded", set())


def test_read_parameters_decrypts_and_keys_by_the_last_segment() -> None:
    ssm = client(
        [{"Name": f"{PATH}ALPACA_API_KEY", "Value": "k"}, {"Name": f"{PATH}EMPTY", "Value": ""}],
        [{"Name": f"{PATH}DATABASE_URL", "Value": "postgres://x"}, {"Value": "nameless"}, {}],
    )
    assert parameters.read_parameters(ssm, PATH) == {
        "ALPACA_API_KEY": "k",
        "EMPTY": "",
        "DATABASE_URL": "postgres://x",
    }
    assert cast(FakeSSM, ssm).calls == [{"Path": PATH, "WithDecryption": True, "Recursive": False}]


def test_load_copies_into_the_environment_without_overriding() -> None:
    env = {parameters.PATH_VARIABLE: PATH, "DATABASE_URL": "set-by-hand"}
    ssm = client(
        [
            {"Name": f"{PATH}DATABASE_URL", "Value": "from-ssm"},
            {"Name": f"{PATH}ALPACA_API_KEY", "Value": "k"},
            {"Name": f"{PATH}ANTHROPIC_API_KEY", "Value": "a"},
        ]
    )
    assert parameters.load_into_environment(env, ssm) == ["ALPACA_API_KEY", "ANTHROPIC_API_KEY"]
    assert env["DATABASE_URL"] == "set-by-hand"
    assert env["ALPACA_API_KEY"] == "k"


def test_load_runs_once_per_container() -> None:
    env = {parameters.PATH_VARIABLE: PATH}
    ssm = client([{"Name": f"{PATH}ALPACA_API_KEY", "Value": "k"}])
    assert parameters.load_into_environment(env, ssm) == ["ALPACA_API_KEY"]
    assert parameters.load_into_environment(env, ssm) == []
    assert len(cast(FakeSSM, ssm).calls) == 1


def test_without_a_path_nothing_is_read() -> None:
    assert parameters.load_into_environment({}, None) == []
    assert parameters.load_into_environment({parameters.PATH_VARIABLE: ""}, None) == []


def test_an_empty_path_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="no parameters under /auratrade/paper/"):
        parameters.load_into_environment({parameters.PATH_VARIABLE: PATH}, client([]))


def test_the_default_client_is_ssm(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    services: list[str] = []

    def make(service: str) -> Any:
        services.append(service)
        return client([{"Name": f"{PATH}X", "Value": "1"}])

    monkeypatch.setattr(boto3, "client", make)
    env = {parameters.PATH_VARIABLE: PATH}
    assert parameters.load_into_environment(env) == ["X"]
    assert services == ["ssm"]


def test_the_process_environment_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(parameters.PATH_VARIABLE, raising=False)
    assert parameters.load_into_environment() == []
