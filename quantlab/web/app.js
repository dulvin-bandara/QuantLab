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
  const res = await fetch("api" + path, init)  // relative, so the app also works under a sub-path like /quantlab/;
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
  shares: (v) => v == null ? dash : Number(v).toLocaleString(undefined, { maximumFractionDigits: 4 }),  // whole or fractional
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
  return [alertsCard(), h("section", { class: "card" },
    h("div", { class: "row between" }, h("h2", {}, "Watchlist"), add), err,
    table(["Symbol", "Last", "Day", "60 days", "Signal", "Score", "P(up, 5d)", "News", ""], body, { empty: "Add a ticker to get started." }),
    h("p", { class: "muted small" }, "Score blends trend, momentum, the ML forecast and news sentiment on a −1 to +1 scale. ",
      asOf ? `Latest bar: ${asOf.as_of}. ` : "", "Click a row for the full breakdown."))];
}

// ---------------------------------------------------------------- alerts and the daily run
function alertsCard() {
  const box = h("section", { class: "card" }, h("h2", {}, "Alerts"), loading());
  async function load() {
    const [a, d] = await Promise.all([api("/alerts"), api("/daily")]);
    const last = d.last && d.last.finished ? new Date(d.last.finished).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : null;
    const push = [a.channels.ntfy ? "phone push" : null, a.channels.email ? "email" : null].filter(Boolean);
    const runBtn = h("button", { onclick: async () => { runBtn.disabled = true; runBtn.textContent = "Running… (a few minutes)"; await api("/daily/run", { method: "POST" }); setTimeout(wait, 5000); } }, "Run daily update now");
    async function wait() { const now = await api("/daily"); if (now.last && (!d.last || now.last.started !== d.last.started) && now.last.finished) render(); else setTimeout(wait, 5000); }
    fill(box,
      h("div", { class: "row between" }, h("h2", {}, "Alerts", a.unread ? h("span", { class: "chip", style: "margin-left:8px" }, `${a.unread} new`) : null),
        h("div", { class: "row" }, runBtn,
          h("button", { onclick: async () => { await api("/alerts/test", { method: "POST" }); load(); } }, "Send a test alert"),
          a.unread ? h("button", { onclick: async () => { await api("/alerts/read", { method: "POST" }); load(); } }, "Mark all read") : null)),
      a.alerts.length ? h("ul", { class: "news scroll", style: "margin-top:12px;max-height:220px" }, a.alerts.map((x) => h("li", {},
        h("span", { class: "dot" + (x.read ? "" : " pos"), title: x.read ? "Read" : "New" }),
        h("div", {}, x.read ? x.message : h("strong", {}, x.message), h("div", { class: "muted small" }, `${new Date(x.ts).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })} · ${x.kind}`)))))
        : h("p", { class: "muted", style: "margin-top:10px" }, "No alerts yet. You are told when a watchlist signal changes, a paper order fills, a new idea appears, or a stock you follow is about to report earnings."),
      h("p", { class: "muted small" }, last ? `Daily update last ran ${last}: ${d.last.signals} signals refreshed, ${(d.last.changes || []).length} changed, ${d.last.ideas ?? 0} ideas.` : "The daily update has not run yet.",
        d.time.toLowerCase() === "off" ? " Automatic runs are switched off." : ` It runs automatically at ${d.time} while the app is open.`,
        push.length ? ` Alerts are also sent by ${push.join(" and ")}.` : " Alerts only show here until phone push or email is set up (see the README)."));
  }
  load().catch((e) => fill(box, h("h2", {}, "Alerts"), errorBox(e)));
  return box;
}

// ---------------------------------------------------------------- fundamentals
function fundamentalsCard(symbol) {
  const box = h("section", { class: "card" }, h("h2", {}, "Company facts"), loading());
  api("/fundamentals?symbol=" + encodeURIComponent(symbol)).then((f) => {
    const big = (v) => v == null ? dash : v >= 1e12 ? fmt.num(v / 1e12, 2) + " tn" : v >= 1e9 ? fmt.num(v / 1e9, 1) + " bn" : fmt.num(v / 1e6, 0) + " m";
    const soon = f.days_to_earnings != null && f.days_to_earnings <= 14;
    if (!f.sector && f.pe == null && f.market_cap == null) { fill(box, h("h2", {}, "Company facts"), h("p", { class: "muted" }, "No company data is available for this ticker.")); return; }
    fill(box,
      h("div", { class: "row between" }, h("h2", {}, "Company facts"), h("span", { class: "muted small" }, [f.sector, f.industry].filter(Boolean).join(" · "))),
      soon ? h("p", { class: "notice", style: "margin-top:10px" }, `Earnings are due on ${f.next_earnings}, ${f.days_to_earnings} day${f.days_to_earnings === 1 ? "" : "s"} away. Results can move the price sharply in either direction, whatever the trend says.`) : null,
      h("div", { class: "tiles", style: "margin-top:12px" },
        tile("Market value", big(f.market_cap)),
        tile("Price / earnings", f.pe == null ? dash : fmt.num(f.pe, 1), f.forward_pe == null ? "" : `${fmt.num(f.forward_pe, 1)} on next year's forecast`),
        tile("Next earnings", f.next_earnings || dash, f.days_to_earnings == null ? "" : `in ${f.days_to_earnings} days`),
        tile("Profit margin", fmt.pct(f.profit_margin)),
        tile("Revenue growth", f.revenue_growth == null ? dash : fmt.spct(f.revenue_growth), "year on year"),
        tile("Debt to equity", f.debt_to_equity == null ? dash : fmt.num(f.debt_to_equity, 2)),
        tile("Dividend yield", fmt.pct(f.dividend_yield, 2)),
        tile("52-week range", f.low_52w == null ? dash : `${fmt.num(f.low_52w, 0)} to ${fmt.num(f.high_52w, 0)}`),
        tile("Analyst target", f.analyst_target == null ? dash : fmt.num(f.analyst_target), f.analyst_rating ? `consensus: ${String(f.analyst_rating).replace(/_/g, " ")}` : "")),
      h("p", { class: "muted small" }, "From Yahoo Finance. These do not feed the signal; they are context for your own judgement."));
  }).catch((e) => fill(box, h("h2", {}, "Company facts"), errorBox(e)));
  return box;
}

// ---------------------------------------------------------------- option strategies
/** Profit or loss at expiry across share prices: one line, a zero line, the current price, hover read-out. */
function payoffChart(points, spot, breakevens) {
  const W = 640, H = 240, m = { l: 56, r: 12, t: 12, b: 28 };
  const xs = points.map((p) => p.price), ys = points.map((p) => p.pnl);
  const x0 = xs[0], x1 = xs.at(-1), lo = Math.min(...ys, 0), hi = Math.max(...ys, 0), pad = (hi - lo) * 0.08 || 1;
  const y0 = lo - pad, y1 = hi + pad;
  const X = (v) => m.l + (v - x0) / (x1 - x0) * (W - m.l - m.r), Y = (v) => H - m.b - (v - y0) / (y1 - y0) * (H - m.t - m.b);
  const grid = cssVar("--border"), text = cssVar("--text-2"), muted = cssVar("--muted");
  let svg = "";
  for (let i = 0; i <= 4; i++) { const v = y0 + (y1 - y0) * i / 4; svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="${grid}"/><text x="${m.l - 6}" y="${Y(v) + 4}" text-anchor="end" font-size="11" fill="${text}">${Math.round(v).toLocaleString()}</text>`; }
  for (let i = 0; i <= 5; i++) { const v = x0 + (x1 - x0) * i / 5; svg += `<text x="${X(v)}" y="${H - 8}" text-anchor="middle" font-size="11" fill="${text}">${v.toFixed(0)}</text>`; }
  svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(0)}" y2="${Y(0)}" stroke="${muted}"/>`;
  svg += `<line x1="${X(spot)}" x2="${X(spot)}" y1="${m.t}" y2="${H - m.b}" stroke="${muted}" stroke-dasharray="4 4"/><text x="${X(spot) + 5}" y="${m.t + 10}" font-size="11" fill="${text}">Now ${spot.toFixed(2)}</text>`;
  for (const be of breakevens.filter((b) => b >= x0 && b <= x1)) svg += `<circle cx="${X(be)}" cy="${Y(0)}" r="4" fill="${cssVar("--surface")}" stroke="${text}" stroke-width="2"/><text x="${X(be)}" y="${Y(0) - 8}" text-anchor="middle" font-size="11" fill="${text}">breakeven ${be.toFixed(2)}</text>`;
  svg += `<polyline fill="none" stroke="${cssVar("--series-1")}" stroke-width="2" points="${points.map((p) => `${X(p.price).toFixed(1)},${Y(p.pnl).toFixed(1)}`).join(" ")}"/><line id="hair" y1="${m.t}" y2="${H - m.b}" stroke="${muted}" visibility="hidden"/>`;
  const el = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  el.setAttribute("viewBox", `0 0 ${W} ${H}`); el.setAttribute("role", "img"); el.setAttribute("aria-label", "Profit or loss at expiry by share price");
  el.style.width = "100%"; el.innerHTML = svg;
  const tip = $("#tooltip"), hair = el.querySelector("#hair");
  el.addEventListener("mousemove", (e) => {
    const box = el.getBoundingClientRect(), vx = (e.clientX - box.left) / box.width * W;
    const p = points.reduce((a, b) => Math.abs(X(b.price) - vx) < Math.abs(X(a.price) - vx) ? b : a);
    hair.setAttribute("x1", X(p.price)); hair.setAttribute("x2", X(p.price)); hair.setAttribute("visibility", "visible");
    fill(tip, h("strong", {}, `Share price ${fmt.num(p.price)}`), h("div", {}, `${p.pnl >= 0 ? "Profit" : "Loss"} at expiry: ${fmt.snum(p.pnl, 0)}`));
    tip.hidden = false; tip.style.left = Math.min(e.clientX + 14, innerWidth - 190) + "px"; tip.style.top = e.clientY + 14 + "px";
  });
  el.addEventListener("mouseleave", () => { tip.hidden = true; hair.setAttribute("visibility", "hidden"); });
  return el;
}

function strategiesCard(symbol) {
  const box = h("section", { class: "card" }, h("h2", {}, "Option strategies"), loading("Building strategies from the chain…"));
  api("/options/strategies?symbol=" + encodeURIComponent(symbol)).then((r) => {
    if (!r.available) { fill(box, h("h2", {}, "Option strategies"), h("p", { class: "notice", style: "margin-top:10px" }, r.reason)); return; }
    const detail = h("div", { class: "stack", style: "margin-top:12px" });
    const pick = h("div", { class: "seg" });
    function show(name) {
      const s = r.strategies.find((x) => x.name === name);
      [...pick.children].forEach((b) => b.classList.toggle("active", b.textContent.replace(" ✓", "") === name));
      fill(detail,
        h("p", { style: "margin:0" }, s.description),
        h("div", { class: "tiles" },
          tile(s.option_cost >= 0 ? "Options cost" : "Premium received", fmt.num(Math.abs(s.option_cost)), s.shares ? `plus ${s.shares} shares (${fmt.num(s.shares * r.spot, 0)})` : "paid up front"),
          tile("Most you can make", s.max_profit == null ? "Unlimited" : fmt.num(s.max_profit, 0)),
          tile("Most you can lose", fmt.num(s.max_loss, 0), s.risk.split(".")[0] + "."),
          tile("Breakeven at expiry", s.breakevens.length ? s.breakevens.map((b) => fmt.num(b)).join(" and ") : dash)),
        table([{ t: "Leg", l: 1 }, "Strike", "Price per share", "Delta"], [
          ...(s.shares ? [h("tr", {}, h("td", {}, `Own ${s.shares} shares`), h("td", {}, ""), h("td", { class: "num" }, fmt.num(r.spot)), h("td", { class: "num" }, "1.00"))] : []),
          ...s.legs.map((l) => h("tr", {}, h("td", {}, `${l.action === "buy" ? "Buy" : "Sell"} 1 ${l.kind}`), h("td", { class: "num" }, fmt.num(l.strike)), h("td", { class: "num" }, fmt.num(l.premium)), h("td", { class: "num" }, fmt.num(l.delta, 2))))]),
        payoffChart(s.payoff, r.spot, s.breakevens),
        h("ul", { class: "reasons" }, h("li", {}, s.risk), s.warnings.map((w) => h("li", {}, w))));
    }
    r.strategies.forEach((s) => pick.append(h("button", { onclick: () => show(s.name) }, s.name + (s.name === r.suggested ? " ✓" : ""))));
    fill(box, h("div", { class: "row between" }, h("h2", {}, "Option strategies"), h("span", { class: "muted small" }, `Contracts expiring ${r.expiry} (${r.days_to_expiry} days)`)),
      h("p", { style: "margin:10px 0" }, r.note, r.suggested ? " The tick marks the simplest fit." : ""), pick, detail,
      h("p", { class: "muted small" }, "Profit and loss are at expiry, per one contract (100 shares). The paper account can buy single calls and puts with the ticket above; spreads and covered calls are shown for study and for your real broker."));
    show(r.suggested || r.strategies[0].name);
  }).catch((e) => fill(box, h("h2", {}, "Option strategies"), errorBox(e)));
  return box;
}

// ---------------------------------------------------------------- my portfolio (holdings and brokers)
async function holdingsView() {
  const [hold, bk] = await Promise.all([api("/holdings"), api("/brokers")]);
  const names = bk.brokers.map((b) => b.name);
  const brokerSelect = (attrs = {}) => h("select", attrs, h("option", { value: "" }, names.length ? "Unassigned" : "No brokers added yet"), names.map((n) => h("option", { value: n }, n)));
  const err = h("p", { class: "error", role: "alert" }), ok = h("p", { class: "small up", role: "status" });
  ok.textContent = sessionStorage.getItem("ql_hold") || ""; sessionStorage.removeItem("ql_hold");
  const done = (msg) => { sessionStorage.setItem("ql_hold", msg); render(); };

  // --- holdings
  const impBroker = brokerSelect({ "aria-label": "Broker for this file" }), impMarket = h("select", { "aria-label": "Codes are from" }, h("option", { value: "US" }, "Codes are US tickers"), h("option", { value: "ASX" }, "Codes are ASX codes"));
  const file = h("input", { type: "file", accept: ".csv,text/csv", "aria-label": "CSV file of holdings", onchange: async () => {
    if (!file.files[0]) return; err.textContent = "";
    try {
      const r = await api("/holdings/import", { body: { csv: await file.files[0].text(), market: impMarket.value, broker: impBroker.value } });
      done(`Imported ${r.imported} holding${r.imported === 1 ? "" : "s"}${r.broker ? " at " + r.broker : ""}.${r.skipped.length ? " Skipped: " + r.skipped.join(" ") : ""}`);
    } catch (ex) { err.textContent = ex.message; file.value = ""; }
  } });
  const addForm = h("form", { class: "fields", onsubmit: async (e) => {
    e.preventDefault(); err.textContent = "";
    const d = Object.fromEntries(new FormData(addForm));
    try { await api("/holdings", { body: { symbol: d.symbol, qty: +d.qty, cost: d.cost ? +d.cost : null, broker: d.broker } }); done(`Saved ${d.symbol.toUpperCase()}.`); } catch (ex) { err.textContent = ex.message; }
  } },
    h("label", {}, "Ticker", h("input", { name: "symbol", required: true, placeholder: "AAPL or BHP.AX" })),
    h("label", {}, "Shares", h("input", { name: "qty", type: "number", min: 0, step: "any", required: true })),
    h("label", {}, "Average cost (optional)", h("input", { name: "cost", type: "number", min: 0, step: "any" })),
    h("label", {}, "Held at", brokerSelect({ name: "broker" })), h("button", { type: "submit" }, "Add or update"));
  const rows = hold.holdings.map((x) => h("tr", {},
    h("td", {}, h("button", { class: "link", onclick: () => go("research", x.symbol) }, x.symbol), h("div", { class: "muted small" }, x.name)),
    h("td", { class: "l" }, x.broker || "Unassigned"), h("td", { class: "num" }, fmt.num(x.qty, x.qty % 1 ? 3 : 0)), h("td", { class: "num" }, x.cost == null ? dash : fmt.num(x.cost)),
    h("td", { class: "num" }, fmt.num(x.price)), h("td", { class: "num" }, fmt.money(x.value, x.currency)), h("td", { class: "num" }, fmt.pct(x.weight)),
    h("td", {}, x.pnl == null ? dash : signed(x.pnl, `${fmt.snum(x.pnl)} (${fmt.spct(x.pnl_pct)})`)), h("td", { class: "num" }, fmt.num(x.beta)),
    h("td", { class: "l" }, x.signal ? chip(x.signal) : dash),
    h("td", {}, h("button", { class: "link", "aria-label": `Remove ${x.symbol}`, onclick: async () => { await api("/holdings", { body: { symbol: x.symbol, qty: 0, broker: x.broker } }); render(); } }, "✕"))));
  const holdingsCard = h("section", { class: "card" }, h("h2", {}, "What I own"),
    hold.books.length ? h("div", { class: "tiles", style: "margin-bottom:12px" }, hold.books.map((b) => [
      tile(`${b.currency} holdings`, fmt.num(b.value), `${b.count} position${b.count === 1 ? "" : "s"}`),
      tile(`${b.currency} profit`, b.pnl == null ? dash : signed(b.pnl, fmt.snum(b.pnl)), b.pnl == null ? "needs average costs" : "on positions with a cost"),
      tile(`${b.currency} beta`, fmt.num(b.beta), "1.00 moves with the market")])) : null,
    hold.warnings.length ? h("ul", { class: "reasons", style: "margin-bottom:12px" }, hold.warnings.map((w) => h("li", {}, w))) : null,
    table([{ t: "Holding", l: 1 }, { t: "Held at", l: 1 }, "Shares", "Avg cost", "Last", "Value", "Weight", "Profit", "Beta", { t: "Trend", l: 1 }, ""], rows,
      { empty: "Nothing recorded yet. Import a CSV export from your broker, or add holdings by hand below." }),
    hold.by_broker.length > 1 ? [h("h3", { style: "margin-top:16px" }, "Split across brokers"),
      table([{ t: "Broker", l: 1 }, "Positions", "Value", "Share of that currency"], hold.by_broker.map((b) => h("tr", {},
        h("td", {}, `${b.broker}, ${b.currency} shares`), h("td", { class: "num" }, b.positions), h("td", { class: "num" }, fmt.money(b.value, b.currency)),
        h("td", {}, h("div", { class: "row", style: "justify-content:flex-end;flex-wrap:nowrap" }, h("div", { class: "meter" }, h("i", { style: `width:${(b.share * 100).toFixed(1)}%` })), h("span", { class: "num" }, fmt.pct(b.share, 0)))))))] : null,
    h("h3", { style: "margin-top:16px" }, "Import from a broker file"),
    h("div", { class: "row", style: "margin-top:8px" }, impBroker, impMarket, file),
    h("p", { class: "muted small" }, "A CSV with a header row containing a ticker column (Symbol, Ticker or Code) and a quantity column (Quantity, Shares or Units); an average cost column is used if present. Importing replaces what is recorded for that broker only. The file is read on this computer and never sent anywhere else."),
    h("h3", { style: "margin-top:12px" }, "Add or change one holding"), addForm, err, ok,
    h("p", { class: "muted small" }, "Holdings are for your information: the app never trades them. They feed the portfolio builder, the daily earnings alerts and the assistant. \"Trend\" uses price trend and momentum only."));

  // --- brokers
  const editBox = h("div", {});
  function editBroker(b) {
    const berr = h("p", { class: "error", role: "alert" });
    const input = (f) => f.kind === "bool" ? h("label", { class: "row small", style: "align-self:center" }, h("input", { type: "checkbox", name: f.key, checked: b ? b[f.key] : f.key !== "us_options" && f.key !== "fractional" }), f.label)
      : f.kind === "str" ? h("label", {}, f.label, h("select", { name: f.key }, ["AUD", "USD"].map((c) => h("option", { value: c, selected: (b ? b[f.key] : "AUD") === c }, c))))
      : h("label", {}, f.label, h("input", { name: f.key, type: "number", min: 0, step: "any", value: b ? b[f.key] : 0 }));
    const form = h("form", { class: "fields", style: "margin-top:12px", onsubmit: async (e) => {
      e.preventDefault(); berr.textContent = "";
      const d = Object.fromEntries(new FormData(form)), data = {};
      for (const f of bk.fields) data[f.key] = f.kind === "bool" ? form.elements[f.key].checked : f.kind === "float" ? +d[f.key] || 0 : d[f.key];
      try { await api("/brokers", { body: { name: d.name, data } }); done(`Saved ${d.name}.`); } catch (ex) { berr.textContent = ex.message; }
    } },
      h("label", {}, "Broker name", h("input", { name: "name", required: true, maxlength: 40, value: b ? b.name : "", readonly: !!b })),
      bk.fields.map(input), h("button", { type: "submit", class: "primary" }, "Save broker"));
    fill(editBox, form, berr, h("p", { class: "muted small" }, "Copy the fees from the broker's current price list. A share trade is costed as the larger of the minimum and (fixed fee + percentage of the trade), plus the conversion percentage when the stock is priced in a different currency from the account. Leave a fee at 0 if it does not apply."));
  }
  const feeText = (b, m) => { const f = b[m + "_fixed"], p = b[m + "_pct"], mn = b[m + "_min"]; return [f ? fmt.num(f) : null, p ? p + "%" : null].filter(Boolean).join(" + ") + (mn ? ` (min ${fmt.num(mn)})` : "") || "free"; };
  const brokersCard = h("section", { class: "card" }, h("div", { class: "row between" }, h("h2", {}, "My brokers"), h("button", { onclick: () => editBroker(null) }, "Add a broker")),
    table([{ t: "Broker", l: 1 }, { t: "Account", l: 1 }, "US shares", "ASX shares", "US options", "Currency conversion", { t: "Stop orders", l: 1 }, ""], bk.brokers.map((b) => h("tr", {},
      h("td", {}, h("strong", {}, b.name)), h("td", { class: "l" }, b.base_currency),
      h("td", { class: "num" }, b.us_stocks ? feeText(b, "us") : "not offered"), h("td", { class: "num" }, b.asx_stocks ? feeText(b, "asx") : "not offered"),
      h("td", { class: "num" }, b.us_options ? `${fmt.num(b.option_per_contract)} / contract` : "not offered"), h("td", { class: "num" }, b.fx_pct ? b.fx_pct + "%" : "none"),
      h("td", { class: "l" }, b.conditional_orders ? "Yes" : "No"),
      h("td", {}, h("button", { onclick: () => editBroker(b) }, "Edit"), " ", h("button", { class: "link", "aria-label": `Delete ${b.name}`, onclick: async () => { if (confirm(`Delete ${b.name}? Holdings recorded there are kept.`)) { await api("/brokers?name=" + encodeURIComponent(b.name), { method: "DELETE" }); render(); } } }, "✕")))),
      { empty: "No brokers yet. Add each trading provider you use with its fees, and the app will work out the cheapest one for each trade." }),
    editBox);

  // --- which broker for this trade
  const recBox = h("div", { style: "margin-top:12px" });
  const recForm = h("form", { class: "fields", onsubmit: async (e) => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(recForm)), opt = d.what.endsWith("option");
    fill(recBox, loading());
    try {
      const r = await api(`/brokers/recommend?symbol=${encodeURIComponent(d.symbol)}&action=${d.what.startsWith("sell") ? "sell" : "buy"}&value=${+d.value || 0}&instrument=${opt ? "option" : "stock"}&contracts=${+d.contracts || 1}`);
      if (!r.available) { fill(recBox, h("p", { class: "notice" }, r.reason)); return; }
      fill(recBox, h("p", {}, h("strong", {}, `Use ${r.recommended}. `), r.why),
        table([{ t: "Broker", l: 1 }, "Commission", "Conversion", "Total cost", "% of trade", "Buy and sell again", { t: "Notes", l: 1 }], r.options.map((o) => h("tr", {},
          h("td", {}, o.broker === r.recommended ? h("strong", {}, o.broker + " ✓") : o.broker), h("td", { class: "num" }, fmt.num(o.commission)), h("td", { class: "num" }, fmt.num(o.fx)),
          h("td", { class: "num" }, fmt.money(o.total, o.currency)), h("td", { class: "num" }, o.pct_of_trade == null ? dash : fmt.pct(o.pct_of_trade, 2)), h("td", { class: "num" }, fmt.num(o.round_trip)),
          h("td", { class: "l wrap" }, o.notes.join(" "))))),
        r.excluded.length ? h("p", { class: "muted small" }, "Left out: " + r.excluded.join(" ")) : null);
    } catch (ex) { fill(recBox, errorBox(ex)); }
  } },
    h("label", {}, "Ticker", h("input", { name: "symbol", required: true, value: state.symbol })),
    h("label", {}, "What you want to do", h("select", { name: "what" }, h("option", { value: "buy" }, "Buy shares"), h("option", { value: "sell" }, "Sell shares"), h("option", { value: "buy option" }, "Buy options"), h("option", { value: "sell option" }, "Sell options"))),
    h("label", {}, "Trade size (money)", h("input", { name: "value", type: "number", min: 0, step: "any", value: 2000, required: true })),
    h("label", {}, "Option contracts", h("input", { name: "contracts", type: "number", min: 1, step: 1, value: 1 })),
    h("button", { type: "submit", class: "primary" }, "Find the best broker"));
  const recCard = h("section", { class: "card" }, h("h2", {}, "Which broker for this trade?"),
    h("p", { class: "muted small", style: "margin-top:-6px" }, "Compares your brokers on the fees you entered. Shares can only be sold where they are held, so a sale looks only at brokers holding the stock. The same recommendation appears in each stock's How to buy panel and in the portfolio builder."),
    recForm, recBox);
  return [holdingsCard, brokersCard, recCard];
}

// ---------------------------------------------------------------- buy guide
/** "How to buy" panel: minimum money, order types for a chosen time, and a suggested option. Loads when opened. */
function buyGuide(symbol, open = false) {
  const body = h("div", { class: "stack", style: "margin-top:12px" });
  const whenIn = h("input", { type: "datetime-local", "aria-label": "Planned time to buy", value: state.buyWhen || "", onchange: () => { state.buyWhen = whenIn.value; load(); } });
  const nowBtn = h("button", { type: "button", onclick: () => { whenIn.value = ""; state.buyWhen = ""; load(); } }, "Now");
  async function load() {
    fill(body, loading("Working out how to buy…"));
    try {
      const when = whenIn.value ? "&when=" + encodeURIComponent(new Date(whenIn.value).toISOString()) : "";
      const g = await api(`/buy-guide?symbol=${encodeURIComponent(symbol)}${when}`);
      const s = g.session, m = g.minimum_equity, o = g.option, cur = g.currency;
      fill(body,
        g.signal_warning ? h("p", { class: "notice" }, g.signal_warning) : null,
        g.earnings_warning ? h("p", { class: "notice" }, g.earnings_warning) : null,
        h("div", { class: "row" }, h("span", { class: "small muted" }, "Planning to buy at"), whenIn, nowBtn,
          h("span", { class: "small" }, `${s.market}: ${s.phase} (${s.exchange_time} exchange time)`,
            s.next_open ? `, next opens ${new Date(s.next_open).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })} your time` : "")),
        h("div", {}, h("h3", {}, "Minimum money needed"),
          h("div", { class: "tiles", style: "margin-top:8px" },
            tile("Smallest purchase", fmt.money(m.one_parcel, cur), `${m.one_parcel_shares} share${m.one_parcel_shares > 1 ? "s" : ""}`),
            tile("Account size for the 1% rule", fmt.money(m.for_risk_rule, cur), "so that parcel risks ≤ 1% at the stop"),
            tile("One option contract", m.option_contract == null ? dash : fmt.money(m.option_contract, cur), m.option_contract == null ? "no option available" : "the most it can lose")),
          h("p", { class: "muted small" }, m.one_parcel_note)),
        h("div", {}, h("h3", {}, "Ways to buy"),
          h("p", { style: "margin:6px 0 10px" }, h("strong", {}, "Suggested: "), g.recommendation),
          table([{ t: "Order type", l: 1 }, "Price", { t: "What it does", l: 1 }, { t: "Use it when", l: 1 }], g.orders.map((x) => h("tr", {},
            h("td", {}, x.recommended ? h("strong", {}, x.type + " ✓") : x.type), h("td", { class: "num" }, x.price == null ? "market" : fmt.num(x.price)),
            h("td", { class: "l wrap" }, x.what), h("td", { class: "l wrap" }, x.use_when)))),
          g.broker.available ? h("p", { style: "margin:8px 0 0" }, h("strong", {}, `Buy through ${g.broker.recommended}. `), g.broker.why) : null,
          h("p", { class: "muted small" }, `After you are in: stop-loss at ${fmt.num(g.after_entry.stop_loss)}, take profit at ${fmt.num(g.after_entry.take_profit)}. ${g.after_entry.note}`)),
        h("div", {}, h("h3", {}, "Option alternative"),
          o.available ? [
            h("p", { style: "margin:6px 0 10px" }, h("strong", {}, o.contract), ` at ${fmt.num(o.premium)} per share (${o.price_basis}). Controls 100 shares for a fraction of their cost; the premium is the most you can lose.`),
            h("div", { class: "tiles" },
              tile("Cost / max loss", fmt.money(o.cost, cur), `${o.days_to_expiry} days to expiry`),
              tile("Breakeven at expiry", fmt.num(o.breakeven), `stock must rise ${fmt.pct(o.breakeven_move)}`),
              tile("Delta", fmt.num(o.delta, 2), `acts like ${fmt.int(o.share_equivalent)} shares`),
              tile("If stock hits target", o.profit_at_target == null ? dash : signed(o.profit_at_target, fmt.snum(o.profit_at_target, 0)), "profit at expiry"),
              tile("If stock hits stop", o.loss_at_stop == null ? dash : signed(o.loss_at_stop, fmt.snum(o.loss_at_stop, 0)), "at expiry"),
              tile("Time decay", fmt.num(o.theta_per_day, 0) + " / day", `implied vol ${fmt.pct(o.iv, 0)}`)),
            o.warnings.length ? h("ul", { class: "reasons", style: "margin-top:8px" }, o.warnings.map((x) => h("li", {}, x))) : null,
            g.option_broker && g.option_broker.available ? h("p", { style: "margin:8px 0 0" }, h("strong", {}, `Buy through ${g.option_broker.recommended}. `), g.option_broker.why) : null,
            h("div", { class: "row", style: "margin-top:10px" }, h("button", { onclick: () => go("options", symbol) }, "Open the option chain to paper trade it")),
          ] : h("p", { class: "notice", style: "margin-top:8px" }, o.reason)),
        h("p", { class: "muted small" }, g.caveats.join(" ")));
    } catch (e) { fill(body, errorBox(e)); }
  }
  if (open) { onMount(load); return body; }
  const details = h("details", { class: "guide", ontoggle: () => { if (details.open && !details.dataset.loaded) { details.dataset.loaded = "1"; load(); } } },
    h("summary", {}, "How to buy: minimum money, order types and an option alternative"), body);
  return details;
}

// ---------------------------------------------------------------- portfolio
async function portfolioView() {
  const out = h("div", { class: "stack" }), err = h("p", { class: "error", role: "alert" }), savedBox = h("div", { class: "stack" });
  const saved = JSON.parse(localStorage.getItem("ql_portfolio") || "null") || { equity: 10000, months: 6, target: 5, market: "US" };
  const btn = h("button", { type: "submit", class: "primary" }, "Build portfolio");
  const frac = h("input", { type: "checkbox", name: "fractional", checked: !!saved.fractional });
  const body = () => { const d = Object.fromEntries(new FormData(form)); return { equity: +d.equity, months: +d.months, target_return: d.target / 100, market: d.market, fractional: frac.checked }; };
  const form = h("form", { class: "fields", onsubmit: async (e) => {
    e.preventDefault(); err.textContent = ""; btn.disabled = true;
    localStorage.setItem("ql_portfolio", JSON.stringify({ ...Object.fromEntries(new FormData(form)), fractional: frac.checked }));
    try { show(await api("/portfolio", { body: body() })); }
    catch (ex) { err.textContent = ex.message; } finally { btn.disabled = false; }
  } },
    h("label", {}, "Amount to invest", h("input", { name: "equity", type: "number", min: 1, step: "any", value: saved.equity, required: true })),
    h("label", {}, "Market", h("select", { name: "market" }, h("option", { value: "US", selected: saved.market === "US" }, "US (USD)"), h("option", { value: "ASX", selected: saved.market === "ASX" }, "ASX (AUD)"))),
    h("label", {}, "Period (months)", h("input", { name: "months", type: "number", min: 1, max: 60, step: 1, value: saved.months, required: true })),
    h("label", {}, "Target return over the period (%)", h("input", { name: "target", type: "number", min: 0, max: 500, step: "any", value: saved.target, required: true })),
    h("label", { class: "row small", style: "align-self:center", title: "Size positions in fractions of a share. Only some brokers sell these, mostly for US shares." }, frac, "Allow fractional shares"), btn);
  function show(p) {
    const cur = p.inputs.currency, a = p.assumptions, owned = p.positions.some((x) => x.held), brokered = p.positions.some((x) => x.broker);
    const rows = p.positions.map((x) => h("tr", { class: "click", onclick: () => go("research", x.symbol) },
      h("td", {}, h("strong", {}, x.symbol), h("div", { class: "muted small" }, x.name)), h("td", { class: "l" }, x.role),
      h("td", { class: "num" }, fmt.shares(x.shares)), owned ? h("td", { class: "num" }, fmt.shares(x.to_buy)) : null,
      h("td", { class: "num" }, fmt.num(x.price)), h("td", { class: "num" }, fmt.money(x.value, cur)),
      h("td", { class: "num" }, fmt.pct(x.weight)), h("td", { class: "num" }, fmt.num(x.beta)), h("td", { class: "num" }, x.stop == null ? dash : fmt.num(x.stop)),
      brokered ? h("td", { class: "l" }, x.broker ? `${x.broker} (${fmt.num(x.fee)})` : dash) : null));
    rows.push(h("tr", {}, h("td", {}, h("strong", {}, "Cash")), h("td", { class: "l" }, "Uninvested"), h("td", {}, ""), owned ? h("td", {}, "") : null, h("td", {}, ""),
      h("td", { class: "num" }, fmt.money(p.cash, cur)), h("td", { class: "num" }, fmt.pct(p.cash_weight)), h("td", { class: "num" }, "0.00"), h("td", {}, ""), brokered ? h("td", {}, "") : null));
    const nameIn = h("input", { placeholder: "Name, e.g. Six-month plan", "aria-label": "Portfolio name", maxlength: 60 });
    const saveErr = h("span", { class: "error" });
    fill(out,
      h("section", { class: "card" }, h("h2", {}, "Is the target realistic?"),
        h("p", { style: "margin:0 0 12px" }, p.verdict),
        h("div", { class: "tiles" },
          tile("Target", fmt.spct(p.inputs.target_return), `${fmt.pct(p.required_annual_return)} a year`),
          tile("Chance of reaching it", fmt.pct(p.probability_of_target, 0), "estimate, not a promise"),
          tile("Expected return", fmt.spct(p.expected_return), `about ${fmt.money(p.expected_value, cur)}`),
          tile("Likely range", `${fmt.spct(p.range_low, 0)} to ${fmt.spct(p.range_high, 0)}`, "9 times in 10"),
          tile("Worst drop, last 2 years", fmt.pct(p.worst_drop_last_2y), "this mix, peak to trough"),
          tile("Loss if every stop is hit", fmt.money(p.loss_if_all_stops_hit, cur), "ideas only; the fund has no stop"))),
      h("section", { class: "card" }, h("div", { class: "row between" }, h("h2", {}, "Recommended portfolio"),
          h("span", { class: "muted small" }, `${fmt.pct(p.invest_share, 0)} invested · beta ${fmt.num(p.beta)} · volatility ${fmt.pct(p.annual_volatility, 0)} a year`,
            p.total_fees != null ? ` · about ${fmt.money(p.total_fees, cur)} in fees to buy` : "")),
        table([{ t: "Holding", l: 1 }, { t: "Role", l: 1 }, "Shares", owned ? "To buy" : null, "Price", "Value", "Weight", "Beta", "Stop", brokered ? { t: "Buy through (fee)", l: 1 } : null].filter(Boolean), rows),
        p.notes.length ? h("ul", { class: "reasons", style: "margin-top:10px" }, p.notes.map((x) => h("li", {}, x))) : null,
        p.positions.length ? h("div", { class: "row", style: "margin-top:12px" }, nameIn, h("button", { onclick: async () => {
          saveErr.textContent = "";
          try { await api("/portfolios", { body: { ...body(), name: nameIn.value } }); nameIn.value = ""; loadSaved(); } catch (ex) { saveErr.textContent = ex.message; }
        } }, "Save and track this portfolio"), saveErr) : null),
      h("section", { class: "card" }, h("h2", {}, "How this was worked out"),
        h("ul", { class: "reasons" },
          h("li", {}, `Structure: about 40% of the invested money in a broad index fund, the rest spread over up to 6 current ideas from the latest scan${a.scan_date ? ` (${new Date(a.scan_date).toLocaleDateString([], { dateStyle: "medium" })})` : ""}, with calmer stocks given more. ${p.inputs.fractional ? "Positions are sized in fractions of a share, so each holding matches its intended weight exactly." : "Whole shares only; tick \"Allow fractional shares\" to size positions exactly."}`),
          h("li", {}, "How much is invested versus held in cash is set by your target: a higher target means more in shares, up to 100%. The app never borrows to reach a target."),
          h("li", {}, `Expected return assumes cash earns ${fmt.pct(a.cash_rate, 0)} a year and the share market ${fmt.pct(a.equity_premium, 0)} a year more than cash, scaled by each holding's beta. It does not assume the ideas keep rising at their recent pace.`),
          h("li", {}, "The range and the chance of reaching the target come from how much this mix moved day to day over the last two years. Real markets produce bigger shocks than that method expects, so treat the low end as optimistic."),
          h("li", {}, "This is a starting allocation, not financial advice. It ignores tax and anything you own that has not been imported under My portfolio."))));
  }
  async function loadSaved() {
    const list = await api("/portfolios");
    fill(savedBox, list.length ? h("section", { class: "card" }, h("h2", {}, "Saved portfolios"),
      table([{ t: "Name", l: 1 }, "Saved", "Amount", "Target", ""], list.map((x) => h("tr", {},
        h("td", {}, h("button", { class: "link", onclick: () => track(x.id) }, x.name)), h("td", {}, new Date(x.created).toLocaleDateString([], { dateStyle: "medium" })),
        h("td", { class: "num" }, fmt.money(x.inputs.equity, x.inputs.currency)), h("td", { class: "num" }, `${fmt.spct(x.inputs.target_return)} in ${x.inputs.months} mo`),
        h("td", {}, h("button", { onclick: () => track(x.id) }, "Track"), " ", h("button", { class: "link", "aria-label": `Delete ${x.name}`, onclick: async () => { if (confirm(`Delete the saved portfolio "${x.name}"?`)) { await api("/portfolios/" + x.id, { method: "DELETE" }); fill(trackBox); loadSaved(); } } }, "✕"))))),
      trackBox) : null);
  }
  const trackBox = h("div", { class: "stack", style: "margin-top:12px" });
  async function track(id) {
    fill(trackBox, loading("Valuing the portfolio…"));
    try {
      const t = await api("/portfolios/" + id), cur = t.inputs.currency, legend = h("div", { class: "legend" }), el = h("div", { class: "chart short" });
      fill(trackBox,
        h("h3", {}, `${t.name}: day ${t.days_elapsed} of ${t.days_total}`),
        h("div", { class: "tiles" },
          tile("Value now", fmt.money(t.value, cur), signed(t.return, fmt.spct(t.return, 2))),
          tile("Where the target needs you", fmt.spct(t.on_track_return, 2), `target ${fmt.spct(t.target_return)} by the end`),
          tile("Against the target path", signed(t.ahead_of_target_path, fmt.spct(t.ahead_of_target_path, 2)), t.ahead_of_target_path >= 0 ? "ahead" : "behind"),
          tile(`Index (${t.index})`, t.index_return == null ? dash : signed(t.index_return, fmt.spct(t.index_return, 2)), "same period")),
        t.history.length > 2 ? [legend, el] : h("p", { class: "muted small" }, "The value chart appears once a few trading days have passed."),
        h("h3", {}, "What to do"), h("ul", { class: "reasons" }, t.suggestions.map((x) => h("li", {}, x))),
        table([{ t: "Holding", l: 1 }, "Shares", "Bought at", "Now", "Return", "Weight then", "Weight now", { t: "Signal", l: 1 }], t.positions.map((x) => h("tr", {},
          h("td", {}, h("strong", {}, x.symbol)), h("td", { class: "num" }, fmt.shares(x.shares)), h("td", { class: "num" }, fmt.num(x.price)), h("td", { class: "num" }, fmt.num(x.price_now)),
          h("td", {}, signed(x.return, fmt.spct(x.return))), h("td", { class: "num" }, fmt.pct(x.weight)), h("td", { class: "num" }, fmt.pct(x.weight_now)),
          h("td", { class: "l" }, x.stop_hit ? "Stop hit" : x.signal || dash)))));
      if (t.history.length > 2) {
        const chart = makeChart(el), c1 = cssVar("--series-1"), c2 = cssVar("--series-2");
        const s1 = chart.addLineSeries(lineOpts(c1)), s2 = chart.addLineSeries(lineOpts(c2));
        s2.setData(t.index_history); s1.setData(t.history); chart.timeScale().fitContent();
        liveLegend(chart, legend, [{ series: s1, label: "Portfolio", color: c1, last: t.history.at(-1) }, { series: s2, label: "Same money in the index", color: c2, last: t.index_history.at(-1) }]);
      }
    } catch (e) { fill(trackBox, errorBox(e)); }
  }
  onMount(() => { form.requestSubmit(); loadSaved(); });
  return [h("section", { class: "card" }, h("h2", {}, "Portfolio builder"),
    h("p", { class: "muted small", style: "margin-top:-6px" }, "Enter how much you have, for how long, and the return you are aiming for. The example below uses 10,000 over 6 months aiming for 5%; change the numbers and rebuild."),
    form, err), savedBox, out];
}

// ---------------------------------------------------------------- ideas
async function ideasView() {
  let st = await api("/ideas");
  const sectorList = await api("/ideas/sectors");
  const box = h("div", { class: "stack" }), btBox = h("div", {});
  const market = h("select", { "aria-label": "Market" }, h("option", { value: "ALL" }, "US and ASX"), h("option", { value: "US" }, "US only"), h("option", { value: "ASX" }, "ASX only"));
  const sector = h("select", { "aria-label": "Sector" }, h("option", { value: "" }, "All sectors"), sectorList.filter((x) => x !== "Funds").map((x) => h("option", { value: x }, x)));
  const mid = h("input", { type: "checkbox" }), etf = h("input", { type: "checkbox" });
  const btn = h("button", { class: "primary", onclick: async () => {
    await api(`/ideas/scan?market_name=${market.value}&mid=${mid.checked}&etf=${etf.checked}${sector.value ? "&sector=" + encodeURIComponent(sector.value) : ""}`, { method: "POST" }); poll();
  } }, "Scan now");
  const progress = h("span", { class: "muted small", role: "status" });
  const token = render.token;
  async function poll() {
    if (render.token !== token) return;  // the user left this screen
    st = await api("/ideas");
    draw();
    if (st.status === "running") setTimeout(poll, 1500);
  }
  function card(it, i, bearish) {
    return h("section", { class: "card" },
      h("div", { class: "row between" },
        h("div", { class: "row" }, h("span", { class: "muted" }, `#${i + 1}`), h("h2", {}, it.symbol), h("span", { class: "muted" }, it.name), chip(it.label), h("span", { class: "muted small" }, it.sector)),
        h("div", { class: "row" }, h("button", { onclick: () => go("research", it.symbol) }, "Research"),
          bearish ? h("button", { class: "primary", onclick: () => go("options", it.symbol) }, "Put strategies") : h("button", { class: "primary", onclick: () => go("paper", it.symbol) }, "Paper trade"))),
      h("div", { class: "tiles", style: "margin:12px 0" },
        tile("Price", fmt.money(it.price, it.currency)),
        tile("Score", fmt.snum(it.score), "scale −1 to +1"),
        tile("Last 20 days", signed(it.ret_20, fmt.spct(it.ret_20))),
        tile("Last 6 months", signed(it.ret_120, fmt.spct(it.ret_120))),
        tile("Price / earnings", it.pe == null ? dash : fmt.num(it.pe, 1), it.next_earnings ? `earnings ${it.next_earnings}` : "no earnings date"),
        bearish ? tile("How to act", "Buy a put", "shares cannot be shorted here") : tile("Stop / target", `${fmt.num(it.plan.stop)} / ${fmt.num(it.plan.target)}`, `size ${fmt.int(it.plan.shares)} sh`)),
      h("ul", { class: "reasons" }, it.reasons.map((x) => h("li", {}, x))),
      bearish ? null : buyGuide(it.symbol));
  }
  function draw() {
    const running = st.status === "running", r = st.result;
    btn.disabled = running; btn.textContent = running ? "Scanning…" : r ? "Scan again" : "Scan now";
    progress.textContent = running ? `${st.stage}${st.total ? ` (${st.done} of ${st.total})` : ""}. This takes a minute or two.` : "";
    if (!r) { fill(box, st.error ? errorBox(new Error(st.error)) : h("p", { class: "notice" }, "No scan yet. Press Scan now to search the largest US and ASX companies for candidates.")); return; }
    const age = (Date.now() - new Date(r.scanned_at)) / 36e5;
    fill(box,
      st.error ? errorBox(new Error(st.error)) : null,
      h("p", { class: "muted small" }, `Scanned ${new Date(r.scanned_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}: ${r.screened} of ${r.universe_size} names screened`,
        r.sector ? ` in ${r.sector}` : "", r.missing ? ` (${r.missing} had no usable data)` : "", `, ${r.shortlisted} got the full signal, ${r.ideas.length} rated Buy or better.`,
        age > 24 ? " This scan is more than a day old; scan again for current prices." : ""),
      r.ideas.length ? r.ideas.map((it, i) => card(it, i, false)) : h("p", { class: "notice" }, "Nothing in this scan rates a Buy. That is a result in itself: when few stocks are in an uptrend, the app does not force ideas."),
      r.bearish.length ? [h("h2", { style: "margin-top:8px" }, "Bearish ideas"),
        h("p", { class: "muted small", style: "margin-top:-10px" }, "US stocks in the weakest downtrends that the full signal rates Sell. The way to act on these is a put option, which profits if the price falls and cannot lose more than it cost."),
        r.bearish.map((it, i) => card(it, i, true))] : null,
      h("section", { class: "card" }, h("h2", {}, "Everything screened"),
        h("p", { class: "muted small", style: "margin-top:-6px" }, "Ranked on price trend and momentum only. The top few per market are then checked with the full signal above. Click a row to research it."),
        table([{ t: "Symbol", l: 1 }, { t: "Sector", l: 1 }, "Last", "Quick score", "", "RSI", "20 days", "6 months"], r.table.map((x) => h("tr", { class: "click", onclick: () => go("research", x.symbol) },
          h("td", {}, h("strong", {}, x.symbol)), h("td", { class: "l" }, x.sector || dash), h("td", { class: "num" }, fmt.money(x.price, x.currency)),
          h("td", { class: "num" }, fmt.snum(x.score)), h("td", {}, scoreBar(x.score)), h("td", { class: "num" }, fmt.num(x.rsi, 0)),
          h("td", {}, signed(x.ret_20, fmt.spct(x.ret_20))), h("td", {}, signed(x.ret_120, fmt.spct(x.ret_120))))), { scroll: true })));
  }
  async function runBacktest(m) {
    fill(btBox, loading("Replaying four years of weekly scans… about half a minute."));
    try {
      const r = await api("/ideas/backtest?market_name=" + m), s = r.stats, legend = h("div", { class: "legend" }), el = h("div", { class: "chart short" });
      const row = (name, key, f) => h("tr", {}, h("td", {}, name), h("td", { class: "num" }, h("strong", {}, f(s.strategy[key]))), h("td", { class: "num" }, f(s.universe[key])), h("td", { class: "num" }, s.index ? f(s.index[key]) : dash));
      const beat = r.beat_universe;
      fill(btBox,
        h("p", { style: "margin:10px 0" }, h("strong", {}, beat > 0.01 ? `The screen beat its universe by ${fmt.pct(beat)} a year.` : beat < -0.01 ? `The screen trailed its universe by ${fmt.pct(-beat)} a year.` : "The screen did about the same as its universe."),
          ` Buying the top ${r.top_n} ${m} large companies by score every ${r.hold_days} trading days, ${r.from} to ${r.to}, after ${r.cost_bps} bps trading costs.`),
        legend, el,
        table(["", "Scanner picks", "All stocks, equal", "Index"], [row("Return per year", "cagr", fmt.spct), row("Volatility", "volatility", fmt.pct), row("Sharpe ratio", "sharpe", fmt.num), row("Worst drop", "max_drawdown", fmt.pct)]),
        h("ul", { class: "reasons", style: "margin-top:10px" },
          h("li", {}, "The fair comparison is against \"All stocks, equal\", not the index: the list is today's biggest companies, which were picked with hindsight and flatter every line here."),
          h("li", {}, "Only the price screen is replayed. The forecast and news parts of the full signal cannot be reconstructed for the past, so the live ideas may differ."),
          h("li", {}, `It held ${fmt.num(r.avg_holdings, 1)} stocks on average and replaced ${fmt.pct(r.avg_turnover_per_review / 2, 0)} of the portfolio at each review. Past results do not predict future returns.`)));
      const chart = makeChart(el), defs = [["Scanner picks", "strategy", "--series-1"], ["All stocks, equal", "universe", "--series-2"], ["Index", "index", "--series-3"]].filter((d) => r.curves[d[1]]);
      liveLegend(chart, legend, defs.map(([label, key, v]) => { const sr = chart.addLineSeries(lineOpts(cssVar(v))); sr.setData(r.curves[key]); return { series: sr, label, color: cssVar(v), last: r.curves[key].at(-1) }; }));
      chart.timeScale().fitContent();
    } catch (e) { fill(btBox, errorBox(e)); }
  }
  draw();
  if (st.status === "running") onMount(poll);
  return [h("section", { class: "card" },
    h("div", { class: "row between" }, h("h2", {}, "Trade ideas"), h("div", { class: "row" }, progress, market, sector,
      h("label", { class: "row small" }, mid, "Mid-sized"), h("label", { class: "row small" }, etf, "Funds"), btn)),
    h("p", { class: "muted small" }, "Scans large US and ASX companies (optionally mid-sized companies and index funds, or one sector) and shortlists the ones in the strongest uptrends that the full signal also rates a Buy. Ideas are a starting point for your own check, chosen mostly on trend and momentum; they are not predictions or financial advice.")),
    box,
    h("section", { class: "card" }, h("div", { class: "row between" }, h("h2", {}, "Has this scanner worked in the past?"),
      h("div", { class: "row" }, h("button", { onclick: () => runBacktest("US") }, "Test on US"), h("button", { onclick: () => runBacktest("ASX") }, "Test on ASX"))),
      h("p", { class: "muted small" }, "Replays the price screen week by week over about four years, using only what was known at the time, and compares its picks with simply holding every stock in the list."), btBox)];
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
            h("div", { class: "muted small" }, `${it.source} · ${new Date(it.published).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })} · ${word} (${fmt.snum(it.sentiment)}${it.ai ? ", read by Claude" : ""})`)));
      })) : h("p", { class: "muted" }, "No recent headlines found."));
  }).catch((e) => fill(newsCard, h("h2", {}, "News"), errorBox(e)));

  return [chartCard, fundamentalsCard(sym), h("div", { class: "grid-even" }, signalCard, newsCard),
    h("section", { class: "card" }, h("h2", {}, `Buying ${sym}`), buyGuide(sym, true))];
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

/** Paper-trading ticket for one expiry. `pick(strike, kind)` loads a contract from the chain table. */
function optionTicket(sym, expiry, calls, puts, strikes, summary) {
  const err = h("p", { class: "error", role: "alert" }), ok = h("p", { class: "small up", role: "status" });
  const est = h("p", { class: "muted small" });
  const kindSel = h("select", { name: "kind", onchange: update }, h("option", { value: "call" }, "Call"), h("option", { value: "put" }, "Put"));
  const strikeSel = h("select", { name: "strike", onchange: update }, strikes.map((k) => h("option", { value: k }, fmt.num(k))));
  const qty = h("input", { name: "contracts", type: "number", min: 1, step: 1, value: 1, required: true, oninput: update });
  if (summary.atm_strike != null) strikeSel.value = summary.atm_strike;
  function update() {
    const o = (kindSel.value === "call" ? calls : puts).get(+strikeSel.value);
    if (!o) { est.textContent = "This contract is not listed for this expiry."; return; }
    const px = o.quoted ? o.ask : o.last, n = Math.max(1, +qty.value || 1);
    est.textContent = px > 0
      ? `Buying fills at ${fmt.num(px)} per share (${o.quoted ? "the ask" : "last trade; no live quote"}), about ${fmt.num(px * 100 * n)} for ${n} contract${n > 1 ? "s" : ""}. Breakeven at expiry: ${fmt.num(kindSel.value === "call" ? +strikeSel.value + px : +strikeSel.value - px)}.`
      : "No bid/ask or last trade for this contract right now, so it cannot be filled.";
  }
  const form = h("form", { class: "fields", onsubmit: async (e) => {
    e.preventDefault(); err.textContent = ""; ok.textContent = "";
    try {
      const r = await api("/paper/options/orders", { body: { symbol: sym, expiry, strike: +strikeSel.value, kind: kindSel.value, side: e.submitter.value, contracts: +qty.value } });
      ok.textContent = `Filled: ${r.side} ${r.contracts} × ${r.contract} at ${fmt.money(r.price, r.currency)} (${r.fill_source}). See it under Paper trading.`;
    } catch (ex) { err.textContent = ex.message; }
  } },
    h("label", {}, "Type", kindSel), h("label", {}, "Strike", strikeSel), h("label", {}, "Contracts", qty),
    h("button", { type: "submit", value: "buy", class: "primary" }, "Buy to open"), h("button", { type: "submit", value: "sell" }, "Sell to close"));
  update();
  const el = h("section", { class: "card" }, h("h2", {}, `Paper trade ${sym} options, expiring ${expiry}`), form, est, err, ok,
    h("p", { class: "muted small" }, "Simulated money. One contract covers 100 shares. Buys fill at the ask and sells at the bid; when there is no live quote, both use the last traded price. You can buy calls and puts and sell what you hold; writing options is not simulated."));
  return { el, pick(strike, kind) { strikeSel.value = strike; kindSel.value = kind; update(); el.scrollIntoView({ behavior: "smooth", block: "center" }); } };
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
      const ticket = optionTicket(sym, expiry, calls, puts, strikes, s);
      const rows = strikes.map((k) => h("tr", { class: "click", title: "Click to load this strike into the order ticket", onclick: (e) => {
        const cell = e.target.closest("td"), i = cell ? [...cell.parentNode.children].indexOf(cell) : 0;
        ticket.pick(k, i > 6 ? "put" : "call");
      } }, cells(calls.get(k), k < s.spot, false), h("td", { class: "strike num" }, fmt.num(k)), cells(puts.get(k), k > s.spot, true)));
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
        ticket.el,
        h("section", { class: "card" }, h("h2", {}, "Implied volatility by strike"), smileChart(c.calls, c.puts, s.spot)),
        h("section", { class: "card" }, h("div", { class: "row between" }, h("h2", {}, "Chain"), h("span", { class: "muted small" }, "Calls left, puts right · shaded = in the money")),
          table([...cols, { t: "Strike" }, ...cols.slice().reverse()], rows, { scroll: true })));
    } catch (e) { fill(body, errorBox(e)); }
  }
  load(select.value);
  return [h("section", { class: "card" }, head, h("div", { class: "row", style: "margin-top:12px" }, h("label", { class: "row" }, "Expiry", select))), body, strategiesCard(sym), calc];
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
  const [acct, trades, perf, orders] = await Promise.all([api("/paper/account"), api("/paper/trades"), api("/signals/performance"), api("/paper/orders")]);
  const err = h("p", { class: "error", role: "alert" }), ok = h("p", { class: "small up", role: "status" });
  async function order(symbol, side, qty, extra = {}) {
    err.textContent = ""; ok.textContent = "";
    try {
      const r = await api("/paper/orders", { body: { symbol, side, qty: +qty, ...extra } });
      sessionStorage.setItem("ql_fill", r.status === "open" ? `Order placed: ${r.side} ${r.qty} ${r.symbol}, ${r.type} at ${fmt.money(r.price, r.currency)}. It fills when a later day's trading reaches that price.`
        : `Filled: ${r.side} ${r.qty} ${r.symbol} at ${fmt.money(r.price, r.currency)}`);
      render();
    } catch (ex) { err.textContent = ex.message; }
  }
  ok.textContent = sessionStorage.getItem("ql_fill") || ""; sessionStorage.removeItem("ql_fill");
  const symIn = h("input", { name: "symbol", value: state.symbol, required: true });
  const qtyIn = h("input", { name: "qty", type: "number", min: 1, step: 1, value: 10, required: true });
  const priceIn = h("input", { name: "price", type: "number", min: 0, step: "any", disabled: true, placeholder: "not needed" });
  const typeSel = h("select", { name: "type", onchange: () => { const m = typeSel.value === "market"; priceIn.disabled = m; priceIn.required = !m; priceIn.placeholder = m ? "not needed" : "trigger price"; if (m) priceIn.value = ""; } },
    h("option", { value: "market" }, "Market (fill now)"), h("option", { value: "limit" }, "Limit (this price or better)"), h("option", { value: "stop" }, "Stop (once price passes this)"));
  const slIn = h("input", { name: "sl", type: "number", min: 0, step: "any", placeholder: "optional" }), tpIn = h("input", { name: "tp", type: "number", min: 0, step: "any", placeholder: "optional" });
  const ticket = h("form", { class: "fields", onsubmit: (e) => {
    e.preventDefault();
    const buy = e.submitter.value === "buy";
    order(symIn.value, e.submitter.value, qtyIn.value, { order_type: typeSel.value, price: priceIn.value ? +priceIn.value : null,
      stop_loss: buy && slIn.value ? +slIn.value : null, take_profit: buy && tpIn.value ? +tpIn.value : null });
  } },
    h("label", {}, "Ticker", symIn), h("label", {}, "Shares", qtyIn), h("label", {}, "Order type", typeSel), h("label", {}, "Price", priceIn),
    h("label", {}, "Stop-loss (buys)", slIn), h("label", {}, "Take-profit (buys)", tpIn),
    h("button", { type: "submit", value: "buy", class: "primary" }, "Buy"), h("button", { type: "submit", value: "sell" }, "Sell"));

  const books = acct.books.map((b) => h("section", { class: "card" }, h("h2", {}, `${b.currency} account`),
    h("div", { class: "tiles" }, tile("Equity", fmt.num(b.equity), h("span", {}, signed(b.pnl, `${fmt.snum(b.pnl)} (${fmt.spct(b.pnl_pct, 2)})`))),
      tile("Cash", fmt.num(b.cash)), tile("Invested", fmt.num(b.invested)), tile("Portfolio beta", fmt.num(b.beta), "options at delta exposure"))));

  const positions = acct.positions.map((p) => h("tr", {},
    h("td", {}, h("button", { class: "link", onclick: () => go("research", p.symbol) }, p.symbol)),
    h("td", { class: "num" }, fmt.int(p.qty)), h("td", { class: "num" }, fmt.num(p.avg_cost)), h("td", { class: "num" }, fmt.num(p.price)),
    h("td", { class: "num" }, fmt.money(p.value, p.currency)), h("td", {}, signed(p.pnl, `${fmt.snum(p.pnl)} (${fmt.spct(p.pnl_pct, 2)})`)),
    h("td", { class: "num" }, fmt.num(p.beta)),
    h("td", {}, h("button", { onclick: () => order(p.symbol, "sell", p.qty) }, "Close"))));

  const optErr = h("p", { class: "error", role: "alert" });
  const optionRows = acct.options.map((o) => h("tr", {},
    h("td", {}, h("button", { class: "link", onclick: () => go("options", o.symbol) }, o.label)),
    h("td", { class: "num" }, fmt.int(o.qty)), h("td", { class: "num" }, fmt.num(o.avg_cost)),
    h("td", { class: "num", title: "Priced from: " + o.mark_source }, fmt.num(o.mark)),
    h("td", { class: "num" }, fmt.money(o.value, o.currency)), h("td", {}, signed(o.pnl, `${fmt.snum(o.pnl)} (${fmt.spct(o.pnl_pct)})`)),
    h("td", { class: "num" }, o.delta_shares == null ? dash : fmt.snum(o.delta_shares, 0)), h("td", { class: "num" }, o.days_left),
    h("td", {}, h("button", { onclick: async () => {
      optErr.textContent = "";
      try {
        const r = await api("/paper/options/orders", { body: { symbol: o.symbol, expiry: o.expiry, strike: o.strike, kind: o.kind, side: "sell", contracts: o.qty } });
        sessionStorage.setItem("ql_fill", `Filled: sell ${r.contracts} × ${r.contract} at ${fmt.money(r.price, r.currency)}`); render();
      } catch (ex) { optErr.textContent = ex.message; }
    } }, "Close"))));

  return [
    h("div", { class: "grid-even" }, books),
    h("section", { class: "card" }, h("h2", {}, "Stock order ticket"), ticket, err, ok,
      h("p", { class: "muted small" }, "Market orders fill now at the latest delayed price (5 bps slippage, 5 bps commission). Limit and stop orders wait and are checked against each later day's high and low. Adding a stop-loss or take-profit to a buy places those sell orders once it fills; whichever triggers first cancels the other. To trade options, open a ticker's Options screen.")),
    orders.length ? h("section", { class: "card" }, h("h2", {}, "Open orders"),
      table([{ t: "Placed", l: 1 }, { t: "Symbol", l: 1 }, { t: "Order", l: 1 }, "Shares", "Price", { t: "Note", l: 1 }, ""], orders.map((o) => h("tr", {},
        h("td", {}, o.created), h("td", { class: "l" }, o.symbol), h("td", { class: "l" }, `${o.side} ${o.type}`), h("td", { class: "num" }, fmt.int(o.qty)), h("td", { class: "num" }, fmt.num(o.price)),
        h("td", { class: "l" }, [o.note, o.stop_loss ? `stop-loss ${fmt.num(o.stop_loss)}` : null, o.take_profit ? `take-profit ${fmt.num(o.take_profit)}` : null].filter(Boolean).join(", ")),
        h("td", {}, h("button", { onclick: async () => { await api("/paper/orders/" + o.id, { method: "DELETE" }); render(); } }, "Cancel")))))) : null,
    h("section", { class: "card" }, h("h2", {}, "Stock positions"),
      table(["Symbol", "Shares", "Avg cost", "Last", "Value", "Unrealised P&L", "Beta", ""], positions, { empty: "No open positions." })),
    h("section", { class: "card" }, h("div", { class: "row between" }, h("h2", {}, "Option positions"),
        h("button", { onclick: () => go("options", state.symbol) }, "Open option chain")),
      table([{ t: "Contract", l: 1 }, "Contracts", "Avg cost", "Mark", "Value", "Unrealised P&L", "Delta (shares)", "Days left", ""], optionRows,
        { empty: "No open option positions. Buy calls or puts from the Options screen." }),
      acct.options.some((o) => o.mark_source !== "live quote") ? h("p", { class: "muted small" }, "Some marks use the last traded price or intrinsic value because there is no live quote right now; hover a mark to see which.") : null,
      optErr),
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

// ---------------------------------------------------------------- assistant
const chat = { messages: [], busy: false };

/** Render the assistant's plain-text reply: paragraphs, "- " bullets and **bold**, built as DOM nodes (never innerHTML). */
function renderReply(text) {
  const inline = (line) => line.split(/(\*\*[^*]+\*\*)/).map((part) => /^\*\*[^*]+\*\*$/.test(part) ? h("strong", {}, part.slice(2, -2)) : part);
  const out = []; let list = null;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    const bullet = /^[-*•]\s+/.test(line);
    if (bullet) { if (!list) out.push((list = h("ul", {}))); list.append(h("li", {}, inline(line.replace(/^[-*•]\s+/, "")))); continue; }
    list = null;
    if (line) out.push(h("p", {}, inline(line.replace(/^#+\s*/, ""))));
  }
  return out;
}

async function assistantView() {
  const status = await api("/chat/status");
  if (!status.enabled) {
    return h("section", { class: "card" }, h("h2", {}, "Assistant"),
      h("p", { class: "notice" }, "The assistant needs an Anthropic API key. Create one at console.anthropic.com, set it as the ANTHROPIC_API_KEY environment variable where QuantLab runs, and restart. Questions are billed to that key."));
  }
  const log = h("div", { class: "chat-log", "aria-live": "polite" });
  const input = h("textarea", { rows: 2, placeholder: `Ask about ${state.symbol}, your watchlist, an option, or your paper account…`, "aria-label": "Your question" });
  const send = h("button", { type: "submit", class: "primary" }, "Send");
  const examples = [`What's the signal on ${state.symbol} and what is driving it?`, "Which names on my watchlist look strongest right now?",
    `What move are options pricing in for ${state.symbol} over the next month?`, "How is my paper account doing?"];
  function draw() {
    fill(log, chat.messages.length ? chat.messages.map((m) => h("div", { class: "msg " + m.role },
        h("div", { class: "who" }, m.role === "user" ? "You" : "Assistant"),
        m.role === "user" ? h("p", {}, m.content) : renderReply(m.content),
        m.tools && m.tools.length ? h("div", { class: "muted small" }, "Looked up: " + [...new Set(m.tools)].map((t) => t.replace(/^get_/, "").replace(/_/g, " ")).join(", ")) : null,
        m.error ? h("p", { class: "error" }, m.error) : null))
      : h("div", { class: "stack" }, h("p", { class: "muted" }, "Ask a question in plain English. The assistant looks up live prices, signals, news, option chains and your paper account to answer. It cannot place trades."),
          h("div", { class: "row" }, examples.map((q) => h("button", { type: "button", onclick: () => ask(q) }, q)))),
      chat.busy ? h("div", { class: "msg assistant muted" }, "Looking things up…") : null);
    log.scrollTop = log.scrollHeight;
    send.disabled = chat.busy;
  }
  async function ask(text) {
    text = text.trim();
    if (!text || chat.busy) return;
    chat.messages.push({ role: "user", content: text }); chat.busy = true; input.value = ""; draw();
    try {
      const sendable = chat.messages.filter((m) => !m.error).map(({ role, content }) => ({ role, content }));
      const r = await api("/chat", { body: { messages: sendable, viewing: state.symbol } });
      chat.messages.push({ role: "assistant", content: r.reply, tools: r.tools_used });
    } catch (ex) { chat.messages.at(-1).error = ex.message + " Your question was not answered; send it again to retry."; }
    chat.busy = false; draw();
  }
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input.value); } });
  draw();
  onMount(() => input.focus());
  return h("section", { class: "card chat" },
    h("div", { class: "row between" }, h("h2", {}, "Assistant"),
      h("button", { onclick: () => { chat.messages = []; draw(); } }, "Clear conversation")),
    log, h("form", { class: "chat-form", onsubmit: (e) => { e.preventDefault(); ask(input.value); } }, input, send),
    h("p", { class: "muted small" }, `Answers come from Claude (${status.model}) using this app's delayed data${status.web_search ? " and web search" : ""}. They are research help, not financial advice, and each question is billed to your Anthropic API key.`));
}

const VIEWS = { dashboard: dashboardView, ideas: ideasView, portfolio: portfolioView, research: researchView, options: optionsView, backtest: backtestView, paper: paperView, holdings: holdingsView, assistant: assistantView };

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
