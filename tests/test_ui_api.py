"""Offline tests for the web UI backend (screener cache, search, company sections)."""
import json
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from finn.market_data import DownstoxClient
from finn.ui_api import CompanyDirectory, announcements_section, company_aggregate, signals_section, stock_section

FIXTURES = Path(__file__).parent / "fixtures"
SCREENER = json.loads((FIXTURES / "downstox-screener.json").read_text())


def _directory(tmp_path, calls, clock, fail=False, ttl=86400):
    def handler(request):
        calls.append(str(request.url))
        if fail:
            return httpx.Response(503)
        return httpx.Response(200, json=SCREENER)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CompanyDirectory(cache_path=tmp_path / "s.json", http=http, ttl=ttl, clock=lambda: clock[0])


def test_screener_cached_and_refreshed_daily(tmp_path):
    calls, clock = [], [1000.0]
    d = _directory(tmp_path, calls, clock)
    d.search("tcs"); d.search("infosys"); d.search("tata")
    assert len(calls) == 1 and calls[0] == "https://downstox.com/api/screener/"
    clock[0] += 86400 + 1
    d.search("tcs")
    assert len(calls) == 2  # refreshed once after a day


def test_cache_survives_restart_and_stale_fallback(tmp_path):
    calls, clock = [], [1000.0]
    _directory(tmp_path, calls, clock).search("tcs")
    d2 = _directory(tmp_path, calls, clock)  # new process, same disk cache
    assert d2.search("tcs")[0]["symbol"] == "TCS" and len(calls) == 1
    clock[0] += 3 * 86400
    d3 = _directory(tmp_path, [], clock, fail=True)  # refresh fails: serve last good copy, flagged
    assert d3.search("reliance")[0]["symbol"] == "RELIANCE"
    assert d3.meta()["stale"] is True


def test_no_cache_and_upstream_down_raises(tmp_path):
    from finn.market_data import MarketDataError
    d = _directory(tmp_path, [], [0.0], fail=True)
    with pytest.raises(MarketDataError):
        d.search("tcs")


def test_search_ranking(tmp_path):
    d = _directory(tmp_path, [], [0.0])
    syms = [m["symbol"] for m in d.search("tata")]
    assert set(syms) == {"TCS", "TATAMOTORS", "TATASTEEL"}
    assert d.search("tcs")[0]["symbol"] == "TCS"            # exact symbol first
    assert d.search("zen tech")[0]["symbol"] == "ZENTEC"     # name substring
    assert d.search("nonexistent corp") == []
    assert d.resolve("infy")["name"] == "Infosys Ltd" and d.resolve("NOPE") is None


@pytest.fixture
def client(tmp_path):
    from finn.web_agent import build_web_app
    app = build_web_app(model="test", client=DownstoxClient(fixtures_dir=FIXTURES))
    return TestClient(app, base_url="http://127.0.0.1")


def test_search_endpoint_states(client):
    ok = client.get("/api/companies/search", params={"q": "infos"}).json()
    assert ok["status"] == "ok" and ok["matches"][0]["symbol"] == "INFY"
    nf = client.get("/api/companies/search", params={"q": "zzzzqq"}).json()
    assert nf["status"] == "empty" and nf["matches"] == []
    assert client.get("/api/companies/search", params={"q": ""}).json()["status"] == "idle"


def test_existing_contracts_unchanged(client):
    assert client.get("/api/health").json() == {"ok": True}
    page = client.get("/")
    assert page.status_code == 200 and "FINN" in page.text and "/static/app.js" in page.text
    assert client.get("/static/app.css").status_code == 200


def test_company_aggregate_ok_and_partial(client):
    d = client.get("/api/company/TCS").json()
    s = d["sections"]
    assert s["stock"]["identity"]["isin"] == "INE467B01029"
    assert s["stock"]["quote"]["status"] == "ok" and "stale" in s["stock"]["quote"]["freshness"]
    assert s["stock"]["fundamentals"]["mcap"] == 904550 and "crore" in s["stock"]["fundamentals"]["mcap_unit"]
    assert "fScore" not in json.dumps(s["stock"])
    assert s["stock"]["peers"]["items"][0]["symbol"] == "INFY"
    assert s["announcements"]["status"] == "partial" and "BSE failed" in s["announcements"]["coverage_banner"]
    assert s["promises"]["status"] == "ok" and s["promises"]["items"][0]["lo"] == 26
    rows = {r["metric"]: r for r in s["results"]["rows"]}
    assert rows["revenue_from_operations"]["value_crore"] == 73188
    assert rows["total_revenue"]["value_crore"] is None and rows["total_revenue"]["state"].startswith("not stated")


def test_unknown_company_errors_per_section_not_500(client):
    d = client.get("/api/company/NOSUCH").json()
    assert d["sections"]["stock"]["status"] == "error"
    assert d["sections"]["promises"]["status"] == "error"
    assert d["sections"]["results"]["loaded"] is False and d["sections"]["results"]["status"] == "empty"
    assert client.get("/api/company/bad%20sym").status_code == 400
    assert client.get("/api/company/TCS/nope").status_code == 404


def test_section_failure_is_isolated(monkeypatch):
    import finn.ui_api as u
    monkeypatch.setattr(u, "promises_section", lambda c, s: (_ for _ in ()).throw(RuntimeError("boom")))
    out = company_aggregate(DownstoxClient(fixtures_dir=FIXTURES), "TCS")
    assert out["sections"]["promises"]["status"] == "error" and "boom" in out["sections"]["promises"]["error"]
    assert out["sections"]["stock"]["status"] == "ok"


def test_stock_partial_fundamentals_and_empty_quote(tmp_path):
    raw = json.loads((FIXTURES / "downstox-stock-TCS.json").read_text())
    raw["fundamentals"].pop("roce"); raw["live"] = None
    (tmp_path / "downstox-stock-TCS.json").write_text(json.dumps(raw))
    out = stock_section(DownstoxClient(fixtures_dir=tmp_path), "TCS")
    assert out["fundamentals"]["status"] == "partial" and out["fundamentals"]["missing"] == ["roce"]
    assert out["quote"]["status"] == "empty"


def test_market_sections_unavailable_without_probe_data(client):
    st = client.get("/api/market/status").json()
    assert st["status"] == "ok" and st["open"] is False and st["last_trading_day"] == "Fri 30 Oct"
    for sec in ("indices", "sentiments", "fiidii"):  # no fixture recorded -> explicit unavailable
        assert client.get(f"/api/market/{sec}").json()["status"] == "unavailable"


def test_market_sections_render_verified_fields(tmp_path):
    (tmp_path / "downstox-india-markets-overview.json").write_text(json.dumps({
        "success": True, "timestamp": "2026-11-01T10:00:00Z",
        "indices": {"nse": {"price": 100.5, "change": 0.5, "changeAbs": 0.5, "isLive": False, "marketTime": "t"}},
        "sectors": [{"name": "Bank Nifty", "price": 5, "change": -1}], "extras": [{"name": "India VIX", "price": 15, "change": 1}]}))
    (tmp_path / "downstox-fiidii-daily.json").write_text(json.dumps({
        "success": True, "date": "30-Oct-2026", "fii": {"buyValue": 2, "sellValue": 1, "netValue": 1}, "dii": None}))
    from finn.ui_api import fiidii_section, indices_section
    c = DownstoxClient(fixtures_dir=tmp_path)
    i = indices_section(c)
    assert i["indices"][0]["label"].startswith("NSE") and i["sectors"][0]["change_pct"] == -1
    f = fiidii_section(c)
    assert f["rows"] == [{"label": "FII/FPI", "buy": 2, "sell": 1, "net": 1}]
    assert f["unit"] == "INR crore" and f["unit_basis"] == "api_documentation"
    assert "unit_note" not in f


def test_signals_threshold_and_corrections(tmp_path):
    stock = json.loads((FIXTURES / "downstox-stock-TCS.json").read_text())
    stock["live"]["previousClose"], stock["live"]["ltp"] = 100.0, 107.0
    (tmp_path / "downstox-stock-TCS.json").write_text(json.dumps(stock))
    base = {"exchange": "NSE", "symbol": "TCS", "company": "TCS", "category": "results",
            "pdfUrl": "https://nsearchives.nseindia.com/a.pdf"}
    anns = [dict(base, subject="Results", publishedAt="2026-10-01T00:00:00Z"),
            dict(base, subject="Results", publishedAt="2026-10-01T00:00:00Z"),   # duplicate
            dict(base, subject="Results (revised)", publishedAt="2026-10-02T00:00:00Z"),  # same PDF, new text
            dict(base, subject="Corrigendum to notice", publishedAt="2026-10-03T00:00:00Z", pdfUrl=None)]
    (tmp_path / "downstox-ann-TCS.json").write_text(json.dumps({
        "success": True, "announcements": anns, "generatedAt": "2026-11-01T00:00:00Z",
        "sources": {"nse": "ok", "bse": "ok"}, "sourceAges": {}}))
    out = signals_section(DownstoxClient(fixtures_dir=tmp_path), "TCS", 5.0)
    kinds = sorted(e["kind"] for e in out["events"])
    assert kinds == ["correction", "issuer_correction", "threshold"]
    assert "duplicate" in " ".join(out["notes"])
    # a bigger threshold clears the price alert
    assert "threshold" not in [e["kind"] for e in signals_section(DownstoxClient(fixtures_dir=tmp_path), "TCS", 10)["events"]]


def test_signals_empty_state_and_validation(client):
    d = client.get("/api/signals", params={"symbol": "TCS"}).json()
    assert d["status"] == "partial" and d["coverage_banner"]  # fixture BSE leg failed
    assert d["events"] == []
    assert client.get("/api/signals", params={"threshold": "abc"}).status_code == 400
    assert client.get("/api/signals", params={"symbol": "a b"}).status_code == 400


def test_announcements_error_envelope(tmp_path):
    out = announcements_section(DownstoxClient(fixtures_dir=tmp_path), "TCS")
    assert out["status"] == "error" and "no fixture" in out["error"]


def test_static_assets_have_no_hardcoded_figures_or_chart():
    root = Path(__file__).parent.parent / "finn" / "static"
    text = (root / "app.js").read_text() + (root / "index.html").read_text()
    for banned in ("canvas", "Win Rate", "AI Accuracy", "Total P/L", "John"):
        assert banned not in text
    assert "prefers-color-scheme: dark" not in (root / "app.css").read_text()


def test_static_ui_provenance_links_not_raw_urls():
    js = (Path(__file__).parent.parent / "finn" / "static" / "app.js").read_text()
    assert "src-link" in js and "unit not stated" not in js
    assert "Source: ${esc(src)}" not in js  # raw URL is no longer printed as text
