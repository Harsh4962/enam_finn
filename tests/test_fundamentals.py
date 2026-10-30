"""Synthetic tables/PDFs exercise accounting, source and cache boundaries."""

from __future__ import annotations

import json
import socket
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

import pymupdf as fitz

from backend.cache import SQLiteCache
from backend.data.fundamentals import (
    DocumentExtractionError,
    DocumentLink,
    FundamentalsClient,
    FundamentalsError,
    PdfPageText,
    SourceFetchError,
    chunk_transcript,
    extract_pdf_text,
    parse_number,
    parse_screener_html,
    validate_public_url,
)
from tests.test_data_plumbing import FakeResponse, FakeSession

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
URL = "https://www.screener.in/company/TESTCO/consolidated/"
QUARTERS = ["Sep 2023", "Dec 2023", "Mar 2024", "Jun 2024", "Sep 2024", "Dec 2024", "Mar 2025",
            "Jun 2025", "Sep 2025", "Dec 2025", "Mar 2026", "Jun 2026", "Sep 2026"]


def table(section_id, labels, rows, *, heading=None, sub="Consolidated Figures in Rs. Crores"):
    header = "<thead><tr><th></th>" + "".join(f"<th>{label}</th>" for label in labels) + "</tr></thead>"
    body = "<tbody>" + "".join("<tr><td>" + label + " +</td>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>" for label, values in rows) + "</tbody>"
    return f'<section id="{section_id}"><h2>{heading or section_id}</h2><p class="sub">{sub}</p><div data-result-table><table>{header}{body}</table></div></section>'


def document_section():
    annuals = "".join(f'<li><a href="https://issuer.example/ar-{year}.pdf">Annual Report {year}</a></li>' for year in (2026, 2025, 2024, 2023))
    calls = []
    for index, label in enumerate(("Oct 2026", "Jul 2026", "Jul 2026", "Apr 2026", "Jan 2026")):
        calls.append(f'<li><div class="nowrap">{label}</div><a href="https://issuer.example/call-{index}.pdf">Transcript</a><button data-url="/concalls/summary/{index}/">AI Summary</button><a href="https://issuer.example/ppt-{index}.pdf">PPT</a><div class="concall-notes">Secondary illustrative notes, not the actual primary-source transcript.</div></li>')
    return f'<section id="documents"><div class="documents annual-reports"><h3>Annual reports</h3><ul>{annuals}</ul></div><div class="documents concalls"><h3>Concalls</h3><ul>{"".join(calls)}</ul></div></section>'


def company_html(*, basis="Consolidated", quarterly_labels=None, quarterly_rows=None, cfo_values=None,
                 pat_values=None, working_values=None, share_pledge=False, empty=False, financial=True):
    labels = quarterly_labels or QUARTERS
    count = len(labels)
    revenue = [100.0] * (count - 1) + [200.0]
    rows = quarterly_rows or [("Sales", revenue), ("Operating Profit", [value * 0.2 for value in revenue]),
        ("OPM %", [20] * count), ("Other Income", [3] * count), ("Interest", [1] * count),
        ("Depreciation", [2] * count), ("Profit before tax", [value * 0.15 for value in revenue]),
        ("Net Profit", [value * 0.1 for value in revenue])]
    sub = basis + " Figures in Rs. Crores"
    header = '<h1>Test Company Limited</h1><a href="https://www.nseindia.com/get-quotes/equity?symbol=TESTCO">NSE: TESTCO</a>'
    top = '<ul id="top-ratios">' + "".join(f'<li><span class="name">{name}</span><span class="value"><span class="number">{value}</span></span></li>'
        for name, value in (("Market Cap", "1,23,456.75"), ("Current Price", "200.50"), ("Stock P/E", "16.5"), ("Book Value", "50"),
                           ("Dividend Yield", "2.5"), ("ROCE", "18"), ("ROE", "15"))) + '</ul>'
    if empty or not financial:
        return header + top + '<section id="quarters"><h2>Quarterly Results</h2></section>'
    years = ["Mar 2024", "Mar 2025", "Mar 2026"]
    annual = [("Sales", [1000] * 3 + [1100]), ("Operating Profit", [200] * 3 + [220]),
              ("Net Profit", (pat_values or [100, 100, 100]) + [110])]
    balance = [("Equity Capital", [10, 10, 10]), ("Reserves", [90, 90, 90]), ("Borrowings", [50, 60, 80]),
               ("Other Liabilities", [10, 10, 10]), ("Fixed Assets", [120, 130, 150]), ("CWIP", [10, 20, 30]),
               ("Investments", [20, 20, 20]), ("Other Assets", [30, 40, 50])]
    cash = [("Cash from Operating Activity", cfo_values or [30, 60, 90]),
            ("Cash from Investing Activity", [-10, -20, -30]), ("Cash from Financing Activity", [5, -5, -15]),
            ("CFO/EBITDA", [999, 999, 999])]
    ratios = [("Debtor Days", [20, 20, 20]), ("Inventory Days", [30, 30, 30]), ("Days Payable", [40, 40, 40]),
              ("Cash Conversion Cycle", [10, 10, 10]), ("Working Capital Days", working_values or [10, 25, 41]), ("ROCE %", [18, 19, 20])]
    shares = [("Promoters", [50, 50, 50]), ("FIIs", [15, 15, 15]), ("DIIs", [10, 10, 10]), ("Public", [25, 25, 25]),
              ("No. of Shareholders", [1000, 1100, 1200])]
    if share_pledge:
        shares.append(("Promoter Pledging %", [1, 2, 3]))
    markup = header + top + table("quarters", labels, rows, heading="Quarterly Results", sub=sub)
    markup += table("profit-loss", years + ["TTM"], annual, sub=sub)
    markup += table("balance-sheet", years, balance, sub=sub)
    markup += table("cash-flow", years, cash, sub=sub)
    markup += table("ratios", years, ratios, sub=basis)
    markup += table("shareholding", years, shares, sub="Numbers in percentages")
    markup += '<section id="analysis">AI says this company will certainly outperform. This is secondary commentary.</section>'
    return markup + document_section()


def parse(html=None, **kwargs):
    return parse_screener_html(html or company_html(), "TESTCO", source_url=URL, fetched_at=NOW, **kwargs)


def document(url="https://issuer.example/call.pdf"):
    return DocumentLink.model_validate_json(json.dumps({"document_id": "concall.test", "ticker": "TESTCO",
        "kind": "concall_transcript", "url": url, "listed_label": "Oct 2026", "listed_month": "2026-10-01"}))


def pdf_bytes(texts):
    with fitz.open() as pdf:
        for text in texts:
            page = pdf.new_page()
            if text:
                page.insert_text((50, 60), text, fontsize=10)
        return pdf.tobytes(garbage=4, deflate=True)


class FinancialParsingTests(unittest.TestCase):
    def test_indian_numbers_missing_values_and_signed_values(self):
        for raw, value in (("1,23,456.75", 123456.75), ("(1,234.50)", -1234.5), ("−12.5%", -12.5), ("0", 0), ("₹ 25 Cr.", 25)):
            with self.subTest(raw=raw):
                self.assertEqual(parse_number(raw), value)
        for raw in ("", "-", "—", "N/A", "NaN", "xxx", "<0.01", "infinity"):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_number(raw))

    def test_all_sections_and_header_ratios(self):
        result = parse()
        self.assertEqual(len(result.quarters.columns), 12)
        self.assertEqual(len(result.header_ratios), 7)
        for name in ("profit_loss", "balance_sheet", "cash_flow", "ratios", "shareholding"):
            self.assertIsNotNone(getattr(result, name))
        self.assertEqual(result.header_ratios[0].value, 123456.75)
        self.assertEqual(result.shareholding.row("no_of_shareholders").unit, "units")

    def test_headline_deltas_and_margins_are_computed(self):
        result = parse()
        table = result.latest_quarter_headlines
        self.assertEqual(result.ebitda_basis, "operating_profit_proxy")
        self.assertEqual(table.current.revenue.value, 200)
        self.assertEqual(table.current.ebitda.value, 40)
        self.assertEqual(table.headline_rows[0].qoq.value_pct, 100)
        self.assertEqual(table.headline_rows[0].yoy.value_pct, 100)
        self.assertEqual(table.current.ebitda_margin_pct, 20)
        self.assertEqual(table.current.pat_margin_pct, 10)
        self.assertIn("proxy", table.current.ebitda.calculation)
        self.assertIn("Operating Profit", table.current.ebitda.citations[0].section)

    def test_ebitda_proxy_can_be_disabled(self):
        result = parse(allow_operating_profit_proxy=False)
        self.assertEqual(result.ebitda_basis, "unavailable")
        self.assertIsNone(result.latest_quarter_headlines.current.ebitda.value)

    def test_financing_profit_is_not_ebitda(self):
        html = company_html().replace("Operating Profit", "Financing Profit")
        result = parse(html)
        self.assertEqual(result.ebitda_basis, "unavailable")
        self.assertIsNone(result.latest_quarter_headlines.current.ebitda.value)
        self.assertIsNone(result.cash_conversion[-1].cfo_to_ebitda)

    def test_missing_previous_quarter_is_not_replaced_by_an_older_quarter(self):
        labels = [label for label in QUARTERS if label != "Jun 2026"]
        result = parse(company_html(quarterly_labels=labels))
        self.assertIsNone(result.latest_quarter_headlines.previous_quarter)
        self.assertIsNotNone(result.latest_quarter_headlines.year_ago)

    def test_pbt_is_derived_only_with_all_four_components(self):
        rows = [("Sales", [100] * 13), ("Operating Profit", [20] * 13), ("Other Income", [3] * 13),
                ("Interest", [1] * 13), ("Depreciation", [2] * 13), ("Net Profit", [15] * 13)]
        result = parse(company_html(quarterly_rows=rows))
        self.assertEqual(result.latest_quarter_headlines.current.pbt.value, 20)
        self.assertIsNotNone(result.latest_quarter_headlines.current.pbt.calculation)
        rows = [row for row in rows if row[0] != "Other Income"]
        self.assertIsNone(parse(company_html(quarterly_rows=rows)).latest_quarter_headlines.current.pbt.value)

    def test_cash_conversion_uses_sums_not_average_ratios_or_widget(self):
        result = parse()
        self.assertEqual(result.cash_conversion[-1].cfo_to_pat, 0.9)
        self.assertEqual(result.cash_conversion[-1].cfo_to_ebitda, 0.45)
        self.assertEqual(result.three_year_cash_conversion.cumulative_cfo_to_pat, 0.6)
        self.assertTrue(result.three_year_cash_conversion.red_alert)
        self.assertIn("cash_conversion_3y_below_70pct", {flag.code for flag in result.flags})

    def test_exact_seventy_percent_is_not_a_red_alert(self):
        result = parse(company_html(cfo_values=[70, 70, 70]))
        self.assertEqual(result.three_year_cash_conversion.cumulative_cfo_to_pat, 0.7)
        self.assertFalse(result.three_year_cash_conversion.red_alert)

    def test_negative_cumulative_pat_has_no_misleading_ratio(self):
        result = parse(company_html(pat_values=[-100, -100, -100]))
        self.assertIsNone(result.three_year_cash_conversion.cumulative_cfo_to_pat)
        self.assertFalse(result.three_year_cash_conversion.red_alert)

    def test_missing_cash_flow_cells_do_not_become_zero(self):
        result = parse(company_html(cfo_values=[30, "", 90]))
        self.assertIsNone(result.cash_conversion[1].cfo.value)
        self.assertIsNone(result.three_year_cash_conversion.cumulative_cfo_to_pat)

    def test_debt_equity_includes_reserves(self):
        self.assertEqual(parse().leverage[-1].debt_to_equity, 0.8)

    def test_working_capital_days_are_distinct_from_cash_cycle(self):
        latest = parse().working_capital[-1]
        self.assertEqual(latest.computed_cash_conversion_cycle_days, 10)
        self.assertEqual(latest.working_capital_days, 41)
        self.assertEqual(latest.yoy_working_capital_change_days, 16)
        self.assertTrue(latest.stretch_alert)
        self.assertFalse(parse(company_html(working_values=[10, 25, 40])).working_capital[-1].stretch_alert)

    def test_pledging_is_missing_unless_actually_disclosed(self):
        self.assertIsNone(parse().shareholding.row("promoter_pledging_pct"))
        self.assertEqual(parse(company_html(share_pledge=True)).shareholding.row("promoter_pledging_pct").values[-1], 3)

    def test_ttm_is_not_matched_with_full_year_cash_flow(self):
        result = parse()
        self.assertEqual(result.profit_loss.columns[-1].kind, "ttm")
        self.assertIsNone(result.profit_loss.columns[-1].end_on)
        self.assertEqual(len(result.cash_conversion), 3)

    def test_interim_cash_flow_does_not_turn_next_full_year_into_half_year(self):
        html = company_html()
        original = table("cash-flow", ["Mar 2024", "Mar 2025", "Mar 2026"], [
            ("Cash from Operating Activity", [30, 60, 90]), ("Cash from Investing Activity", [-10, -20, -30]),
            ("Cash from Financing Activity", [5, -5, -15]), ("CFO/EBITDA", [999, 999, 999])])
        replacement = table("cash-flow", ["Mar 2024", "Mar 2025", "Sep 2025", "Mar 2026"], [
            ("Cash from Operating Activity", [30, 60, 40, 90])])
        result = parse(html.replace(original, replacement))
        self.assertEqual([column.kind for column in result.cash_flow.columns], ["annual", "annual", "half_year", "annual"])
        self.assertEqual(len(result.cash_conversion), 3)

    def test_secondary_analysis_is_not_imported_into_fundamentals(self):
        self.assertNotIn("certainly outperform", parse().model_dump_json())

    def test_page_identity_and_reporting_basis_are_checked(self):
        with self.assertRaises(FundamentalsError):
            parse(company_html().replace("NSE: TESTCO", "NSE: WRONGCO"))
        html = company_html().replace('<p class="sub">Consolidated</p>', '<p class="sub">Standalone</p>')
        with self.assertRaises(FundamentalsError):
            parse(html)

    def test_half_year_results_are_not_quarterly_headlines(self):
        result = parse(company_html().replace("Quarterly Results", "Half Yearly Results"))
        self.assertIsNone(result.latest_quarter_headlines)

    def test_future_financial_columns_do_not_become_actual_data(self):
        result = parse(company_html().replace("Mar 2026", "Mar 2027"))
        self.assertTrue(all(column.end_on is None or column.end_on <= NOW.date() for column in result.balance_sheet.columns))
        self.assertFalse(result.three_year_cash_conversion.red_alert)

    def test_fiscal_year_change_preserves_new_full_year_and_excludes_transition(self):
        html = company_html()
        start = html.index('<section id="profit-loss">')
        end = html.index('</section>', start) + len('</section>')
        labels = ["Dec 2022", "Dec 2023", "Mar 2025", "Mar 2026", "TTM"]
        replacement = table("profit-loss", labels, [("Sales", [1000] * 5), ("Operating Profit", [200] * 5), ("Net Profit", [100] * 5)])
        html = html[:start] + replacement + html[end:]
        start = html.index('<section id="cash-flow">')
        end = html.index('</section>', start) + len('</section>')
        replacement = table("cash-flow", labels[:-1], [("Cash from Operating Activity", [50, 60, 70, 80])])
        html = html[:start] + replacement + html[end:]
        result = parse(html)
        self.assertEqual(result.profit_loss.columns[2].kind, "custom")
        self.assertEqual(result.cash_conversion[-1].period.end_on, date(2026, 3, 31))
        self.assertEqual(result.cash_conversion[-1].cfo_to_pat, 0.8)
        self.assertNotIn(date(2025, 3, 31), [item.period.end_on for item in result.cash_conversion])


class DocumentTests(unittest.TestCase):
    def test_four_distinct_listing_quarters_and_annual_years(self):
        docs = parse().documents
        self.assertEqual(len(docs.concall_transcripts), 4)
        self.assertEqual([item.listed_month.month for item in docs.concall_transcripts], [10, 7, 4, 1])
        self.assertFalse(docs.concall_transcripts[1].alternate_urls)
        self.assertEqual(len(docs.annual_reports), 4)
        self.assertEqual(len(docs.investor_presentations), 4)

    def test_notes_are_secondary_and_not_primary_transcript_text(self):
        item = parse().documents.concall_transcripts[0]
        self.assertIn("Secondary", item.embedded_notes)
        self.assertIsNotNone(item.notes_url)
        self.assertFalse(item.notes_are_primary)

    def test_old_markup_without_nowrap_still_has_months(self):
        docs = parse(company_html().replace('class="nowrap"', 'class="date"')).documents
        self.assertEqual(len(docs.concall_transcripts), 4)
        self.assertTrue(all(item.listed_month for item in docs.concall_transcripts))

    def test_bare_document_headings_and_fiscal_ranges(self):
        html = company_html().replace(document_section(), '''<section id="documents">
            <h3>Annual reports</h3><ul><li><a href="https://issuer.example/ar.pdf">Annual Report 2025-26</a></li></ul>
            <h3>Concalls</h3><ul><li>Oct 2026 <a href="https://issuer.example/call.pdf">Transcript</a></li></ul>
            </section>''')
        docs = parse(html).documents
        self.assertEqual(docs.annual_reports[0].reporting_year, 2026)
        self.assertEqual(docs.concall_transcripts[0].listed_month, date(2026, 10, 1))


class PdfTests(unittest.TestCase):
    def test_page_limit_and_management_qa_chunks(self):
        pages = ["Transcript cover and management participant details."] + [
            "Management Opening Remarks\nWelcome to the earnings conference call. Revenue increased and we will discuss operating performance." if index == 1 else
            "Moderator: We will now begin the question and answer session.\nAnalyst: Please explain capital expenditure.\nCFO: We will discuss the assumptions." if index == 2 else
            f"Continuation page {index}: management answers analyst questions about revenue and margins."
            for index in range(1, 30)]
        result = extract_pdf_text(pdf_bytes(pages), document(), extracted_at=NOW)
        self.assertEqual(len(result.pages), 25)
        self.assertEqual(result.total_pages, 30)
        self.assertTrue(result.truncated)
        self.assertEqual(result.status, "partial")
        self.assertIn("management_opening", {chunk.section for chunk in result.chunks})
        self.assertIn("analyst_qa", {chunk.section for chunk in result.chunks})
        self.assertTrue(all(1 <= chunk.page_start <= chunk.page_end <= 25 for chunk in result.chunks))

    def test_blank_pdf_reports_ocr_need_instead_of_faking_text(self):
        result = extract_pdf_text(pdf_bytes([""]), document(), extracted_at=NOW)
        self.assertEqual(result.status, "needs_ocr")
        self.assertEqual(result.chunks, ())

    def test_non_pdf_is_rejected(self):
        with self.assertRaises(DocumentExtractionError):
            extract_pdf_text(b"<html>Access denied</html>", document())

    def test_pdf_page_limit_cannot_exceed_25(self):
        with self.assertRaises(ValueError):
            extract_pdf_text(pdf_bytes(["text"]), document(), max_pages=26)

    def test_chunk_size_overlap_and_stable_ids(self):
        pages = (PdfPageText(page=1, text="Management Opening Remarks\n" + "Long management sentence.\n" * 70),)
        chunks = chunk_transcript(pages, "test", max_chars=600, overlap_chars=60)
        self.assertTrue(all(len(chunk.text) <= 600 for chunk in chunks))
        self.assertEqual(chunks, chunk_transcript(pages, "test", max_chars=600, overlap_chars=60))
        with self.assertRaises(ValueError):
            chunk_transcript(pages, "test", max_chars=600, overlap_chars=600)

    def test_page_break_does_not_glue_words(self):
        pages = (PdfPageText(page=1, text="Management Opening Remarks\nFirst sentence."), PdfPageText(page=2, text="Second sentence."))
        chunks = chunk_transcript(pages, "test")
        self.assertIn("sentence.\nSecond", chunks[0].text)

    def test_explicit_reporting_period_and_stale_listing_are_detected(self):
        payload = pdf_bytes(["Management Opening Remarks\nResults for the quarter ended September 30, 2026. Revenue and margins were discussed."])
        result = extract_pdf_text(payload, document(), extracted_at=NOW)
        self.assertEqual(result.reported_quarter_end, date(2026, 9, 30))
        self.assertFalse(result.listing_period_mismatch)
        old = pdf_bytes(["Management Opening Remarks\nResults for the quarter ended June 30, 2025. Revenue and operating performance were discussed."])
        result = extract_pdf_text(old, document(), extracted_at=NOW)
        self.assertTrue(result.listing_period_mismatch)
        self.assertEqual(result.status, "partial")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.cache = SQLiteCache(":memory:", clock=lambda: NOW.timestamp())

    def tearDown(self):
        self.cache.close()

    def client(self, session):
        return FundamentalsClient(self.cache, session=session, clock=lambda: NOW, url_validator=lambda url: url)

    def test_empty_consolidated_falls_back_as_a_whole_to_standalone(self):
        session = FakeSession([FakeResponse(body=company_html(empty=True).encode()),
                               FakeResponse(body=company_html(basis="Standalone").encode())])
        result = self.client(session).fetch("TESTCO")
        self.assertEqual(result.basis, "standalone")
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(session.calls[1][0].endswith("/company/TESTCO/"))
        self.assertTrue(all(fact.pat.basis == "standalone" for fact in result.cash_conversion))

    def test_404_consolidated_falls_back_but_403_does_not_hammer_another_url(self):
        session = FakeSession([FakeResponse(status=404), FakeResponse(body=company_html(basis="Standalone").encode())])
        self.assertEqual(self.client(session).fetch("TESTCO").basis, "standalone")
        session = FakeSession([FakeResponse(status=403)])
        with self.assertRaises(SourceFetchError):
            self.client(session).fetch("TESTCO", force_refresh=True)
        self.assertEqual(len(session.calls), 1)

    def test_html_cache_avoids_http_and_dns_revalidation(self):
        session = FakeSession([FakeResponse(body=company_html().encode())])
        client = self.client(session)
        client.fetch("TESTCO")
        client._validate_url = lambda url: (_ for _ in ()).throw(AssertionError("cache hit must not need network/DNS"))
        result = client.fetch("TESTCO")
        self.assertTrue(result.cache_hit)
        self.assertEqual(len(session.calls), 1)

    def test_pdf_and_text_cache_avoid_both_redownload_and_reextraction(self):
        payload = pdf_bytes(["Management Opening Remarks\nRevenue and margins for the quarter ended September 30, 2026 were discussed."])
        session = FakeSession([FakeResponse(body=payload, content_type="application/pdf")])
        client = self.client(session)
        first = client.fetch_transcript(document())
        with patch("backend.data.fundamentals.extract_pdf_text", side_effect=AssertionError("must reuse extraction")):
            second = client.fetch_transcript(document())
        self.assertEqual(first.status, "ok")
        self.assertTrue(second.cache_hit)
        self.assertEqual(len(session.calls), 1)

    def test_observed_pdf_viewer_iframe_is_resolved(self):
        payload = pdf_bytes(["Management Opening Remarks\nFinancial performance and revenue for the quarter ended September 30, 2026."])
        session = FakeSession([FakeResponse(body=b'<iframe src="/actual.pdf"></iframe>', content_type="text/html"),
                               FakeResponse(body=payload, content_type="application/pdf")])
        result = self.client(session).fetch_transcript(document("https://issuer.example/viewer"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(str(result.resolved_url), "https://issuer.example/actual.pdf")

    def test_changed_document_month_invalidates_period_alignment_cache(self):
        payload = pdf_bytes(["Management Opening Remarks\nFinancial results for the quarter ended September 30, 2026 were discussed."])
        session = FakeSession([FakeResponse(body=payload), FakeResponse(body=payload)])
        client = self.client(session)
        self.assertFalse(client.fetch_transcript(document()).listing_period_mismatch)
        changed = DocumentLink.model_validate(document().model_copy(update={"listed_month": date(2025, 10, 1), "listed_label": "Oct 2025"}))
        self.assertTrue(client.fetch_transcript(changed).listing_period_mismatch)

    def test_download_failure_is_per_document_and_not_fake_text(self):
        result = self.client(FakeSession([FakeResponse(status=403)])).fetch_transcript(document())
        self.assertEqual(result.status, "error")
        self.assertEqual(result.pages, ())
        self.assertEqual(result.chunks, ())

    def test_transcripts_are_optional_and_do_not_block_financials(self):
        session = FakeSession([FakeResponse(body=company_html().encode())])
        result = self.client(session).fetch("TESTCO")
        self.assertEqual(len(result.documents.concall_transcripts), 4)
        self.assertEqual(result.transcripts, ())
        self.assertEqual(len(session.calls), 1)

    def test_notes_login_wall_is_not_treated_as_transcript(self):
        item = parse().documents.concall_transcripts[0]
        item = DocumentLink.model_validate(item.model_copy(update={"embedded_notes": None}))
        session = FakeSession([FakeResponse(body=b'<form action="/login/">Login required</form>')])
        result = self.client(session).fetch_concall_notes(item)
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.is_primary)

    def test_private_hosts_and_positive_private_dns_are_rejected(self):
        for url in ("http://issuer.example/x.pdf", "https://127.0.0.1/x.pdf", "https://localhost/x.pdf", "https://user:pass@issuer.example/x.pdf"):
            with self.subTest(url=url), self.assertRaises(SourceFetchError):
                validate_public_url(url)
        address = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        with patch("socket.getaddrinfo", return_value=address), self.assertRaises(SourceFetchError):
            validate_public_url("https://www.screener.in/company/TESTCO/")

    def test_runtime_proxy_resolution_is_limited_to_observed_hosts(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror):
            self.assertEqual(validate_public_url(URL), URL)
            with self.assertRaises(SourceFetchError):
                validate_public_url("https://unknown.example/file.pdf")

    def test_zero_llm_usage(self):
        self.client(FakeSession([FakeResponse(body=company_html().encode())])).fetch("TESTCO")
        telemetry = self.cache.telemetry()
        self.assertEqual((telemetry.tier1_calls, telemetry.tier2_calls, telemetry.estimated_cost_usd), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
