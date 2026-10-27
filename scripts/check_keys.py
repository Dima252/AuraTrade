"""Confirm every external key in .env works (task 0.2).

Only free, read-only endpoints are called: no orders, no tokens spent. Every output
line goes through `redact()`, so a secret never reaches the terminal even if a vendor
echoes it back in an error.

    uv run python scripts/check_keys.py                        # checks .env
    uv run python scripts/check_keys.py --env-file .env.paper  # the other paper account
    uv run python scripts/check_keys.py --only alpaca_account anthropic
    uv run python scripts/check_keys.py --telegram-test-message
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import anthropic
import httpx
import psycopg
from alpaca.common.exceptions import APIError as AlpacaAPIError
from alpaca.trading.client import TradingClient
from alpaca.trading.models import TradeAccount
from dotenv import dotenv_values

Env = Mapping[str, str]

SECRET_VARS = (
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ANTHROPIC_API_KEY",
    "FRED_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "DATABASE_URL",
)
DEFAULT_MODEL = "claude-opus-5-5"
ALLOWED_TRADING_MODES = ("dry_run", "paper")
TIMEOUT_S = 15.0

ALPACA_DATA_URL = "https://data.alpaca.markets/v2/stocks/bars/latest"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/{method}"


class Status(StrEnum):
    OK = "OK"
    FAIL = "FAIL"
    MISSING = "MISSING"


@dataclass(frozen=True)
class Result:
    name: str
    status: Status
    detail: str


@dataclass(frozen=True)
class Options:
    telegram_test_message: bool = False


Check = Callable[[Env, httpx.Client, Options], Result]


class MissingVarsError(Exception):
    def __init__(self, names: list[str]) -> None:
        super().__init__(", ".join(names))
        self.names = names


def require(env: Env, *names: str) -> list[str]:
    missing = [n for n in names if not env.get(n)]
    if missing:
        raise MissingVarsError(missing)
    return [env[n] for n in names]


def redact(text: str, secrets: Iterable[str]) -> str:
    # Longest first, so a secret that contains another is fully replaced.
    for secret in sorted((s for s in secrets if len(s) >= 6), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def http_failure(response: httpx.Response) -> str:
    # Status only: error bodies (FRED in particular) may echo the key back.
    return f"HTTP {response.status_code}"


def check_trading_mode(env: Env, http: httpx.Client, opts: Options) -> Result:
    mode = env.get("TRADING_MODE") or "dry_run"
    if mode in ALLOWED_TRADING_MODES:
        return Result("trading_mode", Status.OK, mode)
    if mode == "live":
        return Result("trading_mode", Status.FAIL, "live is not allowed before Phase 8")
    return Result("trading_mode", Status.FAIL, f"unknown mode {mode!r}")


def make_trading_client(key: str, secret: str) -> TradingClient:
    # paper=True is hard-wired: this script never talks to the live endpoint.
    return TradingClient(key, secret, paper=True)


def check_alpaca_account(env: Env, http: httpx.Client, opts: Options) -> Result:
    key, secret = require(env, "ALPACA_API_KEY", "ALPACA_SECRET_KEY")
    try:
        account = make_trading_client(key, secret).get_account()
    except AlpacaAPIError as exc:
        hint = " (live keys are rejected by the paper endpoint)" if exc.status_code == 401 else ""
        return Result("alpaca_account", Status.FAIL, f"HTTP {exc.status_code}{hint}")
    if not isinstance(account, TradeAccount):
        return Result("alpaca_account", Status.FAIL, "unexpected raw response")
    equity = float(account.equity or 0)
    notes = []
    if account.trading_blocked or account.account_blocked:
        notes.append("BLOCKED")
    # No PDT note: FINRA dropped the pattern-day-trader rule in 2026 (README decision #123).
    detail = (
        f"paper account ...{account.account_number[-4:]}, {account.status}, equity ${equity:,.2f}"
    )
    if notes:
        detail += f" [{'; '.join(notes)}]"
    return Result("alpaca_account", Status.OK, detail)


def check_alpaca_data(env: Env, http: httpx.Client, opts: Options) -> Result:
    key, secret = require(env, "ALPACA_API_KEY", "ALPACA_SECRET_KEY")
    response = http.get(
        ALPACA_DATA_URL,
        params={"symbols": "SPY", "feed": "iex"},
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
    )
    if response.status_code != 200:
        return Result("alpaca_data", Status.FAIL, http_failure(response))
    bar = response.json().get("bars", {}).get("SPY")
    if not bar:
        return Result("alpaca_data", Status.FAIL, "no SPY bar in response")
    return Result("alpaca_data", Status.OK, f"latest IEX SPY bar {bar['t']}")


def make_anthropic_client(key: str) -> anthropic.Anthropic:
    # The explicit key stops the SDK falling back to another credential source.
    return anthropic.Anthropic(api_key=key, max_retries=0, timeout=TIMEOUT_S)


def check_anthropic(env: Env, http: httpx.Client, opts: Options) -> Result:
    (key,) = require(env, "ANTHROPIC_API_KEY")
    model_id = env.get("ANALYST_MODEL") or DEFAULT_MODEL
    try:
        # Models API: free, no tokens. Confirms both the key and access to the model.
        model = make_anthropic_client(key).models.retrieve(model_id)
    except anthropic.AuthenticationError:
        return Result("anthropic", Status.FAIL, "key rejected (401)")
    except anthropic.PermissionDeniedError:
        return Result("anthropic", Status.FAIL, "key lacks permission (403)")
    except anthropic.NotFoundError:
        return Result("anthropic", Status.FAIL, f"key works, but {model_id} is not available")
    except anthropic.APIStatusError as exc:
        return Result("anthropic", Status.FAIL, f"HTTP {exc.status_code}")
    except anthropic.APIConnectionError:
        return Result("anthropic", Status.FAIL, "connection failed")
    return Result("anthropic", Status.OK, f"{model.id} available ({model.display_name})")


def check_fred(env: Env, http: httpx.Client, opts: Options) -> Result:
    (key,) = require(env, "FRED_API_KEY")
    response = http.get(
        FRED_URL,
        params={
            "series_id": "VIXCLS",
            "api_key": key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1,
        },
    )
    if response.status_code != 200:
        return Result("fred", Status.FAIL, http_failure(response))
    observations = response.json().get("observations") or []
    if not observations:
        return Result("fred", Status.FAIL, "no VIXCLS observations")
    latest = observations[0]
    return Result("fred", Status.OK, f"VIXCLS {latest['date']} = {latest['value']}")


class TelegramError(Exception):
    pass


def telegram(http: httpx.Client, token: str, method: str, **params: Any) -> dict[str, Any]:
    response = http.post(TELEGRAM_URL.format(token=token, method=method), json=params)
    body: dict[str, Any] = response.json() if response.content else {}
    if response.status_code != 200 or not body.get("ok"):
        raise TelegramError(f"{method}: HTTP {response.status_code}")
    return body


def check_telegram(env: Env, http: httpx.Client, opts: Options) -> Result:
    token, chat_id = require(env, "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    try:
        bot = telegram(http, token, "getMe")["result"]
        telegram(http, token, "getChat", chat_id=chat_id)
        if opts.telegram_test_message:
            telegram(http, token, "sendMessage", chat_id=chat_id, text="AuraTrade key check OK")
    except TelegramError as exc:
        return Result("telegram", Status.FAIL, str(exc))
    sent = ", test message sent" if opts.telegram_test_message else ""
    return Result("telegram", Status.OK, f"bot @{bot.get('username')}, chat reachable{sent}")


def check_database(env: Env, http: httpx.Client, opts: Options) -> Result:
    (url,) = require(env, "DATABASE_URL")
    # SQLAlchemy-style URL -> libpq URL.
    conninfo = url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        with psycopg.connect(conninfo, connect_timeout=int(TIMEOUT_S)) as conn:
            row = conn.execute("select current_setting('server_version'), current_database()")
            version, database = row.fetchone() or ("?", "?")
            host = conn.info.host
    except psycopg.Error as exc:
        # The exception text can contain the connection string; show the class only.
        return Result("database", Status.FAIL, f"{type(exc).__name__}: check URL / server")
    return Result("database", Status.OK, f"{host}/{database}, Postgres {version}")


CHECKS: dict[str, Check] = {
    "trading_mode": check_trading_mode,
    "alpaca_account": check_alpaca_account,
    "alpaca_data": check_alpaca_data,
    "anthropic": check_anthropic,
    "fred": check_fred,
    "telegram": check_telegram,
    "database": check_database,
}


def run_one(name: str, check: Check, env: Env, http: httpx.Client, opts: Options) -> Result:
    try:
        return check(env, http, opts)
    except MissingVarsError as exc:
        return Result(name, Status.MISSING, f"set {exc} in the env file")
    except httpx.HTTPError as exc:
        # str(exc) can include the request URL (FRED's key, Telegram's token).
        return Result(name, Status.FAIL, f"{type(exc).__name__}: request failed")
    except Exception as exc:
        return Result(name, Status.FAIL, f"unexpected {type(exc).__name__}")


def run_checks(env: Env, names: Iterable[str], http: httpx.Client, opts: Options) -> list[Result]:
    return [run_one(name, CHECKS[name], env, http, opts) for name in names]


def format_results(results: list[Result], env: Env) -> str:
    secrets = [env[v] for v in SECRET_VARS if env.get(v)]
    lines = [f"{r.status:<8} {r.name:<15} {r.detail}" for r in results]
    return redact("\n".join(lines), secrets)


def load_env(path: Path) -> dict[str, str]:
    file_values = {k: v for k, v in dotenv_values(path).items() if v is not None}
    # The env file wins: the point is to test what is in it.
    return {**os.environ, **file_values}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--only", nargs="+", choices=list(CHECKS), default=list(CHECKS))
    parser.add_argument("--telegram-test-message", action="store_true")
    args = parser.parse_args(argv)

    if not args.env_file.is_file():
        print(f"{args.env_file} not found: copy .env.example and fill it in", file=sys.stderr)
        return 2
    env = load_env(args.env_file)
    opts = Options(telegram_test_message=args.telegram_test_message)
    with httpx.Client(timeout=TIMEOUT_S) as http:
        results = run_checks(env, args.only, http, opts)
    print(format_results(results, env))
    return 0 if all(r.status is Status.OK for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
