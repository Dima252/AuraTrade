// The public dashboard (README §5.8, task 6.2): reads data/snapshot.json, which `simulate`
// publishes each evening, and draws it into #dash. Without the file (before the paper trial)
// or without JavaScript, the static text in #dash stays. Every value reaches the page through
// textContent or an SVG attribute, never as markup.
(() => {
  "use strict";

  const root = document.getElementById("dash");
  if (!root) return;

  const SVG_NS = "http://www.w3.org/2000/svg";
  // Fixed order: the book, its twin, the market, then the paper account (validated palette).
  const SERIES = [
    { key: "book", label: "Book", long: "The book (Claude's decisions)", cls: "s1" },
    { key: "twin", label: "Twin", long: "The twin (rules only)", cls: "s2" },
    { key: "spy", label: "S&P 500", long: "S&P 500, bought and held", cls: "s3" },
    { key: "account", label: "Paper account", long: "The paper account (its own fills)", cls: "s4" },
  ];
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // --- helpers ---------------------------------------------------------------------------

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "text") node.textContent = v;
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const child of children) if (child) node.append(child);
    return node;
  }

  function svg(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "text") node.textContent = v;
      else node.setAttribute(k, v);
    }
    return node;
  }

  const isNum = (x) => typeof x === "number" && Number.isFinite(x);

  function parseDay(s) {
    const [y, m, d] = s.split("-").map(Number);
    return { y, m, d };
  }

  function fmtDay(s) {
    const { y, m, d } = parseDay(s);
    return `${d} ${MONTHS[m - 1]} ${y}`;
  }

  function fmtMonth(s) {
    const { y, m } = parseDay(s);
    return `${MONTHS[m - 1]} ${y}`;
  }

  function fmtPct(x, digits = 1, signed = true) {
    if (!isNum(x)) return "–";
    const v = (x * 100).toFixed(digits);
    const sign = signed && x > 0 ? "+" : "";
    return `${sign}${v.replace("-", "−")}%`;
  }

  function fmtPts(x, digits = 1) {
    if (!isNum(x)) return "–";
    const v = (x * 100).toFixed(digits);
    return `${x > 0 ? "+" : ""}${v.replace("-", "−")} pts`;
  }

  function fmtLevel(x) {
    return isNum(x) ? `${x.toFixed(2).replace(/0$/, "")}x` : "–";
  }

  function fmtUsd(x) {
    if (!isNum(x)) return "–";
    return `$${Math.round(x).toLocaleString("en-US")}`;
  }

  function niceStep(span, count) {
    const raw = span / Math.max(count, 1);
    const mag = 10 ** Math.floor(Math.log10(raw));
    const norm = raw / mag;
    return (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  }

  // --- the data --------------------------------------------------------------------------

  // Each series as returns since the trial's first session; after tax, the value a sale of
  // everything would leave, against the same starting equity.
  function returns(rows, key, afterTax) {
    const field = afterTax ? `${key}_after_tax` : key;
    let start = null;
    return rows.map((row) => {
      if (start === null && isNum(row[key])) start = row[key];
      const v = row[field];
      return start !== null && isNum(v) ? v / start - 1 : null;
    });
  }

  function present(rows) {
    return SERIES.filter((s) => rows.some((row) => isNum(row[s.key])));
  }

  function last(values) {
    for (let i = values.length - 1; i >= 0; i--) if (isNum(values[i])) return values[i];
    return null;
  }

  // The level each session held: the latest decision (or event) on or before it.
  function levels(rows, exposure) {
    const sorted = [...exposure].sort((a, b) => (a.session < b.session ? -1 : 1));
    let j = -1;
    const rule = [];
    const level = [];
    for (const row of rows) {
      while (j + 1 < sorted.length && sorted[j + 1].session <= row.session) j++;
      const e = j >= 0 ? sorted[j] : null;
      rule.push(e ? e.rule_level : null);
      level.push(e && isNum(e.level) ? e.level : e ? e.rule_level : null);
    }
    return { rule, level };
  }

  // --- the line chart --------------------------------------------------------------------

  // A responsive SVG line chart over sessions (evenly spaced: trading days, not calendar days),
  // with direct labels at the line ends, a crosshair and one tooltip for every series.
  function lineChart(host, { days, lines, format, axisFormat, domain, step, label, zero }) {
    const wrap = el("div", { class: "chart" });
    const tip = el("div", { class: "tip", role: "status", hidden: true });
    host.replaceChildren(wrap);

    function draw() {
      const width = Math.max(wrap.clientWidth, 280);
      const narrow = width < 560;
      const height = narrow ? 240 : 300;
      const m = { top: 12, right: narrow ? 12 : 128, bottom: 28, left: 52 };
      const w = width - m.left - m.right;
      const h = height - m.top - m.bottom;

      const all = lines.flatMap((l) => l.values.filter(isNum));
      let [lo, hi] = domain || [Math.min(...all), Math.max(...all)];
      if (zero) {
        lo = Math.min(lo, 0);
        hi = Math.max(hi, 0);
      }
      if (hi - lo < 1e-9) {
        lo -= 0.01;
        hi += 0.01;
      }
      const stepY = niceStep(hi - lo, narrow ? 4 : 5);
      lo = Math.floor(lo / stepY) * stepY;
      hi = Math.ceil(hi / stepY) * stepY;
      const n = days.length;
      const x = (i) => m.left + (n <= 1 ? w / 2 : (i / (n - 1)) * w);
      const y = (v) => m.top + h - ((v - lo) / (hi - lo)) * h;

      const chart = svg("svg", {
        width,
        height,
        viewBox: `0 0 ${width} ${height}`,
        role: "img",
        "aria-label": label,
      });

      // Grid and the y axis.
      for (let v = lo; v <= hi + stepY / 2; v += stepY) {
        const yy = y(v);
        const isZero = zero && Math.abs(v) < stepY / 1000;
        chart.append(
          svg("line", { x1: m.left, x2: m.left + w, y1: yy, y2: yy, class: isZero ? "axis" : "grid" }),
          svg("text", { x: m.left - 8, y: yy, class: "tick", "text-anchor": "end", dy: "0.32em", text: axisFormat(v) }),
        );
      }

      // The x axis: the first session of each month, thinned to fit.
      const starts = [];
      days.forEach((d, i) => {
        if (i === 0 || d.slice(0, 7) !== days[i - 1].slice(0, 7)) starts.push(i);
      });
      // A series starting at a month's last session (the trial starts on R) has the next
      // month's first session beside it: that month is labelled, not the stub. Any other label
      // that would crowd the one before it is left out.
      if (starts.length > 1 && x(starts[1]) - x(starts[0]) < 64) starts.shift();
      const every = Math.max(1, Math.ceil(starts.length / Math.max(2, Math.floor(w / 80))));
      let shownYear = null;
      let shownX = -Infinity;
      starts.forEach((i, k) => {
        if (k % every || x(i) - shownX < 64) return;
        const { y: yr, m: mo } = parseDay(days[i]);
        chart.append(
          svg("text", {
            x: x(i),
            y: m.top + h + 18,
            class: "tick",
            "text-anchor": i === 0 ? "start" : "middle",
            text: yr !== shownYear ? `${MONTHS[mo - 1]} ${yr}` : MONTHS[mo - 1],
          }),
        );
        shownYear = yr;
        shownX = x(i);
      });

      // The lines, broken where a series has no value.
      for (const line of lines) {
        let d = "";
        let pen = false;
        line.values.forEach((v, i) => {
          if (!isNum(v)) {
            pen = false;
            return;
          }
          if (!pen) d += `M${x(i)},${y(v)}`;
          else if (step) d += `H${x(i)}V${y(v)}`;
          else d += `L${x(i)},${y(v)}`;
          pen = true;
        });
        chart.append(svg("path", { d, class: `line ${line.cls}${line.dashed ? " dashed" : ""}` }));
        // A value with no neighbour draws no line: mark it.
        line.values.forEach((v, i) => {
          if (isNum(v) && !isNum(line.values[i - 1]) && !isNum(line.values[i + 1])) {
            chart.append(svg("circle", { cx: x(i), cy: y(v), r: 3, class: `pt ${line.cls}` }));
          }
        });
      }

      // Direct labels at the right end, pushed apart so they don't collide.
      if (!narrow) {
        const ends = lines
          .map((l) => ({ l, v: last(l.values) }))
          .filter((e) => isNum(e.v))
          .map((e) => ({ ...e, yy: y(e.v) }))
          .sort((a, b) => a.yy - b.yy);
        for (let i = 1; i < ends.length; i++) ends[i].yy = Math.max(ends[i].yy, ends[i - 1].yy + 15);
        const overflow = ends.length ? ends[ends.length - 1].yy - (m.top + h) : 0;
        if (overflow > 0) ends.forEach((e) => (e.yy -= overflow));
        for (const e of ends) {
          const t = svg("text", { x: m.left + w + 8, y: e.yy, dy: "0.32em", class: "end" });
          t.append(svg("tspan", { class: "end-value", text: format(e.v) }), svg("tspan", { dx: "6", text: e.l.label }));
          chart.append(t);
        }
      }

      // The crosshair: a hairline and a dot per series at the nearest session.
      const hair = svg("line", { y1: m.top, y2: m.top + h, class: "hair", visibility: "hidden" });
      const dots = lines.map((l) => svg("circle", { r: 4, class: `dot ${l.cls}`, visibility: "hidden" }));
      const hit = svg("rect", {
        x: m.left,
        y: m.top,
        width: w,
        height: h,
        class: "hit",
        tabindex: "0",
        "aria-label": `${label}: use the arrow keys to read each session`,
      });
      chart.append(hair, ...dots, hit);

      let at = null;
      function show(i) {
        at = Math.max(0, Math.min(n - 1, i));
        const xx = x(at);
        hair.setAttribute("x1", xx);
        hair.setAttribute("x2", xx);
        hair.setAttribute("visibility", "visible");
        const rows = [];
        lines.forEach((l, k) => {
          const v = l.values[at];
          if (isNum(v)) {
            dots[k].setAttribute("cx", xx);
            dots[k].setAttribute("cy", y(v));
            dots[k].setAttribute("visibility", "visible");
          } else dots[k].setAttribute("visibility", "hidden");
          rows.push({ l, v });
        });
        rows.sort((a, b) => (isNum(b.v) ? b.v : -Infinity) - (isNum(a.v) ? a.v : -Infinity));
        tip.replaceChildren(
          el("div", { class: "tip-day", text: fmtDay(days[at]) }),
          ...rows.map(({ l, v }) =>
            el("div", { class: "tip-row" }, el("span", { class: `key ${l.cls}` }), el("strong", { text: format(v) }), el("span", { text: l.label })),
          ),
        );
        tip.hidden = false;
        const box = wrap.getBoundingClientRect();
        const left = xx + 14 + tip.offsetWidth > box.width ? xx - 14 - tip.offsetWidth : xx + 14;
        tip.style.left = `${Math.max(0, left)}px`;
        tip.style.top = `${m.top}px`;
      }
      function hide() {
        at = null;
        tip.hidden = true;
        hair.setAttribute("visibility", "hidden");
        dots.forEach((d) => d.setAttribute("visibility", "hidden"));
      }
      hit.addEventListener("pointermove", (ev) => {
        const box = chart.getBoundingClientRect();
        show(Math.round(((ev.clientX - box.left - m.left) / w) * (n - 1)));
      });
      hit.addEventListener("pointerleave", hide);
      hit.addEventListener("blur", hide);
      hit.addEventListener("focus", () => show(n - 1));
      hit.addEventListener("keydown", (ev) => {
        const moves = { ArrowLeft: -1, ArrowRight: 1, PageUp: -21, PageDown: 21 };
        if (ev.key in moves) {
          ev.preventDefault();
          show((at === null ? n - 1 : at) + moves[ev.key]);
        } else if (ev.key === "Home" || ev.key === "End") {
          ev.preventDefault();
          show(ev.key === "Home" ? 0 : n - 1);
        }
      });

      wrap.replaceChildren(chart, tip);
    }

    draw();
    let width = wrap.clientWidth;
    new ResizeObserver(() => {
      if (wrap.clientWidth !== width) {
        width = wrap.clientWidth;
        draw();
      }
    }).observe(wrap);
  }

  function legend(series) {
    return el(
      "ul",
      { class: "legend", "aria-label": "Series" },
      ...series.map((s) => el("li", {}, el("span", { class: `key ${s.cls}${s.dashed ? " dashed" : ""}` }), el("span", { text: s.long }))),
    );
  }

  // --- the sections ----------------------------------------------------------------------

  function tiles(rows, series, afterTax) {
    const totals = Object.fromEntries(series.map((s) => [s.key, last(returns(rows, s.key, afterTax))]));
    const items = series.map((s) =>
      el("li", {}, el("span", { class: `key ${s.cls}` }), el("strong", { text: fmtPct(totals[s.key]) }), el("span", { text: s.long })),
    );
    const edge = isNum(totals.book) && isNum(totals.twin) ? totals.book - totals.twin : null;
    items.push(
      el(
        "li",
        { class: "edge" },
        el("strong", { text: fmtPts(edge, 2) }),
        el("span", { text: "Claude's edge: the book less its twin" }),
      ),
    );
    return el("ul", { class: "dash-tiles", "aria-label": "Returns since the trial began" }, ...items);
  }

  function performance(host, doc, afterTax) {
    const rows = doc.series;
    const days = rows.map((r) => r.session);
    const series = present(rows);
    const lines = series.map((s) => ({ ...s, values: returns(rows, s.key, afterTax) }));
    const chartHost = el("div");
    host.replaceChildren(
      tiles(rows, series, afterTax),
      el("h3", { text: `Return since ${fmtDay(days[0])}, ${afterTax ? "after" : "before"} the estimated tax` }),
      legend(series),
      chartHost,
      table(rows, series, afterTax),
    );
    lineChart(chartHost, {
      days,
      lines,
      format: (v) => fmtPct(v),
      axisFormat: (v) => fmtPct(v, Math.abs(v) < 0.1 && v !== 0 ? 1 : 0),
      label: `Return since the trial began, ${afterTax ? "after" : "before"} tax`,
      zero: true,
    });
  }

  // The chart's numbers at each month's last session, for readers who can't use the chart.
  function table(rows, series, afterTax) {
    const values = series.map((s) => returns(rows, s.key, afterTax));
    const head = el("tr", {}, el("th", { scope: "col", text: "Month end" }), ...series.map((s) => el("th", { scope: "col", text: s.label })));
    const body = [];
    rows.forEach((row, i) => {
      const next = rows[i + 1];
      if (next && next.session.slice(0, 7) === row.session.slice(0, 7)) return;
      body.push(
        el("tr", {}, el("th", { scope: "row", text: fmtDay(row.session) }), ...values.map((v) => el("td", { text: fmtPct(v[i]) }))),
      );
    });
    return el(
      "details",
      { class: "as-table" },
      el("summary", { text: "The numbers as a table" }),
      el("div", { class: "table-scroll" }, el("table", { class: "data" }, el("thead", {}, head), el("tbody", {}, ...body.reverse()))),
    );
  }

  function exposureSection(host, doc) {
    const rows = doc.series;
    const days = rows.map((r) => r.session);
    const { rule, level } = levels(rows, doc.exposure);
    const lines = [
      { ...SERIES[0], label: "Book", long: "The book's level (after Claude's shift)", values: level },
      { ...SERIES[1], label: "Rules", long: "The rule level (the twin's)", values: rule, dashed: true },
    ];
    const chartHost = el("div");
    const latest = doc.exposure[doc.exposure.length - 1];
    host.replaceChildren(
      el("h3", { text: "S&P 500 exposure: the rule level and the book's" }),
      legend(lines),
      chartHost,
      latest ? satellite(latest) : null,
    );
    lineChart(chartHost, {
      days,
      lines,
      format: fmtLevel,
      axisFormat: fmtLevel,
      domain: [0, 1.5],
      step: true,
      label: "S&P 500 exposure over time",
    });
  }

  function satellite(entry) {
    const weights = Object.entries(entry.satellite || {}).sort((a, b) => b[1] - a[1]);
    const total = weights.reduce((sum, [, w]) => sum + w, 0);
    const scale = Math.max(0.1, ...weights.map(([, w]) => w));
    const list = el(
      "ul",
      { class: "bars", "aria-label": "The satellite's weights" },
      ...weights.map(([symbol, w]) => {
        const bar = svg("svg", { class: "bar", viewBox: "0 0 100 10", preserveAspectRatio: "none", "aria-hidden": "true" });
        bar.append(svg("rect", { x: 0, y: 0, height: 10, width: Math.max(1, (w / scale) * 100), rx: 2 }));
        return el("li", {}, el("span", { class: "sym", text: symbol }), bar, el("span", { class: "num", text: fmtPct(w, 1, false) }));
      }),
    );
    return el(
      "div",
      { class: "satellite" },
      el("h3", { text: `The satellite since ${fmtDay(entry.session)}` }),
      el("p", {
        class: "note",
        text: `${fmtPct(total, 0, false)} of the account, set by Claude among the approved ETFs; at most 10% in any one.${
          entry.held_satellite ? " Last month's weights, held." : ""
        }`,
      }),
      list,
    );
  }

  function scorecard(host, card) {
    const items = [
      ["Months scored", isNum(card.months_complete) ? String(card.months_complete) : "–", "complete months, E to the next E"],
      ["Edge after tax", fmtPts(card.excess_after_tax, 2), "the book less the twin, all months"],
      ["Edge before tax", fmtPts(card.excess_before_tax, 2), "the same, before the estimated tax"],
    ];
    if (isNum(card.excess_after_tax_a_year)) {
      items.push([
        "A year, after tax",
        fmtPts(card.excess_after_tax_a_year, 1),
        `95% interval ${fmtPts(card.excess_after_tax_a_year_low, 1)} to ${fmtPts(card.excess_after_tax_a_year_high, 1)}`,
      ]);
    }
    items.push(
      ["From the satellite", fmtPts(card.tilt_score, 2), "the tilts' part of the edge"],
      ["From the shift", fmtPts(card.shift_score, 2), "the core shift's part of the edge"],
      ["Tax realized", fmtUsd(card.tax_realized), `the twin: ${fmtUsd(card.twin_tax_realized)}`],
    );
    if (isNum(card.expectation_direction_rate)) {
      items.push([
        "Expectations right",
        fmtPct(card.expectation_direction_rate, 0, false),
        `in direction, of ${card.expectations_scored}; ${fmtPct(card.expectation_size_rate, 0, false)} in size too`,
      ]);
    }
    host.replaceChildren(
      el("h3", { text: "The scorecard" }),
      el(
        "dl",
        { class: "score" },
        ...items.map(([k, v, note]) => el("div", {}, el("dt", { text: k }), el("dd", { text: v }), el("span", { text: note }))),
      ),
    );
  }

  function journalSection(host, doc) {
    const exposure = Object.fromEntries(doc.exposure.map((e) => [e.session, e]));
    const entries = [...doc.journal].reverse();
    const recent = entries.slice(0, 3).map((j) => entry(j, exposure[j.session]));
    const older = entries.slice(3).map((j) => entry(j, exposure[j.session]));
    host.replaceChildren(
      el("h3", { text: "The decision journal" }),
      el("p", { class: "note", text: "Each month's decision in Claude's words, the limits code enforced on it, and how the month turned out." }),
      ...recent,
      older.length ? el("details", { class: "older" }, el("summary", { text: `Earlier months (${older.length})` }), ...older) : null,
    );
  }

  function entry(j, e) {
    const chips = el("div", { class: "chips" });
    if (e) {
      chips.append(
        el("span", { class: `pill ${e.trend === "UP" ? "pill-up" : "pill-corr"}`, text: e.trend === "UP" ? "Up-trend" : "Down-trend" }),
        el("span", { class: "chip", text: `Rules ${fmtLevel(e.rule_level)} → book ${fmtLevel(isNum(e.level) ? e.level : e.rule_level)}` }),
      );
      if (e.capped) chips.append(el("span", { class: "pill pill-press", text: "Volatility cap" }));
      if (e.halted) chips.append(el("span", { class: "pill pill-corr", text: "Drawdown halt" }));
      if (e.kind === "EVENT") chips.append(el("span", { class: "chip", text: "Event" }));
      if (e.fell_back) chips.append(el("span", { class: "chip", text: "No valid decision: held" }));
    }
    const body = [el("p", { class: "summary", text: j.summary || "No summary: the rules' allocation held." })];
    const decision = j.decision;
    if (decision && Array.isArray(decision.satellite)) {
      const rows = decision.satellite.map((w) =>
        el(
          "tr",
          {},
          el("th", { scope: "row", text: w.symbol }),
          el("td", { class: "num", text: fmtPct(w.weight, 1, false) }),
          el("td", { class: "num", text: fmtPts(w.expected_vs_market, 1) }),
          el("td", { text: w.expectation || "" }),
        ),
      );
      body.push(
        el(
          "div",
          { class: "table-scroll" },
          el(
            "table",
            { class: "data" },
            el(
              "thead",
              {},
              el(
                "tr",
                {},
                el("th", { scope: "col", text: "ETF" }),
                el("th", { scope: "col", text: "Weight" }),
                el("th", { scope: "col", text: "vs market" }),
                el("th", { scope: "col", text: "Expectation" }),
              ),
            ),
            el("tbody", {}, ...rows),
          ),
        ),
      );
    } else if (decision) {
      body.push(el("p", { class: "note", text: "The satellite: last month's weights, held." }));
    }
    if (decision && (decision.core_reasoning || decision.tax_note)) {
      body.push(
        el(
          "details",
          {},
          el("summary", { text: "Reasoning" }),
          decision.core_reasoning ? el("p", { text: decision.core_reasoning }) : null,
          decision.tax_note ? el("p", { class: "note", text: `Tax: ${decision.tax_note}` }) : null,
        ),
      );
    }
    if (j.limits && j.limits.length) {
      body.push(
        el(
          "ul",
          { class: "limits", "aria-label": "Limits code enforced" },
          ...j.limits.map((c) => el("li", { text: c.message || `${c.rule}: ${c.outcome}` })),
        ),
      );
    }
    const r = j.results;
    if (r && r.after_tax) {
      const a = r.after_tax;
      const paper = r.paper && isNum(r.paper.after_tax) ? `; the paper account ${fmtPct(r.paper.after_tax)}` : "";
      body.push(
        el("p", {
          class: "result",
          text: `To ${fmtDay(r.end)}${r.complete ? "" : " (so far)"}, after tax: the book ${fmtPct(a.book)}, the twin ${fmtPct(a.twin)}, the S&P 500 ${fmtPct(a.spy)}${paper}. Edge ${fmtPts(a.excess, 2)}.`,
        }),
      );
    }
    return el(
      "article",
      { class: "entry" },
      el("header", {}, el("h4", {}, el("time", { datetime: j.session, text: fmtMonth(j.session) })), chips),
      ...body,
    );
  }

  // --- start -----------------------------------------------------------------------------

  function render(doc) {
    let afterTax = true;
    const perf = el("div", { class: "dash-block" });
    const toggle = el("div", { class: "toggle", role: "group", "aria-label": "Tax" });
    const buttons = [true, false].map((value) => {
      const b = el("button", { type: "button", "aria-pressed": String(value === afterTax), text: value ? "After tax" : "Before tax" });
      b.addEventListener("click", () => {
        afterTax = value;
        buttons.forEach((x, k) => x.setAttribute("aria-pressed", String([true, false][k] === afterTax)));
        performance(perf, doc, afterTax);
      });
      return b;
    });
    toggle.append(...buttons);
    const exposureBlock = el("div", { class: "dash-block" });
    const cardBlock = el("div", { class: "dash-block" });
    const journalBlock = el("div", { class: "dash-block" });

    root.replaceChildren(
      el(
        "div",
        { class: "dash-meta" },
        el("p", {
          text: `Through ${fmtDay(doc.through)} · ${doc.mode === "paper" ? "Alpaca paper account" : "simulated (dry run)"} · ${doc.note}`,
        }),
        toggle,
      ),
      perf,
      exposureBlock,
      cardBlock,
      journalBlock,
    );
    performance(perf, doc, afterTax);
    if (doc.exposure.length) exposureSection(exposureBlock, doc);
    if (doc.scorecard) scorecard(cardBlock, doc.scorecard);
    if (doc.journal.length) journalSection(journalBlock, doc);
  }

  fetch("data/snapshot.json", { cache: "no-cache" })
    .then((res) => (res.ok ? res.json() : null))
    .then((doc) => {
      if (!doc || doc.schema !== 1 || !Array.isArray(doc.series) || !doc.series.length) return;
      doc.exposure = Array.isArray(doc.exposure) ? doc.exposure : [];
      doc.journal = Array.isArray(doc.journal) ? doc.journal : [];
      render(doc);
    })
    .catch(() => {});
})();
