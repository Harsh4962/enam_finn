"""Revenue definitions stay distinct end to end: schema, mapping, ledger, grounding, render."""
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from finn.extract import (extract_pages, kpi_to_results_filings, parse_kpi_table,
                          verify_page_grounding)
from finn.ledger import growth_pct, promised_vs_delivered
from finn.models import GuidanceStatement, Metric, MoneyValue, ResultsFiling

ZENTEC_PDF = Path(__file__).parent / "fixtures" / "zentec-q2fy27.pdf"


def _filing(ops=None, total=None, end=date(2026, 9, 30)):
    return ResultsFiling(company="X", period_end=end, revenue_from_operations=ops,
                         total_revenue=total, pbt=MoneyValue(amount=15), pat=MoneyValue(amount=10))


def test_schema_requires_one_revenue_definition():
    with pytest.raises(ValidationError):
        _filing()
    assert _filing(ops=MoneyValue(amount=100)).total_revenue is None
    assert _filing(total=MoneyValue(amount=110)).revenue_from_operations is None


def test_kpi_mapping_keeps_both_definitions_with_pages():
    table = parse_kpi_table(extract_pages(ZENTEC_PDF))
    q2fy27 = kpi_to_results_filings(table, "Zen Technologies Limited")[0]
    assert q2fy27.revenue_from_operations.amount == 345.13
    assert q2fy27.total_revenue.amount == 361.84
    assert q2fy27.revenue_from_operations.page == table.source_page
    assert q2fy27.total_revenue.page == table.source_page


def test_ambiguous_revenue_guidance_is_not_comparable():
    ops = lambda a, end: _filing(ops=MoneyValue(amount=a), total=MoneyValue(amount=a * 2), end=end)
    fs = [ops(100, date(2026, 6, 30)), ops(120, date(2026, 9, 30))]
    g = [GuidanceStatement(company="X", metric="revenue", stated_target="10% growth",
                           quote="Revenue to grow 10%.", target_pct=10.0)]
    entries = promised_vs_delivered("X", g, fs)
    amb = [e for e in entries if e["metric"] == "revenue"]
    assert len(amb) == 1 and amb[0]["status"] == "not_comparable"
    assert "not interchangeable" in amb[0]["note"]
    assert not any(e["status"] in ("delivered", "missed") for e in entries)


def test_explicit_definition_guidance_compares_like_for_like():
    fs = [_filing(ops=MoneyValue(amount=100), total=MoneyValue(amount=200), end=date(2026, 6, 30)),
          _filing(ops=MoneyValue(amount=120), total=MoneyValue(amount=210))]
    g = [GuidanceStatement(company="X", metric="revenue from operations", stated_target="10%",
                           quote="q", target_pct=10.0)]
    e = next(e for e in promised_vs_delivered("X", g, fs) if e["metric"] == "revenue_from_operations")
    assert e["status"] == "delivered" and e["actual_pct"] == 20.0
    assert growth_pct(fs[1], fs[0], Metric.TOTAL_REVENUE) == 5.0
    only_ops = _filing(ops=MoneyValue(amount=100))
    assert growth_pct(fs[1], only_ops, Metric.TOTAL_REVENUE) is None


def test_grouped_no_guidance_entry_wording():
    fs = [_filing(ops=MoneyValue(amount=100))]
    entries = promised_vs_delivered("X", [], fs, sources=["TCS press release"])
    assert len(entries) == 1
    assert entries[0]["status"] == "no_stated_guidance"
    assert entries[0]["note"] == ("Extraction found no stated guidance in the loaded source(s): "
                                  "TCS press release. Add concall transcripts to compare.")


def test_verify_page_grounding_three_statuses():
    pages = [(1, "intro"), (2, "Sales 345.13 Total Revenue 1,361.84")]  # single-line layout
    f = ResultsFiling(company="X", period_end=date(2026, 9, 30),
                      revenue_from_operations=MoneyValue(amount=345.13, page=2),
                      total_revenue=MoneyValue(amount=1361.84, page=1),
                      pbt=MoneyValue(amount=15), pat=MoneyValue(amount=10, page=2))
    status = verify_page_grounding(f, pages)
    assert status["revenue_from_operations"] == "verified"
    assert status["total_revenue"] == "page_mismatch"
    assert status["pbt"] == "unverified" and f.pbt.page is None
    assert status["pat"] == "page_mismatch"
    comma = ResultsFiling(company="X", period_end=date(2026, 9, 30),
                          total_revenue=MoneyValue(amount=1361.84, page=2),
                          pbt=MoneyValue(amount=1), pat=MoneyValue(amount=1))
    assert verify_page_grounding(comma, pages)["total_revenue"] == "verified"


def test_digest_render_shows_both_labeled_rows_and_grouped_card():
    from finn.web import build, render_filing_rows, render_ledger_rows
    f = _filing(ops=MoneyValue(amount=345.13, page=2), total=MoneyValue(amount=361.84, page=2))
    out = render_filing_rows(f, {"revenue_from_operations": "verified", "total_revenue": "page_mismatch"})
    assert "Revenue from operations" in out and "Total revenue (incl. other income)" in out
    assert "page verified" in out and "page mismatch" in out
    rows, _ = render_ledger_rows(promised_vs_delivered("X", [], [f]))
    assert rows.count('class="row"') == 1
    page = build()
    assert "Revenue from operations" in page and "Total revenue (incl. other income)" in page


def _grounding(field, amount, text):
    kw = {"revenue_from_operations": MoneyValue(amount=-1), "pbt": MoneyValue(amount=-1),
          "pat": MoneyValue(amount=-1)}  # required fields, page unset
    kw[field] = MoneyValue(amount=amount, page=1)
    f = ResultsFiling(company="X", period_end=date(2026, 9, 30), **kw)
    return verify_page_grounding(f, [(1, text)])[field]


def test_same_figure_under_different_metric_label_is_not_verified():
    text = "Particulars Q2\nSales 345.13\nTotal Revenue 361.84\nEBITDA 90.00"
    assert _grounding("pat", 345.13, text) == "label_unconfirmed"
    assert _grounding("ebitda", 345.13, text) != "verified"


def test_figure_with_correct_nearby_label_is_verified():
    same_line = "Profit After Tax 41.20 38.10"
    adjacent = "Profit before exceptional items\n  55.75   50.10"
    assert _grounding("pat", 41.20, same_line) == "verified"
    assert _grounding("pbt", 55.75, adjacent) == "verified"
    assert _grounding("revenue_from_operations", 1361.84, "Sales\n1,361.84") == "verified"


def test_wrong_quarter_column_same_metric_is_known_unchecked_gap():
    # ACCEPTED GAP: period-column disambiguation is not deterministic yet.
    # The model cites the prior-quarter value (38.10) for the current quarter;
    # the metric label matches on the same line, so this still reads "verified".
    # If column checking is added, flip this assertion to != "verified".
    text = "Particulars Q2FY27 Q1FY27\nProfit After Tax 41.20 38.10"
    assert _grounding("pat", 38.10, text) == "verified"


def test_label_unconfirmed_not_rendered_as_verified():
    from finn.web import render_filing_rows
    f = _filing(ops=MoneyValue(amount=345.13, page=2))
    out = render_filing_rows(f, {"revenue_from_operations": "label_unconfirmed"})
    assert "page verified" not in out and "label not confirmed" in out
