"""Backend for the FINN web UI: company directory + per-section company data.

Every Downstox call is proxied here (the browser never talks to Downstox) and
reuses DownstoxClient, freshness_label and the announcement feed's degraded-
source handling. Each section is returned as an envelope
  {"status": "ok" | "partial" | "empty" | "error" | "unavailable", ...,
   "source": ..., "fetched_at": ...}
so one failing upstream never blanks the page. No figure is invented: missing
data is reported as such.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .alerts import price_volume_alert
from .market_data import BASE_URL, DownstoxClient, MarketDataError, freshness_label
from .filings import FilingIngestError, ingest_pdf_bytes, load_filing_cache
from .models import Announcement, Metric
from .real_seed import SOURCE_URL as TCS_RESULTS_URL
from .real_seed import tcs_filings
from .store import AnnouncementStore

STATIC_DIR = Path(__file__).parent / "static"
SCREENER_URL = f"{BASE_URL}/api/screener/"
SCREENER_TTL_S = 24 * 3600
SCREENER_MAX_BYTES = 8_000_000
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9&._-]{1,20}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def _num(v) -> Optional[float]:
    """Screener fields arrive as strings ('1165.00'); None when absent/garbage."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class CompanyDirectory:
    """Server-side cache of the Downstox screener list (symbol + name).

    Refreshed at most once per `ttl` seconds; persisted to `cache_path` so a
    restart does not re-download ~2MB. If a refresh fails the last good copy is
    served (flagged stale) instead of failing the typeahead."""

    def __init__(self, cache_path: Optional[Path] = None, http: Optional[httpx.Client] = None,
                 url: str = SCREENER_URL, ttl: float = SCREENER_TTL_S, clock: Callable[[], float] = time.time,
                 fixture: Optional[Path] = None):
        self.cache_path = Path(cache_path) if cache_path else Path.home() / ".cache" / "finn" / "screener.json"
        self._http = http
        self.url = url
        self.ttl = ttl
        self._clock = clock
        self._fixture = Path(fixture) if fixture else None
        self._lock = threading.Lock()
        self._rows: list[dict] = []
        self._fetched_ts: Optional[float] = None
        self._last_error: Optional[str] = None
        self.refresh_count = 0

    # -- loading -----------------------------------------------------------
    def _download(self) -> list[dict]:
        if self._fixture is not None:
            payload = json.loads(self._fixture.read_text())
        else:
            http = self._http or httpx
            resp = http.get(self.url, timeout=90.0)  # ~2MB; upstream can be slow
            if resp.status_code != 200:
                raise MarketDataError(f"screener HTTP {resp.status_code}")
            if len(resp.content) > SCREENER_MAX_BYTES:
                raise MarketDataError("screener response too large")
            payload = resp.json()
        if not payload.get("success", False) or not isinstance(payload.get("stocks"), list):
            raise MarketDataError("screener payload malformed")
        rows = [{"symbol": str(s["symbol"]).upper(), "name": str(s["name"]),
                 "sector": s.get("sector"), "industry": s.get("industry"), "universe": s.get("universe")}
                for s in payload["stocks"] if s.get("symbol") and s.get("name")]
        if not rows:
            raise MarketDataError("screener returned no stocks")
        return rows

    def _load_disk(self) -> None:
        if self._fixture is not None:
            return
        try:
            blob = json.loads(self.cache_path.read_text())
            self._rows, self._fetched_ts = blob["rows"], float(blob["fetched_ts"])
        except Exception:
            pass

    def _save_disk(self) -> None:
        if self._fixture is not None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"rows": self._rows, "fetched_ts": self._fetched_ts}))
            tmp.replace(self.cache_path)
        except OSError:
            pass  # cache is an optimisation only

    def ensure(self) -> None:
        with self._lock:
            if not self._rows:
                self._load_disk()
            fresh = self._fetched_ts is not None and self._clock() - self._fetched_ts < self.ttl
            if self._rows and fresh:
                return
            try:
                self._rows = self._download()
                self._fetched_ts = self._clock()
                self._last_error = None
                self.refresh_count += 1
                self._save_disk()
            except Exception as exc:
                self._last_error = str(exc)
                if not self._rows:
                    raise MarketDataError(f"company list unavailable: {exc}") from exc

    # -- queries -----------------------------------------------------------
    def meta(self) -> dict:
        return {"count": len(self._rows), "source": "downstox screener", "source_url": self.url,
                "fetched_at": _iso(datetime.fromtimestamp(self._fetched_ts, timezone.utc)) if self._fetched_ts else None,
                "stale": self._last_error is not None, "refresh_error": self._last_error}

    def search(self, q: str, limit: int = 8) -> list[dict]:
        self.ensure()
        needle = re.sub(r"\s+", " ", q.strip().lower())
        if not needle:
            return []
        scored = []
        for r in self._rows:
            sym, name = r["symbol"].lower(), r["name"].lower()
            if sym == needle:
                rank = 0
            elif name == needle:
                rank = 1
            elif sym.startswith(needle):
                rank = 2
            elif name.startswith(needle):
                rank = 3
            elif any(w.startswith(needle) for w in name.split()):
                rank = 4
            elif needle in name:
                rank = 5
            elif needle in sym:
                rank = 6
            else:
                continue
            scored.append((rank, len(name), r))
        scored.sort(key=lambda t: (t[0], t[1], t[2]["symbol"]))
        return [{"symbol": r["symbol"], "name": r["name"], "sector": r["sector"]} for _, _, r in scored[:limit]]

    def resolve(self, symbol: str) -> Optional[dict]:
        self.ensure()
        s = symbol.upper()
        for r in self._rows:
            if r["symbol"] == s:
                return {"symbol": r["symbol"], "name": r["name"], "sector": r["sector"]}
        return None


# -- section builders --------------------------------------------------------

def _env(status: str, source: str, **kw) -> dict:
    return {"status": status, "source": source, "fetched_at": _iso(_now()), **kw}


def _market_open(client: DownstoxClient) -> Optional[bool]:
    try:
        return client.get_market_status().open
    except Exception:
        return None


def stock_section(client: DownstoxClient, symbol: str) -> dict:
    """identity + quote + fundamentals + peers from one /api/stocks/ response,
    each with its own state."""
    sym = symbol.upper()
    src = f"{client.base_url}/api/stocks/{sym}"
    try:
        raw = client.get_raw(f"/api/stocks/{sym}", f"downstox-stock-{sym}.json")
    except MarketDataError as exc:
        return _env("error", src, error=str(exc))
    st = raw.get("stock") or {}
    if not st.get("symbol"):
        return _env("empty", src, message="Downstox returned no stock record for this symbol.")
    identity = {"status": "ok", "name": st.get("name"), "symbol": st.get("symbol"), "exchange": st.get("exchange"),
                "isin": st.get("isin"), "sector": raw.get("sector"), "indices": raw.get("indices") or []}
    live = raw.get("live")
    if live:
        try:
            quoted = datetime.fromisoformat(live["quotedAt"].replace("Z", "+00:00"))
            mo = _market_open(client)
            fresh = freshness_label(quoted, _now(), bool(mo))
            quote = {"status": "ok", **{k: live.get(k) for k in
                     ("ltp", "previousClose", "changePct", "open", "high", "low", "volume", "quotedAt")},
                     "freshness": fresh, "market_open": mo,
                     "upstream_stale_flag": live.get("stale")}
        except Exception as exc:
            quote = {"status": "error", "error": f"quote malformed: {exc}"}
    else:
        quote = {"status": "empty", "message": "No live quote returned for this symbol."}
    f = raw.get("fundamentals")
    if f:
        keys = ("cmp", "pe", "priceToBook", "bookValue", "roe", "roce", "divYield", "mcap", "updatedAt")
        have = {k: f.get(k) for k in keys}
        missing = [k for k, v in have.items() if v is None]
        fundamentals = {"status": "partial" if missing else "ok", **have, "missing": missing,
                        "mcap_unit": "INR crore (checked: mcap / cmp gives the share count in crore)"}
    else:
        fundamentals = {"status": "empty", "message": "No fundamentals returned for this symbol."}
    related = [{"symbol": r["symbol"], "name": r.get("name")} for r in raw.get("related") or [] if r.get("symbol")]
    peers = {"status": "ok" if related else "empty", "items": related}
    return _env("ok", src, identity=identity, quote=quote, fundamentals=fundamentals, peers=peers)


def announcements_section(client: DownstoxClient, symbol: str, limit: int = 20) -> dict:
    sym = symbol.upper()
    src = f"{client.base_url}/api/announcements/?symbol={sym}"
    try:
        feed = client.get_announcements(symbol=sym, limit=limit)
    except MarketDataError as exc:
        return _env("error", src, error=str(exc))
    bad = feed.degraded_sources()
    sources = [{"name": s.name, "status": s.status, "withheld": s.withheld, "newest_at": _iso(s.newest_at)}
               for s in feed.sources]
    items = [{"exchange": a.exchange, "subject": a.subject, "category": a.category,
              "published_at": _iso(a.published_at), "pdf_url": a.pdf_url} for a in feed.items]
    status = "partial" if bad else ("ok" if items else "empty")
    banner = feed.coverage_note() if bad else None
    return _env(status, src, items=items, sources=sources, coverage_banner=banner,
                generated_at=_iso(feed.generated_at))



def filings_section(client: DownstoxClient, symbol: str) -> dict:
    """Read-only view of the filing passage cache for a symbol.

    Automated retrieval of exchange filing PDFs is disabled (NSE/BSE website
    terms prohibit systematic/automated retrieval without written consent).
    This serves only permitted inputs: PDFs the user uploaded and the repo's
    manually transcribed seed filing. Announcement metadata with source-PDF
    deep links stays available via the announcements section."""
    sym = symbol.upper()
    data = load_filing_cache(sym)
    filings = []
    if data:
        filings = [{"subject": f["subject"], "origin": f.get("origin"),
                    "source_note": f.get("source_note"),
                    "page_count": f["page_count"],
                    "passage_count": len(f["passages"])} for f in data["filings"]]
    seed_available = sym == "TCS"  # repo's manually transcribed TCS Q2 FY27 seed
    status = "ok" if (filings or seed_available) else "empty"
    return _env(status, "user-supplied documents + bundled manual seed (no automated fetching)",
                filings=filings, seed_available=seed_available,
                how_to_add="Download a filing PDF yourself; uploads are restricted on this shared preview. The bundled seed and operator-ingested documents are what chat can cite today.")


def promises_section(client: DownstoxClient, symbol: str) -> dict:
    sym = symbol.upper()
    src = f"{client.base_url}/api/promises/?symbol={sym}"
    try:
        promises = client.get_promises(sym)
    except MarketDataError as exc:
        return _env("error", src, error=str(exc))
    items = []
    for p in promises:
        t = p.norm_target
        items.append({"quote": p.quote, "metric": p.metric, "target": p.target_raw,
                      "comparator": t.comparator if t and t.parsed else None,
                      "lo": t.lo if t and t.parsed else None, "hi": t.hi if t and t.parsed else None,
                      "value": t.value if t and t.parsed else None, "unit": t.unit if t and t.parsed else None,
                      "hedge": t.hedge if t else None, "timeframe": p.timeframe or None,
                      "said_on": _iso(p.said_on), "source_url": p.source_url})
    return _env("ok" if items else "empty", src, items=items)


# Results the app has actually loaded (hand-transcribed from filed PDFs).
# Anything else is reported as "not loaded", never estimated.
_RESULTS = {"TCS": (tcs_filings, TCS_RESULTS_URL,
                    "Hand-transcribed from the filed press release; page numbers recorded, "
                    "not re-verified against the PDF at runtime.")}


def results_section(symbol: str) -> dict:
    entry = _RESULTS.get(symbol.upper())
    if entry is None:
        return _env("empty", "finn extracted results", loaded=False,
                    message="Results not loaded for this company. FINN has no extracted filing for it yet; "
                            "no figures are estimated.")
    build, url, note = entry
    filing = build()[-1]
    rows = []
    for m, label in ((Metric.REVENUE_FROM_OPERATIONS, "Revenue from operations"),
                     (Metric.TOTAL_REVENUE, "Total revenue (incl. other income)"),
                     (Metric.EBITDA, "EBITDA"), (Metric.PBT, "PBT"), (Metric.PAT, "PAT")):
        v = filing.metric_value(m)
        rows.append({"metric": m.value, "label": label,
                     "value_crore": v.in_crore() if v else None,
                     "page": v.page if v else None,
                     "state": "not stated in this filing" if v is None else
                              ("page recorded" if v.page is not None else "page not established")})
    return _env("ok", url, loaded=True, company=filing.company, period_end=filing.period_end.isoformat(),
                consolidated=filing.consolidated, standard=filing.standard, rows=rows,
                verification=note, source_url=url)


def company_aggregate(client: DownstoxClient, symbol: str) -> dict:
    """All sections in parallel; each fails independently."""
    jobs = {"stock": lambda: stock_section(client, symbol),
            "announcements": lambda: announcements_section(client, symbol),
            "filings": lambda: filings_section(client, symbol),
            "promises": lambda: promises_section(client, symbol),
            "results": lambda: results_section(symbol)}
    out: dict = {"symbol": symbol.upper(), "sections": {}}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {k: pool.submit(fn) for k, fn in jobs.items()}
        for k, fut in futs.items():
            try:
                out["sections"][k] = fut.result()
            except Exception as exc:  # a bug in one section must not sink the rest
                out["sections"][k] = _env("error", k, error=f"{type(exc).__name__}: {exc}")
    return out


# -- market overview ---------------------------------------------------------

def market_section(client: DownstoxClient) -> dict:
    try:
        ms = client.get_market_status()
        raw = client.get_raw("/api/holidays/", "downstox-holidays.json")
    except MarketDataError as exc:
        return _env("unavailable", f"{client.base_url}/api/holidays/", error=str(exc))
    return _env("ok", f"{client.base_url}/api/holidays/", open=ms.open, state=ms.state, reason=ms.reason,
                last_trading_day=ms.last_trading_day, next_trading_day=ms.next_trading_day,
                upcoming=[{"date": h.get("date"), "name": h.get("name")} for h in (raw.get("upcoming") or [])[:4]])


def indices_section(client: DownstoxClient) -> dict:
    src = f"{client.base_url}/api/india-markets/overview"
    try:
        raw = client.get_raw("/api/india-markets/overview", "downstox-india-markets-overview.json")
    except MarketDataError as exc:
        return _env("unavailable", src, error=str(exc))
    names = {"nse": "NSE (Nifty 50)", "bse": "BSE (Sensex)", "giftnifty": "GIFT Nifty"}
    idx = []
    for k, label in names.items():
        v = (raw.get("indices") or {}).get(k)
        if v:
            idx.append({"key": k, "label": label, "price": v.get("price"), "change_pct": v.get("change"),
                        "change_abs": v.get("changeAbs"), "is_live": v.get("isLive"),
                        "as_of": v.get("marketTime")})
    sectors = [{"name": s.get("name"), "price": s.get("price"), "change_pct": s.get("change")}
               for s in raw.get("sectors") or []]
    extras = [{"name": e.get("name"), "unit": e.get("unit"), "price": e.get("price"),
               "change_pct": e.get("change"), "as_of": e.get("marketTime")}
              for e in raw.get("extras") or [] if e.get("name") in ("India VIX", "USD/INR", "Gold", "Crude Oil")]
    status = "ok" if idx else "empty"
    return _env(status, src, indices=idx, sectors=sectors, extras=extras, upstream_timestamp=raw.get("timestamp"),
                session=raw.get("session"))


def sentiments_section(client: DownstoxClient) -> dict:
    src = f"{client.base_url}/api/markets/sentiments"
    try:
        raw = client.get_raw("/api/markets/sentiments", "downstox-markets-sentiments.json")
    except MarketDataError as exc:
        return _env("unavailable", src, error=str(exc))
    data = raw.get("data") or {}
    keep = ("nse", "bse", "giftnifty", "nyse", "nasdaq", "lse", "xetra", "tse", "hkex", "sse")
    items = [{"market": k.upper(), "label": data[k].get("label"), "reason": data[k].get("reason")}
             for k in keep if k in data]
    return _env("ok" if items else "empty", src, items=items, refresh_at=raw.get("refreshAt"))


def fiidii_section(client: DownstoxClient) -> dict:
    src = f"{client.base_url}/api/fiidii/daily"
    try:
        raw = client.get_raw("/api/fiidii/daily", "downstox-fiidii-daily.json")
    except MarketDataError as exc:
        return _env("unavailable", src, error=str(exc))
    rows = []
    for key, label in (("fii", "FII/FPI"), ("dii", "DII")):
        v = raw.get(key)
        if v:
            rows.append({"label": label, "buy": v.get("buyValue"), "sell": v.get("sellValue"), "net": v.get("netValue")})
    return _env("ok" if rows else "empty", src, rows=rows, trade_date=raw.get("date"),
                unit="INR crore", unit_basis="api_documentation")


# -- live signals ------------------------------------------------------------

_CORRECTION_RE = re.compile(r"\b(correction|corrigendum|revised|revision|addendum|amendment)\b", re.I)


def _to_store_announcement(a) -> Announcement:
    # Downstox has no announcement id: key on the filing PDF when present so a
    # re-filed item with changed text is a correction; else on symbol+time.
    basis = a.pdf_url or f"{a.symbol}|{a.published_at.isoformat()}"
    return Announcement(exchange=a.exchange if a.exchange in ("NSE", "BSE") else "NSE",
                        announcement_id=hashlib.sha1(basis.encode()).hexdigest()[:16],
                        company=a.company, headline=a.subject, filed_at=a.published_at,
                        category=a.category or "unclassified")


def signals_section(client: DownstoxClient, symbol: Optional[str], threshold_pct: float = 5.0) -> dict:
    sym = symbol.upper() if symbol else None
    events: list[dict] = []
    notes: list[str] = []
    src = f"{client.base_url}/api/announcements/" + (f"?symbol={sym}" if sym else "")
    status = "ok"
    if sym:
        try:
            snap = client.get_stock(sym)
            if snap.live is not None:
                al = price_volume_alert([snap.live.previous_close, snap.live.ltp], [0, 0], threshold_pct)
                if al:
                    events.append({"kind": "threshold", "title": f"{sym}: {al.detail}",
                                   "detail": "Day move computed from previous close to last price.",
                                   "as_of": _iso(snap.live.quoted_at), "source": snap.source_url})
                notes.append(f"Price threshold +/-{threshold_pct:g}% checked against the day move "
                             f"({snap.live.change_pct:+.2f}% as of {_iso(snap.live.quoted_at)}).")
            else:
                notes.append("No live quote, price threshold not evaluated.")
        except MarketDataError as exc:
            notes.append(f"Price threshold not evaluated: {exc}")
            status = "partial"
    try:
        feed = client.get_announcements(symbol=sym, limit=50)
    except MarketDataError as exc:
        return _env("error" if not events else "partial", src, events=events, notes=notes, error=str(exc),
                    threshold_pct=threshold_pct)
    store = AnnouncementStore()
    dup = 0
    for a in sorted(feed.items, key=lambda x: x.published_at):
        out = store.ingest(_to_store_announcement(a))
        if out.kind == "duplicate":
            dup += 1
        elif out.kind == "correction":
            events.append({"kind": "correction", "title": f"{a.symbol}: {a.subject}",
                           "detail": "Same filing re-published with changed content; replaces the earlier version."
                                     + (f" Supersedes {out.supersedes}." if out.supersedes else ""),
                           "as_of": _iso(a.published_at), "source": a.pdf_url or src})
        elif _CORRECTION_RE.search(a.subject):
            events.append({"kind": "issuer_correction", "title": f"{a.symbol}: {a.subject}",
                           "detail": "Subject line mentions a correction/revision (keyword match, not verified).",
                           "as_of": _iso(a.published_at), "source": a.pdf_url or src})
    if dup:
        notes.append(f"{dup} duplicate announcement(s) suppressed.")
    bad = feed.degraded_sources()
    if bad:
        status = "partial"
    events.sort(key=lambda e: e["as_of"] or "", reverse=True)
    if not events and status == "ok":
        status = "empty"
    return _env(status, src, events=events, notes=notes, threshold_pct=threshold_pct,
                coverage_banner=feed.coverage_note() if bad else None, scope=sym or "all companies")


# -- routes ------------------------------------------------------------------

def _valid(symbol: str) -> bool:
    return bool(_SYMBOL_RE.match(symbol))


def _default_directory(client: DownstoxClient) -> CompanyDirectory:
    fx = client.fixtures_dir / "downstox-screener.json" if client.fixtures_dir else None
    return CompanyDirectory(fixture=fx if fx and fx.exists() else None)


def build_ui_routes(client: DownstoxClient, directory: Optional[CompanyDirectory] = None) -> list:
    directory = directory or _default_directory(client)
    no_store = {"Cache-Control": "no-store"}

    async def index(request: Request):
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def search(request: Request):
        q = request.query_params.get("q", "")[:80]
        try:
            matches = await run_in_threadpool(directory.search, q)
        except MarketDataError as exc:
            return JSONResponse({"status": "error", "error": str(exc), "matches": []}, 503, headers=no_store)
        meta = directory.meta()
        status = "ok" if matches else ("empty" if q.strip() else "idle")
        return JSONResponse({"status": status, "query": q, "matches": matches, "list": meta}, headers=no_store)

    async def filing_upload(request: Request):
        """Ingest a filing PDF the user downloaded themselves (raw body).

        No automated retrieval: the bytes come from the user's own upload.
        Title via X-Filing-Title header (defaults to a generic label).

        Shared preview safety: this deployment is publicly reachable without
        per-visitor auth, so uploads are held unless the operator-set
        FINN_UPLOAD_TOKEN env var is present AND the caller supplies it in
        X-Upload-Token. Arbitrary visitors must not be able to overwrite or
        add to the cached evidence the chat cites."""
        import os, hmac
        tok = os.environ.get("FINN_UPLOAD_TOKEN")
        supplied = request.headers.get("x-upload-token") or ""
        if not tok or not hmac.compare_digest(supplied, tok):
            return JSONResponse(_env("error", "user upload",
                                     error="uploads are restricted on this shared preview"),
                                status_code=403)
        symbol = request.path_params["symbol"].upper()
        if not _valid(symbol):
            return JSONResponse(_env("error", "user upload", error="invalid symbol"), status_code=400)
        data = await request.body()
        title = request.headers.get("x-filing-title") or "Uploaded filing PDF"
        try:
            out = await run_in_threadpool(ingest_pdf_bytes, symbol, data, title=title)
        except FilingIngestError as exc:
            return JSONResponse(_env("error", "user upload", error=str(exc)), status_code=400)
        f = out["filings"][-1]
        return JSONResponse(_env("ok", "user upload",
                                 ingested={"subject": f["subject"], "page_count": f["page_count"],
                                           "passage_count": len(f["passages"]), "origin": f["origin"]}))

    async def company(request: Request):
        sym = request.path_params["symbol"]
        if not _valid(sym):
            return JSONResponse({"status": "error", "error": "invalid symbol"}, 400)
        try:
            known = await run_in_threadpool(directory.resolve, sym)
        except MarketDataError:
            known = None  # list unavailable: let upstream decide
        section = request.path_params.get("section")
        if section is None:
            data = await run_in_threadpool(company_aggregate, client, sym)
            return JSONResponse({**data, "directory_match": known}, headers=no_store)
        fn = {"stock": lambda: stock_section(client, sym),
              "announcements": lambda: announcements_section(client, sym),
              "filings": lambda: filings_section(client, sym),
              "promises": lambda: promises_section(client, sym),
              "results": lambda: results_section(sym)}.get(section)
        if fn is None:
            return JSONResponse({"status": "error", "error": "unknown section"}, 404)
        return JSONResponse(await run_in_threadpool(fn), headers=no_store)

    async def market(request: Request):
        section = request.path_params["section"]
        fn = {"status": market_section, "indices": indices_section,
              "sentiments": sentiments_section, "fiidii": fiidii_section}.get(section)
        if fn is None:
            return JSONResponse({"status": "error", "error": "unknown section"}, 404)
        return JSONResponse(await run_in_threadpool(fn, client), headers=no_store)

    async def signals(request: Request):
        sym = request.query_params.get("symbol") or None
        if sym and not _valid(sym):
            return JSONResponse({"status": "error", "error": "invalid symbol"}, 400)
        try:
            thr = float(request.query_params.get("threshold", "5"))
            if not 0 < thr <= 100:
                raise ValueError
        except ValueError:
            return JSONResponse({"status": "error", "error": "threshold must be in (0, 100]"}, 400)
        return JSONResponse(await run_in_threadpool(signals_section, client, sym, thr), headers=no_store)

    return [
        Route("/", index, methods=["GET"]),
        Route("/api/companies/search", search, methods=["GET"]),
        Route("/api/company/{symbol}", company, methods=["GET"]),
        Route("/api/company/{symbol}/filing-upload", filing_upload, methods=["POST"]),
        Route("/api/company/{symbol}/{section}", company, methods=["GET"]),
        Route("/api/market/{section}", market, methods=["GET"]),
        Route("/api/signals", signals, methods=["GET"]),
        Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
    ]


def mount_ui(app: Starlette, client: DownstoxClient, directory: Optional[CompanyDirectory] = None,
             warm: bool = False) -> Starlette:
    """Put the FINN UI routes ahead of the chat app's own '/' and '/{id}' routes.
    /api/chat, /api/health and the rest of the /api mount are untouched.
    warm=True pre-loads the company list in a background thread at startup."""
    directory = directory or _default_directory(client)
    if warm:
        def _warm():
            try:
                directory.ensure()
            except Exception:
                pass  # first search will report the error honestly
        threading.Thread(target=_warm, daemon=True).start()
    for i, route in enumerate(build_ui_routes(client, directory)):
        app.router.routes.insert(i, route)
    return app
