from datetime import date, datetime

from pydantic_ai.models.test import TestModel

from finn.agents import build_classifier, build_guidance_extractor, build_results_extractor
from finn.alerts import price_volume_alert
from finn.ledger import growth_pct, promised_vs_delivered
from finn.models import (Announcement, GuidanceStatement, Metric, MoneyValue,
                         ResultsFiling, Unit)
from finn.store import AnnouncementStore


def filing(pat, unit=Unit.CRORE, consolidated=True, end=date(2026, 9, 30)):
    return ResultsFiling(company="X", period_end=end, consolidated=consolidated,
                         revenue_from_operations=MoneyValue(amount=100, unit=unit),
                         ebitda=MoneyValue(amount=20, unit=unit),
                         pbt=MoneyValue(amount=15, unit=unit),
                         pat=MoneyValue(amount=pat, unit=unit))


def test_lakh_converts_to_crore():
    assert MoneyValue(amount=250, unit=Unit.LAKH).in_crore() == 2.5


def test_growth_normalises_units():
    cur, prev = filing(12, Unit.CRORE), filing(1000, Unit.LAKH)  # 1000 lakh = 10 crore
    assert growth_pct(cur, prev, Metric.PAT) == 20.0


def test_basis_change_refuses_comparison():
    cur, prev = filing(12, consolidated=True), filing(10, consolidated=False, end=date(2026, 6, 30))
    assert growth_pct(cur, prev, Metric.PAT) is None


def test_duplicate_is_never_repushed():
    s = AnnouncementStore()
    a = Announcement(exchange="NSE", announcement_id="A1", company="X",
                     headline="Results", filed_at=datetime(2026, 10, 31, 9))
    assert s.ingest(a).kind == "new"
    assert s.ingest(a).kind == "duplicate"
    assert len(s) == 1


def test_revision_same_id_is_correction_and_updates():
    s = AnnouncementStore()
    base = Announcement(exchange="NSE", announcement_id="A1", company="X",
                        headline="Results", filed_at=datetime(2026, 10, 31, 9), body_text="PAT 100")
    revised = base.model_copy(update={"body_text": "PAT 95", "filed_at": datetime(2026, 10, 31, 13)})
    s.ingest(base)
    out = s.ingest(revised)
    assert out.kind == "correction"
    assert s.get("NSE", "A1").body_text == "PAT 95"


def test_revision_new_id_supersedes_original():
    s = AnnouncementStore()
    base = Announcement(exchange="NSE", announcement_id="A1", company="X",
                        headline="Results", filed_at=datetime(2026, 10, 31, 9))
    rev = Announcement(exchange="NSE", announcement_id="A1R", company="X", revision_of="A1",
                       headline="Revised results", filed_at=datetime(2026, 10, 31, 13))
    s.ingest(base)
    out = s.ingest(rev)
    assert out.kind == "correction" and out.supersedes == "A1"
    assert ("NSE", "A1") in s.superseded


def test_ledger_abstains_without_stated_guidance():
    entries = promised_vs_delivered("X", [], [filing(10), filing(11)])
    assert all(e["status"] == "no_stated_guidance" for e in entries)


def test_ledger_compares_only_numeric_guidance():
    g = [GuidanceStatement(company="X", metric="pat", stated_target="10% growth",
                           quote="We guide to 10% PAT growth.", target_pct=10.0)]
    entries = promised_vs_delivered("X", g, [filing(10), filing(12)])
    pat = next(e for e in entries if e["metric"] == "pat")
    assert pat["status"] == "delivered" and pat["actual_pct"] == 20.0
    qualitative = next(e for e in entries if e["metric"] == "revenue_from_operations")
    assert qualitative["status"] == "no_stated_guidance"


def test_bound_at_least_evaluates_as_bound_not_point():
    g = [GuidanceStatement(company="X", metric="pat", stated_target="double-digit growth",
                           quote="We expect double-digit PAT growth.", target_pct=10.0, bound="at_least")]
    entries = promised_vs_delivered("X", g, [filing(10), filing(12)])
    pat = next(e for e in entries if e["metric"] == "pat")
    assert pat["status"] == "delivered" and pat["target_display"] == ">=10.0%"
    g[0].bound = "at_most"
    entries = promised_vs_delivered("X", g, [filing(10), filing(12)])
    assert next(e for e in entries if e["metric"] == "pat")["status"] == "missed"


def test_qualitative_guidance_is_quoted_not_compared():
    g = [GuidanceStatement(company="X", metric="ebitda", stated_target="stable margins",
                           quote="Margins should stay stable.", target_pct=None)]
    entries = promised_vs_delivered("X", g, [filing(10), filing(12)])
    ebitda = next(e for e in entries if e["metric"] == "ebitda")
    assert ebitda["status"] == "qualitative" and "quoted verbatim" in ebitda["note"]


def test_horizon_mismatch_not_compared():
    g = [GuidanceStatement(company="X", metric="pat", stated_target="10% growth in FY27",
                           quote="We guide to 10% PAT growth in FY27.", target_pct=10.0, horizon="fy")]
    entries = promised_vs_delivered("X", g, [filing(10), filing(12)])
    pat = next(e for e in entries if e["metric"] == "pat")
    assert pat["status"] == "not_comparable" and "Horizon mismatch" in pat["note"]


def test_price_and_volume_thresholds_configurable():
    assert price_volume_alert([100, 106], [10, 10]) is not None
    assert price_volume_alert([100, 103], [10, 10]) is None
    assert price_volume_alert([100, 101], [10, 25]) is not None
    assert price_volume_alert([100, 101], [10, 25], volume_multiple=3.0) is None


def test_classifier_agent_typed_output():
    agent = build_classifier(model=TestModel())
    result = agent.run_sync("Board approves Q2 results")
    assert isinstance(result.output.category, str)
    assert isinstance(result.output.material, bool)


def test_extractors_typed_output():
    r = build_results_extractor(model=TestModel(custom_output_args={"company": "X", "period_end": "2026-09-30",
                                          "revenue_from_operations": {"amount": 100, "page": 1},
                                          "pbt": {"amount": 15}, "pat": {"amount": 10}})).run_sync("Revenue 100 crore, PAT 10 crore")
    assert (r.output.revenue_from_operations or r.output.total_revenue).in_crore() >= 0
    g = build_guidance_extractor(model=TestModel()).run_sync("We guide to margin stability.")
    assert isinstance(g.output, list)


def test_real_filing_growth_matches_stated():
    from finn.real_seed import STATED_QOQ_PCT, tcs_filings
    fs = tcs_filings()
    qoq = growth_pct(fs[-1], fs[-2], Metric.REVENUE_FROM_OPERATIONS)
    assert abs(qoq - STATED_QOQ_PCT) < 0.1  # computed 1.26% vs press release's stated +1.3%
    assert growth_pct(fs[-1], fs[-2], Metric.EBITDA) is None  # EBITDA not stated: abstain
