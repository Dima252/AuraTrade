"""Security classification (README §5.1, #111). Alpaca's asset list has no type flag, so:
the Nasdaq Trader directory's ETF flag → the symbol suffix → name rules → EDGAR SIC 6770.

The name rules read the security description where the directory gives one (Nasdaq names are
"Company - Description", so "Unit Corporation - Common Stock" is a common stock), and run most
specific first. "Common Stock / Ordinary Shares" is tested before "Trust", so REITs named
"... Trust Inc. Common Stock" stay common (spike 0.3a); ADRs are their own type, in the
universe (#111). Names alone can't tell every SPAC shell, so EDGAR's SIC 6770 has the last word.
"""

from __future__ import annotations

import re
from enum import StrEnum


class SecurityType(StrEnum):
    COMMON = "COMMON"
    ADR = "ADR"
    ETF = "ETF"
    ETN = "ETN"
    FUND = "FUND"
    PREFERRED = "PREFERRED"
    UNIT = "UNIT"
    WARRANT = "WARRANT"
    RIGHT = "RIGHT"
    NOTE = "NOTE"
    SPAC = "SPAC"
    OTHER = "OTHER"


BLANK_CHECK_SIC = 6770

# Alpaca writes class shares with a dot and one letter (BRK.B, MOG.A); `.U` is a unit.
_CLASS_SHARE = re.compile(r"^[A-Z]+\.[A-TV-Z]$")
_SUFFIXES: tuple[tuple[re.Pattern[str], SecurityType], ...] = (
    (re.compile(r"\.P[A-Z]*$"), SecurityType.PREFERRED),
    (re.compile(r"\.U[A-Z]?$"), SecurityType.UNIT),
    (re.compile(r"\.W[A-Z]*(\.[A-Z])?$"), SecurityType.WARRANT),
    (re.compile(r"\.R[A-Z]?$"), SecurityType.RIGHT),
)


def _words(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE)


_ETN = _words(r"ETNs?|Exchange[- ]Traded Notes?")
# On the description: these name what the security is.
_DESCRIPTION_RULES: tuple[tuple[re.Pattern[str], SecurityType], ...] = (
    (_words(r"Preferred|Pfd|Preference Shares?"), SecurityType.PREFERRED),
    (_words(r"Warrants?"), SecurityType.WARRANT),
    (_words(r"Rights?"), SecurityType.RIGHT),
    (_words(r"Units?"), SecurityType.UNIT),
    (_words(r"Notes? due|Debentures?|Senior Notes?|Subordinated Notes?"), SecurityType.NOTE),
)
_SPAC = _words(r"(?:Acquisition|Merger) Corp(?:oration)?")
_FUND = _words(r"Fund|ETF|Exchange[- ]Traded|Index")
_ADR = _words(r"American Depositary|ADSs?|ADRs?|Depositary (?:Shares?|Receipts?)")
_COMMON = _words(r"Common Stock|Common Shares?|Ordinary Shares?|Capital Stock|Class [A-Z] Shares?")
_TRUST = _words(r"Trust|Beneficial Interest")


def description(name: str) -> str:
    """The security description: the part after the last " - " in Nasdaq-style names."""
    return name.rsplit(" - ", 1)[-1]


def classify(symbol: str, name: str, *, is_etf: bool) -> SecurityType:
    """The type of a listed security from its directory ETF flag, symbol and name."""
    if is_etf:
        return SecurityType.ETF
    if "." in symbol and not _CLASS_SHARE.match(symbol):
        for pattern, kind in _SUFFIXES:
            if pattern.search(symbol):
                return kind
        return SecurityType.OTHER
    if _ETN.search(name):
        return SecurityType.ETN
    desc = description(name)
    for pattern, kind in _DESCRIPTION_RULES:
        if pattern.search(desc):
            return kind
    if _SPAC.search(name):
        return SecurityType.SPAC
    if _FUND.search(name):
        return SecurityType.FUND
    if _ADR.search(desc):
        return SecurityType.ADR
    if _COMMON.search(desc):
        return SecurityType.COMMON
    if _TRUST.search(name):
        return SecurityType.FUND
    return SecurityType.COMMON


def with_sic(kind: SecurityType, sic: int | None) -> SecurityType:
    """EDGAR SIC 6770 (blank checks) marks a SPAC shell whatever its name says."""
    if sic == BLANK_CHECK_SIC and kind in (SecurityType.COMMON, SecurityType.ADR):
        return SecurityType.SPAC
    return kind
