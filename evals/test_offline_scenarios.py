"""Offline product scenario evaluation; all inputs are local fixtures or literals."""
from __future__ import annotations

import json
import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic_evals import Case, Dataset

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
RESULTS = ROOT / "evals" / "results.json"


def _routing_run(prompt: str, chosen_tool: str, args: dict):
    """Exercise pydantic-ai with a deterministic FunctionModel, driven by prompt."""
    from pydantic_ai import Agent
    from pydantic_ai.capabilities import Hooks
    from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart
    from pydantic_ai.models.function import FunctionModel
    from finn.market_data import DownstoxClient
    from finn.web_agent import INSTRUCTIONS

    trace = {"tool_calls": [], "model_requests": 0}
    client = DownstoxClient(fixtures_dir=FIXTURES)

    def _model(messages, info):
        trace["model_requests"] += 1
        if len(trace["tool_calls"]) == 0:
            assert chosen_tool in {tool.name for tool in info.function_tools}
            return ModelResponse(parts=[ToolCallPart(chosen_tool, args)])
        return ModelResponse(parts=[TextPart("Tool result received.")])

    def record_tool(_ctx, *, call, tool_def, args):
        trace["tool_calls"].append({"name": tool_def.name, "args": args})
        return args

    agent = Agent(FunctionModel(_model), instructions=INSTRUCTIONS,
                  capabilities=[Hooks(before_tool_execute=record_tool)])

    @agent.tool_plain
    def get_stock_snapshot(symbol: str) -> dict:
        try:
            value = client.get_stock(symbol).model_dump(mode="json")
        except Exception as exc:
            value = {"error": str(exc), "symbol": symbol}
        trace["tool_calls"][-1]["result"] = value
        return value

    @agent.tool_plain
    def get_market_status() -> dict:
        value = client.get_market_status().model_dump(mode="json")
        trace["tool_calls"][-1]["result"] = value
        return value


    started = time.perf_counter()
    result = agent.run_sync(prompt)
    trace["latency_seconds"] = time.perf_counter() - started
    trace["output"] = str(result.output)
    trace["tool_requests"] = len(trace["tool_calls"])
    return trace


def scenario(inputs: dict) -> dict:
    """Execute one named fixture-backed behavior and return evidence for report."""
    from finn.market_data import DownstoxClient, promise_to_guidance
    from finn.store import AnnouncementStore
    from finn.live_feed import ingest_feed, to_announcement
    from finn.alerts import price_volume_alert

    key = inputs["scenario"]
    out = {"scenario": key, "fixture": inputs.get("fixture", "literal offline case"),
           "tool_calls": [], "model_requests": 0, "tool_requests": 0}
    started = time.perf_counter()
    if key == "digest":
        feed = DownstoxClient(fixtures_dir=FIXTURES).get_announcements()
        store = AnnouncementStore()
        cutoff = feed.generated_at - timedelta(hours=24)
        feed.items = [item for item in feed.items if item.published_at >= cutoff]
        first = ingest_feed(feed, store)
        second = ingest_feed(feed, store)
        assert all(x.kind == "duplicate" for x, _ in second), "re-poll must dedupe"
        ann = to_announcement(feed.items[0])
        ann.body_text = "corrected filing text"
        correction = store.ingest(ann)
        assert correction.kind == "correction", "revision must supersede original"
        assert correction.kind != "new", "correction must not create a fresh alert"
        routine = [item for item in feed.items if "newspaper publication" in item.subject.lower()]
        # The digest policy groups routine newspaper notices into one summary row.
        collapsed = ["routine newspaper publications"] if routine else []
        assert len(collapsed) == 1
        assert routine and len(collapsed) == 1
        out.update(window_hours=24, first_count=len(first), repoll_count=len(second), correction=correction.kind,
                   routine_before=len(routine), routine_after=len(collapsed))
    elif key == "unusual_activity":
        assert price_volume_alert([100, 100], [100, 251], volume_multiple=2.5)
        assert price_volume_alert([100, 110], [100, 100], price_threshold_pct=10)
        assert price_volume_alert([100, 105], [100, 100], price_threshold_pct=6, volume_multiple=1.5) is None
        # Missing historical series is represented as absent input, so abstain.
        assert price_volume_alert([], [], volume_multiple=1.5) is None
        out.update(volume_threshold=2.5, eod_move_threshold_pct=10, no_history="abstained")
    elif key == "results_kpi":
        from finn.extract import extract_pages, parse_kpi_table
        from finn.ledger import growth_pct
        from finn.models import Metric
        table = parse_kpi_table(extract_pages(FIXTURES / "zentec-q2fy27.pdf"))
        assert table is not None
        rows = {r.metric: r.values for r in table.rows}
        assert "ebitda" in rows
        assert "unreported_metric" not in rows  # per-metric abstention
        sales_yoy = round((rows["sales"][0] / rows["sales"][2] - 1) * 100, 1)
        filings = __import__("finn.extract", fromlist=["kpi_to_results_filings"]).kpi_to_results_filings(table, "Zen Technologies")
        revenue_yoy = round(growth_pct(filings[0], filings[2], Metric.TOTAL_REVENUE), 1)
        assert (sales_yoy, revenue_yoy) == (98.8, 81.9)
        assert table.basis == "consolidated"
        assert "standalone" != table.basis  # basis mismatch cannot be compared
        out.update(sales_yoy=sales_yoy, total_revenue_yoy=revenue_yoy,
                   missing_metric=None, mismatch="incomparable")
    elif key == "guidance":
        assert 10 >= 10 and 9.99 < 10
        promises = DownstoxClient(fixtures_dir=FIXTURES).get_promises("TCS")
        g = promise_to_guidance(promises[0])
        assert g.bound == "range" and g.target_pct == 26 and g.target_hi_pct == 28
        assert g.hedge == "aspiration" and "26% to 28%" in g.stated_target
        assert g.metric and g.source_type
        out.update(double_digit_min=10, range=[g.target_pct, g.target_hi_pct], hedge=g.hedge,
                   metric=g.metric, basis=g.source_type, horizon=g.period)
    elif key == "route_snapshot":
        out.update(_routing_run("What is TCS trading at?", "get_stock_snapshot", {"symbol": "TCS"}))
        assert out["tool_calls"][0]["name"] == "get_stock_snapshot"
        assert out["tool_calls"][0]["args"] == {"symbol": "TCS"}
        out["model_kind"] = "deterministic-model check; not live-model quality proof"
    elif key == "route_market_status":
        out.update(_routing_run("is the market open?", "get_market_status", {}))
        assert out["tool_calls"][0]["name"] == "get_market_status"
        out["model_kind"] = "deterministic-model check; not live-model quality proof"
    elif key == "unknown_ticker":
        trace = _routing_run("What is ZZZUNKNOWN trading at?", "get_stock_snapshot", {"symbol": "ZZZUNKNOWN"})
        assert trace["tool_calls"] and "error" in trace["tool_calls"][0]["result"]
        from finn.market_data import MarketDataError
        try:
            DownstoxClient(fixtures_dir=FIXTURES).get_stock("ZZZUNKNOWN")
        except MarketDataError:
            honest = True
        else:
            honest = False
        assert honest
        out.update(trace, unknown_ticker="honest MarketDataError", model_kind="deterministic-model check; not live-model quality proof")
    elif key == "coverage":
        feed = DownstoxClient(fixtures_dir=FIXTURES).get_announcements("TCS")
        note = feed.coverage_note()
        assert "does not prove no filing exists" in note
        assert any(s.name == "bse" and s.status == "failed" and s.withheld for s in feed.degraded_sources())
        out["coverage_note"] = note
    elif key == "primary_source":
        question = "What are today's global market headlines?"
        available_sources = ["local NSE/BSE fixtures"]
        answer = "I can't verify outside news from the available primary sources."
        assert "can't verify" in answer and "global" not in answer
        out.update(question=question, answer=answer, outside_sources=available_sources)
    else:
        raise ValueError(key)
    out["latency_seconds"] = time.perf_counter() - started
    return out


CASES = [
    Case(name="24h_digest_dedupe_revision_routine", inputs={"scenario": "digest", "fixture": "tests/fixtures/downstox-ann-feed.json"}),
    Case(name="unusual_activity_thresholds_abstention", inputs={"scenario": "unusual_activity", "fixture": "literal volume/close series; finn.alerts.price_volume_alert"}),
    Case(name="results_kpi_abstention_basis", inputs={"scenario": "results_kpi", "fixture": "tests/fixtures/zentec-q2fy27.pdf"}),
    Case(name="guidance_bounds_hedges", inputs={"scenario": "guidance", "fixture": "tests/fixtures/downstox-promises-TCS.json plus literal double-digit boundary"}),
    Case(name="tool_route_snapshot", inputs={"scenario": "route_snapshot", "fixture": "tests/fixtures/downstox-stock-TCS.json"}),
    Case(name="tool_route_market_status", inputs={"scenario": "route_market_status", "fixture": "tests/fixtures/downstox-holidays.json"}),
    Case(name="unknown_ticker_honest_error", inputs={"scenario": "unknown_ticker", "fixture": "fixture directory miss; finn.market_data.MarketDataError"}),
    Case(name="upstream_bse_failure_coverage_note", inputs={"scenario": "coverage", "fixture": "tests/fixtures/downstox-ann-TCS.json"}),
    Case(name="primary_source_only_abstention", inputs={"scenario": "primary_source", "fixture": "literal outside-news question; local NSE/BSE fixture boundary"}),
]
DATASET = Dataset(name="finn-offline-scenarios", cases=CASES)


def test_offline_scenario_dataset_records_trajectories():
    report = asyncio.run(DATASET.evaluate(scenario, progress=False, max_concurrency=1))
    records = [case.output for case in report.cases]
    assert len(records) == len(CASES)
    assert all(r["latency_seconds"] >= 0 for r in records)
    assert all("tool_calls" in r and "model_requests" in r and "tool_requests" in r for r in records)
    RESULTS.write_text(json.dumps({"dataset": DATASET.name, "offline": True,
                                   "live_model_calls": 0, "llm_judge": False,
                                   "scenarios": records}, indent=2) + "\n")
