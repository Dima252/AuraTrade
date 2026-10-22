"""Secrets from SSM Parameter Store, for the Lambda functions (README §12).

Locally the settings come from `.env`. In AWS each function's environment names a parameter
path (`SSM_PARAMETER_PATH=/auratrade/paper/`), and the parameters under it are read once per
container, before the settings are built: `/auratrade/paper/ALPACA_API_KEY` becomes the
`ALPACA_API_KEY` setting. CloudFormation can't put SecureString values into a function's
environment, so they are read at run time instead, and never appear in the stack.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_ssm import SSMClient

PATH_VARIABLE = "SSM_PARAMETER_PATH"

_loaded: set[str] = set()


def read_parameters(client: SSMClient, path: str) -> dict[str, str]:
    """Every parameter directly under `path`, decrypted, keyed by its last name segment."""
    out: dict[str, str] = {}
    for page in client.get_paginator("get_parameters_by_path").paginate(
        Path=path, WithDecryption=True, Recursive=False
    ):
        for parameter in page.get("Parameters", []):
            name, value = parameter.get("Name"), parameter.get("Value")
            if name and value is not None:
                out[name.rsplit("/", 1)[-1]] = value
    return out


def load_into_environment(
    environ: MutableMapping[str, str] | None = None, client: SSMClient | None = None
) -> list[str]:
    """When `SSM_PARAMETER_PATH` is set, copy its parameters into the environment (a variable
    already set wins, as for `.env`). Once per container. Returns the names loaded."""
    env = os.environ if environ is None else environ
    path = env.get(PATH_VARIABLE)
    if not path or path in _loaded:
        return []
    if client is None:
        import boto3

        client = boto3.client("ssm")
    values = read_parameters(client, path)
    if not values:
        # A wrong path would otherwise run every job without keys.
        raise RuntimeError(f"no parameters under {path}")
    loaded = []
    for name, value in values.items():
        if name not in env:
            env[name] = value
            loaded.append(name)
    _loaded.add(path)
    return sorted(loaded)
