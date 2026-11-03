from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from finn.market_data import (DownstoxClient, MarketDataError,
                              promise_to_guidance)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def client() -> DownstoxClient:
    return DownstoxClient(fixtures_dir=FIXTURES)


def test_stock_snapshot_typed_with_provenance(client):
    s = client.get_stock("tcs")  # case-insensitive
    assert s.symbol == "TCS" and s.exchange == "NSE"
    assert s.live is not None and s.live.ltp == 2502.6
    assert s.live.stale is False
    assert s.live.quoted_at.year == 2026
    assert s.fundamentals is not None and s.fundamentals.pe == 16.6
    assert s.source == "downstox" and "downstox.com" in s.source_url
    assert s.fetched_at.tzinfo is not None


def test_announcement_feed_exposes_degraded_source(client):
    feed = client.get_announcements("TCS")
    # BSE failed+withheld in the fixture: abstention must be explicit, never
    # "no filings exist".
    assert feed.items == []
    degraded = feed.degraded_sources()
    assert any(s.name == "bse" and s.status == "failed" and s.withheld for s in degraded)
    assert "does not prove no filing exists" in feed.coverage_note()


def test_announcement_feed_items_parse(client):
    feed = client.get_announcements()
    assert len(feed.items) == 5
    first = feed.items[0]
    assert first.exchange in ("NSE", "BSE")
    assert first.pdf_url is None or first.pdf_url.startswith("https://")
    assert first.published_at.tzinfo is not None


def test_promise_range_bound_preserved(client):
    promises = client.get_promises("TCS")
    g = promise_to_guidance(promises[0])
    # "26% to 28%" stays a range - never collapsed to a point target.
    assert g.bound == "range"
    assert g.target_pct == 26.0 and g.target_hi_pct == 28.0
    assert g.hedge == "aspiration"
    assert "26% to 28%" in g.stated_target
    assert g.quote.startswith("Overall, our long-term aspiration")
    assert "nsearchives.nseindia.com" in (g.source_type or "")


def test_promise_without_parse_abstains(client):
    promises = client.get_promises("TCS")
    for p in promises:
        g = promise_to_guidance(p)
        if not (p.norm_target and p.norm_target.parsed):
            assert g.target_pct is None  # abstain, don't invent


def test_market_status_weekend(client):
    ms = client.get_market_status()
    assert ms.open is False and ms.state == "weekend"
    assert ms.last_trading_day and ms.next_trading_day


def test_missing_fixture_raises_not_fabricates(client):
    with pytest.raises(MarketDataError):
        client.get_stock("NOSUCHSYMBOL")


def test_mcp_tools_roundtrip(monkeypatch):
    monkeypatch.setenv("FINN_FIXTURES", str(FIXTURES))
    import finn.mcp_server as srv
    importlib.reload(srv)
    snap = srv.get_stock_snapshot.fn("TCS") if hasattr(srv.get_stock_snapshot, "fn") else srv.get_stock_snapshot("TCS")
    assert snap["symbol"] == "TCS" and snap["live"]["ltp"] == 2502.6
    ann = srv.get_latest_announcements.fn("TCS") if hasattr(srv.get_latest_announcements, "fn") else srv.get_latest_announcements("TCS")
    assert ann["items"] == [] and any(s["status"] == "failed" for s in ann["sources"])
    status = srv.get_market_status.fn() if hasattr(srv.get_market_status, "fn") else srv.get_market_status()
    assert status["state"] == "weekend"


def test_agent_tools_callable_offline():
    from pydantic_ai.models.test import TestModel
    from finn.web_agent import build_agent
    agent = build_agent(model="test", client=DownstoxClient(fixtures_dir=FIXTURES))
    assert isinstance(agent.model, TestModel)
    result = agent.run_sync("What is TCS trading at?")
    assert result.output  # TestModel exercises the tool surface end to end


def test_web_app_constructs():
    from finn.web_agent import build_web_app
    app = build_web_app(model="test", client=DownstoxClient(fixtures_dir=FIXTURES))
    assert app is not None


def test_live_feed_dedupe_and_revision():
    from finn.live_feed import ingest_feed, to_announcement, map_category
    from finn.store import AnnouncementStore
    client = DownstoxClient(fixtures_dir=FIXTURES)
    feed = client.get_announcements()
    store = AnnouncementStore()
    first = ingest_feed(feed, store)
    assert all(o.kind == "new" for o, _ in first)
    second = ingest_feed(feed, store)
    assert all(o.kind == "duplicate" for o, _ in second)  # never re-pushed
    # a changed body under the same id is a correction, not a fresh alert
    ann = to_announcement(feed.items[0])
    ann.body_text = "revised content"
    outcome = store.ingest(ann)
    assert outcome.kind == "correction"


def test_category_mapping():
    from finn.live_feed import map_category
    assert map_category("Change in Director(s)", "...") == "management_change"
    assert map_category("Press Release", "announced its financial results") == "results"
    assert map_category(None, "Credit rating reaffirmed") == "credit_rating"
    assert map_category(None, "something else entirely") == "unclassified"


def test_watchlist_filter():
    from finn.live_feed import ingest_feed
    from finn.store import AnnouncementStore
    client = DownstoxClient(fixtures_dir=FIXTURES)
    feed = client.get_announcements()
    outcomes = ingest_feed(feed, AnnouncementStore(), watchlist=["ZENTEC"])
    assert outcomes and all("Zen" in a.company for _, a in outcomes)


def test_live_digest_render_has_provenance(tmp_path):
    from finn.live_feed import run_live_digest
    client = DownstoxClient(fixtures_dir=FIXTURES)
    out = tmp_path / "live.html"
    feed, outcomes = run_live_digest(client, str(out))
    text = out.read_text()
    assert "LIVE DATA - third-party public feed (Downstox)" in text
    assert "does not prove no filing exists" in text  # BSE leg failed in fixture
    assert "nsearchives.nseindia.com" in text  # source filing links preserved
    assert "SEEN" not in text  # first ingest is all NEW


def test_non_https_pdf_link_dropped():
    from finn.market_data import AnnouncementItem
    item = AnnouncementItem(exchange="NSE", symbol="X", company="X Co",
                            subject="s", publishedAt="2026-11-01T00:00:00Z",
                            pdfUrl="javascript:alert(1)")
    assert item.pdf_url is None
    ok = AnnouncementItem(exchange="NSE", symbol="X", company="X Co",
                          subject="s", publishedAt="2026-11-01T00:00:00Z",
                          pdfUrl="https://nsearchives.nseindia.com/corporate/a.pdf")
    assert ok.pdf_url is not None


def test_demo_answer_degrades_when_upstream_down():
    from finn.web_agent import _demo_answer

    class DownClient:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise RuntimeError("upstream unreachable")
            return boom

    reply = _demo_answer("What is TCS trading at?", DownClient())
    assert "temporarily unreachable" in reply
    assert "not an AI research answer" in reply
