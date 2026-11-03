"""The dashboard's number formats (web/chart.js, spec §5.8), run in Node: the axis labels follow
the tick step, and a value that rounds to zero carries no sign."""

import json
import shutil
import subprocess

import pytest

from auratrade.config import REPO_ROOT

NODE = shutil.which("node")
M = "\N{MINUS SIGN}"  # the formats print a true minus, not a hyphen
pytestmark = pytest.mark.skipif(NODE is None, reason="needs Node")

# chart.js is a browser script that assigns window.AuraChart; nothing in it touches the DOM
# until a chart is drawn, so the formats run without one.
HARNESS = """
globalThis.window = globalThis;
require("vm").runInThisContext(require("fs").readFileSync(process.argv[1], "utf8"));
const C = window.AuraChart;
const calls = JSON.parse(process.argv[2]);
console.log(JSON.stringify(calls.map(([f, ...args]) => C[f](...args))));
"""


def _run(*calls: list[object]) -> list[object]:
    assert NODE is not None
    out = subprocess.run(  # noqa: S603 - our own script, fixed arguments
        [NODE, "-e", HARNESS, str(REPO_ROOT / "web" / "chart.js"), json.dumps(calls)],
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    result: list[object] = json.loads(out.stdout)
    return result


def test_no_negative_zero() -> None:
    assert _run(
        ["fmtPct", -0.0004],
        ["fmtPct", 0.0004],
        ["fmtPct", -1e-17, 0],
        ["fmtPct", -0.0006],
        ["fmtPct", 0.0012, 2],
        ["fmtPts", -0.00001, 2],
        ["fmtPts", 0.0003, 2],
    ) == ["0.0%", "0.0%", "0%", f"{M}0.1%", "+0.12%", "0.00 pts", "+0.03 pts"]


def test_step_digits() -> None:
    assert _run(
        ["stepDigits", 10],
        ["stepDigits", 2.5],
        ["stepDigits", 0.5],
        ["stepDigits", 0.05000000000000001],
        ["stepDigits", 0.025],
    ) == [0, 1, 1, 2, 3]


def test_a_narrow_range_has_distinct_axis_labels() -> None:
    # The first real snapshot: every line within ±0.15%, a 0.05% step.
    step = 0.0005
    ticks = [round(-0.0015 + k * step, 10) for k in range(7)]
    labels = _run(*[["pctAxis", t, step] for t in ticks])
    assert labels == [f"{M}0.15%", f"{M}0.10%", f"{M}0.05%", "0.00%", "+0.05%", "+0.10%", "+0.15%"]
    assert _run(["pctValue", -0.0005, step], ["pctValue", -0.0006, step]) == [
        f"{M}0.05%",
        f"{M}0.06%",
    ]


def test_a_wide_range_keeps_whole_percents() -> None:
    assert _run(["pctAxis", 0.1, 0.05], ["pctAxis", -0.05, 0.05], ["pctValue", 0.1234, 0.05]) == [
        "+10%",
        f"{M}5%",
        "+12.3%",
    ]
