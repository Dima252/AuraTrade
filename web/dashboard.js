// The public dashboard (README §5.8, task 6.2): reads data/snapshot.json, which `simulate`
// publishes each evening, and draws it into #dash with chart.js. Without the file (before the
// paper trial) or without JavaScript, the static text in #dash stays. Every value reaches the
// page through textContent or an SVG attribute, never as markup.
(() => {
  "use strict";

  const root = document.getElementById("dash");
  if (!root || !window.AuraChart) return;

  const { SERIES, el, isNum, fmtDay, fmtMonth, fmtPct, fmtPts, fmtLevel, fmtUsd } = window.AuraChart;
  const { returns, present, last, levels, lineChart, legend, bars } = window.AuraChart;

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
    const total = Object.values(entry.satellite || {}).reduce((sum, w) => sum + w, 0);
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
      bars(entry.satellite),
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
