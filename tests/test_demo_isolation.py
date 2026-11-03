"""Fictional seed/Demotech data must never reach a live-company path."""
import ast
import json
from pathlib import Path

from finn import seed
from finn.ui_api import company_aggregate, results_section

FINN = Path(__file__).parent.parent / "finn"
LIVE_MODULES = ["ui_api.py", "pull.py", "market_data.py", "web_agent.py", "mcp_server.py",
                "live_feed.py", "real_seed.py", "agents.py", "alerts.py", "ledger.py"]
DEMO_NAMES = ("Demotech", "Meridian Demo", "Example Components")


def test_live_modules_do_not_import_seed():
    for name in LIVE_MODULES:
        path = FINN / name
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            mods = []
            if isinstance(node, ast.ImportFrom):
                mods = [node.module or ""] + [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            assert not any(m == "seed" or m.endswith(".seed") for m in mods), f"{name} imports seed"


def test_live_results_path_never_returns_demotech_rows():
    for sym in ("DEMOTECH", "INFY", "RELIANCE", "MERIDIAN"):
        out = results_section(sym)
        assert out["status"] == "empty" and out["loaded"] is False
        assert "rows" not in out
        assert not any(n in json.dumps(out) for n in DEMO_NAMES)
    assert results_section("TCS")["company"] == "Tata Consultancy Services"
    demo_cos = {f.company for f in seed.demo_filings()}
    assert results_section("TCS")["company"] not in demo_cos


def test_company_aggregate_results_section_has_no_seed_rows():
    class Dead:
        base_url = "http://invalid.local"
        fixtures_dir = None
        def __getattr__(self, name):
            def boom(*a, **k):
                from finn.market_data import MarketDataError
                raise MarketDataError("offline")
            return boom
    out = company_aggregate(Dead(), "INFY")
    res = out["sections"]["results"]
    assert res["status"] == "empty" and res.get("loaded") is False
    assert not any(n in json.dumps(out, default=str) for n in DEMO_NAMES)


def test_demo_surfaces_are_labelled_fictional():
    from finn.web import build
    page = build()
    assert "FICTIONAL" in page
    assert page.count("Demotech Industries (FICTIONAL demo)") >= 3
    assert "FICTIONAL" in seed.DEMO_LABEL
