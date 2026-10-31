// The dashboard's drawing (README §5.8): the DOM and SVG helpers, the formats, the series' math
// and the charts, shared by the site's dashboard (dashboard.js) and the MCP server's (README
// §5.10), which inlines this file. Every value reaches the page through textContent or
// an SVG attribute, never as markup.
window.AuraChart = (() => {
  "use strict";

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

  function fmtUsd(x, signed = false) {
    if (!isNum(x)) return "–";
    const v = Math.round(x);
    const sign = v < 0 ? "−" : signed && v > 0 ? "+" : "";
    return `${sign}$${Math.abs(v).toLocaleString("en-US")}`;
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
      const m = { top: 12, right: narrow ? 12 : 148, bottom: 28, left: 52 };
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

  // The satellite's weights as bars, the largest first, scaled to the largest (at least 10%).
  function bars(satellite) {
    const weights = Object.entries(satellite || {}).sort((a, b) => b[1] - a[1]);
    const scale = Math.max(0.1, ...weights.map(([, w]) => w));
    return el(
      "ul",
      { class: "bars", "aria-label": "The satellite's weights" },
      ...weights.map(([symbol, w]) => {
        const bar = svg("svg", { class: "bar", viewBox: "0 0 100 10", preserveAspectRatio: "none", "aria-hidden": "true" });
        bar.append(svg("rect", { x: 0, y: 0, height: 10, width: Math.max(1, (w / scale) * 100), rx: 2 }));
        return el("li", {}, el("span", { class: "sym", text: symbol }), bar, el("span", { class: "num", text: fmtPct(w, 1, false) }));
      }),
    );
  }

  return {
    SERIES,
    el,
    svg,
    isNum,
    fmtDay,
    fmtMonth,
    fmtPct,
    fmtPts,
    fmtLevel,
    fmtUsd,
    returns,
    present,
    last,
    levels,
    lineChart,
    legend,
    bars,
  };
})();
