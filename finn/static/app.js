"use strict";
/* FINN UI: vanilla JS, talks only to this server's /api routes. No figure is hardcoded;
   everything rendered comes from a fetch() response and carries its source + time where the source provides one. */
const $ = (s, r = document) => r.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const TABS = [
  ["dashboard", "Dashboard", '<path d="M3 12 12 4l9 8M5 10v10h14V10"/>'],
  ["market", "Market Overview", '<path d="M4 20V10M10 20V4M16 20v-8M22 20H2"/>'],
  ["signals", "Signals", '<path d="M6 8a8 8 0 0 1 12 0M9 11a4 4 0 0 1 6 0M12 15h.01M4 4a12 12 0 0 1 16 0"/>'],
  ["risk", "AI Risk Analyzer", '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/>'],
];
const state = { tab: "dashboard", company: null, chat: [], chatBusy: false, seq: 0 };

/* ---------- formatting (null-safe: absent data shows an explicit dash) ---------- */
const NA = "not reported";
const fnum = (v, d = 2) => (v == null || isNaN(v) ? NA : Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }));
const inr = (v, d = 2) => (v == null ? NA : "₹" + fnum(v, d));
const pct = (v) => (v == null ? NA : (v > 0 ? "+" : "") + fnum(v, 2) + "%");
const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
function mcapCr(v) { // verified server-side: mcap is INR crore
  if (v == null) return NA;
  return v >= 100000 ? "₹" + fnum(v / 100000, 2) + " lakh Cr" : "₹" + fnum(v, 0) + " Cr";
}
function ts(iso) {
  if (!iso) return "time not provided";
  const d = new Date(iso);
  return isNaN(d) ? esc(iso) : d.toLocaleString("en-IN", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }) + " (local)";
}
function srcLabel(u) {
  let h; try { h = new URL(u).hostname.replace(/^www\./, ""); } catch (_) { return null; }
  if (/downstox/i.test(h)) return "Downstox";
  if (/nsearchives|nseindia/i.test(h)) return "NSE archive";
  if (/bseindia/i.test(h)) return "BSE";
  return h;
}
const srcLine = (src, fetched, asof) => {
  const label = /^https?:\/\//.test(src || "") ? srcLabel(src) : null;
  const who = label ? `<a class="src-link" href="${esc(src)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>` : esc(src || "source not stated");
  return `<div class="src">Source: ${who}<span class="src-time">fetched ${ts(fetched)} · data as of ${asof ? ts(asof) : "time not provided"}</span></div>`;
};
const skeleton = () => '<div class="skel lg"></div><div class="skel"></div><div class="skel sm"></div>';
const stateBox = (kind, title, msg) => `<div class="state ${kind}"><b>${esc(title)}</b>${esc(msg)}</div>`;
const card = (title, sub, body, extra = "") => `<section class="card ${extra}"><h2>${esc(title)}</h2>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}${body}</section>`;

async function api(path, opts) {
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (_) { /* non-JSON */ }
  if (!r.ok && !data) throw new Error("HTTP " + r.status);
  return data || { status: "error", error: "HTTP " + r.status };
}
const failed = (e) => ({ status: "error", error: String(e.message || e), fetched_at: new Date().toISOString(), source: "this server" });

/* ---------- navigation ---------- */
function buildNav() {
  document.querySelectorAll("[data-nav]").forEach((n) => {
    n.innerHTML = TABS.map(([k, label, ico]) => `<a href="#${k}" data-tab="${k}" title="${label}" aria-label="${label}"><svg viewBox="0 0 24 24">${ico}</svg><span>${label.replace("Market Overview", "Market").replace("AI Risk Analyzer", "AI Risk")}</span></a>`).join("");
  });
}
function route() {
  const h = location.hash.replace(/^#/, "").split("?")[0];
  state.tab = TABS.some((t) => t[0] === h) ? h : "dashboard";
  document.querySelectorAll("[data-tab]").forEach((a) => {
    const on = a.dataset.tab === state.tab;
    a.classList.toggle("active", on);
    on ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current");
  });
  render();
}
window.addEventListener("hashchange", route);

/* ---------- company picker ---------- */
const input = $("#company-input"), suggest = $("#suggest"), msg = $("#picker-msg");
let sugItems = [], sugIdx = -1, debounce = null, searchSeq = 0;
function closeSuggest() { suggest.hidden = true; input.setAttribute("aria-expanded", "false"); sugIdx = -1; }
function paintSuggest() {
  suggest.innerHTML = sugItems.map((m, i) => `<li role="option" data-i="${i}" aria-selected="${i === sugIdx}"><span class="nm">${esc(m.name)}</span><span class="sy">${esc(m.symbol)}</span></li>`).join("");
  suggest.hidden = !sugItems.length;
  input.setAttribute("aria-expanded", String(!!sugItems.length));
}
async function doSearch(q) {
  const my = ++searchSeq;
  if (!q.trim()) { sugItems = []; paintSuggest(); setMsg(""); return; }
  setMsg("Searching…");
  let d;
  try { d = await api("/api/companies/search?q=" + encodeURIComponent(q)); } catch (e) { d = { status: "error", error: String(e.message), matches: [] }; }
  if (my !== searchSeq) return; // stale response
  sugItems = d.matches || []; sugIdx = -1; paintSuggest();
  if (d.status === "error") setMsg("Company list unavailable: " + (d.error || "unknown error"), true);
  else if (!sugItems.length) setMsg(`Not found: no listed company matches “${q.trim()}”. Check the spelling or try the NSE symbol.`, true);
  else setMsg(d.list && d.list.stale ? "Company list may be out of date (refresh failed)." : "");
}
function setMsg(t, err) { msg.textContent = t; msg.classList.toggle("err", !!err); }
input.addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(() => doSearch(input.value), 150); });
input.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); sugIdx = Math.min(sugIdx + 1, sugItems.length - 1); paintSuggest(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); sugIdx = Math.max(sugIdx - 1, 0); paintSuggest(); }
  else if (e.key === "Escape") closeSuggest();
  else if (e.key === "Enter") {
    e.preventDefault();
    if (sugItems[sugIdx]) return pick(sugItems[sugIdx]);
    if (sugItems.length === 1) return pick(sugItems[0]);
    if (sugItems.length) return pick(sugItems[0]);
    doSearch(input.value);
  }
});
suggest.addEventListener("mousedown", (e) => { const li = e.target.closest("li"); if (li) { e.preventDefault(); pick(sugItems[+li.dataset.i]); } });
document.addEventListener("click", (e) => { if (!e.target.closest(".picker")) closeSuggest(); });
$("#btn-search").addEventListener("click", () => { window.scrollTo({ top: 0, behavior: "smooth" }); input.focus(); input.select(); });
$("#btn-refresh").addEventListener("click", () => { loadMarketChip(); render(true); });

function pick(m) {
  closeSuggest();
  state.company = { symbol: m.symbol, name: m.name };
  input.value = m.name;
  setMsg("");
  history.replaceState(null, "", "?c=" + encodeURIComponent(m.symbol) + location.hash);
  render();
}

function paintHero() {
  const hero = $("#hero"), c = state.company;
  hero.classList.toggle("selected", !!c);
  const h = new Date().getHours();
  const greet = h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  $("#hero-title").textContent = c ? `${greet}. Viewing ${c.name}` : "Which company are you researching?";
  $("#hero-sub").textContent = c ? `NSE symbol ${c.symbol}. Search to switch company.` : "Type a company name or NSE symbol. Figures are labelled with their source and time; missing or stale data is shown as such.";
}

async function loadMarketChip() {
  const chip = $("#chip-market");
  let d;
  try { d = await api("/api/market/status"); } catch (e) { d = failed(e); }
  chip.className = "chip" + (d.status === "ok" ? (d.open ? " open" : " closed") : "");
  chip.textContent = d.status === "ok" ? `Market ${d.open ? "open" : "closed"}: ${d.reason || d.state}` : "Market status unavailable";
}

/* ---------- views ---------- */
function render(force) {
  paintHero();
  paintDockContext();
  const v = $("#view");
  const n = ++state.seq;
  if (state.tab === "dashboard") return renderDashboard(v, n);
  if (state.tab === "market") return renderMarket(v, n);
  if (state.tab === "signals") return renderSignals(v, n);
  return renderRisk(v);
}
const live = (n) => n === state.seq; // drop results of superseded renders

/* Dashboard */
function renderDashboard(v, n) {
  const c = state.company;
  if (!c) { v.innerHTML = `<div class="card empty-view"><b>No company selected yet.</b><br>Enter a company name above to load its quote, fundamentals, announcements and guidance.</div>`; return; }
  const sym = encodeURIComponent(c.symbol);
  v.innerHTML = `
    <div class="kpis" id="kpis">${[0, 1, 2, 3].map(() => `<section class="card kpi">${skeleton()}</section>`).join("")}</div>
    <div class="grid g3" style="margin-top:12px">
      <div class="card" id="c-ident">${skeleton()}</div>
      <div class="card cc" id="c-fund"></div>
      <div class="card cc" id="c-peers"></div>
      <div class="card span2" id="c-ann">${skeleton()}</div>
      <div class="card cc" id="c-res"></div>
      <div class="card span2 cc" id="c-prom"></div>
    </div>`;
  [["c-fund", "Fundamentals"], ["c-peers", "Related companies"], ["c-res", "Extracted results"], ["c-prom", "Management guidance"]]
    .forEach(([cid, t]) => foldCard(cid, t, "", skeleton(), { sum: "Tap to expand" }));
  api(`/api/company/${sym}/stock`).catch(failed).then((d) => { if (live(n)) paintStock(d); });
  api(`/api/company/${sym}/announcements`).catch(failed).then((d) => { if (live(n)) paintAnn(d); });
  api(`/api/company/${sym}/promises`).catch(failed).then((d) => { if (live(n)) paintProm(d); });
  api(`/api/company/${sym}/results`).catch(failed).then((d) => { if (live(n)) paintRes(d); });
}
const setCard = (id, title, sub, body) => { const el = $("#" + id); if (el) el.innerHTML = `<h2>${esc(title)}</h2>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}${body}`; };
/* Collapsible dashboard cards: open set persists in localStorage (per user, not per company). */
const CARDS_KEY = "finn.cards";
function openCards() { try { const o = JSON.parse(localStorage.getItem(CARDS_KEY)); return o && typeof o === "object" ? o : {}; } catch (_) { return {}; } }
function foldCard(id, title, sub, body, o) {
  const el = $("#" + id); if (!el) return;
  const open = !!openCards()[id], st = o.st || "";
  el.innerHTML = `<h2><button type="button" class="cc-btn" aria-expanded="${open}" aria-controls="${id}-body"><span class="cc-t">${esc(title)}</span><span class="cc-sum ${st}">${esc(o.sum)}</span><span class="cc-chev" aria-hidden="true"></span></button></h2>
    <div class="cc-body" id="${id}-body"${open ? "" : " hidden"}>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}${body}</div>`;
  const b = el.querySelector(".cc-btn");
  b.addEventListener("click", () => {
    const now = b.getAttribute("aria-expanded") !== "true";
    b.setAttribute("aria-expanded", String(now));
    $("#" + id + "-body").hidden = !now;
    const all = openCards(); if (now) all[id] = true; else delete all[id];
    try { localStorage.setItem(CARDS_KEY, JSON.stringify(all)); } catch (_) { /* storage blocked */ }
  });
}
const errBody = (d, what) => d.status === "error" ? stateBox("error", "Error", `${what} could not be loaded: ${d.error || "unknown error"}`) :
  d.status === "unavailable" ? stateBox("unavailable", "Unavailable", `${what} is unavailable from the source right now.${d.error ? " " + d.error : ""}`) : null;

const stateSum = (d) => d.status === "error" ? { sum: "Error loading - tap for details", st: "error" } : { sum: "Unavailable from source - tap for details", st: "unavailable" };

function paintStock(d) {
  const k = $("#kpis");
  const bad = errBody(d, "Company data") || (d.status === "empty" ? stateBox("empty", "Not found", d.message) : null);
  if (bad) {
    k.innerHTML = `<section class="card span2" style="grid-column:1/-1">${bad}${srcLine(d.source, d.fetched_at)}</section>`;
    ["c-ident", "c-fund", "c-peers"].forEach((id) => { const e = $("#" + id); if (e) e.remove(); });
    return;
  }
  const q = d.quote, f = d.fundamentals, id = d.identity;
  const qsrc = srcLine(d.source, d.fetched_at, q.quotedAt);
  const noq = (t) => `<section class="card kpi"><div class="lbl">${t}</div>${stateBox(q.status === "error" ? "error" : "empty", q.status === "error" ? "Error" : "Empty", q.error || q.message || "No quote")}</section>`;
  if (q.status !== "ok") k.innerHTML = ["Last price", "Day range", "Volume", "Market cap"].map((t, i) => i < 3 ? noq(t) : "").join("") + kpiCap(d);
  else {
    const span = q.high - q.low, posn = span > 0 ? Math.min(100, Math.max(0, ((q.ltp - q.low) / span) * 100)) : 50;
    const stale = /stale|delayed|skew/.test(q.freshness);
    k.innerHTML = `
      <section class="card kpi"><div class="lbl">Last price</div><div class="val">${inr(q.ltp)}</div>
        <div class="row2"><span class="pill ${cls(q.changePct) || ""} ${q.changePct > 0 ? "up" : q.changePct < 0 ? "down" : ""}">${pct(q.changePct)}</span><span>prev close ${inr(q.previousClose)}</span></div>
        <div class="row2" style="margin-top:6px"><span class="pill ${stale ? "warn" : "up"}">${esc(q.freshness)}</span></div>${qsrc}</section>
      <section class="card kpi"><div class="lbl">Day range</div><div class="val">${inr(q.low)} – ${inr(q.high)}</div>
        <div class="rangebar" aria-label="Last price position within day range"><i style="left:${posn}%"></i></div>
        <div class="row2"><span>open ${inr(q.open)}</span><span>${fnum(posn, 0)}% of range</span></div>${qsrc}</section>
      <section class="card kpi"><div class="lbl">Volume</div><div class="val">${fnum(q.volume, 0)}</div>
        <div class="row2"><span>shares traded</span><span class="pill">${q.market_open == null ? "market state unknown" : q.market_open ? "market open" : "market closed"}</span></div>${qsrc}</section>` + kpiCap(d);
  }
  const rows = [["Name", id.name], ["Symbol", id.symbol], ["Exchange", id.exchange], ["ISIN", id.isin], ["Sector", id.sector]]
    .map(([a, b]) => `<div class="r"><span>${a}</span><span>${b ? esc(b) : NA}</span></div>`).join("");
  const idx = (id.indices || []).length ? `<div class="tags" style="margin-top:10px">${id.indices.map((i) => `<span class="pill blue">${esc(i)}</span>`).join("")}</div>` : `<div class="sub" style="margin-top:8px">No index membership reported.</div>`;
  setCard("c-ident", "Company", "Identity", `<div class="rows">${rows}</div>${idx}${srcLine(d.source, d.fetched_at)}`);
  paintFund(d);
  const p = d.peers;
  foldCard("c-peers", "Related companies", "Same sector peers (select to switch)",
    p.status === "ok" ? `<div class="tags">${p.items.map((x) => `<button class="tag" data-sym="${esc(x.symbol)}" data-name="${esc(x.name || x.symbol)}">${esc(x.symbol)}<small>${esc(x.name || "")}</small></button>`).join("")}</div>` : stateBox("empty", "Empty", "No related companies reported.")
    + srcLine(d.source, d.fetched_at),
    p.status === "ok" ? { sum: `${p.items.length} ${p.items.length === 1 ? "company" : "companies"} · tap to expand` } : { sum: "None reported - tap for details", st: "empty" });
  if (p.status === "ok") $("#c-peers-body").insertAdjacentHTML("beforeend", srcLine(d.source, d.fetched_at));
  document.querySelectorAll("#c-peers .tag").forEach((b) => b.addEventListener("click", () => {
    pick({ symbol: b.dataset.sym, name: b.dataset.name });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }));
}
function kpiCap(d) {
  const f = d.fundamentals;
  if (f.status === "empty") return `<section class="card kpi"><div class="lbl">Market cap</div>${stateBox("empty", "Empty", f.message)}</section>`;
  return `<section class="card kpi"><div class="lbl">Market cap</div><div class="val">${mcapCr(f.mcap)}</div>
    <div class="row2"><span>P/E ${f.pe == null ? NA : fnum(f.pe, 1)}</span><span>P/B ${f.priceToBook == null ? NA : fnum(f.priceToBook, 2)}</span></div>${srcLine(d.source, d.fetched_at, f.updatedAt)}</section>`;
}
function paintFund(d) {
  const f = d.fundamentals;
  if (f.status === "empty") return foldCard("c-fund", "Fundamentals", "", stateBox("empty", "Empty", f.message) + srcLine(d.source, d.fetched_at), { sum: "No data reported - tap for details", st: "empty" });
  const R = (a, b, note) => `<div class="r"><span>${a}${note ? `<small>${note}</small>` : ""}</span><span>${b}</span></div>`;
  const body = (f.status === "partial" ? stateBox("partial", "Partial data", "Not reported by the source: " + f.missing.join(", ")) : "") +
    `<div class="rows">${R("CMP (fundamentals feed)", inr(f.cmp), "may differ from live quote")}${R("P/E", f.pe == null ? NA : fnum(f.pe, 1))}${R("Price / book", f.priceToBook == null ? NA : fnum(f.priceToBook, 2))}${R("Book value", inr(f.bookValue))}${R("ROE", f.roe == null ? NA : fnum(f.roe, 1) + "%")}${R("ROCE", f.roce == null ? NA : fnum(f.roce, 1) + "%")}${R("Dividend yield", f.divYield == null ? NA : fnum(f.divYield, 2) + "%")}${R("Market cap", mcapCr(f.mcap), "INR crore")}</div>`;
  foldCard("c-fund", "Fundamentals", "Snapshot", body + srcLine(d.source, d.fetched_at, f.updatedAt),
    f.status === "partial" ? { sum: "Partial data - tap for details", st: "partial" } : { sum: `P/E ${f.pe == null ? "not reported" : fnum(f.pe, 1)} · tap to expand` });
}
function paintAnn(d) {
  const bad = errBody(d, "Announcements");
  let body = "";
  if (bad) body = bad;
  else {
    if (d.coverage_banner) body += stateBox("banner", "Partial coverage", d.coverage_banner);
    const health = (d.sources || []).map((s) => `<span class="pill ${s.status === "ok" && !s.withheld ? "up" : "down"}">${esc(s.name.toUpperCase())}: ${esc(s.status)}${s.withheld ? " (withheld)" : ""}${s.newest_at ? " · newest " + ts(s.newest_at) : ""}</span>`).join(" ");
    if (health) body += `<div class="tags" style="margin:10px 0">${health}</div>`;
    body += d.items.length ? `<ul class="list">${d.items.map((a) => `<li><div class="t">${esc(a.subject)}</div><div class="m"><span class="pill">${esc(a.exchange)}</span>${a.category ? `<span class="pill blue">${esc(a.category)}</span>` : ""}<span>${ts(a.published_at)}</span>${a.pdf_url ? `<a href="${esc(a.pdf_url)}" target="_blank" rel="noopener noreferrer">PDF</a>` : ""}</span></div></li>`).join("")}</ul>`
      : stateBox("empty", "No announcements returned", "None in the sources that responded." + (d.coverage_banner ? " A failed exchange feed means absence is not proof that no filing exists." : ""));
  }
  setCard("c-ann", "Announcements", "Exchange filings for this company", body + srcLine(d.source, d.fetched_at, d.generated_at));
}
function boundText(p) {
  if (p.comparator === "range" && p.lo != null) return `range ${p.lo}–${p.hi}${p.unit === "percent" ? "%" : p.unit ? " " + p.unit : ""}`;
  if (p.comparator && p.value != null) return `${p.comparator.replace("_", " ")} ${p.value}${p.unit === "percent" ? "%" : p.unit ? " " + p.unit : ""}`;
  return "target not parsed";
}
function paintProm(d) {
  const bad = errBody(d, "Guidance");
  const body = bad || (d.items.length ? `<ul class="list">${d.items.map((p) => `<li><div class="quote">“${esc(p.quote)}”</div><div class="t">${esc(p.metric)}: ${esc(p.target)}</div>
    <div class="m"><span class="pill blue">${esc(boundText(p))}</span>${p.hedge ? `<span class="pill warn">hedge: ${esc(p.hedge)}</span>` : ""}<span>${p.timeframe ? "timeframe: " + esc(p.timeframe) : "timeframe not stated"}</span><span>said ${ts(p.said_on)}</span><a href="${esc(p.source_url)}" target="_blank" rel="noopener noreferrer">source filing</a></div></li>`).join("")}</ul>`
    : stateBox("empty", "No guidance found", "No management guidance statements are on record for this company."));
  foldCard("c-prom", "Management guidance", "Verbatim statements; ranges stay ranges, hedges stay hedges", body + srcLine(d.source, d.fetched_at),
    bad ? stateSum(d) : d.items.length ? { sum: `${d.items.length} ${d.items.length === 1 ? "statement" : "statements"} · tap to expand` } : { sum: "No guidance found - tap for details", st: "empty" });
}
function paintRes(d) {
  let body;
  if (errBody(d, "Results")) body = errBody(d, "Results");
  else if (!d.loaded) body = stateBox("empty", "Not loaded", d.message);
  else {
    body = `<div class="sub">${esc(d.company)} · period ending ${esc(d.period_end)} · ${d.consolidated ? "consolidated" : "standalone"}${d.standard ? " · " + esc(d.standard) : ""}</div><div class="rows">` +
      d.rows.map((r) => `<div class="r"><span>${esc(r.label)}<small>${esc(r.state)}${r.page != null ? " · p." + r.page : ""}</small></span><span>${r.value_crore == null ? "not stated" : "₹" + fnum(r.value_crore, 0) + " Cr"}</span></div>`).join("") +
      `</div><div class="state" style="margin-top:10px"><b>Verification</b>${esc(d.verification)}</div>`;
  }
  foldCard("c-res", "Extracted results", "Revenue definitions are kept separate", body + srcLine(d.source, d.fetched_at, null),
    errBody(d, "Results") ? stateSum(d) : !d.loaded ? { sum: "Not loaded - tap for details", st: "empty" } : { sum: `period ending ${d.period_end} · tap to expand` });
}

/* Market Overview */
function renderMarket(v, n) {
  v.innerHTML = `<div class="grid g2">
    <div class="card" id="m-status">${skeleton()}</div><div class="card" id="m-fii">${skeleton()}</div>
    <div class="card span2" id="m-idx">${skeleton()}</div><div class="card" id="m-sent">${skeleton()}</div><div class="card" id="m-sec">${skeleton()}</div></div>`;
  api("/api/market/status").catch(failed).then((d) => {
    if (!live(n)) return;
    const body = errBody(d, "Market status") || `<div class="rows"><div class="r"><span>Status</span><span><span class="pill ${d.open ? "up" : "down"}">${d.open ? "Open" : "Closed"}</span></span></div>
      <div class="r"><span>Reason</span><span>${esc(d.reason || d.state)}</span></div><div class="r"><span>Last trading day</span><span>${esc(d.last_trading_day || NA)}</span></div>
      <div class="r"><span>Next trading day</span><span>${esc(d.next_trading_day || NA)}</span></div></div>
      ${(d.upcoming || []).length ? `<div class="sub" style="margin-top:10px">Upcoming holidays: ${d.upcoming.map((h) => esc(h.name) + " (" + esc(h.date) + ")").join(", ")}</div>` : ""}`;
    setCard("m-status", "Market status", "Indian equity market", body + srcLine(d.source, d.fetched_at));
  });
  api("/api/market/fiidii").catch(failed).then((d) => {
    if (!live(n)) return;
    const body = errBody(d, "FII / DII flows") || (d.rows.length ? `<div class="scroll-x"><table class="tbl"><tr><th></th><th class="n">Buy (₹ crore)</th><th class="n">Sell (₹ crore)</th><th class="n">Net (₹ crore)</th></tr>${d.rows.map((r) => `<tr><td><b>${esc(r.label)}</b></td><td class="n">${fnum(r.buy)}</td><td class="n">${fnum(r.sell)}</td><td class="n ${cls(r.net)}"><b>${fnum(r.net)}</b></td></tr>`).join("")}</table></div><div class="unit-note">Values in ${d.unit === "INR crore" ? "₹ crore" : esc(d.unit || "unit not provided")}${d.unit_basis === "api_documentation" ? " (unit per API documentation)" : ""}</div>` : stateBox("empty", "Empty", "No flow rows returned."));
    setCard("m-fii", "FII / DII activity", d.trade_date ? "Trading date " + d.trade_date : "", body + srcLine(d.source, d.fetched_at, null));
  });
  api("/api/market/indices").catch(failed).then((d) => {
    if (!live(n)) return;
    let body = errBody(d, "Index levels");
    if (!body) {
      body = d.indices.length ? `<div class="grid g3">${d.indices.map((i) => `<div class="state"><b>${esc(i.label)}</b><span style="font-size:22px;font-weight:700;color:var(--ink)">${fnum(i.price)}</span><br><span class="pill ${cls(i.change_pct)}">${pct(i.change_pct)}</span> <span class="sub">${i.is_live ? "live" : "not live"} · as of ${ts(i.as_of)}</span></div>`).join("")}</div>` : stateBox("empty", "Empty", "No index levels returned.");
      if (d.extras.length) body += `<div class="tags" style="margin-top:12px">${d.extras.map((x) => `<span class="pill">${esc(x.name)} ${fnum(x.price)}${x.unit ? " " + esc(x.unit) : ""} <span class="${cls(x.change_pct)}">${pct(x.change_pct)}</span></span>`).join("")}</div>`;
    }
    setCard("m-idx", "Index levels", "Last reported levels; check the live flag and time", body + srcLine(d.source, d.fetched_at, d.upstream_timestamp));
    const sec = errBody(d, "Sector indices") || (d.sectors.length ? `<div class="sectors">${d.sectors.map((s) => `<div>${esc(s.name)}<b>${fnum(s.price)}</b><span class="${cls(s.change_pct)}">${pct(s.change_pct)}</span></div>`).join("")}</div>` : stateBox("empty", "Empty", "No sector data."));
    setCard("m-sec", "Sector indices", "", sec + srcLine(d.source, d.fetched_at, d.upstream_timestamp));
  });
  api("/api/market/sentiments").catch(failed).then((d) => {
    if (!live(n)) return;
    const body = errBody(d, "Market sentiment") || (d.items.length ? `<div class="rows">${d.items.map((s) => `<div class="r"><span>${esc(s.market)}</span><span><span class="pill ${/bull/i.test(s.label) ? "up" : /bear/i.test(s.label) ? "down" : ""}">${esc(s.label)}</span><small>${esc(s.reason)}</small></span></div>`).join("")}</div>` : stateBox("empty", "Empty", "No sentiment rows."));
    setCard("m-sent", "Global market sentiment", "Source-assigned labels", body + srcLine(d.source, d.fetched_at, d.refresh_at));
  });
}

/* Signals (alerts on the latest available quote) */
function renderSignals(v, n) {
  const c = state.company;
  const thr = state.thr || 5;
  v.innerHTML = `<div class="card"><h2>Threshold</h2><div class="sub">Checks the latest available move (previous close to last price) when this page is loaded or refreshed. No background watching or notifications.</div>
    <form class="thr" id="thr-form"><label for="thr">Price move ±%</label><input id="thr" type="number" min="0.1" max="100" step="0.1" value="${esc(thr)}"><button class="btn" type="submit">Apply</button></form></div>
    <div class="card" id="s-body" style="margin-top:12px">${skeleton()}</div>`;
  $("#thr-form").addEventListener("submit", (e) => { e.preventDefault(); state.thr = parseFloat($("#thr").value) || 5; render(); });
  api("/api/signals?threshold=" + encodeURIComponent(thr) + (c ? "&symbol=" + encodeURIComponent(c.symbol) : "")).catch(failed).then((d) => {
    if (!live(n)) return;
    let body = errBody(d, "Alerts");
    if (!body || d.status === "partial") {
      body = (d.coverage_banner ? stateBox("banner", "Partial coverage", d.coverage_banner) : "") + (d.events && d.events.length ? `<ul class="list">${d.events.map((e) => `<li><div class="m"><span class="pill ${e.kind === "threshold" ? "warn" : "blue"}">${e.kind === "threshold" ? "threshold alert" : e.kind === "correction" ? "correction" : "issuer-labelled correction"}</span><span>${ts(e.as_of)}</span></div><div class="t">${esc(e.title)}</div><div class="sub">${esc(e.detail)}</div>${/^https:/.test(e.source) ? `<a href="${esc(e.source)}" target="_blank" rel="noopener noreferrer">source</a>` : ""}</li>`).join("")}</ul>`
        : stateBox("empty", "No alerts right now", "A threshold result appears here when the selected company's latest available day move crosses your threshold at load/refresh time. Correction events appear when an exchange re-publishes a filing with changed content or labels it a correction/revision. FINN does not generate trade entries, exits, win rates or accuracy figures.")) +
        (d.notes && d.notes.length ? `<div class="sub" style="margin-top:10px">${d.notes.map(esc).join("<br>")}</div>` : "");
      if (!c) body += `<div class="sub">Scope: all companies. Select a company for its threshold check.</div>`;
    } else if (d.notes && d.notes.length) body += `<div class="sub" style="margin-top:10px">${d.notes.map(esc).join("<br>")}</div>`;
    $("#s-body").innerHTML = `<h2>Alerts &amp; corrections</h2><div class="sub">${esc(d.scope || (c ? c.symbol : "all companies"))}</div>${body}${srcLine(d.source, d.fetched_at)}`;
  });
}

/* AI Risk Analyzer (existing guarded chat via /api/chat) */
function linkify(t) {
  return esc(t).replace(/\[([^\]]+)\]\((https:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[\s(])(https:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
}
function renderRisk(v) {
  v.innerHTML = card("AI Risk Analyzer", "Ask about risks, guidance and announcements in the chat dock. Answers come from the guarded FINN agent; no risk scores are computed here.",
    '<button class="btn" type="button" data-open-chat>Open chat</button>');
}

/* ---------- chat dock (persistent DOM in index.html; never re-rendered by render()) ---------- */
const dockToggle = $("#btn-chat"), wide = window.matchMedia("(min-width:960px)");
const DOCK_KEY = "finn.dock";
let dockOpen = false, dockReturn = null;
function storedDock() { try { return localStorage.getItem(DOCK_KEY); } catch (_) { return null; } }
function setDock(open, o = {}) {
  if (open && !dockOpen) dockReturn = document.activeElement;
  dockOpen = open;
  document.body.classList.toggle("dock-open", open);
  dockToggle.setAttribute("aria-expanded", String(open));
  if (o.persist && wide.matches) { try { localStorage.setItem(DOCK_KEY, open ? "open" : "closed"); } catch (_) { /* storage blocked */ } }
  if (open && o.focus) $("#chat-in").focus();
  if (!open && o.restore) {
    const t = dockReturn && document.contains(dockReturn) && dockReturn.offsetParent !== null ? dockReturn : dockToggle;
    t.focus(); dockReturn = null;
  }
}
function syncDock() { setDock(wide.matches ? storedDock() !== "closed" : false); }
function paintDockContext() {
  const c = state.company;
  $("#dock-ctx").textContent = c ? `${c.name} (${c.symbol})` : "No company selected";
  $("#chat-note").textContent = (c ? `Context: ${c.name} (${c.symbol}) is sent with each question.` : "No company selected; pick one to give the analyst context.")
    + " Answers come from the guarded FINN agent; no risk scores are computed here.";
}
dockToggle.addEventListener("click", () => setDock(!dockOpen, { persist: true, focus: !dockOpen, restore: dockOpen }));
$("#chat-fab").addEventListener("click", () => setDock(true, { persist: true, focus: true }));
$("#dock-close").addEventListener("click", () => setDock(false, { persist: true, restore: true }));
$("#view").addEventListener("click", (e) => { if (e.target.closest("[data-open-chat]")) setDock(true, { persist: true, focus: true }); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && dockOpen && !wide.matches) setDock(false, { restore: true }); });
wide.addEventListener("change", syncDock);
$("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendChat(); });
function addMsg(log, kind, text, co) {
  const empty = log.querySelector(".state"); if (empty) empty.remove();
  const el = document.createElement("div");
  el.className = "msg " + kind;
  const cap = document.createElement("div");
  cap.className = "msg-co";
  cap.textContent = co ? "Context: " + co : "No company context";
  el.appendChild(cap);
  const body = document.createElement("div");
  kind === "bot" ? (body.innerHTML = linkify(text)) : (body.textContent = text);
  el.appendChild(body); el._body = body;
  log.appendChild(el); log.scrollTop = log.scrollHeight;
  return el;
}
async function sendChat() {
  if (state.chatBusy) return;
  const inp = $("#chat-in"), text = inp.value.trim();
  if (!text) return;
  inp.value = "";
  const c = state.company, log = $("#chat-log"), co = c ? c.symbol : null;
  const sent = c ? `[Context: the user is viewing ${c.name} (NSE: ${c.symbol}) in the FINN dashboard.] ${text}` : text;
  const uid = "u" + Date.now();
  state.chat.push({ id: uid, role: "user", text: sent, display: text, co });
  addMsg(log, "user", text, co);
  const botEl = addMsg(log, "bot", "…", co);
  state.chatBusy = true; $("#chat-send").disabled = true;
  let acc = "";
  try {
    const messages = state.chat.map((m) => ({ id: m.id, role: m.role, parts: [{ type: "text", text: m.text }] }));
    const r = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ trigger: "submit-message", id: "finn-" + uid, messages }) });
    if (!r.ok) throw new Error("chat service returned HTTP " + r.status);
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (!payload || payload === "[DONE]") continue;
        let ev; try { ev = JSON.parse(payload); } catch (_) { continue; }
        if (ev.type === "text-delta" && ev.delta) { acc += ev.delta; botEl._body.innerHTML = linkify(acc); log.scrollTop = log.scrollHeight; }
        else if (ev.type === "error") throw new Error(ev.errorText || "model error");
      }
    }
    if (!acc) throw new Error("the agent returned no text");
    state.chat.push({ id: "a" + Date.now(), role: "assistant", text: acc, co });
  } catch (e) {
    botEl.classList.add("err");
    botEl._body.textContent = (acc ? acc + "\n\n" : "") + "Chat error: " + (e.message || e);
    if (acc) state.chat.push({ id: "a" + Date.now(), role: "assistant", text: acc, co });
  } finally { state.chatBusy = false; const b = $("#chat-send"); if (b) b.disabled = false; }
}

/* ---------- boot ---------- */
buildNav();
syncDock();
const initial = new URLSearchParams(location.search).get("c");
loadMarketChip();
if (initial && /^[A-Za-z0-9&._-]{1,20}$/.test(initial)) {
  api("/api/companies/search?q=" + encodeURIComponent(initial)).then((d) => {
    const m = (d.matches || []).find((x) => x.symbol === initial.toUpperCase());
    if (m) { state.company = { symbol: m.symbol, name: m.name }; input.value = m.name; }
  }).catch(() => {}).then(route);
} else route();
