"""Replay one seeded day through the pipeline and print the push digest.

Usage: python -m finn.demo            (offline fixture classifier)
       FINN_MODEL=google-gla:gemini-3.8-flash python -m finn.demo --live
"""
from __future__ import annotations

import sys

from .agents import build_classifier
from .ledger import growth_pct, promised_vs_delivered
from .models import Metric
from .seed import DEMO_LABEL, classify_fixture, demo_announcements, demo_filings, demo_guidance
from .store import AnnouncementStore

MATERIAL = {"results", "M&A", "fund_raise", "management_change", "litigation", "credit_rating", "pledging"}


def run(live: bool = False) -> str:
    store = AnnouncementStore()
    agent = build_classifier() if live else None
    pushed: dict[str, str] = {}   # announcement_id -> digest line
    corrections: list[str] = []
    collapsed = 0
    for ann in demo_announcements():
        outcome = store.ingest(ann)
        if outcome.kind == "duplicate":
            continue
        if live:
            cls = agent.run_sync(f"{ann.headline}\n\n{ann.body_text}").output
            ann.category, material, summary = cls.category, cls.material, cls.one_line_summary
        else:
            ann.category = classify_fixture(ann.headline)
            material, summary = ann.category in MATERIAL, ann.headline
        line = f"[{ann.category}] {ann.company}: {summary}"
        if outcome.kind == "correction":
            if outcome.supersedes:
                pushed.pop(outcome.supersedes, None)  # a correction replaces, never duplicates
            corrections.append("CORRECTION " + line + (f" (supersedes {outcome.supersedes})" if outcome.supersedes else ""))
        elif material:
            pushed[ann.announcement_id] = line
        else:
            collapsed += 1

    filings = demo_filings()
    entries = promised_vs_delivered("Demotech Industries", demo_guidance(), filings)
    qoq = growth_pct(filings[-1], filings[-2], Metric.REVENUE_FROM_OPERATIONS)
    ledger_lines = [
        f"  {e['metric']}: {e['status']}"
        + (f" (target {e.get('target_display', e.get('target_pct'))}, actual {e.get('actual_pct')}%)" if "actual_pct" in e else "")
        + (f" - {e.get('note', '')}" if e["status"] in ("no_stated_guidance", "not_comparable") else "")
        for e in entries
    ]
    digest = "\n".join([
        f"=== FINN digest (last 24h) - {DEMO_LABEL} ===",
        "", "Priority:", *[f"  {p}" for p in corrections + list(pushed.values())],
        f"  ({collapsed} routine item(s) collapsed)",
        "", f"Demotech Industries QoQ revenue-from-operations growth: {qoq:.1f}%" if qoq is not None else "QoQ not comparable",
        "Guidance ledger:", *ledger_lines,
    ])
    print(digest)
    return digest


if __name__ == "__main__":
    run(live="--live" in sys.argv)
