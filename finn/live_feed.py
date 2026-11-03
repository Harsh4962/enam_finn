"""Live announcement feed -> dedupe/revision store -> labeled digest.

Bridges the Downstox merged NSE/BSE feed into the same AnnouncementStore the
graded core uses, so dedupe and correction semantics apply to real data.
Every emitted item keeps provenance: exchange, published_at, source PDF URL,
plus the feed's per-exchange health (a failed/withheld leg is surfaced as
partial coverage, never read as "no filings").
"""
from __future__ import annotations

import hashlib
import html
from datetime import datetime
from typing import Optional

from .market_data import AnnouncementFeed, AnnouncementItem, DownstoxClient
from .models import Announcement
from .store import AnnouncementStore, IngestOutcome

CATEGORY_MAP = {
    "financial results": "results",
    "result": "results",
    "press release": "press_release",
    "change in director": "management_change",
    "change in management": "management_change",
    "resignation": "management_change",
    "appointment": "management_change",
    "dividend": "dividend",
    "bonus": "bonus_split",
    "split": "bonus_split",
    "credit rating": "credit_rating",
    "earnings call": "earnings_call",
    "concall": "earnings_call",
    "investor meet": "investor_meetings",
    "analyst": "investor_meetings",
    "acquisition": "M&A",
    "merger": "M&A",
    "amalgamation": "M&A",
    "litigation": "litigation",
    "pledge": "pledging",
    "fund raising": "fund_raise",
    "preferential": "fund_raise",
    "agm": "agm_egm",
    "egm": "agm_egm",
    "postal ballot": "postal_ballot",
    "capex": "capex",
    "insider": "insider_trading",
}


def map_category(upstream_category: Optional[str], subject: str) -> str:
    text = f"{upstream_category or ''} {subject}".lower()
    for needle, cat in CATEGORY_MAP.items():
        if needle in text:
            return cat
    return "unclassified"


def _stable_id(item: AnnouncementItem) -> str:
    if item.pdf_url:
        return item.pdf_url.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return hashlib.sha1(f"{item.exchange}|{item.symbol}|{item.published_at}|{item.subject}".encode()).hexdigest()[:16]


def to_announcement(item: AnnouncementItem) -> Announcement:
    return Announcement(
        exchange=item.exchange,  # type: ignore[arg-type]
        announcement_id=_stable_id(item),
        company=item.company,
        headline=item.subject,
        filed_at=item.published_at,
        category=map_category(item.category, item.subject),
        body_text="",
    )


def ingest_feed(feed: AnnouncementFeed, store: AnnouncementStore,
                watchlist: Optional[list[str]] = None) -> list[tuple[IngestOutcome, Announcement]]:
    """Ingest feed items through the dedupe/revision store. Returns outcomes in
    feed order. watchlist (NSE symbols) filters when provided."""
    out = []
    watch = {s.upper() for s in watchlist} if watchlist else None
    for item in feed.items:
        if watch and item.symbol.upper() not in watch:
            continue
        ann = to_announcement(item)
        out.append((store.ingest(ann), ann))
    return out


LIVE_BANNER = ("LIVE DATA - third-party public feed (Downstox), not an official "
               "exchange feed. Fetched {generated_at} UTC. {coverage}")


def render_live_digest(feed: AnnouncementFeed, outcomes: list[tuple[IngestOutcome, Announcement]],
                       path: str) -> str:
    generated = feed.generated_at.strftime("%Y-%m-%d %H:%M")
    banner = LIVE_BANNER.format(generated_at=generated, coverage=feed.coverage_note())
    rows = []
    for outcome, ann in outcomes:
        item = next(i for i in feed.items if _stable_id(i) == ann.announcement_id)
        link = f'<a href="{html.escape(item.pdf_url)}">source filing</a>' if item.pdf_url else "no PDF link"
        label = {"new": "NEW", "correction": "CORRECTION", "duplicate": "SEEN"}[outcome.kind]
        rows.append(f"""<div class="card {outcome.kind}">
<span class="pill">{label} · {html.escape(ann.category)} · {html.escape(ann.exchange)}</span>
<h3>{html.escape(ann.company)} ({html.escape(item.symbol)})</h3>
<p>{html.escape(ann.headline)}</p>
<p class="meta">filed {ann.filed_at.strftime('%Y-%m-%d %H:%M')} UTC · {link}</p>
</div>""")
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{font-family:Georgia,serif;background:#faf7f2;color:#1c1a17;margin:0;padding:16px;max-width:640px;margin:0 auto}}
.banner{{background:#e8f0fd;color:#1d3d8f;border-radius:10px;padding:10px 14px;font-size:13px;font-weight:600;margin-bottom:16px}}
.card{{background:#fff;border:1px solid #e5ded3;border-radius:12px;padding:12px 14px;margin-bottom:10px}}
.card.correction{{border-left:4px solid #c2410c}}
.pill{{font-size:11px;font-weight:700;letter-spacing:.4px;color:#5b564c}}
h3{{margin:6px 0 4px;font-size:16px}} p{{margin:4px 0;font-size:14px}}
.meta{{font-size:12px;color:#7a7468}} a{{color:#1d3d8f}}
</style></head><body>
<div class="banner">{html.escape(banner)}</div>
<h2>Announcements · live feed</h2>
<p class="meta">{len(outcomes)} items after dedupe; generated {html.escape(generated)} UTC</p>
{''.join(rows)}
</body></html>"""
    with open(path, "w") as f:
        f.write(doc)
    return path


def run_live_digest(client: DownstoxClient, path: str, watchlist: Optional[list[str]] = None,
                    limit: int = 20) -> tuple[AnnouncementFeed, list[tuple[IngestOutcome, Announcement]]]:
    feed = client.get_announcements(limit=limit)
    store = AnnouncementStore()
    outcomes = ingest_feed(feed, store, watchlist=watchlist)
    render_live_digest(feed, outcomes, path)
    return feed, outcomes


if __name__ == "__main__":
    import sys
    run_live_digest(DownstoxClient(), sys.argv[1] if len(sys.argv) > 1 else "live_digest.html")
