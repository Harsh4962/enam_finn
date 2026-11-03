from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from finn.extract import build_extraction_agent, extract_pages, parse_kpi_table
from finn.fetch_filing import (FilingFetchError, check_filing_url,
                               fetch_filing_pdf)
from finn.market_data import freshness_label

FIXTURES = Path(__file__).parent / "fixtures"
ZENTEC_PDF = FIXTURES / "zentec-q2fy27.pdf"


def test_parse_real_kpi_table():
    table = parse_kpi_table(extract_pages(ZENTEC_PDF))
    assert table is not None
    assert table.basis == "consolidated" and table.unit == "crore"
    assert table.columns[:3] == ["Q2FY27", "Q1FY27", "Q2FY26"]
    by_metric = {r.metric: r for r in table.rows}
    # Q2FY27 values as printed in the filing (page 3, consolidated, Rs crore)
    assert by_metric["revenue"].values[0] == 361.84
    assert by_metric["ebitda"].values[0] == 148.70
    assert by_metric["pat"].values[0] == 89.54
    assert all(r.source_page >= 1 for r in table.rows)


def test_parser_abstains_on_unsupported_layout():
    tcs = extract_pages(FIXTURES / "tcs-q2fy27.pdf")
    assert parse_kpi_table(tcs) is None  # abstain, never invent


def test_url_allowlist_and_scheme():
    assert check_filing_url("https://nsearchives.nseindia.com/corporate/x.pdf")
    with pytest.raises(FilingFetchError):
        check_filing_url("http://nsearchives.nseindia.com/corporate/x.pdf")
    with pytest.raises(FilingFetchError):
        check_filing_url("https://evil.example.com/x.pdf")
    with pytest.raises(FilingFetchError):
        check_filing_url("https://127.0.0.1/x.pdf")
    with pytest.raises(FilingFetchError):
        check_filing_url("https://169.254.169.254/latest/meta-data")


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_fetch_refuses_redirect(tmp_path):
    def handler(request):
        return httpx.Response(302, headers={"location": "https://nsearchives.nseindia.com/other.pdf"})
    with pytest.raises(FilingFetchError, match="redirect refused"):
        fetch_filing_pdf("https://nsearchives.nseindia.com/corporate/x.pdf",
                         tmp_path / "x.pdf", http=_mock_client(handler))


def test_fetch_refuses_non_pdf_and_bad_magic(tmp_path):
    def html_handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"%PDF-fake")
    with pytest.raises(FilingFetchError, match="content-type"):
        fetch_filing_pdf("https://nsearchives.nseindia.com/corporate/x.pdf",
                         tmp_path / "x.pdf", http=_mock_client(html_handler))

    def magic_handler(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"not a pdf")
    with pytest.raises(FilingFetchError, match="magic"):
        fetch_filing_pdf("https://nsearchives.nseindia.com/corporate/x.pdf",
                         tmp_path / "x.pdf", http=_mock_client(magic_handler))


def test_fetch_size_cap(tmp_path):
    def big_handler(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"},
                              content=b"%PDF-" + b"0" * 1024)
    with pytest.raises(FilingFetchError, match="exceeds"):
        fetch_filing_pdf("https://nsearchives.nseindia.com/corporate/x.pdf",
                         tmp_path / "x.pdf", max_bytes=100, http=_mock_client(big_handler))


def test_fetch_happy_path_mocked(tmp_path):
    data = b"%PDF-1.7 fake bytes"
    def ok_handler(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=data)
    out = fetch_filing_pdf("https://nsearchives.nseindia.com/corporate/x.pdf",
                           tmp_path / "x.pdf", http=_mock_client(ok_handler))
    assert out.read_bytes() == data


def test_freshness_injected_clock():
    quoted = datetime(2026, 10, 30, 9, 58, tzinfo=timezone.utc)  # Fri session
    sunday = datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc)
    assert freshness_label(quoted, sunday, market_open=False).startswith("stale")
    live = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)  # Mon 09:30 IST
    assert freshness_label(live, live, market_open=True) == "live (this session)"
    assert freshness_label(live, live, market_open=False) == "recent (market closed)"
    later = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    assert freshness_label(live, later, market_open=False).startswith("delayed")


def test_extraction_agent_offline_output_type():
    from pydantic_ai.models.test import TestModel
    agent = build_extraction_agent(model=TestModel(custom_output_args={"company": "X", "period_end": "2026-09-30",
                                          "revenue_from_operations": {"amount": 100, "page": 1},
                                          "pbt": {"amount": 15}, "pat": {"amount": 10}}))
    result = agent.run_sync("filing text here")
    from finn.models import ResultsFiling
    assert isinstance(result.output, ResultsFiling)


def test_kpi_to_filings_and_metric_definition_honesty():
    from finn.extract import kpi_to_results_filings
    from finn.ledger import growth_pct
    from finn.models import Metric
    table = parse_kpi_table(extract_pages(ZENTEC_PDF))
    filings = kpi_to_results_filings(table, "Zen Technologies Limited")
    assert [f.period_end.isoformat() for f in filings] == ["2026-09-30", "2026-06-30", "2025-09-30"]
    q2fy27, _, q2fy26 = filings
    # the filing's own "98.8% YoY" claim is on Sales, and it reproduces exactly
    sales = {r.metric: r.values for r in table.rows}["sales"]
    assert round((sales[0] / sales[2] - 1) * 100, 1) == 98.8
    # total revenue (incl. other income) is a different basis and a different number
    assert round(growth_pct(q2fy27, q2fy26, Metric.TOTAL_REVENUE), 1) == 81.9
    assert "pre-exceptional" in q2fy27.standard and "NCI" in q2fy27.standard
    assert q2fy27.consolidated and q2fy27.source_page >= 1


def test_quarter_label_mapping():
    from finn.extract import quarter_label_to_period
    assert quarter_label_to_period("Q2FY27").isoformat() == "2026-09-30"
    assert quarter_label_to_period("Q4FY27").isoformat() == "2027-03-31"
