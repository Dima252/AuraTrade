import importlib
from pathlib import Path

import pytest

from auratrade.config import REPO_ROOT
from auratrade.mcp_server import docs

SPEC = """# Title

## 5. Components

Intro.

### 5.1 Ingest (`ingest`)

Ingest text.

### 5.2 Regime

Regime text.

---

## 16. Decision log

| # | Decision | Detail |
|---|---|---|
| 1 | First | The first one |
| 2 | Second | A | B |

---

## 17. Known limitations

Limits.
"""


def test_sections_run_to_the_next_heading_of_their_level() -> None:
    found = docs.sections(SPEC)

    assert list(found) == ["5", "5.1", "5.2", "16", "17"]
    assert found["5.1"].title == "Ingest (`ingest`)"
    assert found["5.1"].text == "### 5.1 Ingest (`ingest`)\n\nIngest text."
    assert "Regime text." in found["5"].text
    assert "Decision log" not in found["5"].text
    assert not found["5.2"].text.endswith("---")


def test_decisions_by_number() -> None:
    assert docs.decisions(SPEC) == {1: ("First", "The first one"), 2: ("Second", "A | B")}
    assert docs.decision(SPEC, 2) == "# Decision #2: Second\n\nA | B"
    with pytest.raises(docs.NotFoundError):
        docs.decision(SPEC, 3)
    assert docs.decisions("## 5. Only\n") == {}


def test_the_indexes() -> None:
    assert "- 5.1 Ingest (`ingest`)" in docs.spec_index(SPEC)
    assert "- #2 Second" in docs.decision_index(SPEC)


def test_a_section_and_the_decision_log_by_entry() -> None:
    assert docs.spec_section(SPEC, " 5.2 ").startswith("### 5.2 Regime")
    assert docs.spec_section(SPEC, "16").startswith("# Decision log")
    with pytest.raises(docs.NotFoundError):
        docs.spec_section(SPEC, "9")


def test_the_real_spec_parses() -> None:
    spec = docs.read(docs.SPEC)

    assert "MCP server" in docs.sections(spec)["5.10"].title
    assert docs.decisions(spec)[37][0] == "The MCP server: strictly read-only"
    assert len(docs.decisions(spec)) >= 39


def test_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(docs.NotFoundError):
        docs.read(tmp_path / "nope.md")


def test_the_documents_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The remote server's image keeps them beside the package.
    monkeypatch.setenv("AURATRADE_DOCS_ROOT", str(tmp_path))
    try:
        moved = importlib.reload(docs)
        assert tmp_path / "docs" / "specification.md" == moved.SPEC
        assert tmp_path / "docs" / "backtests" / "portfolio.md" == moved.BACKTEST
    finally:
        monkeypatch.delenv("AURATRADE_DOCS_ROOT")
        importlib.reload(docs)
    assert docs.SPEC == REPO_ROOT / "docs" / "specification.md"
