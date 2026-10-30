"""Regressions for reviewed proposals; all issuer/LLM data here is synthetic."""
from __future__ import annotations

import json
import unittest
from datetime import date
from unittest.mock import patch

from pydantic import ValidationError

from backend.cache import SQLiteCache
from backend.config import LLMRouterConfig
from backend.data.fundamentals import (
    DocumentLink,
    FinancialRow,
    FundamentalsClient,
    PdfPageText,
    _Resource,
    chunk_transcript,
    extract_pdf_text,
)
from backend.engines.pull_engine import (
    GroundingError,
    PullEngine,
    _numbers_supported,
    _one_sentence,
    _Quote,
    _reporting_basis,
    _speaker_turn,
    _verify_quote,
)
from backend.engines.push_engine import GeminiLLMClient, _Takeaway
from tests.test_data_plumbing import FakeResponse, FakeSession
from tests.test_fundamentals import NOW, company_html, document, parse, pdf_bytes
from tests.test_gemini_free import FixtureSDK, sdk_response
from tests.test_pull_engine import SETTINGS, Documents, Models, updated
from tests.test_pull_quota_commentary import commentary_corpus


class PacingTests(unittest.TestCase):
    def test_router_pacing_is_configurable_without_changing_either_route(self):
        before = LLMRouterConfig()
        after = LLMRouterConfig(gemini_min_request_interval_seconds=12.1)
        self.assertEqual(before.tier1, after.tier1)
        self.assertEqual(before.tier2, after.tier2)
        for interval in (-1.0, 61.0, float("nan")):
            with self.subTest(interval=interval), self.assertRaises(ValidationError):
                LLMRouterConfig(gemini_min_request_interval_seconds=interval)

    def test_live_default_and_injected_default_are_explicit(self):
        self.assertEqual(GeminiLLMClient()._min_interval, 4.1)
        self.assertEqual(GeminiLLMClient(sdk_client=object())._min_interval, 0.0)

    def test_spacing_is_per_model_and_counts_request_starts(self):
        sdk = FixtureSDK([sdk_response() for _ in range(3)])
        client = GeminiLLMClient(api_key="fixture-key", free_tier_project_confirmed=True,
            sdk_client=sdk, min_request_interval_seconds=4.1)
        clock = [0.0]
        def advance(delay):
            clock[0] += delay
        with patch("backend.engines.push_engine.time.monotonic", side_effect=lambda: clock[0]), \
                patch("backend.engines.push_engine.time.sleep", side_effect=advance) as sleep:
            client.complete(LLMRouterConfig().tier1, "Synthetic", {}, _Takeaway)
            clock[0] = 1.0
            client.complete(LLMRouterConfig().tier1, "Synthetic", {}, _Takeaway)
            client.complete(LLMRouterConfig().tier2, "Synthetic", {}, _Takeaway)
        self.assertAlmostEqual(sleep.call_args.args[0], 3.1)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(len(sdk.calls), 3)

    def test_invalid_pacing_never_reaches_provider(self):
        for value in (True, -1, 61, float("nan"), float("inf"), "4.1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                GeminiLLMClient(min_request_interval_seconds=value)


class EvidenceNormalizationTests(unittest.TestCase):
    def test_typography_normalization_retains_declared_source(self):
        corpus = commentary_corpus(["Anita Rao:\nOur com\xadpany’s revenue increased – demand stayed firm."])
        proof = _Quote(source_ref="block.0", quote="Our company's revenue increased - demand stayed firm.")
        self.assertEqual(_verify_quote(proof, corpus.blocks).ref, "block.0")
        PullEngine._speaker("Anita Rao", _Quote(source_ref="block.0", quote="Anita Rao:"),
            proof, corpus.blocks, corpus.coverage[0])

    def test_wrong_block_is_not_silently_reassigned(self):
        corpus = commentary_corpus(["A different disclosure on this page.", "Company revenue increased by 12 percent."])
        with self.assertRaises(GroundingError):
            _verify_quote(_Quote(source_ref="block.0", quote=corpus.blocks[1].text), corpus.blocks)

    def test_normalization_never_changes_numbers_or_words(self):
        corpus = commentary_corpus(["Company revenue increased by 12 percent."])
        for quote in ("Company revenue increased by 13 percent.", "Company profit increased by 12 percent."):
            with self.subTest(quote=quote), self.assertRaises(GroundingError):
                _verify_quote(_Quote(source_ref="block.0", quote=quote), corpus.blocks)

    def test_financial_rounding_is_bounded_without_zero_or_integer_fabrication(self):
        _numbers_supported("Margin is 15.35%.", "Margin: 15.354414672922564 percent")
        _numbers_supported("Margin is 15.4%.", "Margin: 15.354414672922564 percent")
        for text, evidence in (("Margin 15%", "15.354414672922564 percent"), ("Cash 0", "Cash 0.004"),
                ("Margin -15.35%", "15.354414672922564 percent"), ("Margin 15.40%", "15.354414672922564 percent"),
                ("Cash 2026", "Cash 100")):
            with self.subTest(text=text), self.assertRaises(GroundingError):
                _numbers_supported(text, evidence)

    def test_abbreviations_and_decimal_points_do_not_split_sentences(self):
        _one_sentence("Mr. Rao cited Rs. 8,600 Cr. of cash and a 15.35% margin.")
        for text in ("Cash rose. Demand fell.", "Cash rose. demand fell.", "Cash rose.\nDemand fell."):
            with self.subTest(text=text), self.assertRaises(GroundingError):
                _one_sentence(text)

    def test_section_heading_keeps_speaker_but_questioner_clears_it(self):
        corpus = commentary_corpus(["Anita Rao:\nNote:\nWe expect strong revenue growth next quarter.",
            "Moderator:\nWe expect the CFO to answer a question."])
        self.assertEqual(_speaker_turn(corpus.blocks, corpus.blocks[0], 40)[0], "Anita Rao")
        self.assertEqual(_speaker_turn(corpus.blocks, corpus.blocks[1], 40)[0], "Moderator")

    def test_basis_can_be_proved_by_same_block_statement_heading(self):
        corpus = commentary_corpus(["Consolidated Statement of Cash Flows\nCash paid for fixed assets was INR 50 crore."])
        proof = _Quote(source_ref="block.0", quote="Cash paid for fixed assets was INR 50 crore.")
        self.assertEqual(_reporting_basis(proof, corpus.blocks, None, "consolidated"), "consolidated")
        with self.assertRaises(GroundingError):
            _reporting_basis(proof, corpus.blocks, None, "standalone")

    def test_basis_is_not_invented_from_table_or_unreviewed_page_gap(self):
        corpus = commentary_corpus(["Consolidated financial statements.", "Cash paid for fixed assets was INR 50 crore."])
        proof = _Quote(source_ref="block.1", quote=corpus.blocks[1].text)
        with self.assertRaises(GroundingError):
            _reporting_basis(proof, corpus.blocks, None, "consolidated")
        mixed = commentary_corpus(["Standalone and consolidated revenue increased this quarter."])
        with self.assertRaises(GroundingError):
            _reporting_basis(_Quote(source_ref="block.0", quote=mixed.blocks[0].text), mixed.blocks, "consolidated")


def schedule_html(*, standalone=False):
    return ('<div id="company-info" data-company-id="123"' + ("" if standalone else ' data-consolidated="true"') + "></div>"
        + company_html(basis="Standalone" if standalone else "Consolidated"))


ASSETS = {"Cash Equivalents": {"Mar 2024": "20", "Mar 2025": "25", "Mar 2026": "30"},
          "Bank Balances": {"Mar 2026": "999"}}
INVESTING = {"Fixed assets purchased": {"Mar 2024": "-10", "Mar 2025": "-20", "Mar 2026": "-30"},
             "Investments purchased": {"Mar 2026": "-900"}, "Fixed assets sold": {"Mar 2026": "25"}}


class ScheduleTests(unittest.TestCase):
    def fetch(self, cache, session):
        return FundamentalsClient(cache, session=session, clock=lambda: NOW, url_validator=lambda u: u).fetch("TESTCO")

    def test_schedules_align_by_period_and_cache_with_provenance(self):
        session = FakeSession([FakeResponse(body=schedule_html().encode()), FakeResponse(ASSETS), FakeResponse(INVESTING)])
        with SQLiteCache(":memory:") as cache:
            company = self.fetch(cache, session)
            again = self.fetch(cache, session)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(len(company.schedule_sources), 2)
        self.assertEqual(company.balance_sheet.at("cash_and_equivalents", date(2026, 3, 31)), 30)
        self.assertEqual(company.cash_flow.at("cash_capex_outflow", date(2026, 3, 31)), -30)
        self.assertEqual(company.cash_flow.rows, again.cash_flow.rows)
        self.assertTrue(all("consolidated=" in url for url, _ in session.calls[1:]))
        self.assertIn(company.cash_flow.row("cash_capex_outflow").source_id, {s.source_id for s in company.schedule_sources})

    def test_standalone_schedule_has_no_consolidated_flag(self):
        standalone = FakeResponse(body=schedule_html(standalone=True).encode())
        session = FakeSession([standalone, FakeResponse(ASSETS), FakeResponse(INVESTING)])
        with SQLiteCache(":memory:") as cache:
            company = self.fetch(cache, session)
        self.assertEqual(company.basis, "standalone")
        self.assertTrue(all("consolidated=" not in url for url, _ in session.calls[1:]))

    def test_bad_optional_schedule_is_visible_without_breaking_core_tables(self):
        session = FakeSession([FakeResponse(body=schedule_html().encode()), FakeResponse(status=403),
            FakeResponse(body=b"<html>login</html>")])
        with SQLiteCache(":memory:") as cache:
            company = self.fetch(cache, session)
        self.assertIsNotNone(company.latest_quarter_headlines)
        self.assertEqual(len(company.schedule_sources), 0)
        self.assertEqual(sum(g.reason == "source_unavailable" for g in company.data_gaps), 2)

    def test_non_cash_rows_and_positive_investing_receipts_are_not_capex(self):
        session = FakeSession([FakeResponse(body=schedule_html().encode()), FakeResponse({"Bank Balances": {"Mar 2026": "200"}}),
            FakeResponse({"Fixed assets purchased": {"Mar 2026": "30"}})])
        with SQLiteCache(":memory:") as cache:
            company = self.fetch(cache, session)
        self.assertIsNone(company.balance_sheet.row("cash_and_equivalents"))
        self.assertIsNone(company.cash_flow.row("cash_capex_outflow"))

    def test_duplicate_cash_aliases_and_missing_periods_do_not_choose_a_winner(self):
        assets = {"Cash Equivalents": {"Mar 2026": "30"}, "Cash and Cash Equivalents": {"Mar 2026": "40"}}
        session = FakeSession([FakeResponse(body=schedule_html().encode()), FakeResponse(assets),
            FakeResponse({"Fixed assets purchased": {"Mar 2001": "-100"}})])
        with SQLiteCache(":memory:") as cache:
            company = self.fetch(cache, session)
        self.assertEqual(company.schedule_sources, ())
        self.assertIsNone(company.balance_sheet.row("cash_and_equivalents"))
        self.assertIsNone(company.cash_flow.row("cash_capex_outflow"))

    def test_schedule_redirect_to_different_issuer_is_not_accepted(self):
        client = FundamentalsClient(SQLiteCache(":memory:"), session=FakeSession([]), clock=lambda: NOW, url_validator=lambda u: u)
        wrong = _Resource(json.dumps(ASSETS).encode(),
            "https://www.screener.in/api/company/999/schedules/?parent=Other+Assets&section=balance-sheet&consolidated=", NOW, False)
        try:
            with patch.object(client, "_get", return_value=wrong):
                company = client._enrich_schedules(parse(), schedule_html().encode(), force_refresh=False)
            self.assertEqual(company.schedule_sources, ())
            self.assertIsNone(company.balance_sheet.row("cash_and_equivalents"))
        finally:
            client.cache.close()

    def test_peer_company_row_cannot_supply_schedule_identity(self):
        session = FakeSession([FakeResponse(body=('<tr data-row-company-id="999"></tr>' + company_html()).encode())])
        with SQLiteCache(":memory:") as cache:
            self.fetch(cache, session)
        self.assertEqual(len(session.calls), 1)

    def test_schedule_cash_and_fcf_flow_into_report_and_copilot_sources(self):
        session = FakeSession([FakeResponse(body=schedule_html().encode()), FakeResponse(ASSETS), FakeResponse(INVESTING)])
        documents = Documents(no_capex=True)
        with SQLiteCache(":memory:") as cache:
            enriched = self.fetch(cache, session)
            documents.company = updated(enriched, documents=documents.company.documents)
            with PullEngine(cache, settings=SETTINGS, clock=lambda: NOW, fundamentals_factory=lambda c: documents,
                    llm_enabled=False) as engine:
                report = engine.generate_report("TESTCO")
                answer = engine.ask_primary_filings("TESTCO", "What was free cash flow?")
        self.assertEqual(report.trajectory_and_balance_sheet.cash_and_equivalents.value, 30)
        self.assertEqual(report.cash_quality.history[-1].capex.value, 30)
        self.assertEqual(report.cash_quality.history[-1].free_cash_flow_inr_crore, 60)
        capex_source = report.cash_quality.history[-1].capex.citations[0].source_id
        self.assertTrue(any(s.source_id == capex_source and "/schedules/" in str(s.url) for s in report.sources))
        self.assertTrue(answer.citations)
        self.assertTrue(all(c.citation.source_id in {s.source_id for s in report.sources} for c in answer.citations))

    def test_conflicting_pdf_and_schedule_capex_do_not_produce_fcf(self):
        docs = Documents()  # Primary PDF states 50; schedule below says 30.
        row = FinancialRow(metric="cash_capex_outflow", source_label="Fixed assets purchased", unit="inr_crore",
            values=(-10.0, -20.0, -30.0), raw_values=("-10", "-20", "-30"))
        docs.company = updated(docs.company, cash_flow=updated(docs.company.cash_flow, rows=docs.company.cash_flow.rows + (row,)))
        with SQLiteCache(":memory:") as cache, PullEngine(cache, settings=SETTINGS, clock=lambda: NOW,
                fundamentals_factory=lambda c: docs, llm_transport=Models(docs)) as engine:
            report = engine.generate_report("TESTCO")
        self.assertIsNone(report.cash_quality.history[-1].free_cash_flow_inr_crore)
        self.assertTrue(any(g.field == "capex_conflict_2026" for g in report.cash_quality.data_gaps))


class AnnualSelectionTests(unittest.TestCase):
    def test_chunks_never_glue_text_across_unselected_pages(self):
        pages = (PdfPageText(page=1, text="Management Opening Remarks\nFirst passage."),
            PdfPageText(page=30, text=""), PdfPageText(page=31, text="Unrelated later financial passage."))
        chunks = chunk_transcript(pages, "annual.synthetic")
        self.assertEqual([(c.page_start, c.page_end) for c in chunks], [(1, 1), (31, 31)])
    @staticmethod
    def annual_document():
        return DocumentLink(document_id="annual.synthetic", ticker="TESTCO", kind="annual_report",
            url="https://issuer.example/annual.pdf", listed_label="Annual Report 2026", reporting_year=2026)

    def test_late_financial_sections_are_retained_with_actual_page_numbers(self):
        pages = ["Annual Report 2025-2026"] + [f"Corporate portraits on page {i}." for i in range(2, 81)]
        for page, text in ((32, "Management Discussion and Analysis"), (43, "Consolidated Balance Sheet"),
                (54, "Consolidated Statement of Cash Flows"), (65, "Segment Reporting"), (74, "Contingent Liabilities")):
            pages[page - 1] = text + "\nSynthetic financial disclosure with supporting table and notes."
        payload = pdf_bytes(pages)
        result = extract_pdf_text(payload, self.annual_document(), extracted_at=NOW)
        selected = {page.page for page in result.pages}
        self.assertEqual(len(selected), 25)
        self.assertTrue({1, 2, 3, 32, 43, 54, 65, 74} <= selected)
        self.assertTrue(result.truncated)
        self.assertEqual(selected, {p.page for p in extract_pdf_text(payload, self.annual_document(), extracted_at=NOW).pages})
        call = extract_pdf_text(payload, document(), extracted_at=NOW)
        self.assertEqual([p.page for p in call.pages], list(range(1, 26)))

    def test_small_page_budgets_and_unscannable_reports_respect_limit(self):
        payload = pdf_bytes(["Annual Report 2025-2026"] + ["No financial headings available."] * 30)
        for limit in (1, 2, 3, 7):
            with self.subTest(limit=limit):
                result = extract_pdf_text(payload, self.annual_document(), max_pages=limit, extracted_at=NOW)
                self.assertEqual(len(result.pages), limit)
                self.assertEqual(result.pages[0].page, 1)

    def test_annual_selection_policy_refresh_reuses_cached_raw_pdf(self):
        payload = pdf_bytes(["Annual Report 2025-2026\nConsolidated financial statements."] + ["Corporate material."] * 29)
        session = FakeSession([FakeResponse(body=payload, content_type="application/pdf")])
        with SQLiteCache(":memory:") as cache:
            client = FundamentalsClient(cache, session=session, clock=lambda: NOW, url_validator=lambda u: u)
            first = client.fetch_transcript(self.annual_document())
            with patch("backend.data.fundamentals.ANNUAL_EXTRACTOR_VERSION", "annual.synthetic-policy-change"):
                refreshed = client.fetch_transcript(self.annual_document())
            cached = client.fetch_transcript(self.annual_document())
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(refreshed.cache_hit)
        self.assertTrue(cached.cache_hit)
        self.assertEqual(first.pages, refreshed.pages)


class FilteredMapTests(unittest.TestCase):
    def test_fully_rejected_batches_are_cached_but_never_count_as_execution_evidence(self):
        documents = Documents(annual=False, shifted=False)
        models = Models(documents, bad_quote=True)
        with SQLiteCache(":memory:") as cache, PullEngine(cache, settings=SETTINGS, clock=lambda: NOW,
                fundamentals_factory=lambda c: documents, llm_transport=models) as engine:
            first = engine.generate_report("TESTCO")
            second = engine.generate_report("TESTCO")
        self.assertGreater(first.telemetry.tier1_calls, 0)
        self.assertEqual(second.telemetry.tier1_calls, 0)
        self.assertGreater(sum(c.rejected_items for c in first.document_coverage), 0)
        self.assertEqual(sum(c.mapped_blocks for c in first.document_coverage), sum(c.rejected_only_blocks for c in first.document_coverage))
        self.assertTrue(all(not q.comparisons and q.verdict is None for q in first.narrative_vs_numbers))
        self.assertIsNone(first.management_execution_score.score_out_of_100)
        self.assertGreater(first.telemetry.failures, 0)

    def test_scrape_window_boundary_growth_is_not_labeled_missing_disclosure(self):
        docs = Documents()
        with SQLiteCache(":memory:") as cache, PullEngine(cache, settings=SETTINGS, clock=lambda: NOW,
                fundamentals_factory=lambda c: docs, llm_enabled=False) as engine:
            report = engine.generate_report("TESTCO")
        earliest = min(col.end_on for col in docs.company.quarters.columns if col.end_on)
        self.assertFalse(any(record.fact.period.end_on == earliest and "growth_" in record.fact.metric
            for record in report.quantitative_facts))
        self.assertTrue(any(reference.quote for reference in report.citation_catalog if reference.citation.page))


if __name__ == "__main__":
    unittest.main()
