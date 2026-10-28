"""The project's documents as MCP resources (README §5.10): the README by section, the decision
log by entry, the runbook and the backtest's report. Pure over the files' text."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from auratrade.config import REPO_ROOT

# The remote server's image keeps the documents beside the package, not two levels above it.
DOCS_ROOT = Path(os.environ.get("AURATRADE_DOCS_ROOT") or REPO_ROOT)
README = DOCS_ROOT / "README.md"
RUNBOOK = DOCS_ROOT / "docs" / "runbook.md"
BACKTEST = DOCS_ROOT / "docs" / "backtests" / "portfolio.md"
DECISION_LOG = "16"

_HEADING = re.compile(r"^(#{2,3}) (\d+(?:\.\d+)?)\.? (.+)$")
_DECISION = re.compile(r"^\| (\d+) \| ([^|]+?) \| (.+) \|\s*$")


class NotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class Section:
    number: str
    title: str
    text: str


def sections(readme: str) -> dict[str, Section]:
    """Each numbered `##`/`###` heading to the next heading of the same or a higher level."""
    lines = readme.splitlines()
    heads = [
        (i, len(m.group(1)), m.group(2), m.group(3).strip())
        for i, line in enumerate(lines)
        if (m := _HEADING.match(line))
    ]
    out: dict[str, Section] = {}
    for k, (start, level, number, title) in enumerate(heads):
        end = next((i for i, lv, _, _ in heads[k + 1 :] if lv <= level), len(lines))
        body = "\n".join(lines[start:end]).rstrip()
        out[number] = Section(number, title, body.removesuffix("---").rstrip())
    return out


def decisions(readme: str) -> dict[int, tuple[str, str]]:
    """The decision log's rows: number → (title, text)."""
    log = sections(readme).get(DECISION_LOG)
    if log is None:
        return {}
    return {
        int(m.group(1)): (m.group(2).strip(), m.group(3).strip())
        for line in log.text.splitlines()
        if (m := _DECISION.match(line))
    }


def readme_index(readme: str) -> str:
    rows = [f"- {s.number} {s.title}" for s in sections(readme).values()]
    return (
        "# README sections\n\nRead one as auratrade://readme/{section} (e.g. 5.4). The "
        "decision log (16) is read by entry: auratrade://decision/{number}.\n\n" + "\n".join(rows)
    )


def readme_section(readme: str, number: str) -> str:
    if number.strip() == DECISION_LOG:
        return decision_index(readme)
    found = sections(readme).get(number.strip())
    if found is None:
        raise NotFoundError(f"no README section {number}: see auratrade://readme")
    return found.text


def decision_index(readme: str) -> str:
    rows = [f"- #{n} {title}" for n, (title, _) in sorted(decisions(readme).items())]
    return (
        "# Decision log\n\nWhy things are the way they are. Read one as "
        "auratrade://decision/{number}.\n\n" + "\n".join(rows)
    )


def decision(readme: str, number: int) -> str:
    found = decisions(readme).get(number)
    if found is None:
        raise NotFoundError(f"no decision #{number}: see auratrade://decisions")
    title, text = found
    return f"# Decision #{number}: {title}\n\n{text}"


def read(path: Path) -> str:
    if not path.is_file():
        raise NotFoundError(f"{path.name} isn't available here")
    return path.read_text(encoding="utf-8")
