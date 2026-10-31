"""The dashboard's page : one self-contained document, its data as text only."""

import re
from pathlib import Path

import pytest

from auratrade.mcp_server import app

BANNED = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function")


def _scripts(page: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", page, flags=re.S)


def test_the_page_inlines_the_sites_drawing() -> None:
    page = app.html()

    for marker, name in app.MARKERS.items():
        assert marker not in page
        assert (app.WEB / name).read_text(encoding="utf-8") in page
    assert "window.AuraChart" in page


def test_the_page_loads_and_fetches_nothing() -> None:
    page = app.html()

    # The host's default policy would block it anyway; the page shouldn't try.
    assert not re.search(r"<(script|link|img|iframe)[^>]+(src|href)=", page)
    assert "fetch(" not in page
    assert "XMLHttpRequest" not in page
    assert "http" not in page.replace("http://www.w3.org/2000/svg", "")


def test_the_page_builds_no_markup() -> None:
    # The snapshot's text (the LLM's reasoning among it) reaches the page as text.
    for script in _scripts(app.html()):
        for banned in BANNED:
            assert banned not in script, banned


def test_the_page_talks_to_its_parent_frame_only() -> None:
    (_, own) = _scripts(app.html())

    assert "ev.source !== window.parent" in own
    assert re.findall(r"(\w+(?:\.\w+)*)\.postMessage\(", own) == ["window.parent"]


def test_a_missing_marker_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    page = tmp_path / "dashboard.html"
    page.write_text("<style>/*@chart.css*/</style>", encoding="utf-8")
    monkeypatch.setattr(app, "PAGE", page)
    app.html.cache_clear()
    try:
        with pytest.raises(ValueError, match=r"chart\.js"):
            app.html()
    finally:
        app.html.cache_clear()


def test_code_that_would_close_its_element_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "chart.css").write_text("a {}", encoding="utf-8")
    (tmp_path / "chart.js").write_text('const s = "</script>";', encoding="utf-8")
    monkeypatch.setattr(app, "WEB", tmp_path)
    app.html.cache_clear()
    try:
        with pytest.raises(ValueError, match=r"chart\.js would close"):
            app.html()
    finally:
        app.html.cache_clear()
