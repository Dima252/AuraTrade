// The public dashboard (README §5.8): reads data/snapshot.json, which `simulate` publishes each
// evening, and draws it into #dash with chart.js, in the MCP dashboard's layout (README §5.10).
// Without the file or without JavaScript, the static text in #dash stays. Every value reaches the
// page through textContent, an SVG attribute or the CSSOM, never as markup or a style attribute
// (the content-security policy allows neither).
(() => {
  "use strict";

  const root = document.getElementById("dash");
  if (!root || !window.AuraChart) return;

  const C = window.AuraChart;
  const { SERIES, el, isNum, fmtDay, fmtMonth, fmtPct, fmtPts, fmtLevel, fmtUsd } = C;

  const signed = (x) => (isNum(x) && x < 0 ? "neg" : isNum(x) && x > 0 ? "pos" : null);
  const rowsWith = (rows, key) => rows.filter((r) => isNum(r[key]));

  // --- the top: the balance, the returns, the exposure, the satellite, the latest decision ---

  // The paper account's own equity, or the book's where there's no account (dry run); its gain
  // since the trial began and over the last session, and the tax picture.
  function balance(doc) {
    const live = rowsWith(doc.series, "account").length > 0;
    const key = live ? "account" : "book";
    const rows = rowsWith(doc.series, key);
    const first = rows[0];
    const now = rows[rows.length - 1];
    const prev = rows.length > 1 ? rows[rows.length - 2] : null;
    const equity = now[key];
    const after = now[`${key}_after_tax`];
    const gain = equity - first[key];
    const day = prev ? equity - prev[key] : null;
    const card = doc.scorecard || {};
    const taxRow = (label, value, note) =>
      el("div", {}, el("dt", {}, label, note ? el("span", { text: note }) : null), el("dd", { text: value }));
    return el(
      "section",
      { class: "card balance-card" },
      el(
        "div",
        {},
        el("p", { class: "label", text: live ? "Paper account balance" : "The book's equity (simulated)" }),
        el("p", { class: "balance", text: fmtUsd(equity) }),
        el(
          "div",
          { class: "deltas" },
          el(
            "span",
            {},
            el("strong", { class: signed(gain), text: `${fmtUsd(gain, true)} (${fmtPct(first[key] > 0 ? gain / first[key] : null, 2)})` }),
            ` since ${fmtDay(first.session)}`,
          ),
          prev
            ? el("span", {}, el("strong", { class: signed(day), text: `${fmtUsd(day, true)} (${fmtPct(day / prev[key], 2)})` }), " last session")
            : null,
        ),
      ),
      el(
        "dl",
        { class: "taxes", "aria-label": "Tax" },
        taxRow("After the estimated tax", fmtUsd(after), "if everything were sold today"),
        taxRow("Tax owed on a sale", fmtUsd(isNum(after) ? equity - after : null), "25% of the open gains"),
        taxRow("Tax realized so far", fmtUsd(card.tax_realized), isNum(card.twin_tax_realized) ? `the twin: ${fmtUsd(card.twin_tax_realized)}` : null),
      ),
    );
  }

  function tiles(rows, afterTax) {
    const series = C.present(rows).filter((s) => s.key !== "account");
    const total = Object.fromEntries(series.map((s) => [s.key, C.last(C.returns(rows, s.key, afterTax))]));
    const subs = { book: "Claude's decisions", twin: "the rules alone", spy: "bought and held" };
    const items = series.map((s) =>
      el(
        "li",
        {},
        el("span", { class: "name" }, el("span", { class: `key ${s.cls}` }), s.label),
        el("strong", { class: signed(total[s.key]), text: fmtPct(total[s.key], 2) }),
        el("span", { class: "sub", text: subs[s.key] }),
      ),
    );
    const edge = isNum(total.book) && isNum(total.twin) ? total.book - total.twin : null;
    const market = isNum(total.book) && isNum(total.spy) ? total.book - total.spy : null;
    items.push(
      el(
        "li",
        { class: "edge" },
        el("span", { class: "name", text: "Claude's edge" }),
        el("strong", { text: fmtPts(edge, 2) }),
        el("span", { class: "sub", text: `the book less the twin; ${fmtPts(market, 2)} on the S&P 500` }),
      ),
    );
    return el("ul", { class: "tiles", "aria-label": `Returns since the trial began, ${afterTax ? "after" : "before"} tax` }, ...items);
  }

  // The chart's numbers at each month's last session, for readers who can't use the chart.
  function table(rows, afterTax) {
    const series = C.present(rows);
    const values = series.map((s) => C.returns(rows, s.key, afterTax));
    const head = el("tr", {}, el("th", { scope: "col", text: "Month end" }), ...series.map((s) => el("th", { scope: "col", text: s.label })));
    const body = [];
    rows.forEach((row, i) => {
      const next = rows[i + 1];
      if (next && next.session.slice(0, 7) === row.session.slice(0, 7)) return;
      body.push(el("tr", {}, el("th", { scope: "row", text: fmtDay(row.session) }), ...values.map((v) => el("td", { text: fmtPct(v[i]) }))));
    });
    return el(
      "details",
      {},
      el("summary", { text: "The numbers as a table" }),
      el("div", { class: "table-scroll" }, el("table", { class: "data" }, el("thead", {}, head), el("tbody", {}, ...body.reverse()))),
    );
  }

  function chart(rows, afterTax) {
    const days = rows.map((r) => r.session);
    const series = C.present(rows);
    const lines = series.map((s) => ({ ...s, values: C.returns(rows, s.key, afterTax) }));
    const host = el("div");
    const card = el(
      "section",
      { class: "card chart-card" },
      el("h3", { text: `Return since ${fmtDay(days[0])}, ${afterTax ? "after" : "before"} tax` }),
      C.legend(series),
      host,
      table(rows, afterTax),
    );
    const draw = () =>
      C.lineChart(host, {
        days,
        lines,
        format: (v) => fmtPct(v),
        axisFormat: (v) => fmtPct(v, Math.abs(v) < 0.1 && v !== 0 ? 1 : 0),
        label: `Return since the trial began, ${afterTax ? "after" : "before"} tax`,
        zero: true,
      });
    return [card, draw];
  }

  // Positions go through the CSSOM: the content-security policy refuses style attributes.
  function placed(node, prop, value) {
    node.style[prop] = value;
    return node;
  }

  function gauge(level, rule) {
    const max = 1.5;
    const at = (x) => `${Math.max(0, Math.min(1, x / max)) * 100}%`;
    const scale = el("div", { class: "scale", "aria-hidden": "true" });
    for (const t of [0, 0.5, 1, 1.5]) scale.append(placed(el("span", { text: fmtLevel(t) }), "left", at(t)));
    return el(
      "div",
      { class: "gauge", role: "img", "aria-label": `Exposure ${fmtLevel(level)} of the S&P 500; the rule level ${fmtLevel(rule)}` },
      el("div", { class: "track" }, placed(el("div", { class: "fill" }), "width", at(level)), placed(el("div", { class: "mark" }), "left", at(rule))),
      scale,
    );
  }

  function exposure(e) {
    const level = isNum(e.level) ? e.level : e.rule_level;
    const shift = isNum(e.shift) ? `${e.shift >= 0 ? "+" : "−"}${fmtLevel(Math.abs(e.shift))}` : "–";
    return el(
      "section",
      { class: "card" },
      el("h3", { text: "S&P 500 exposure" }),
      el("div", { class: "level" }, el("strong", { text: fmtLevel(level) }), el("span", { text: `rules ${fmtLevel(e.rule_level)}, Claude's shift ${shift}` })),
      el(
        "div",
        { class: "pills" },
        el("span", { class: `pill ${e.trend === "UP" ? "pill-up" : "pill-corr"}`, text: e.trend === "UP" ? "Up-trend" : "Down-trend" }),
        e.capped ? el("span", { class: "pill pill-press", text: "Volatility cap" }) : null,
        e.halted ? el("span", { class: "pill pill-corr", text: "Drawdown halt" }) : null,
        e.fell_back ? el("span", { class: "chip", text: "No valid decision: held" }) : null,
      ),
      gauge(level, e.rule_level),
      el(
        "div",
        { class: "gauge-key" },
        el("span", {}, el("span", { class: "key s1" }), "The book"),
        el("span", {}, el("span", { class: "key s2 dashed" }), "The rule level (the twin)"),
        el("span", { text: `since ${fmtDay(e.session)}` }),
      ),
    );
  }

  function satellite(e) {
    const total = Object.values(e.satellite || {}).reduce((sum, w) => sum + w, 0);
    return el(
      "section",
      { class: "card satellite" },
      el("h3", { text: `The satellite · ${fmtPct(total, 0, false)} of the account` }),
      C.bars(e.satellite),
      el("p", { class: "note", text: `Claude's weights among the approved ETFs; all in SPY is neutral.${e.held_satellite ? " Last month's weights, held." : ""}` }),
    );
  }

  // One decision: the summary, the weights with their expectations, the reasoning, the limits
  // code enforced, the month's result. The latest is a card; earlier ones are entries in a fold.
  function decisionBody(j, e) {
    const chips = el("div", { class: "chips" });
    if (e) {
      chips.append(el("span", { class: "chip", text: `Rules ${fmtLevel(e.rule_level)} → book ${fmtLevel(isNum(e.level) ? e.level : e.rule_level)}` }));
      if (e.kind === "EVENT") chips.append(el("span", { class: "chip", text: "Event" }));
      if (e.fell_back) chips.append(el("span", { class: "chip", text: "No valid decision: held" }));
    }
    const body = [
      el("header", {}, el("h4", {}, el("time", { datetime: j.session, text: fmtMonth(j.session) })), chips),
      el("p", { class: "summary", text: j.summary || "No summary: the rules' allocation held." }),
    ];
    const d = j.decision;
    if (d && Array.isArray(d.satellite) && d.satellite.length) {
      body.push(
        el(
          "ul",
          { class: "expect", "aria-label": "Weights and expectations" },
          ...d.satellite.map((w) =>
            el(
              "li",
              {},
              el("span", { class: "sym", text: w.symbol }),
              el("span", { class: "w", text: fmtPct(w.weight, 1, false) }),
              el("span", { class: "why", text: [w.expectation, isNum(w.expected_vs_market) ? `(${fmtPts(w.expected_vs_market, 1)} vs the market)` : null].filter(Boolean).join(" ") }),
            ),
          ),
        ),
      );
    } else if (d) {
      body.push(el("p", { class: "result", text: "The satellite: last month's weights, held." }));
    }
    if (d && (d.core_reasoning || d.tax_note)) {
      body.push(
        el(
          "details",
          {},
          el("summary", { text: "Claude's reasoning" }),
          d.core_reasoning ? el("p", { text: d.core_reasoning }) : null,
          d.tax_note ? el("p", { text: `Tax: ${d.tax_note}` }) : null,
        ),
      );
    }
    if (j.limits && j.limits.length) {
      body.push(el("ul", { class: "limits", "aria-label": "Limits code enforced" }, ...j.limits.map((c) => el("li", { text: c.message || `${c.rule}: ${c.outcome}` }))));
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
    return body;
  }

  function latestDecision(j, e) {
    return el("section", { class: "card decision" }, el("h3", { text: "Claude's latest decision" }), ...decisionBody(j, e));
  }

  // --- the folds: the scorecard, the exposure over time, the earlier months ---------------

  function scorecard(card) {
    const items = [
      ["Months scored", isNum(card.months_complete) ? String(card.months_complete) : "–", "complete months, E to the next E"],
      ["Edge after tax", fmtPts(card.excess_after_tax, 2), "the book less the twin, all months"],
      ["Edge before tax", fmtPts(card.excess_before_tax, 2), "the same, before the estimated tax"],
    ];
    if (isNum(card.excess_after_tax_a_year)) {
      items.push([
        "A year, after tax",
        fmtPts(card.excess_after_tax_a_year, 1),
        `90% interval ${fmtPts(card.excess_after_tax_a_year_low, 1)} to ${fmtPts(card.excess_after_tax_a_year_high, 1)}`,
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
    return el(
      "details",
      { class: "more" },
      el("summary", { text: "The scorecard: is Claude adding value?" }),
      el("dl", { class: "score" }, ...items.map(([k, v, note]) => el("div", {}, el("dt", { text: k }), el("dd", { text: v }), el("span", { text: note })))),
    );
  }

  function exposureHistory(doc) {
    const rows = doc.series;
    const { rule, level } = C.levels(rows, doc.exposure);
    const lines = [
      { ...SERIES[0], label: "Book", long: "The book's level (after Claude's shift)", values: level },
      { ...SERIES[1], label: "Rules", long: "The rule level (the twin's)", values: rule, dashed: true },
    ];
    const host = el("div");
    const fold = el("details", { class: "more" }, el("summary", { text: "The S&P 500 exposure over time" }), C.legend(lines), host);
    let drawn = false;
    // Drawn when first opened: a closed fold has no width to size the chart to.
    fold.addEventListener("toggle", () => {
      if (!fold.open || drawn) return;
      drawn = true;
      C.lineChart(host, {
        days: rows.map((r) => r.session),
        lines,
        format: fmtLevel,
        axisFormat: fmtLevel,
        domain: [0, 1.5],
        step: true,
        label: "S&P 500 exposure over time",
      });
    });
    return fold;
  }

  function earlier(doc, byDay) {
    const older = doc.journal.slice(0, -1).reverse();
    if (!older.length) return null;
    return el(
      "details",
      { class: "more" },
      el("summary", { text: `Earlier decisions (${older.length})` }),
      ...older.map((j) => el("article", { class: "entry" }, ...decisionBody(j, byDay[j.session]))),
    );
  }

  // --- start -------------------------------------------------------------------------------

  function render(doc) {
    let afterTax = true;
    const returnsHost = el("div", { class: "stack" });
    const toggle = el("div", { class: "toggle", role: "group", "aria-label": "Tax" });
    const buttons = [true, false].map((value) => {
      const b = el("button", { type: "button", "aria-pressed": String(value === afterTax), text: value ? "After tax" : "Before tax" });
      b.addEventListener("click", () => {
        afterTax = value;
        buttons.forEach((x, k) => x.setAttribute("aria-pressed", String([true, false][k] === afterTax)));
        returns();
      });
      return b;
    });
    toggle.append(...buttons);

    function returns() {
      const [card, draw] = chart(doc.series, afterTax);
      returnsHost.replaceChildren(tiles(doc.series, afterTax), card);
      draw();
    }

    const where = doc.mode === "paper" ? "Alpaca paper account" : "simulated (dry run)";
    const latest = doc.exposure[doc.exposure.length - 1];
    const byDay = Object.fromEntries(doc.exposure.map((e) => [e.session, e]));
    const entry = doc.journal[doc.journal.length - 1];
    root.replaceChildren(
      el("div", { class: "dash-meta" }, el("p", { text: `Through ${fmtDay(doc.through)} · ${where}` }), toggle),
      balance(doc),
      returnsHost,
      latest ? el("div", { class: "pair" }, exposure(latest), satellite(latest)) : null,
      entry ? latestDecision(entry, byDay[entry.session]) : null,
      doc.scorecard ? scorecard(doc.scorecard) : null,
      doc.exposure.length ? exposureHistory(doc) : null,
      earlier(doc, byDay),
      doc.note ? el("p", { class: "note", text: doc.note }) : null,
    );
    returns();
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
