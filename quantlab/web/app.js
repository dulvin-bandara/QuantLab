"use strict";

// ---------------------------------------------------------------- helpers
const $ = (sel) => document.querySelector(sel);
const view = $("#view");
const state = { view: "dashboard", symbol: localStorage.getItem("ql_symbol") || "AAPL", years: 1 };
let charts = [];
let mountQueue = [];
/** Run after the current view is attached to the page (charts and forms need a live DOM). */
const onMount = (fn) => mountQueue.push(fn);
/** replaceChildren that accepts nested arrays and skips empty values, like h(). */
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false));

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body) { init.method = opts.method || "POST"; init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const res = await fetch("/api" + path, init);
  if (res.status === 401 && path !== "/login") { showLogin(); throw new Error("Please log in."); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : `Request failed (${res.status})`);
  }
  return data;
}

const dash = "–";
const fmt = {
  num: (v, d = 2) => v == null ? dash : (Math.abs(v) < 0.5 / 10 ** d ? 0 : Number(v)).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }),
  int: (v) => v == null ? dash : Math.round(v).toLocaleString(),
  pct: (v, d = 1) => v == null ? dash : (v * 100).toFixed(d) + "%",
  spct: (v, d = 1) => v == null ? dash : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v * 100).toFixed(d) + "%",
  snum: (v, d = 2) => v == null ? dash : (v > 0 ? "+" : v < 0 ? "−" : "") + fmt.num(Math.abs(v), d),
  money: (v, cur) => v == null ? dash : fmt.num(v) + (cur ? " " + cur : ""),
};
const tone = (v) => v == null || v === 0 ? "" : v > 0 ? "up" : "down";
const signed = (v, text) => h("span", { class: "num " + tone(v) }, text);
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const tile = (k, v, s) => h("div", { class: "tile" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v), s ? h("div", { class: "s" }, s) : null);
const loading = (text = "Loading…") => h("div", { class: "loading" }, text);
const errorBox = (e) => h("div", { class: "notice error" }, e.message || String(e));

function chip(label) {
  const cls = /Buy/.test(label) ? "buy" : /Sell/.test(label) ? "sell" : "";
  return h("span", { class: "chip " + cls }, label);
}
function scoreBar(score) {
  const w = Math.min(Math.abs(score), 1) * 50;
  return h("div", { class: "bar", title: `Score ${fmt.snum(score)} on a −1 to +1 scale` },
    h("i", { class: score >= 0 ? "pos" : "neg", style: `width:${w}%` }));
}
function table(headers, rows, opts = {}) {
  return h("div", { class: "table-wrap" + (opts.scroll ? " scroll" : "") },
    h("table", {}, h("thead", {}, h("tr", {}, headers.map((x) => h("th", { class: x.l ? "l" : null }, x.t ?? x)))),
      h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { colspan: headers.length, class: "l muted" }, opts.empty || "Nothing to show yet.")))));
}
function sparkline(values) {
  const w = 96, ht = 26, min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1) * w).toFixed(1)},${(ht - 2 - (v - min) / span * (ht - 4)).toFixed(1)}`).join(" ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", w); svg.setAttribute("height", ht); svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Last 60 days");
  svg.innerHTML = `<polyline points="${pts}" fill="none" stroke="${cssVar("--series-1")}" stroke-width="1.5"/>`;
  return svg;
}

// ---------------------------------------------------------------- charts
function makeChart(el) {
  const chart = LightweightCharts.createChart(el, {
    autoSize: true,
    layout: { background: { type: "solid", color: cssVar("--surface") }, textColor: cssVar("--text-2"), fontFamily: "system-ui, sans-serif" },
    grid: { vertLines: { visible: false }, horzLines: { color: cssVar("--border") } },
    rightPriceScale: { borderColor: cssVar("--border") },
    timeScale: { borderColor: cssVar("--border") },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });
  charts.push(chart);
  return chart;
}
const lineOpts = (color) => ({ color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerRadius: 4 });
const swatch = (color, label) => h("span", {}, h("i", { style: `background:${color}` }), label);

/** Legend row that shows each series' value under the crosshair (or the latest when idle). */
function liveLegend(chart, el, series) {
  const draw = (param) => {
    fill(el, series.map((s) => {
      let v = param && param.seriesData ? param.seriesData.get(s.series) : null;
      if (!v) v = s.last;
      const text = !v ? "" : s.ohlc ? ` O ${fmt.num(v.open)}  H ${fmt.num(v.high)}  L ${fmt.num(v.low)}  C ${fmt.num(v.close)}` : " " + fmt.num(v.value);
      return h("span", { class: "num" }, s.color ? h("i", { style: `background:${s.color}` }) : null, s.label, text);
    }), param && param.time ? h("span", { class: "muted" }, String(param.time)) : "");
  };
  chart.subscribeCrosshairMove(draw);
  draw(null);
}

// ---------------------------------------------------------------- routing
function go(viewName, symbol) {
  if (symbol) { state.symbol = symbol.toUpperCase(); localStorage.setItem("ql_symbol", state.symbol); }
  state.view = viewName;
  location.hash = viewName === "dashboard" ? "dashboard" : `${viewName}/${encodeURIComponent(state.symbol)}`;
}
function render() {
  const [name, sym] = location.hash.slice(1).split("/");
  if (VIEWS[name]) state.view = name;
  if (sym) state.symbol = decodeURIComponent(sym).toUpperCase();
  charts.forEach((c) => c.remove()); charts = []; mountQueue = [];
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === state.view));
  view.replaceChildren(loading());
  const token = (render.token = Symbol());
  VIEWS[state.view]().then((nodes) => {
    if (render.token !== token) return;
    fill(view, nodes);
    mountQueue.splice(0).forEach((fn) => fn());
  })
    .catch((e) => { if (render.token === token) view.replaceChildren(errorBox(e)); });
}

// ---------------------------------------------------------------- dashboard
async function dashboardView() {
  const rows = await api("/overview");
  const err = h("p", { class: "error", role: "alert" });
  const input = h("input", { placeholder: "Add ticker (ASX: BHP.AX)", "aria-label": "Ticker to add" });
  const add = h("form", { class: "row", onsubmit: async (e) => {
    e.preventDefault(); err.textContent = "";
    try { await api("/watchlist", { body: { symbol: input.value } }); render(); } catch (ex) { err.textContent = ex.message; }
  } }, input, h("button", { type: "submit" }, "Add"));

  const body = rows.map((r) => r.error
    ? h("tr", {}, h("td", {}, r.symbol), h("td", { colspan: 7, class: "l muted" }, r.error), h("td", {}, removeBtn(r.symbol)))
    : h("tr", { class: "click", onclick: () => go("research", r.symbol) },
      h("td", {}, h("strong", {}, r.symbol), h("div", { class: "muted small" }, r.name)),
      h("td", { class: "num" }, fmt.money(r.price, r.currency)),
      h("td", {}, signed(r.change_pct, fmt.spct(r.change_pct, 2))),
      h("td", {}, sparkline(r.spark)),
      h("td", {}, chip(r.label)),
      h("td", {}, h("div", { class: "row", style: "justify-content:flex-end;flex-wrap:nowrap" }, scoreBar(r.score), h("span", { class: "num" }, fmt.snum(r.score)))),
      h("td", { class: "num", title: "Model's probability of a higher close in 5 trading days" }, fmt.pct(r.prob_up, 0)),
      h("td", { class: "num", title: `${r.news_count} headlines in the last 7 days` }, r.news_count ? fmt.snum(r.news_score) : dash),
      h("td", {}, removeBtn(r.symbol))));
  function removeBtn(symbol) {
    return h("button", { class: "link", title: `Remove ${symbol}`, "aria-label": `Remove ${symbol}`, onclick: async (e) => {
      e.stopPropagation(); await api("/watchlist?symbol=" + encodeURIComponent(symbol), { method: "DELETE" }); render();
    } }, "✕");
  }
  const asOf = rows.find((r) => r.as_of);
  return h("section", { class: "card" },
    h("div", { class: "row between" }, h("h2", {}, "Watchlist"), add), err,
    table(["Symbol", "Last", "Day", "60 days", "Signal", "Score", "P(up, 5d)", "News", ""], body, { empty: "Add a ticker to get started." }),
    h("p", { class: "muted small" }, "Score blends trend, momentum, the ML forecast and news sentiment on a −1 to +1 scale. ",
      asOf ? `Latest bar: ${asOf.as_of}. ` : "", "Click a row for the full breakdown."));
}

// ---------------------------------------------------------------- research
async function researchView() {
  const sym = state.symbol;
  const hist = await api(`/history?symbol=${encodeURIComponent(sym)}&years=${state.years}`);
  const q = hist.quote, st = hist.stats;

  const range = h("div", { class: "seg" }, [["3M", 0.25], ["1Y", 1], ["2Y", 2], ["5Y", 5]].map(([label, y]) =>
    h("button", { class: state.years === y ? "active" : null, onclick: () => { state.years = y; render(); } }, label)));
  const legend = h("div", { class: "legend" });
  const chartEl = h("div", { class: "chart" });
  const chartCard = h("section", { class: "card" },
    h("div", { class: "row between" },
      h("div", {}, h("h1", {}, q.symbol, " ", h("span", { class: "muted", style: "font-size:14px;font-weight:400" }, q.name)),
        h("div", { class: "row" }, h("span", { class: "num", style: "font-size:20px;font-weight:600" }, fmt.money(q.price, q.currency)),
          signed(q.change, `${fmt.snum(q.change)} (${fmt.spct(q.change_pct, 2)})`), h("span", { class: "muted small" }, `as of ${q.as_of}`))),
      h("div", { class: "row" }, range,
        h("button", { onclick: () => go("options", sym) }, "Options"),
        h("button", { onclick: () => go("backtest", sym) }, "Backtest"),
        h("button", { class: "primary", onclick: () => go("paper", sym) }, "Trade"))),
    legend, chartEl,
    h("div", { class: "tiles", style: "margin-top:12px" },
      tile("Period return", signed(st.period_return, fmt.spct(st.period_return))),
      tile("Volatility (annual)", fmt.pct(st.volatility)),
      tile("Sharpe ratio", fmt.num(st.sharpe)),
      tile("Max drawdown", fmt.pct(st.max_drawdown)),
      tile("Beta", fmt.num(st.beta), `vs ${st.benchmark}, 2 years`),
      tile("20-day realised vol", fmt.pct(st.realized_vol_20))));

  onMount(() => {
    const chart = makeChart(chartEl);
    const up = cssVar("--up"), down = cssVar("--down");
    const candles = chart.addCandlestickSeries({ upColor: up, downColor: down, wickUpColor: up, wickDownColor: down, borderVisible: false });
    candles.setData(hist.candles);
    const defs = [["EMA 20", hist.ema20, "--series-1"], ["EMA 50", hist.ema50, "--series-2"], ["SMA 200", hist.sma200, "--series-3"]];
    const series = [{ series: candles, label: "", ohlc: true, last: hist.candles.at(-1) }];
    for (const [label, data, v] of defs) {
      const s = chart.addLineSeries(lineOpts(cssVar(v)));
      s.setData(data);
      series.push({ series: s, label, color: cssVar(v), last: data.at(-1) });
    }
    chart.timeScale().fitContent();
    liveLegend(chart, legend, series);
  });

  const signalCard = h("section", { class: "card" }, h("h2", {}, "Signal"), loading("Computing signal…"));
  const newsCard = h("section", { class: "card" }, h("h2", {}, "News, last 7 days"), loading());

  api("/signal?symbol=" + encodeURIComponent(sym)).then((s) => {
    const fc = s.forecast, p = s.plan, long = s.score >= 0;
    fill(signalCard, 
      h("div", { class: "row between" }, h("h2", {}, "Signal"), h("span", { class: "muted small" }, `bar of ${s.as_of}`)),
      h("div", { class: "row", style: "margin:10px 0" }, chip(s.label), h("span", { class: "num", style: "font-size:20px;font-weight:600" }, fmt.snum(s.score)),
        h("span", { class: "muted small" }, "scale −1 to +1")),
      Object.entries(s.components).map(([name, c]) => h("div", { class: "comp" },
        h("span", {}, { trend: "Trend", momentum: "Momentum", ml: "ML forecast", news: "News" }[name], h("span", { class: "muted small" }, ` ${fmt.pct(c.weight, 0)}`)),
        scoreBar(c.score), h("span", { class: "num", style: "text-align:right" }, fmt.snum(c.score)),
        h("div", { class: "note" }, c.note))),
      h("h3", { style: "margin-top:16px" }, `Trade plan if you go ${long ? "long" : "short"}`),
      h("div", { class: "tiles", style: "margin-top:8px" },
        tile("Stop", fmt.num(p.stop), "2 × ATR away"), tile("Target", fmt.num(p.target), "3 × ATR away"),
        tile("Size", fmt.int(p.shares) + " sh", `risks ${fmt.int(p.risk_amount)} ${s.currency} (1%)`)),
      fc && fc.available ? [
        h("h3", { style: "margin-top:16px" }, `ML forecast, ${fc.horizon}-day direction`),
        h("div", { class: "tiles", style: "margin-top:8px" },
          tile("P(higher close)", fmt.pct(fc.prob_up, 0)),
          tile("Tested accuracy", fmt.pct(fc.validation.accuracy, 0), `always-up scores ${fmt.pct(fc.validation.always_up_accuracy, 0)}`),
          tile("AUC", fmt.num(fc.validation.auc), "0.50 = coin flip")),
        h("p", { class: "muted small" }, `Tested on ${fmt.int(fc.validation.samples)} unseen days (${fc.validation.from} to ${fc.validation.to}). `,
          fc.skill > 0 ? "The model showed some edge in testing, so it counts toward the score in proportion." : "The model showed no edge in testing for this stock, so it is excluded from the score."),
      ] : h("p", { class: "muted small" }, fc ? fc.reason : ""));
  }).catch((e) => fill(signalCard, h("h2", {}, "Signal"), errorBox(e)));

  api("/news?symbol=" + encodeURIComponent(sym)).then((n) => {
    const s = n.summary;
    fill(newsCard, 
      h("div", { class: "row between" }, h("h2", {}, "News, last 7 days"),
        h("span", { class: "small muted" }, `${s.count} headlines · ${s.positive} positive · ${s.negative} negative · sentiment ${fmt.snum(s.score)}`)),
      n.items.length ? h("ul", { class: "news scroll", style: "margin-top:12px" }, n.items.map((it) => {
        const cls = it.sentiment > 0.15 ? "pos" : it.sentiment < -0.15 ? "neg" : "";
        const word = cls === "pos" ? "Positive" : cls === "neg" ? "Negative" : "Neutral";
        const safe = /^https?:\/\//.test(it.link);
        return h("li", {}, h("span", { class: "dot " + cls, title: word }),
          h("div", {}, safe ? h("a", { href: it.link, target: "_blank", rel: "noopener noreferrer" }, it.title) : it.title,
            h("div", { class: "muted small" }, `${it.source} · ${new Date(it.published).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })} · ${word} (${fmt.snum(it.sentiment)})`)));
      })) : h("p", { class: "muted" }, "No recent headlines found."));
  }).catch((e) => fill(newsCard, h("h2", {}, "News"), errorBox(e)));

  return [chartCard, h("div", { class: "grid-even" }, signalCard, newsCard)];
}

// ---------------------------------------------------------------- options
function smileChart(calls, puts, spot) {
  const pts = (rows) => rows.filter((r) => r.iv && r.quoted).map((r) => ({ x: r.strike, y: r.iv }));
  const sets = [{ label: "Calls", color: cssVar("--series-1"), data: pts(calls) }, { label: "Puts", color: cssVar("--series-2"), data: pts(puts) }];
  const all = sets.flatMap((s) => s.data);
  if (all.length < 3) return h("p", { class: "notice" }, "Not enough live two-sided quotes to draw the volatility smile (the market may be closed).");
  const W = 640, H = 260, m = { l: 44, r: 12, t: 10, b: 28 };
  const x0 = Math.min(...all.map((p) => p.x)), x1 = Math.max(...all.map((p) => p.x));
  const y1 = Math.min(Math.max(...all.map((p) => p.y)) * 1.08, 3), y0 = 0;
  const X = (v) => m.l + (v - x0) / (x1 - x0 || 1) * (W - m.l - m.r), Y = (v) => H - m.b - (Math.min(v, y1) - y0) / (y1 - y0) * (H - m.t - m.b);
  const grid = cssVar("--border"), text = cssVar("--text-2");
  let svg = "";
  for (let i = 0; i <= 4; i++) {
    const v = y0 + (y1 - y0) * i / 4;
    svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="${grid}"/><text x="${m.l - 6}" y="${Y(v) + 4}" text-anchor="end" font-size="11" fill="${text}">${(v * 100).toFixed(0)}%</text>`;
  }
  for (let i = 0; i <= 5; i++) {
    const v = x0 + (x1 - x0) * i / 5;
    svg += `<text x="${X(v)}" y="${H - 8}" text-anchor="middle" font-size="11" fill="${text}">${v.toFixed(0)}</text>`;
  }
  if (spot >= x0 && spot <= x1) svg += `<line x1="${X(spot)}" x2="${X(spot)}" y1="${m.t}" y2="${H - m.b}" stroke="${cssVar("--muted")}" stroke-dasharray="4 4"/><text x="${X(spot) + 5}" y="${m.t + 10}" font-size="11" fill="${text}">Spot ${spot.toFixed(2)}</text>`;
  for (const s of sets) {
    svg += `<polyline fill="none" stroke="${s.color}" stroke-width="2" points="${s.data.map((p) => `${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join(" ")}"/>`;
  }
  svg += `<line id="hair" y1="${m.t}" y2="${H - m.b}" stroke="${cssVar("--muted")}" visibility="hidden"/>`;
  const el = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  el.setAttribute("viewBox", `0 0 ${W} ${H}`); el.setAttribute("role", "img"); el.setAttribute("aria-label", "Implied volatility by strike");
  el.style.width = "100%"; el.innerHTML = svg;
  const strikes = [...new Set(all.map((p) => p.x))].sort((a, b) => a - b), tip = $("#tooltip"), hair = el.querySelector("#hair");
  el.addEventListener("mousemove", (e) => {
    const box = el.getBoundingClientRect(), vx = (e.clientX - box.left) / box.width * W;
    const k = strikes.reduce((a, b) => Math.abs(X(b) - vx) < Math.abs(X(a) - vx) ? b : a);
    hair.setAttribute("x1", X(k)); hair.setAttribute("x2", X(k)); hair.setAttribute("visibility", "visible");
    fill(tip, h("strong", {}, `Strike ${fmt.num(k)}`), ...sets.map((s) => {
      const p = s.data.find((d) => d.x === k);
      return h("div", { class: "legend", style: "margin:2px 0 0" }, swatch(s.color, `${s.label} IV ${p ? fmt.pct(p.y) : dash}`));
    }));
    tip.hidden = false; tip.style.left = Math.min(e.clientX + 14, innerWidth - 170) + "px"; tip.style.top = e.clientY + 14 + "px";
  });
  el.addEventListener("mouseleave", () => { tip.hidden = true; hair.setAttribute("visibility", "hidden"); });
  return h("div", {}, h("div", { class: "legend" }, sets.map((s) => swatch(s.color, s.label))), el);
}

function calculator(spot, vol, rate) {
  const out = h("div", { class: "tiles", style: "margin-top:12px" });
  const err = h("p", { class: "error", role: "alert" });
  const f = (name, label, value, step = "any") => h("label", {}, label, h("input", { name, type: "number", step, value, required: true }));
  const form = h("form", { class: "fields", onsubmit: async (e) => {
    e.preventDefault(); err.textContent = "";
    const d = Object.fromEntries(new FormData(form));
    try {
      const r = await api("/options/price", { body: { kind: d.kind, spot: +d.spot, strike: +d.strike, days: +d.days, vol: d.vol / 100, rate: d.rate / 100, dividend_yield: d.div / 100 } });
      fill(out, tile("Fair value", fmt.num(r.price), "per share; ×100 per contract"), tile("Breakeven", fmt.num(r.breakeven)),
        tile("Delta", fmt.num(r.delta, 3)), tile("Gamma", fmt.num(r.gamma, 4)), tile("Theta / day", fmt.num(r.theta, 3)), tile("Vega / vol pt", fmt.num(r.vega, 3)));
    } catch (ex) { err.textContent = ex.message; }
  } },
    h("label", {}, "Type", h("select", { name: "kind" }, h("option", { value: "call" }, "Call"), h("option", { value: "put" }, "Put"))),
    f("spot", "Share price", spot.toFixed(2)), f("strike", "Strike", Math.round(spot)), f("days", "Days to expiry", 30),
    f("vol", "Volatility %", (vol * 100).toFixed(1)), f("rate", "Interest rate %", (rate * 100).toFixed(1)), f("div", "Dividend yield %", 0),
    h("button", { type: "submit", class: "primary" }, "Price"));
  onMount(() => form.requestSubmit());
  return h("section", { class: "card" }, h("h2", {}, "Option calculator"),
    h("p", { class: "muted small", style: "margin-top:-6px" }, "Black-Scholes value and Greeks for any contract. Volatility defaults to the stock's recent realised volatility."),
    form, err, out);
}

async function optionsView() {
  const sym = state.symbol;
  const [exp, hist] = await Promise.all([api("/options/expiries?symbol=" + encodeURIComponent(sym)), api(`/history?symbol=${encodeURIComponent(sym)}&years=0.25`)]);
  const q = hist.quote;
  const head = h("div", { class: "row between" },
    h("h1", {}, `${q.symbol} options `, h("span", { class: "muted", style: "font-size:14px;font-weight:400" }, `${q.name} · ${fmt.money(q.price, q.currency)}`)),
    h("button", { onclick: () => go("research", sym) }, "Back to research"));
  const calc = calculator(q.price, hist.stats.realized_vol_20 || 0.25, 0.04);
  if (!exp.expiries.length) {
    return [h("section", { class: "card" }, head, h("p", { class: "notice", style: "margin-top:12px" },
      `No option chain is available for ${q.symbol} from the free data source. ASX option chains are not published there; you can still price contracts with the calculator below.`)), calc];
  }
  const body = h("div", { class: "stack" }, loading());
  const select = h("select", { "aria-label": "Expiry", onchange: () => load(select.value) }, exp.expiries.map((e) => h("option", { value: e }, e)));
  // Default to the first expiry at least a week out: same-week chains are mostly intrinsic value.
  const soon = exp.expiries.find((e) => (new Date(e) - Date.now()) / 864e5 >= 7);
  if (soon) select.value = soon;
  async function load(expiry) {
    fill(body, loading());
    try {
      const c = await api(`/options/chain?symbol=${encodeURIComponent(sym)}&expiry=${expiry}`), s = c.summary;
      const puts = new Map(c.puts.map((p) => [p.strike, p])), calls = new Map(c.calls.map((p) => [p.strike, p]));
      const strikes = [...new Set([...calls.keys(), ...puts.keys()])].sort((a, b) => a - b);
      const cells = (o, itm, mirror) => {
        const xs = o ? [fmt.int(o.open_interest), fmt.int(o.volume), fmt.pct(o.iv), fmt.num(o.delta, 2), fmt.num(o.bid), fmt.num(o.ask)] : Array(6).fill(dash);
        return (mirror ? xs.reverse() : xs).map((x) => h("td", { class: "num" + (itm ? " itm" : "") }, x));
      };
      const rows = strikes.map((k) => h("tr", {}, cells(calls.get(k), k < s.spot, false), h("td", { class: "strike num" }, fmt.num(k)), cells(puts.get(k), k > s.spot, true)));
      const cols = ["OI", "Vol", "IV", "Delta", "Bid", "Ask"];
      const all = [...c.calls, ...c.puts], live = all.filter((o) => o.quoted).length;
      fill(body,
        live < all.length * 0.3 ? h("p", { class: "notice" }, "Yahoo is not returning live bid/ask quotes for this chain right now, which is normal outside US market hours. Implied volatility and Greeks below are estimated from each contract's last trade and can be stale; check again during the session.") : null,
        h("div", { class: "tiles" },
          tile("Days to expiry", fmt.num(s.days_to_expiry, 0)),
          tile("At-the-money IV", fmt.pct(s.atm_iv), `strike ${fmt.num(s.atm_strike)}`),
          tile("Expected move", "±" + fmt.num(s.expected_move), `${fmt.pct(s.expected_move_pct)} by expiry`),
          tile("Put/call open interest", fmt.num(s.put_call_oi), s.put_call_oi == null ? "" : s.put_call_oi > 1 ? "more puts outstanding" : "more calls outstanding"),
          tile("Max pain", fmt.num(s.max_pain)),
          tile("Skew", s.skew == null ? dash : fmt.snum(s.skew * 100, 1) + " pts", "5% OTM put IV − call IV")),
        h("section", { class: "card" }, h("h2", {}, "Implied volatility by strike"), smileChart(c.calls, c.puts, s.spot)),
        h("section", { class: "card" }, h("div", { class: "row between" }, h("h2", {}, "Chain"), h("span", { class: "muted small" }, "Calls left, puts right · shaded = in the money")),
          table([...cols, { t: "Strike" }, ...cols.slice().reverse()], rows, { scroll: true })));
    } catch (e) { fill(body, errorBox(e)); }
  }
  load(select.value);
  return [h("section", { class: "card" }, head, h("div", { class: "row", style: "margin-top:12px" }, h("label", { class: "row" }, "Expiry", select))), body, calc];
}

// ---------------------------------------------------------------- backtest
async function backtestView() {
  const strategies = await api("/strategies");
  const results = h("div", { class: "stack" });
  const desc = h("p", { class: "muted small" });
  const paramBox = h("div", { style: "display:contents" });
  const err = h("p", { class: "error", role: "alert" });
  const stratSel = h("select", { name: "strategy", onchange: drawParams }, strategies.map((s) => h("option", { value: s.id }, s.label)));
  function drawParams() {
    const s = strategies.find((x) => x.id === stratSel.value);
    desc.textContent = s.description;
    paramBox.replaceChildren(...Object.entries(s.params).map(([k, v]) => h("label", {}, k.replace(/_/g, " "), h("input", { name: "p_" + k, type: "number", step: "any", value: v, required: true }))));
  }
  drawParams();
  const btn = h("button", { type: "submit", class: "primary" }, "Run backtest");
  const form = h("form", { class: "fields", onsubmit: async (e) => {
    e.preventDefault(); err.textContent = ""; btn.disabled = true;
    const d = Object.fromEntries(new FormData(form)), params = {};
    for (const [k, v] of Object.entries(d)) if (k.startsWith("p_")) params[k.slice(2)] = +v;
    try {
      const r = await api("/backtest", { body: { symbol: d.symbol, strategy: d.strategy, params, years: +d.years, cost_bps: +d.cost, initial: +d.initial } });
      state.symbol = d.symbol.toUpperCase(); show(r, strategies.find((x) => x.id === d.strategy).label, state.symbol);
    } catch (ex) { err.textContent = ex.message; } finally { btn.disabled = false; }
  } },
    h("label", {}, "Ticker", h("input", { name: "symbol", value: state.symbol, required: true })),
    h("label", {}, "Strategy", stratSel), paramBox,
    h("label", {}, "Years of history", h("input", { name: "years", type: "number", min: 1, max: 10, step: 1, value: 5 })),
    h("label", {}, "Cost per trade (bps)", h("input", { name: "cost", type: "number", min: 0, step: "any", value: 5 })),
    h("label", {}, "Starting capital", h("input", { name: "initial", type: "number", min: 1, step: "any", value: 10000 })), btn);

  function show(r, label, sym) {
    charts.forEach((c) => c.remove()); charts = [];
    const a = r.stats, b = r.benchmark, legend = h("div", { class: "legend" }), el = h("div", { class: "chart short" });
    const cmp = (name, fn, key, better) => {
      const win = a[key] != null && b[key] != null && (better === "high" ? a[key] > b[key] : a[key] < b[key]);
      return h("tr", {}, h("td", {}, name), h("td", { class: "num" }, h(win ? "strong" : "span", {}, fn(a[key]))), h("td", { class: "num" }, fn(b[key])));
    };
    fill(results, 
      h("section", { class: "card" }, h("div", { class: "row between" }, h("h2", {}, `${label} on ${sym}`), h("span", { class: "muted small" }, `${r.from} to ${r.to}`)), legend, el),
      h("div", { class: "grid-even" },
        h("section", { class: "card" }, h("h2", {}, "Strategy vs buy and hold"),
          table(["", "Strategy", "Buy & hold"], [
            cmp("Total return", fmt.spct, "total_return", "high"), cmp("Annual return", fmt.spct, "cagr", "high"),
            cmp("Volatility", fmt.pct, "volatility", "low"), cmp("Sharpe ratio", fmt.num, "sharpe", "high"),
            cmp("Sortino ratio", fmt.num, "sortino", "high"), cmp("Max drawdown", fmt.pct, "max_drawdown", "high"),
            cmp("1-day 95% VaR", fmt.pct, "var_95", "low")]),
          h("p", { class: "muted small" }, `${a.trades} trades · win rate ${fmt.pct(a.win_rate, 0)} · average trade ${fmt.spct(a.avg_trade)} · invested ${fmt.pct(a.exposure, 0)} of the time. Bold marks the better figure. Past results do not predict future returns.`)),
        h("section", { class: "card" }, h("h2", {}, "Trades"),
          table([{ t: "Entry", l: 1 }, "Price", "Exit", "Price", "Return"], r.trades.slice().reverse().map((t) => h("tr", {},
            h("td", {}, t.entry_date), h("td", { class: "num" }, fmt.num(t.entry_price)), h("td", {}, t.exit_date || "open"),
            h("td", { class: "num" }, fmt.num(t.exit_price)), h("td", {}, signed(t.return, fmt.spct(t.return))))), { scroll: true, empty: "The strategy never traded in this period." }))));
    const chart = makeChart(el), c1 = cssVar("--series-1"), c2 = cssVar("--series-2");
    const s1 = chart.addLineSeries(lineOpts(c1)), s2 = chart.addLineSeries(lineOpts(c2));
    s2.setData(r.benchmark_equity); s1.setData(r.equity); chart.timeScale().fitContent();
    liveLegend(chart, legend, [{ series: s1, label: "Strategy", color: c1, last: r.equity.at(-1) }, { series: s2, label: "Buy & hold", color: c2, last: r.benchmark_equity.at(-1) }]);
  }
  onMount(() => form.requestSubmit());
  return [h("section", { class: "card" }, h("h2", {}, "Backtest a strategy"), form, desc, err,
    h("p", { class: "muted small" }, "Trades are decided at each day's close and take effect the next day, so results never use information from the future.")), results];
}

// ---------------------------------------------------------------- paper trading
async function paperView() {
  const [acct, trades, perf] = await Promise.all([api("/paper/account"), api("/paper/trades"), api("/signals/performance")]);
  const err = h("p", { class: "error", role: "alert" }), ok = h("p", { class: "small up", role: "status" });
  async function order(symbol, side, qty) {
    err.textContent = ""; ok.textContent = "";
    try {
      const r = await api("/paper/orders", { body: { symbol, side, qty: +qty } });
      sessionStorage.setItem("ql_fill", `Filled: ${r.side} ${r.qty} ${r.symbol} at ${fmt.money(r.price, r.currency)}`);
      render();
    } catch (ex) { err.textContent = ex.message; }
  }
  ok.textContent = sessionStorage.getItem("ql_fill") || ""; sessionStorage.removeItem("ql_fill");
  const symIn = h("input", { name: "symbol", value: state.symbol, required: true });
  const qtyIn = h("input", { name: "qty", type: "number", min: 1, step: 1, value: 10, required: true });
  const ticket = h("form", { class: "fields", onsubmit: (e) => { e.preventDefault(); order(symIn.value, e.submitter.value, qtyIn.value); } },
    h("label", {}, "Ticker", symIn), h("label", {}, "Shares", qtyIn),
    h("button", { type: "submit", value: "buy", class: "primary" }, "Buy"), h("button", { type: "submit", value: "sell" }, "Sell"));

  const books = acct.books.map((b) => h("section", { class: "card" }, h("h2", {}, `${b.currency} account`),
    h("div", { class: "tiles" }, tile("Equity", fmt.num(b.equity), h("span", {}, signed(b.pnl, `${fmt.snum(b.pnl)} (${fmt.spct(b.pnl_pct, 2)})`))),
      tile("Cash", fmt.num(b.cash)), tile("Invested", fmt.num(b.invested)), tile("Portfolio beta", fmt.num(b.beta), "cash counts as 0"))));

  const positions = acct.positions.map((p) => h("tr", {},
    h("td", {}, h("button", { class: "link", onclick: () => go("research", p.symbol) }, p.symbol)),
    h("td", { class: "num" }, fmt.int(p.qty)), h("td", { class: "num" }, fmt.num(p.avg_cost)), h("td", { class: "num" }, fmt.num(p.price)),
    h("td", { class: "num" }, fmt.money(p.value, p.currency)), h("td", {}, signed(p.pnl, `${fmt.snum(p.pnl)} (${fmt.spct(p.pnl_pct, 2)})`)),
    h("td", { class: "num" }, fmt.num(p.beta)),
    h("td", {}, h("button", { onclick: () => order(p.symbol, "sell", p.qty) }, "Close"))));

  return [
    h("div", { class: "grid-even" }, books),
    h("section", { class: "card" }, h("h2", {}, "Order ticket"), ticket, err, ok,
      h("p", { class: "muted small" }, "Simulated market orders fill at the latest delayed price with 5 bps slippage and 5 bps commission. Long stock positions only.")),
    h("section", { class: "card" }, h("h2", {}, "Positions"),
      table(["Symbol", "Shares", "Avg cost", "Last", "Value", "Unrealised P&L", "Beta", ""], positions, { empty: "No open positions." })),
    h("div", { class: "grid-even" },
      h("section", { class: "card" }, h("h2", {}, "Trade history"),
        table([{ t: "Time", l: 1 }, { t: "Symbol", l: 1 }, { t: "Side", l: 1 }, "Shares", "Price", "Realised P&L"], trades.map((t) => h("tr", {},
          h("td", {}, new Date(t.ts).toLocaleString([], { dateStyle: "short", timeStyle: "short" })), h("td", { class: "l" }, t.symbol), h("td", { class: "l" }, t.side),
          h("td", { class: "num" }, fmt.int(t.qty)), h("td", { class: "num" }, fmt.money(t.price, t.currency)),
          h("td", {}, t.realized == null ? dash : signed(t.realized, fmt.snum(t.realized))))), { scroll: true, empty: "No trades yet." }),
        h("div", { class: "row", style: "margin-top:12px" }, h("button", { onclick: async () => {
          if (confirm("Reset the paper account? This deletes all simulated positions and trade history.")) { await api("/paper/reset", { method: "POST" }); render(); }
        } }, "Reset paper account"))),
      h("section", { class: "card" }, h("h2", {}, "Signal track record"),
        table([{ t: "Signal", l: 1 }, "Count", `Avg ${perf.horizon}-day return`, "Went up"], perf.by_label.map((r) => h("tr", {},
          h("td", {}, chip(r.label)), h("td", { class: "num" }, r.count), h("td", {}, signed(r.avg_return, fmt.spct(r.avg_return, 2))), h("td", { class: "num" }, fmt.pct(r.pct_up, 0)))),
          { empty: "No signals are old enough to score yet." }),
        h("p", { class: "muted small" }, `Each day's signal is logged, then scored on what the price did over the next ${perf.horizon} trading days. ${perf.logged} logged, ${perf.pending} still waiting for enough days to pass.`))),
  ];
}

const VIEWS = { dashboard: dashboardView, research: researchView, options: optionsView, backtest: backtestView, paper: paperView };

// ---------------------------------------------------------------- startup
function showLogin() { $("#login").hidden = false; $("#login-password").focus(); }
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault(); $("#login-error").textContent = "";
  try { await api("/login", { body: { password: $("#login-password").value } }); $("#login").hidden = true; $("#login-password").value = ""; $("#logout").hidden = false; render(); }
  catch (ex) { $("#login-error").textContent = ex.message; }
});
$("#logout").addEventListener("click", async () => { await api("/logout", { method: "POST" }); showLogin(); });
$("#tabs").addEventListener("click", (e) => { if (e.target.dataset.view) go(e.target.dataset.view); });
$("#search").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = $("#search-input").value.trim();
  if (v) { $("#search-input").value = ""; go(state.view === "dashboard" ? "research" : state.view, v); }
});
window.addEventListener("hashchange", render);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);

(async function start() {
  const s = await api("/session");
  $("#logout").hidden = !s.login_required;
  if (!s.logged_in) return showLogin();
  if (!location.hash) location.hash = "dashboard"; else render();
})();
