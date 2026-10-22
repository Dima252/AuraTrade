"""Copy an environment's secrets from its env file into SSM Parameter Store (README §12).

    uv run python scripts/put_parameters.py --env paper --env-file .env.paper            # plan
    uv run python scripts/put_parameters.py --env paper --env-file .env.paper --apply \
        --profile auratrade                                                               # write

Each required key becomes a SecureString `/auratrade/<env>/<KEY>`, read by the Lambda functions
at start (`auratrade.parameters`). Only names are printed, never values. Without `--apply`
nothing is sent to AWS.

The account's own keys (Alpaca, the database, the dead-man's switch) must be in the env file.
The shared ones (Anthropic, FRED, Finnhub, EDGAR, Telegram) fall back to `.env` when the file
leaves them empty. `TRADING_MODE` and the other settings the stack sets are never uploaded: the
mode is a deploy-time choice (`-c trading_mode=paper`), not a parameter a file could change.

Refused before anything is written: a missing key, a `DATABASE_URL` on this machine (a Lambda
can't reach it), or the `dev` account's Alpaca key (two schedulers on one account would trade
against each other's positions).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from dotenv import dotenv_values

if TYPE_CHECKING:
    from mypy_boto3_ssm import SSMClient

ENVIRONMENTS = ("paper",)
ACCOUNT_KEYS = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "DATABASE_URL", "HEALTHCHECKS_PING_URL")
SHARED_KEYS = (
    "ANTHROPIC_API_KEY",
    "FRED_API_KEY",
    "FINNHUB_API_KEY",
    "EDGAR_USER_AGENT",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)
REQUIRED = (*ACCOUNT_KEYS, *SHARED_KEYS)
# Set by the stack (infra/stacks/trading.py); never from a parameter.
STACK_OWNED = (
    "TRADING_MODE",
    "ENVIRONMENT",
    "SSM_PARAMETER_PATH",
    "STORAGE_BACKEND",
    "STORAGE_S3_BUCKET",
    "SCHEDULE_GROUP",
)
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})


class RefusedError(Exception):
    pass


@dataclass(frozen=True)
class Plan:
    values: dict[str, str]  # every required key, resolved
    shared_from_dev: list[str]  # the keys taken from `.env`
    left_out: list[str]  # keys in the file that aren't uploaded


def parameter_path(env_name: str) -> str:
    return f"/auratrade/{env_name}/"


def make_plan(values: Mapping[str, str], dev_values: Mapping[str, str]) -> Plan:
    """Raises `RefusedError` on anything that must not reach AWS."""
    resolved = {key: values.get(key, "") for key in ACCOUNT_KEYS}
    from_dev = []
    for key in SHARED_KEYS:
        if values.get(key):
            resolved[key] = values[key]
        elif dev_values.get(key):
            resolved[key] = dev_values[key]
            from_dev.append(key)
        else:
            resolved[key] = ""
    problems = [f"{key} is missing or empty" for key in REQUIRED if not resolved[key]]
    if urlsplit(resolved["DATABASE_URL"]).hostname in LOCAL_HOSTS:
        problems.append("DATABASE_URL points at this machine; use the Neon branch's URL")
    dev_key = dev_values.get("ALPACA_API_KEY")
    if dev_key and resolved["ALPACA_API_KEY"] == dev_key:
        problems.append("ALPACA_API_KEY is the dev account's key (.env)")
    if problems:
        raise RefusedError("; ".join(problems))
    left_out = sorted(k for k in values if k not in REQUIRED)
    return Plan({k: resolved[k] for k in REQUIRED}, from_dev, left_out)


def put(client: SSMClient, env_name: str, values: Mapping[str, str]) -> None:
    for key, value in values.items():
        client.put_parameter(
            Name=parameter_path(env_name) + key,
            Value=value,
            Type="SecureString",
            Overwrite=True,
            Tier="Standard",
        )


def read_env_file(path: Path) -> dict[str, str]:
    return {k: v.strip() for k, v in dotenv_values(path).items() if v is not None}


def ssm_client(profile: str | None) -> SSMClient:
    import boto3

    return boto3.Session(profile_name=profile, region_name="us-east-1").client("ssm")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--env", required=True, choices=ENVIRONMENTS)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--dev-env-file", type=Path, default=Path(".env"))
    parser.add_argument("--apply", action="store_true", help="write to SSM (default: plan only)")
    parser.add_argument("--profile", help="AWS CLI profile, e.g. auratrade")
    args = parser.parse_args(argv)

    if not args.env_file.is_file():
        print(f"no such file: {args.env_file}", file=sys.stderr)
        return 2
    values = read_env_file(args.env_file)
    dev = read_env_file(args.dev_env_file) if args.dev_env_file.is_file() else {}
    try:
        plan = make_plan(values, dev)
    except RefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    path = parameter_path(args.env)
    verb = "put" if args.apply else "would put"
    for key in plan.values:
        source = f" (from {args.dev_env_file})" if key in plan.shared_from_dev else ""
        print(f"{verb} {path}{key}{source}")
    for key in plan.left_out:
        why = "set by the stack" if key in STACK_OWNED else "not used in AWS"
        print(f"left out: {key} ({why})")
    if not args.apply:
        print("plan only: run again with --apply to write")
        return 0
    put(ssm_client(args.profile), args.env, plan.values)
    print(f"done: {len(plan.values)} SecureString parameters under {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
