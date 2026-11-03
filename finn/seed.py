"""Synthetic demo fixtures. Fictional companies and numbers, clearly labeled.

!!! FICTIONAL DATA !!! Demotech Industries, Meridian Demo Foods, Example
Components and every figure here (including the synthetic total_revenue) are
invented for the replay demo. They are NOT real filings and must never be
returned for a real company/symbol. Only the offline replay digests (finn.demo,
finn.web) may import this module, and they must label output as demo/fictional.
Live company paths (finn.ui_api, finn.pull, MCP/chat) must not import it and
must show explicit not-loaded/empty states when real data is missing.

For the submission demo, replace these with one day of real manually downloaded
filings replayed through the same path (the live NSE/BSE feeds have no
documented free API and both exchanges prohibit scraping)."""
from __future__ import annotations

from datetime import date, datetime

from .models import Announcement, GuidanceStatement, MoneyValue, ResultsFiling, Unit

DEMO_LABEL = "FICTIONAL DEMO DATA (Demotech etc.) - synthetic fixtures, not live exchange alerts"


def demo_announcements() -> list[Announcement]:
    base = datetime(2026, 10, 31)
    return [
        Announcement(exchange="NSE", announcement_id="DEMO-001", company="Demotech Industries",
                     headline="Unaudited financial results for the quarter ended 30 Sep 2026",
                     filed_at=base.replace(hour=9), category="results",
                     body_text="Revenue 1240 crore, EBITDA 310 crore, PBT 240 crore, PAT 178 crore (consolidated)."),
        Announcement(exchange="NSE", announcement_id="DEMO-002", company="Demotech Industries",
                     headline="Appointment of compliance officer",
                     filed_at=base.replace(hour=10), category="unclassified"),
        Announcement(exchange="BSE", announcement_id="DEMO-003", company="Meridian Demo Foods",
                     headline="Resignation of Chief Financial Officer",
                     filed_at=base.replace(hour=11), category="management_change"),
        # Duplicate redelivery of DEMO-001 (feeds resend items).
        Announcement(exchange="NSE", announcement_id="DEMO-001", company="Demotech Industries",
                     headline="Unaudited financial results for the quarter ended 30 Sep 2026",
                     filed_at=base.replace(hour=12), category="results",
                     body_text="Revenue 1240 crore, EBITDA 310 crore, PBT 240 crore, PAT 178 crore (consolidated)."),
        # Revision of the results filing: PAT restated. Must surface as a correction.
        Announcement(exchange="NSE", announcement_id="DEMO-001R", company="Demotech Industries",
                     headline="Revised: Unaudited financial results for the quarter ended 30 Sep 2026",
                     filed_at=base.replace(hour=13), category="results", revision_of="DEMO-001",
                     body_text="Revenue 1240 crore, EBITDA 310 crore, PBT 240 crore, PAT 171 crore (consolidated)."),
        Announcement(exchange="NSE", announcement_id="DEMO-004", company="Demotech Industries",
                     headline="Acquisition of 51% stake in Example Components Pvt Ltd",
                     filed_at=base.replace(hour=15), category="M&A"),
    ]


def demo_filings() -> list[ResultsFiling]:
    return [
        ResultsFiling(company="Demotech Industries", period_end=date(2026, 6, 30),
                      revenue_from_operations=MoneyValue(amount=1150, page=2),
                      total_revenue=MoneyValue(amount=1162, page=2), ebitda=MoneyValue(amount=276),
                      pbt=MoneyValue(amount=218), pat=MoneyValue(amount=161)),
        ResultsFiling(company="Demotech Industries", period_end=date(2026, 9, 30),
                      revenue_from_operations=MoneyValue(amount=1240, page=2),
                      total_revenue=MoneyValue(amount=1255, page=2), ebitda=MoneyValue(amount=310),
                      pbt=MoneyValue(amount=240), pat=MoneyValue(amount=171)),
    ]


def demo_guidance() -> list[GuidanceStatement]:
    return [
        GuidanceStatement(company="Demotech Industries", metric="revenue_from_operations",
                          stated_target="double-digit sequential revenue-from-operations growth (a >=10% bound, not a point)",
                          quote="We expect double-digit revenue from operations growth quarter on quarter through FY27.",
                          page=4, period="FY27", target_pct=10.0, bound="at_least", horizon="qoq", source_type="concall_transcript"),
        GuidanceStatement(company="Demotech Industries", metric="ebitda",
                          stated_target="stable to improving margins",
                          quote="We expect margins to remain stable to improving through the year.",
                          page=5, period="FY27", target_pct=None, source_type="concall_transcript"),
    ]


def classify_fixture(text: str) -> str:
    """Offline keyword classifier so the replay runs with no model key."""
    t = text.lower()
    for kw, cat in [("result", "results"), ("acquisition", "M&A"), ("resignation", "management_change"),
                    ("appointment", "unclassified"), ("dividend", "dividend"), ("buyback", "fund_raise")]:
        if kw in t:
            return cat
    return "press_release"
