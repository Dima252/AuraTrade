"""Shared helpers for the Alpaca spikes: env loading, paper-only clients, redacted JSONL logs."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from dotenv import dotenv_values
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
ENV_FILES = {"dev": ROOT / ".env", "paper": ROOT / ".env.paper"}
SECRET_VARS = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")


class Env:
    def __init__(self, account: str = "dev") -> None:
        values = dotenv_values(ENV_FILES[account])
        key, secret = values.get("ALPACA_API_KEY"), values.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            sys.exit(f"ALPACA_API_KEY / ALPACA_SECRET_KEY missing in {ENV_FILES[account].name}")
        self.account = account
        self.key = key
        self.secret = secret

    def trading(self) -> TradingClient:
        # paper=True is hard-wired: spikes never touch the live endpoint (CLAUDE.md §3).
        return TradingClient(self.key, self.secret, paper=True)

    def data(self) -> StockHistoricalDataClient:
        return StockHistoricalDataClient(self.key, self.secret)

    def headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}

    def secrets(self) -> list[str]:
        return [self.key, self.secret]


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in sorted((s for s in secrets if len(s) >= 6), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return to_jsonable(obj.model_dump())
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, UUID | Decimal):
        return str(obj)
    return obj


class Log:
    """Appends one JSON object per event to spikes/<spike>/output/<name>.jsonl (git-ignored)."""

    def __init__(self, spike_dir: Path, name: str, secrets: Iterable[str]) -> None:
        out = spike_dir / "output"
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.path = out / f"{stamp}-{name}.jsonl"
        self.secrets = list(secrets)

    def event(self, kind: str, **fields: Any) -> None:
        record = {"ts": datetime.now(UTC).isoformat(), "event": kind, **to_jsonable(fields)}
        line = redact(json.dumps(record, default=str), self.secrets)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        brief = {k: v for k, v in record.items() if k not in ("ts", "event")}
        print(f"[{kind}] {redact(json.dumps(brief, default=str), self.secrets)[:600]}")
