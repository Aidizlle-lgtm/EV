// F.R.I.D.A.Y.'s workspace: her own screen inside E.V. (route #/friday).
//
// Layout follows the user's design reference. Every indicator shows real backend state:
//   * model status  ← /api/agents/friday/status (configured / tested / error / cooling down / working)
//   * market data   ← the market-data source and its labels (TEST DATA today)
//   * voice         ← not connected yet, so Listening/Speaking and the mic stay disabled
// Panels the design shows for features that don't exist yet (paper account, portfolio, trade journal,
// alerts, J.A.R.V.I.S. hand-off) are kept in place but say "Not built yet" instead of showing numbers.
import { api, post, put, streamPost } from "./api.js";
import { h, icon, toast, markdown, relTime, fmtDate } from "./ui.js";

const AGENT = "friday";
const PORTRAIT = "/static/assets/friday/portrait.webp";
const NAV = [
  { id: "overview", label: "Overview", icon: "candles" },
  { id: "research", label: "Company Research", icon: "search" },
  { id: "portfolio", label: "Portfolio", icon: "pie", soon: true },
  { id: "paper", label: "Paper Trading", icon: "flask", soon: true },
  { id: "journal", label: "Trade Journal", icon: "journal", soon: true },
  { id: "alerts", label: "Alerts", icon: "bell", soon: true },
];
const RANGES = ["1m", "3m", "6m", "1y", "5y"];
const DATA_TAG = {
  TEST_DATA: ["TEST DATA", "warn"], LIVE: ["LIVE", "ok"], DELAYED: ["DELAYED", "warn"], CACHED: ["CACHED", "warn"],
  END_OF_DAY: ["END OF DAY", "warn"], MIXED: ["MIXED FRESHNESS", "warn"], NONE: ["NO MARKET DATA", "warn"],
};
const MODEL_STATE = {
  connected: ["ok", "Ready"], configured: ["warn", "Configured — not tested yet"], cooling_down: ["warn", "Cooling down"],
  setup_required: ["err", "Setup required"], error: ["err", "Model error"],
};

let F = null;   // state of the open workspace

export function renderFriday(root, { query = {} } = {}) {
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(`friday.${k}`); localStorage.setItem(`friday.${k}`, v); } catch { /* storage off */ } return null; };
  F = {
    root, store, status: null, section: NAV.some((n) => n.id === query.s) ? query.s : (store("section") || "research"),
    symbol: (query.sym || store("symbol") || "MSFT").toUpperCase(), range: store("range") || "1m",
    convId: query.c ? Number(query.c) : null, chat: { busy: false, runId: null, ctrl: null, bubble: null, text: "" },
    analysis: { busy: false, runId: null, ctrl: null, steps: [] }, watchlist: [], timers: [], els: {},
  };
  root.classList.remove("hidden");
  root.replaceChildren(header(), F.els.banner = h("div", { class: "f-banner" }, "Checking market-data source…"),
    h("div", { class: "f-body" }, nav(), F.els.main = h("main", { class: "f-main", tabindex: "-1" }), side()));
  showSection(F.section);
  refreshStatus();
  loadConversation(F.convId);
  tickClock();
  F.timers.push(setInterval(tickClock, 1000 * 15), setInterval(refreshStatus, 10000));
  const onKey = (e) => { if (e.key === "Escape") F?.els.pop?.remove(); };
  document.addEventListener("keydown", onKey);
  return () => {
    F.timers.forEach(clearInterval);
    document.removeEventListener("keydown", onKey);
    root.classList.add("hidden");
    root.replaceChildren();
    F = null;
  };
}

// ------------------------------------------------------------------ header, nav, clock, status
function header() {
  const input = h("input", { type: "search", placeholder: "Search ticker (e.g. MSFT)…", "aria-label": "Ticker symbol", maxlength: 10 });
  F.els.aiPill = h("button", { class: "f-pill", onclick: () => toggleModelPop(), "aria-label": "F.R.I.D.A.Y.'s AI model" }, h("span", { class: "f-dot" }), "AI: …");
  F.els.mdPill = h("span", { class: "f-pill", title: "Market-data source (separate from AI keys)" }, icon("chart"), h("span", { class: "k" }, "MARKET DATA:"), "…");
  F.els.clock = h("div", { class: "f-clock", "aria-live": "off" });
  return h("header", { class: "f-top" },
    h("div", { class: "f-brand" }, h("b", {}, "F.R.I.D.A.Y."), h("span", {}, "E.V. / FINANCIAL INTELLIGENCE")),
    h("form", { class: "f-search", onsubmit: (e) => { e.preventDefault(); const v = input.value.trim().toUpperCase(); if (v) { setSymbol(v); input.value = ""; } } }, icon("search"), input),
    F.els.aiPill, F.els.mdPill,
    h("span", { class: "f-pill gold", title: "Paper trading isn't built yet; F.R.I.D.A.Y. does research only." }, icon("doc"), "RESEARCH", h("span", { class: "k" }, "/ PAPER: NOT BUILT")),
    F.els.clock,
    h("button", { class: "f-icon-btn", "aria-label": "Model settings", onclick: () => toggleModelPop() }, icon("gear")));
}

function nav() {
  F.els.nav = h("nav", { class: "f-nav", "aria-label": "F.R.I.D.A.Y. sections" },
    NAV.map((n) => h("a", { href: `#/friday?s=${n.id}`, "data-s": n.id, onclick: (e) => { e.preventDefault(); showSection(n.id); } },
      icon(n.icon), h("span", { class: "lbl" }, n.label), n.soon ? h("span", { class: "soon" }, "SOON") : null)),
    h("div", { class: "spacer" }),
    h("a", { class: "f-hub", href: "#/dashboard", title: "Back to E.V.'s hub" }, h("img", { src: "/static/assets/ev-logo.webp", alt: "" }), h("b", {}, "E.V. HUB")),
    h("button", { class: "f-hub off", disabled: true, title: "J.A.R.V.I.S. isn't built yet" }, h("div", { class: "shield" }, "J"), h("b", {}, "J.A.R.V.I.S."), h("span", { class: "f-sub" }, "Not built yet")));
  return F.els.nav;
}

function tickClock() {
  if (!F) return;
  const now = new Date();
  const d = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", day: "numeric", month: "short", year: "numeric" }).format(now);
  const t = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" }).format(now);
  F.els.clock.replaceChildren(h("span", {}, d), h("b", {}, `${t} ET`));
}

async function refreshStatus() {
  if (!F) return;
  try { F.status = await api(`/api/agents/${AGENT}/status`); } catch (e) { F.status = null; }
  paintStatus();
}

function working() { return !!(F && (F.chat.busy || F.analysis.busy || F.status?.working)); }

function paintStatus() {
  if (!F) return;
  const st = F.status;
  const m = st?.model;
  const [tone, label] = working() ? ["busy", "Working"] : m ? (MODEL_STATE[m.state] || ["warn", m.state]) : ["err", "Unavailable"];
  F.els.aiPill.replaceChildren(h("span", { class: `f-dot ${tone}` }), h("span", { class: "k" }, "AI:"),
    m ? `${m.label} · ${m.model || "no model"}` : "status unavailable");
  F.els.aiPill.title = m ? `${label}. ${m.detail || ""}` : "Couldn't load status";
  const md = st?.market_data;
  const [mdText] = md ? (md.source === "none" ? ["OFF"] : DATA_TAG[md.data_status] || [md.data_status]) : ["…"];
  F.els.mdPill.replaceChildren(icon("chart"), h("span", { class: "k" }, "MARKET DATA:"), mdText);
  if (md) {
    const live = md.is_live && md.data_status === "LIVE";
    F.els.banner.className = `f-banner${live ? " live" : ""}`;
    F.els.banner.textContent = md.source === "none" ? "Market data is off  •  Research only — no orders"
      : md.data_status === "TEST_DATA" ? "Test data — synthetic numbers, not real market information  •  Research only — no orders"
      : `${(DATA_TAG[md.data_status] || [md.data_status])[0]} data from ${md.label}  •  Research only — no orders`;
  }
  if (F.els.sideDot) {
    F.els.sideDot.className = `f-dot ${tone}`;
    F.els.sideState.textContent = label;
    F.els.sideState.title = m?.detail || "";
  }
  if (F.els.modelLine && m) {
    const fb = (m.fallbacks || []).map((f) => `${f.label} · ${f.model || "default"}`).join(", ");
    F.els.modelLine.textContent = `Thinking with ${m.label} · ${m.model || "no model"}${st.inherits_ev_model ? " (same as E.V.)" : ""}${fb ? ` — fallback: ${fb}` : ""}`;
  }
  paintPresence();
}

// ------------------------------------------------------------------ sections
function showSection(id) {
  F.section = id;
  F.store("section", id);
  F.els.nav.querySelectorAll("a[data-s]").forEach((a) => a.classList.toggle("active", a.dataset.s === id));
  F.els.main.replaceChildren();
  if (id === "research") renderResearch();
  else if (id === "overview") renderOverview();
  else renderSoon(id);
  F.els.main.scrollTop = 0;
}

function setSymbol(sym) {
  F.symbol = sym;
  F.store("symbol", sym);
  if (F.section !== "research") showSection("research"); else renderResearch();
}

function renderSoon(id) {
  const n = NAV.find((x) => x.id === id);
  const needs = {
    portfolio: ["Holdings with cost basis, cash, and buying power", "A live or delayed price source to value positions", "Clear separation of paper and real-account records"],
    paper: ["A deterministic paper-trade simulator (fills, fees, slippage, gaps)", "Trade records with IDs, thesis, entry/exit logic", "Paper results kept apart from any real budget"],
    journal: ["Stored trade decisions and outcomes", "Strategy versions, so losing trades are never rewritten"],
    alerts: ["A live data feed and a scheduler", "Alert thresholds you approve", "A delivery route (E.V. notification)"],
  }[id] || [];
  F.els.main.append(h("div", { class: "f-panel f-placeholder" },
    h("h2", { class: "f-title" }, n.label), h("p", {}, h("span", { class: "f-tag off" }, "NOT BUILT YET"), "  This screen is part of your design but isn't built in this milestone, so it shows no numbers."),
    h("p", { class: "f-sub" }, "It will need:"), h("ul", {}, needs.map((x) => h("li", {}, x)))));
}

async function renderOverview() {
  const main = F.els.main;
  main.append(h("div", { class: "f-head" }, h("h1", {}, "Overview")), h("div", { class: "f-empty" }, "Loading…"));
  await refreshStatus();
  const st = F.status;
  if (!st || F.section !== "overview") return;
  const m = st.model;
  const reports = await api(`/api/agents/${AGENT}/reports?limit=8`).catch(() => []);
  main.replaceChildren(h("div", { class: "f-head" }, h("h1", {}, "Overview")),
    h("div", { class: "f-three" },
      h("div", {}, h("h4", {}, "AI model"), h("div", {}, h("span", { class: `f-dot ${(MODEL_STATE[m.state] || ["warn"])[0]}`, style: { display: "inline-block", marginRight: "6px" } }), `${m.label} · ${m.model || "no model"}`),
        h("div", { class: "f-sub" }, m.detail || ""), h("button", { class: "f-btn", style: { marginTop: "8px" }, onclick: () => toggleModelPop() }, icon("gear"), "Change model")),
      h("div", {}, h("h4", {}, "Market data"), h("div", {}, st.market_data.label), dataTagEl(st.market_data.source === "none" ? "NONE" : st.market_data.data_status),
        h("div", { class: "f-sub", style: { marginTop: "6px" } }, "Live sources that could be added: ", st.market_data.options.map((o) => o.name).join(", "))),
      h("div", {}, h("h4", {}, "Personality prompt"), h("div", {}, h("code", {}, st.prompt?.file || "—")), h("div", { class: "f-sub" }, st.prompt?.detail || ""))),
    h("div", { class: "f-three" },
      h("div", {}, h("h4", {}, "Permissions (enforced in code)"), h("ul", {}, h("li", {}, `Action level: ${st.limits.max_level} only — no trades, writes, or sends`),
        h("li", {}, `${st.limits.max_steps} steps and ${Math.round(st.limits.timeout_s)} s per task`), h("li", {}, `Tools: ${(st.tools || []).join(", ")}`))),
      h("div", {}, h("h4", {}, "Voice"), h("div", {}, h("span", { class: "f-tag off" }, "NOT CONNECTED")), h("div", { class: "f-sub", style: { marginTop: "6px" } }, st.voice.reason || ""),
        st.voice.intended_style ? h("div", { class: "f-sub", style: { marginTop: "4px" } }, `Planned style: ${st.voice.intended_style}`) : null),
      h("div", {}, h("h4", {}, "Paper trading"), h("span", { class: "f-tag off" }, "NOT BUILT"), h("div", { class: "f-sub", style: { marginTop: "6px" } }, st.paper_trading.detail))),
    h("div", { class: "f-panel" }, h("h2", { class: "f-title" }, "Recent reports"),
      reports.length ? h("div", { class: "f-watch" }, h("table", {}, h("tr", {}, h("th", {}, "Symbol"), h("th", {}, "Requested by"), h("th", {}, "Data"), h("th", {}, "When")),
        reports.map((r) => h("tr", { onclick: () => { F.reportId = r.id; setSymbol(r.symbol || F.symbol); } },
          h("td", {}, r.symbol || "—"), h("td", {}, r.requested_by === "ev" ? "E.V." : "You"), h("td", {}, dataTagEl(r.data_status)), h("td", {}, relTime(r.created_at))))))
        : h("div", { class: "f-empty" }, "No reports yet. Run “Analyze company”, or ask E.V. to have F.R.I.D.A.Y. research something.")));
}

// ------------------------------------------------------------------ company research
async function renderResearch() {
  const sym = F.symbol;
  const main = F.els.main;
  F.els.snap = h("span", { class: "f-sub" });
  F.els.cards = h("div", { class: "f-cards" }, [0, 1, 2, 3].map(() => h("div", { class: "f-card" }, h("div", { class: "k" }, "…"))));
  F.els.chart = h("div", { class: "f-panel f-chart" }, h("div", { class: "f-empty" }, "Loading price history…"));
  F.els.watch = h("div", { class: "f-panel f-watch" });
  F.els.thesis = h("div", { class: "f-panel f-thesis" }, h("div", { class: "f-empty" }, "Loading reports…"));
  F.els.actions = h("div", { class: "f-actions" });
  F.els.handoffs = h("div", { class: "f-handoffs" });
  main.replaceChildren(
    h("div", { class: "f-head" }, h("h1", {}, "Company Research"),
      F.els.symSel = h("div", { class: "f-select" }, h("b", {}, sym), h("span", { class: "f-sub" }, "…")), h("span", { class: "grow", style: { flex: 1 } }), F.els.snap),
    F.els.cards, h("div", { class: "f-grid2" }, F.els.chart, F.els.watch), F.els.thesis, F.els.actions, F.els.handoffs);
  paintActions();
  loadWatchlist();
  loadHandoffs();
  const [quote, overview] = await Promise.all([market("quote", sym), market("overview", sym)]);
  if (!F || F.symbol !== sym || F.section !== "research") return;
  paintCards(quote, overview);
  loadChart();
  loadReport();
}

async function market(kind, sym, params = "") {
  try { return await api(`/api/market/${kind}/${encodeURIComponent(sym)}${params}`); } catch (e) { return { error: e.message }; }
}

function dataTagEl(status) {
  const [txt, tone] = DATA_TAG[status] || [status || "—", "off"];
  return h("span", { class: `f-tag ${tone}` }, txt);
}

const money = (v, cur = "USD") => (typeof v === "number" ? v.toLocaleString("en-US", { style: "currency", currency: cur, maximumFractionDigits: 2 }) : "—");
const pct = (v) => (typeof v === "number" ? `${v > 0 ? "+" : ""}${v.toFixed(2)}%` : "—");
const big = (v) => (typeof v === "number" ? (v >= 1e12 ? `$${(v / 1e12).toFixed(2)}T` : v >= 1e9 ? `$${(v / 1e9).toFixed(2)}B` : v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : money(v)) : "—");
const tone = (v) => (typeof v === "number" ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");

function paintCards(quote, overview) {
  const err = quote.error || overview.error;
  const q = quote.data || {};
  const o = overview.data || {};
  const prov = quote.provenance;
  F.els.symSel.replaceChildren(h("b", {}, F.symbol), h("span", { class: "f-sub" }, o.name || (err ? "unavailable" : "")));
  F.els.snap.replaceChildren(prov ? `Price observed ${fmtDate(prov.as_of)} · fetched ${relTime(prov.retrieved_at)} ` : "", prov ? dataTagEl(prov.data_status) : null);
  if (err) {
    F.els.cards.replaceChildren(h("div", { class: "f-card", style: { gridColumn: "1 / -1" } }, h("div", { class: "k" }, "Market data"), h("div", { class: "s" }, err)));
    return;
  }
  const card = (k, v, s, cls = "") => h("div", { class: "f-card" }, h("div", { class: "k" }, k), h("div", { class: `v ${cls}` }, v), h("div", { class: "s" }, s));
  F.els.cards.replaceChildren(
    card("Last price", money(q.price, q.currency), [dataTagEl(prov.data_status), ` ${q.date || ""}`]),
    card("Day change", `${q.change > 0 ? "+" : ""}${money(q.change, q.currency)}`, pct(q.change_percent), tone(q.change)),
    card("52-week range", `${money(o.week52_low)} – ${money(o.week52_high)}`, "low – high"),
    card("Market cap", big(o.market_cap_usd), o.pe_ratio ? `P/E ${o.pe_ratio}` : "P/E n/a"));
}

async function loadChart() {
  const sym = F.symbol;
  const hist = await market("history", sym, `?range=${F.range}`);
  if (!F || F.symbol !== sym || !F.els.chart.isConnected) return;
  const ranges = h("div", { class: "f-ranges", role: "group", "aria-label": "Range" },
    RANGES.map((r) => h("button", { class: r === F.range ? "on" : "", onclick: () => { F.range = r; F.store("range", r); loadChart(); } }, r.toUpperCase())));
  if (hist.error) { F.els.chart.replaceChildren(h("div", { class: "f-row" }, h("b", {}, sym), h("span", { class: "grow" }), ranges), h("div", { class: "f-empty" }, hist.error)); return; }
  const d = hist.data;
  F.els.chart.replaceChildren(
    h("div", { class: "f-row" }, h("b", { class: "f-h", style: { fontSize: "16px" } }, sym), h("b", {}, money(d.end.close)),
      h("span", { class: tone(d.change_percent) }, `${pct(d.change_percent)} over ${d.range}`), dataTagEl(hist.provenance.data_status), h("span", { class: "grow" }), ranges),
    lineChart(d.closes, hist.provenance.data_status === "TEST_DATA"),
    h("div", { class: "f-sub" }, `Daily closes (${d.interval}). Candles and volume need OHLC data the current source doesn't provide.`));
}

function lineChart(points, test) {
  const W = 760, H = 220, pad = { l: 8, r: 46, t: 10, b: 22 };
  const ys = points.map((p) => p.close);
  const min = Math.min(...ys), max = Math.max(...ys), span = max - min || 1;
  const x = (i) => pad.l + (i / Math.max(1, points.length - 1)) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - min) / span) * (H - pad.t - pad.b);
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.close).toFixed(1)}`).join("");
  const area = `${line}L${x(points.length - 1).toFixed(1)},${H - pad.b}L${x(0).toFixed(1)},${H - pad.b}Z`;
  const up = ys[ys.length - 1] >= ys[0];
  const ticks = [max, (max + min) / 2, min];
  const labels = [0, Math.floor(points.length / 2), points.length - 1];
  const svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Closing prices">
    <defs><linearGradient id="fg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#f0a73a" stop-opacity="0.35"/><stop offset="1" stop-color="#f0a73a" stop-opacity="0"/></linearGradient></defs>
    ${ticks.map((t) => `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(t)}" y2="${y(t)}" stroke="rgba(232,163,61,0.15)"/><text x="${W - pad.r + 4}" y="${y(t) + 4}" fill="#a99c86" font-size="11">${t.toFixed(2)}</text>`).join("")}
    <path d="${area}" fill="url(#fg)"/><path d="${line}" fill="none" stroke="${up ? "#ffc766" : "#ff8a5c"}" stroke-width="2" vector-effect="non-scaling-stroke"/>
    ${labels.map((i) => `<text x="${x(i)}" y="${H - 6}" fill="#a99c86" font-size="11" text-anchor="${i === 0 ? "start" : i === points.length - 1 ? "end" : "middle"}">${points[i].date}</text>`).join("")}
    ${test ? `<text x="${(W - pad.r) / 2}" y="${H / 2}" fill="rgba(255,181,71,0.18)" font-size="34" font-weight="700" text-anchor="middle" letter-spacing="6">TEST DATA</text>` : ""}
  </svg>`;
  return h("div", { html: svg });
}

// ------------------------------------------------------------------ watchlist
async function loadWatchlist() {
  try { F.watchlist = (await api("/api/market/watchlist")).symbols; } catch { F.watchlist = []; }
  paintWatchlist();
  paintActions();
}

async function saveWatchlist(list) {
  try { F.watchlist = (await put("/api/market/watchlist", { symbols: list })).symbols; } catch (e) { toast(e.message, "err"); }
  paintWatchlist();
  paintActions();
}

async function paintWatchlist() {
  if (!F?.els.watch?.isConnected) return;
  const add = h("button", { class: "f-icon-btn", "aria-label": `Add ${F.symbol} to watchlist`, title: `Add ${F.symbol}`, onclick: () => !F.watchlist.includes(F.symbol) && saveWatchlist([...F.watchlist, F.symbol]) }, icon("plus"));
  const headRow = h("div", { class: "f-row", style: { marginBottom: "6px" } }, h("h2", { class: "f-title" }, "Watchlist"), h("span", { class: "grow" }), add);
  if (!F.watchlist.length) { F.els.watch.replaceChildren(headRow, h("div", { class: "f-empty" }, "Empty. Use + to add the symbol you're viewing.")); return; }
  const quotes = await Promise.all(F.watchlist.map((s) => market("quote", s)));
  if (!F?.els.watch?.isConnected) return;
  F.els.watch.replaceChildren(headRow, h("table", {}, h("tr", {}, h("th", {}, "Symbol"), h("th", {}, "Price"), h("th", {}, "Change")),
    F.watchlist.map((s, i) => {
      const q = quotes[i].data || {};
      return h("tr", { class: s === F.symbol ? "sel" : "", onclick: () => setSymbol(s) },
        h("td", {}, h("b", {}, s)), h("td", {}, quotes[i].error ? "—" : (q.price ?? "—").toFixed?.(2) ?? "—"),
        h("td", { class: tone(q.change_percent) }, quotes[i].error ? "n/a" : pct(q.change_percent),
          h("button", { class: "f-x", "aria-label": `Remove ${s}`, onclick: (e) => { e.stopPropagation(); saveWatchlist(F.watchlist.filter((x) => x !== s)); } }, "×")));
    })),
    quotes[0]?.provenance ? h("div", { class: "f-sub", style: { marginTop: "6px" } }, dataTagEl(quotes[0].provenance.data_status), " prices") : null);
}

// ------------------------------------------------------------------ reports (Analyze company)
async function loadReport() {
  const sym = F.symbol;
  let rep = null;
  try {
    const list = await api(`/api/agents/${AGENT}/reports?symbol=${encodeURIComponent(sym)}&limit=1`);
    const id = F.reportId || list[0]?.id;
    F.reportId = null;
    if (id) rep = await api(`/api/agents/${AGENT}/reports/${id}`);
  } catch (e) { /* shown as empty */ }
  if (!F || F.symbol !== sym || !F.els.thesis?.isConnected || F.analysis.busy) return;
  paintReport(rep);
}

function paintReport(r) {
  const el = F.els.thesis;
  const headRow = (extra) => h("div", { class: "f-row" }, h("h2", { class: "f-title" }, "Investment thesis"), h("span", { class: "grow" }),
    h("span", { class: "f-sub" }, "Illustrative scenarios • Not probabilities"), extra);
  if (!r) {
    el.replaceChildren(headRow(), h("div", { class: "f-empty" }, `No report for ${F.symbol} yet. Press “Analyze company” and F.R.I.D.A.Y. will research it with her tools. Nothing here is filled in until she does.`));
    return;
  }
  const rep = r.report || {};
  const scen = { bull: null, base: null, bear: null };
  (rep.scenarios || []).forEach((s) => { scen[s.case] = s; });
  const list = (items) => (items?.length ? h("ul", {}, items.map((x) => h("li", {}, x))) : h("div", { class: "f-empty" }, "—"));
  el.replaceChildren(
    headRow(dataTagEl(r.data_status)),
    h("h2", {}, rep.headline || rep.summary?.split(". ")[0] || "Report"),
    r.data_notice ? h("div", { class: "f-notice", style: { textAlign: "left" } }, r.data_notice) : null,
    h("div", { class: "f-grid2", style: { gridTemplateColumns: "minmax(0,1fr) minmax(0,1.6fr)" } },
      h("div", { class: "f-evid" }, h("h4", {}, "Key evidence"),
        rep.findings?.length ? h("ul", {}, rep.findings.map((f) => h("li", {}, f.point, h("span", { class: "f-basis" }, { tool_data: "tool data", model_knowledge: "model knowledge", inference: "inference" }[f.basis] || f.basis))))
          : h("div", { class: "f-empty" }, "No findings listed.")),
      h("div", { class: "f-scen" }, ["bull", "base", "bear"].map((c) => {
        const s = scen[c];
        return h("div", { class: c }, h("b", {}, c.toUpperCase()), h("div", { class: "f-sub" }, s?.description || "Not provided"),
          s && typeof s.illustrative_change_percent === "number" ? h("div", { class: `pct ${tone(s.illustrative_change_percent)}`, title: "Illustration of the stated assumptions — not a forecast" }, pct(s.illustrative_change_percent).replace(".00", "")) : null,
          s?.assumptions?.length ? h("ul", {}, s.assumptions.map((a) => h("li", {}, a))) : null);
      }))),
    h("div", { class: "f-three" },
      h("div", {}, h("h4", {}, "Key risks"), list(rep.risks)),
      h("div", {}, h("h4", {}, "Action plan"), list(rep.action_plan)),
      h("div", {}, h("h4", {}, "Short summary"), h("div", {}, rep.summary || "—"), h("div", { class: "f-sub", style: { marginTop: "6px" } }, `Confidence: ${rep.confidence || "—"}`))),
    h("div", { class: "f-sources" }, h("b", { class: "f-h", style: { fontSize: "11px" } }, "Sources"),
      (r.provenance || []).length ? r.provenance.map((p) => h("span", {}, `${p.tool} ${p.symbol || ""}: ${p.source} — observed ${fmtDate(p.as_of)}, fetched ${fmtDate(p.retrieved_at)} `, dataTagEl(p.data_status)))
        : h("span", {}, "No market data was retrieved for this report."),
      h("span", {}, `Report ${fmtDate(r.created_at)} · ${r.requested_by === "ev" ? "requested by E.V." : "requested by you"} · ${r.model?.connection || ""} ${r.model?.model || ""}${r.status !== "completed" ? ` · ${r.status_detail || r.status}` : ""}`)));
}

function paintActions() {
  if (!F?.els.actions?.isConnected) return;
  const a = F.analysis;
  const inList = F.watchlist.includes(F.symbol);
  F.els.actions.replaceChildren(
    a.busy ? h("button", { class: "f-btn stop", onclick: stopAnalysis }, icon("stop"), "Stop analysis")
      : h("button", { class: "f-btn primary", onclick: analyze, disabled: F.chat.busy }, icon("search"), "Analyze company"),
    h("button", { class: "f-btn", disabled: F.chat.busy || a.busy, onclick: () => sendChat(`Compare the bull, base, and bear scenarios for ${F.symbol}. Make the assumptions explicit and tell me what would invalidate each.`) }, icon("chart"), "Compare scenarios"),
    h("button", { class: "f-btn", disabled: true, title: "Paper trading isn't built yet" }, icon("flask"), "Simulate trade"),
    h("button", { class: "f-btn", onclick: () => saveWatchlist(inList ? F.watchlist.filter((x) => x !== F.symbol) : [...F.watchlist, F.symbol]) }, icon(inList ? "check" : "plus"), inList ? "On watchlist" : "Add to watchlist"));
}

async function analyze() {
  const a = F.analysis;
  if (a.busy) return;
  const sym = F.symbol;
  a.busy = true; a.runId = null; a.steps = []; a.ctrl = new AbortController();
  paintActions(); paintStatus();
  const prog = h("ul", { class: "f-progress" });
  F.els.thesis.replaceChildren(h("div", { class: "f-row" }, h("h2", { class: "f-title" }, "Investment thesis"), h("span", { class: "grow" }), h("span", { class: "f-sub" }, `F.R.I.D.A.Y. is researching ${sym}…`)), prog);
  let final = null;
  try {
    await streamPost(`/api/agents/${AGENT}/analyze`, { symbol: sym }, (ev) => {
      if (ev.type === "run") a.runId = ev.run_id;
      else if (ev.type === "agent_progress") {
        if (ev.event === "tool") prog.append(h("li", { "data-tool": ev.name }, h("code", {}, ev.name), ` ${ev.args?.symbol || ""} ${ev.args?.range || ""} …`));
        else if (ev.event === "tool_done") { const li = [...prog.querySelectorAll(`li[data-tool="${ev.name}"]`)].find((x) => !x.dataset.done); if (li) { li.dataset.done = "1"; li.lastChild.textContent += ev.status === "ok" ? " ✓" : ` ${ev.status}`; } }
        else if (ev.event === "fallback") prog.append(h("li", {}, `${ev.from} unavailable — using a fallback connection`));
        else if (ev.stage === "started") prog.append(h("li", {}, "Started"));
      } else if (ev.type === "report") final = ev.report;
      else if (ev.type === "cancelled") prog.append(h("li", {}, "Stopped."));
      else if (ev.type === "error") prog.append(h("li", { class: "down" }, ev.message));
    }, a.ctrl.signal);
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, "err");
  } finally {
    a.busy = false; a.runId = null;
    if (F) {
      paintActions(); paintStatus();
      if (final && F.symbol === sym && F.els.thesis?.isConnected) paintReport(final);
      loadHandoffs();
    }
  }
}

async function stopAnalysis() {
  const a = F.analysis;
  stopSpeech();
  const ctrl = a.ctrl;   // abort only THIS request, never one started after Stop
  if (a.runId) { try { await post(`/api/runs/${a.runId}/cancel`); } catch { /* fall through */ } }
  setTimeout(() => ctrl?.abort(), 1500);
}

async function loadHandoffs() {
  if (!F?.els.handoffs?.isConnected) return;
  const reps = await api(`/api/agents/${AGENT}/reports?limit=20`).catch(() => []);
  const toEv = reps.find((r) => r.requested_by === "ev");
  if (!F?.els.handoffs?.isConnected) return;
  F.els.handoffs.replaceChildren(
    h("div", { class: "f-handoff", style: toEv ? { cursor: "pointer" } : {}, onclick: toEv ? () => { F.reportId = toEv.id; setSymbol(toEv.symbol || F.symbol); } : null },
      h("div", { class: "ico" }, "EV"),
      h("div", { style: { minWidth: 0, flex: 1 } }, h("div", { class: "k" }, "To E.V. • latest report"),
        h("b", {}, toEv ? `${toEv.symbol || "Report"} — ${toEv.status}` : "No reports sent to E.V. yet"),
        h("div", { class: "f-sub" }, toEv ? `${fmtDate(toEv.created_at)} · delivered to E.V. in her conversation` : "Appears when E.V. delegates research to F.R.I.D.A.Y.")),
      toEv ? dataTagEl(toEv.data_status) : null),
    h("div", { class: "f-handoff off", title: "J.A.R.V.I.S. isn't built yet" }, h("div", { class: "ico" }, "J"),
      h("div", { style: { minWidth: 0, flex: 1 } }, h("div", { class: "k" }, "To J.A.R.V.I.S."), h("b", {}, "Budget hand-offs"),
        h("div", { class: "f-sub" }, "Not built yet — no hand-offs are sent.")), h("span", { class: "f-tag off" }, "NOT BUILT")));
}

// ------------------------------------------------------------------ chat with F.R.I.D.A.Y.
function side() {
  F.els.sideDot = h("span", { class: "f-dot" });
  F.els.sideState = h("span", { class: "f-sub" }, "…");
  F.els.log = h("div", { class: "f-log", "aria-live": "polite" });
  F.els.input = h("textarea", { rows: 1, placeholder: "Ask F.R.I.D.A.Y.…", "aria-label": "Message F.R.I.D.A.Y.", maxlength: 20000 });
  F.els.send = h("button", { class: "f-send", type: "submit", "aria-label": "Send" }, icon("send"));
  F.els.input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); F.els.form.requestSubmit(); } });
  F.els.input.addEventListener("input", () => { const t = F.els.input; t.style.height = "auto"; t.style.height = `${Math.min(140, t.scrollHeight)}px`; });
  F.els.form = h("form", { class: "f-compose", onsubmit: (e) => { e.preventDefault(); if (F.chat.busy) { if (e.submitter === F.els.send) stopChat(); } else sendChat(F.els.input.value); } },
    F.els.input, F.els.send);
  F.els.quick = h("div", { class: "f-quick" });
  F.els.presence = h("div", { class: "f-presence" });
  F.els.modelLine = h("div", { class: "f-model-line" }, "…");
  const sideEl = h("aside", { class: "f-side", style: { position: "relative" } },
    h("div", { class: "f-side-head" }, h("h2", { class: "f-title" }, "Ask F.R.I.D.A.Y."), F.els.sideDot, F.els.sideState, h("span", { style: { flex: 1 } }),
      h("button", { class: "f-icon-btn", "aria-label": "Her conversations", onclick: toggleHistory }, icon("history")),
      h("button", { class: "f-icon-btn", "aria-label": "New conversation", onclick: newConversation }, icon("plus"))),
    F.els.modelLine, F.els.log, F.els.quick, F.els.form, F.els.presence);
  F.els.side = sideEl;
  paintQuick();
  return sideEl;
}

function paintQuick() {
  const busy = F.chat.busy;
  F.els.quick.replaceChildren(
    h("button", { disabled: busy, onclick: () => sendChat(`Explain the main risks for ${F.symbol} and what evidence would change your view.`) }, "Explain risk"),
    h("button", { disabled: busy, onclick: () => sendChat(`Compare bull, base, and bear scenarios for ${F.symbol} with explicit assumptions.`) }, "Compare scenarios"),
    h("button", { disabled: busy, onclick: () => sendChat(`What data are you working with right now, and how fresh is it?`) }, "What data do you have?"));
}

function paintPresence() {
  if (!F?.els.presence) return;
  const v = F.status?.voice;
  const busy = working();
  const st = (name, ic, on, off) => h("div", { class: `f-state${on ? " on" : ""}${off ? " off" : ""}`, title: off ? "Voice not connected" : "" }, icon(ic), name);
  F.els.presence.replaceChildren(
    h("div", { class: "f-portrait" }, h("img", { src: PORTRAIT, alt: "F.R.I.D.A.Y." }), h("span", {}, "F.R.I.D.A.Y.")),
    h("div", { class: "f-states" },
      st("Idle", "system", !busy, false), st("Analyzing", "chart", busy, false),
      st("Listening", "mic", false, true), st("Speaking", "speaker", false, true),
      h("div", { class: "f-mic", title: v?.intended_style || "" }, icon("mic"), v?.available ? "Voice ready" : "Voice not connected")));
}

const fAvatar = () => h("img", { src: PORTRAIT, alt: "" });

function bubble(role, content, status = "complete") {
  if (role === "user") return h("div", { class: "f-msg user" }, h("div", { class: "f-bubble" }, content));
  const b = h("div", { class: "f-bubble" });
  b.innerHTML = markdown(content);
  return h("div", { class: `f-msg${status === "error" ? " error" : ""}` }, fAvatar(), h("div", { style: { minWidth: 0 } }, h("div", { class: "who" }, "F.R.I.D.A.Y."), b));
}

const toolChip = (name, status, summary) => h("div", { class: `f-chip${["ok", "running"].includes(status) ? "" : " err"}` }, h("code", {}, name), " ", status === "running" ? "running…" : summary || status);

async function loadConversation(id) {
  try {
    if (!id) {
      const list = await api(`/api/conversations?agent=${AGENT}&limit=1`);
      id = list[0]?.id || null;
    }
    if (!id) return emptyChat();
    const conv = await api(`/api/conversations/${id}`);
    if (conv.agent !== AGENT) { toast("That conversation belongs to E.V., not F.R.I.D.A.Y.", "err"); return emptyChat(); }
    F.convId = conv.id;
    F.els.log.replaceChildren(...conv.messages.flatMap((m) => {
      if (m.role === "user") return [bubble("user", m.content)];
      if (m.role === "assistant" && (m.content || m.status === "error")) return [bubble("assistant", m.content, m.status)];
      if (m.role === "tool") return [toolChip(m.name, m.tool_status, m.summary)];
      return [];
    }));
    F.els.log.scrollTop = F.els.log.scrollHeight;
  } catch (e) { toast(e.message, "err"); emptyChat(); }
}

function emptyChat() {
  F.convId = null;
  F.els.log.replaceChildren(h("div", { class: "f-msg" }, fAvatar(), h("div", {}, h("div", { class: "who" }, "F.R.I.D.A.Y."),
    h("div", { class: "f-bubble" }, "This is my own channel — separate from E.V.'s conversations. Ask about a company or a concept; I'll use my market tools and tell you exactly what the data is."))));
}

function newConversation() {
  if (F.chat.busy) return toast("Stop the current reply first.", "err");
  emptyChat();
  F.els.input.focus();
}

async function toggleHistory() {
  const open = F.els.side.querySelector(".f-hist");
  if (open) return open.remove();
  const panel = h("div", { class: "f-hist" }, h("div", { class: "f-empty" }, "Loading…"));
  F.els.side.append(panel);
  try {
    const convs = await api(`/api/conversations?agent=${AGENT}&limit=50`);
    panel.replaceChildren(h("div", { class: "f-row" }, h("b", {}, "F.R.I.D.A.Y.'s conversations"), h("span", { class: "grow" }), h("button", { class: "f-x", onclick: () => panel.remove(), "aria-label": "Close" }, "×")),
      convs.length ? convs.map((c) => h("button", { class: "row", onclick: () => { panel.remove(); if (!F.chat.busy) loadConversation(c.id); } }, c.title, h("span", {}, relTime(c.updated_at))))
        : h("div", { class: "f-empty" }, "None yet."));
  } catch (e) { panel.replaceChildren(h("div", { class: "f-empty" }, e.message)); }
}

function setChatBusy(on) {
  F.chat.busy = on;
  F.els.send.className = `f-send${on ? " stop" : ""}`;
  F.els.send.replaceChildren(icon(on ? "stop" : "send"));
  F.els.send.setAttribute("aria-label", on ? "Stop" : "Send");
  paintQuick(); paintActions(); paintStatus();
}

export async function sendChat(raw) {
  const text = (raw || "").trim();
  if (!F || !text || F.chat.busy) return;
  F.els.input.value = ""; F.els.input.style.height = "auto";
  if (!F.convId) {
    try { F.convId = (await post("/api/conversations", { agent: AGENT })).id; F.els.log.replaceChildren(); } catch (e) { return toast(e.message, "err"); }
  }
  const log = F.els.log;
  const scroll = () => { log.scrollTop = log.scrollHeight; };
  log.append(bubble("user", text)); scroll();
  const c = F.chat;
  c.ctrl = new AbortController(); c.runId = null; c.text = ""; c.bubble = null;
  const chips = {};
  setChatBusy(true);
  const flush = () => { if (c.bubble) { c.bubble.querySelector(".f-bubble").innerHTML = markdown(c.text); scroll(); } };
  try {
    await streamPost(`/api/conversations/${F.convId}/messages`, { text }, (ev) => {
      switch (ev.type) {
        case "run": c.runId = ev.run_id; break;
        case "delta":
          if (!c.bubble) { c.bubble = bubble("assistant", ""); log.append(c.bubble); }
          c.text += ev.text; flush(); break;
        case "tool_start": c.bubble = null; c.text = ""; chips[ev.id] = toolChip(ev.name, "running"); log.append(chips[ev.id]); scroll(); break;
        case "tool_result": { const chip = toolChip(ev.name, ev.status, ev.summary); chips[ev.id]?.replaceWith(chip); chips[ev.id] = chip; break; }
        case "notice": log.append(h("div", { class: "f-notice" }, ev.message)); scroll(); break;
        case "cancelled": log.append(h("div", { class: "f-notice" }, ev.reason === "timeout" ? "Stopped at the time limit." : "Stopped.")); scroll(); break;
        case "error": log.append(bubble("assistant", `**${ev.kind === "setup" ? "Setup required" : "Couldn't complete that"}** — ${ev.message}`, "error")); scroll(); break;
        default: break;
      }
    }, c.ctrl.signal);
  } catch (e) {
    if (e.name !== "AbortError") log.append(bubble("assistant", `**Couldn't complete that** — ${e.message}`, "error"));
  } finally {
    c.runId = null;
    if (F) { setChatBusy(false); refreshStatus(); }
  }
}

async function stopChat() {
  const c = F.chat;
  stopSpeech();
  const ctrl = c.ctrl;   // abort only THIS request, never one started after Stop
  if (c.runId) { try { await post(`/api/runs/${c.runId}/cancel`); } catch { /* fall through to abort */ } }
  setTimeout(() => ctrl?.abort(), 1500);
}

// Voice isn't connected for F.R.I.D.A.Y. yet. Stop already calls this so that, once a voice
// provider exists, pressing Stop also halts speech playback — no other change needed here.
function stopSpeech() { /* no playback exists yet */ }

// ------------------------------------------------------------------ model / connection settings
async function toggleModelPop() {
  if (F.els.pop?.isConnected) { F.els.pop.remove(); return; }
  const pop = h("div", { class: "f-pop", role: "dialog", "aria-label": "F.R.I.D.A.Y.'s model" }, h("div", { class: "f-empty" }, "Loading…"));
  F.els.pop = pop;
  F.root.append(pop);
  try {
    const [{ connections }, routing] = await Promise.all([api("/api/connections"), api(`/api/agents/${AGENT}/routing`)]);
    const opts = (sel, first) => [h("option", { value: "" }, first), ...connections.map((c) => h("option", { value: c.id, selected: c.id === sel }, `${c.name}${c.builtin ? " (built-in)" : ""}`))];
    const conn = h("select", { "aria-label": "Connection" }, opts(routing.connection, "Same as E.V."));
    const dl = h("datalist", { id: "f-models" });
    const model = h("input", { type: "text", value: routing.model, list: "f-models", placeholder: "connection's default model", "aria-label": "Model" });
    const fb = routing.fallbacks[0] || {};
    const fconn = h("select", { "aria-label": "Fallback connection" }, opts(fb.connection, "— none —"));
    const fmodel = h("input", { type: "text", value: fb.model || "", placeholder: "default model", "aria-label": "Fallback model" });
    const fill = () => dl.replaceChildren(...(connections.find((c) => c.id === conn.value)?.models || []).map((m) => h("option", { value: m })));
    conn.onchange = fill; fill();
    pop.replaceChildren(h("h2", { class: "f-title" }, "F.R.I.D.A.Y.'s model"),
      h("p", { class: "f-sub" }, "Used for both direct chat and E.V.'s delegated tasks. Keys are managed in E.V.'s AI Core and never shown here."),
      h("label", {}, "Connection"), conn, h("label", {}, "Model"), model, dl,
      h("label", {}, "Fallback (a different provider is used after a rate limit)"), fconn, fmodel,
      h("div", { class: "f-row", style: { marginTop: "12px" } },
        h("button", { class: "f-btn primary", onclick: async () => {
          const body = { connection: conn.value, model: model.value.trim(), fallbacks: fconn.value ? [{ connection: fconn.value, model: fmodel.value.trim() }] : [] };
          try { await put(`/api/agents/${AGENT}/routing`, body); toast("F.R.I.D.A.Y.'s model saved.", "ok"); pop.remove(); refreshStatus(); } catch (e) { toast(e.message, "err"); }
        } }, icon("check"), "Save"),
        h("a", { class: "f-btn", href: "#/core" }, icon("key"), "Connections in AI Core"),
        h("button", { class: "f-btn", onclick: () => pop.remove() }, "Close")));
  } catch (e) { pop.replaceChildren(h("div", { class: "f-empty" }, e.message)); }
}
