"""Synthetic primary documents exercise the complete PULL pipeline offline."""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from datetime import date, datetime, timezone

from bs4 import BeautifulSoup

from backend.cache import SQLiteCache
from backend.config import LLMRouterConfig, Settings
from backend.data.fundamentals import DocumentCatalog, DocumentLink, PdfPageText, TranscriptExtraction, parse_documents
from backend.engines.pull_engine import (
    NOT_DISCLOSED,
    GroundingError,
    PullEngine,
    PullResearchReport,
    _amount,
    _fiscal_label,
    _metric_supported,
    _quarter,
    _shift,
)
from backend.engines.push_engine import LLMCompletion, LLMUsage
from backend.schemas import PullResearchReport as CorePullReport
from tests.test_fundamentals import QUARTERS, company_html, parse, table

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
SETTINGS = Settings(router=LLMRouterConfig(free_tier_project_confirmed=True))
OVERVIEW = "We manufacture precision components for industrial and mobility customers."
SEGMENT = "The controls segment contributed 60% of revenue in FY26."
MOAT = "Management describes lengthy customer qualification requirements as supporting repeat business."
DRIVER = "The product range includes controls, sensors and precision assemblies."
CAPACITY = "New capacity is planned to support customer shipment requirements."
RISK1 = "Raw-material price changes can pressure margins before customer price resets."
RISK2 = "Revenue is concentrated among a small set of major customers."
RISK3 = "Capacity commissioning delays could affect the shipment schedule."
CAPEX = "Cash payments to purchase property, plant and equipment for FY26 were INR 50 crore."
REVISION = "We defer our Q2 FY27 revenue growth guidance to a later quarter."


def updated(model, **kwargs):
    return type(model).model_validate(model.model_copy(update=kwargs))


def company(*, cagr=False, pledged=False):
    count = len(QUARTERS)
    sales = [100.0] * count
    sales[-4:] = [130.0, 115.0, 105.0, 100.0]
    rows = [("Sales", sales), ("Operating Profit", [v * 0.2 for v in sales]),
            ("Other Income", [3] * count), ("Interest", [1] * count), ("Depreciation", [2] * count),
            ("Profit before tax", [v * 0.15 for v in sales]), ("Net Profit", [v * 0.1 for v in sales])]
    html = company_html(quarterly_rows=rows, share_pledge=pledged)
    if cagr:
        soup = BeautifulSoup(html, "html.parser")
        labels = [f"Mar {year}" for year in range(2021, 2027)]
        revenue = [100.0 * 1.1**i for i in range(6)]
        new = table("profit-loss", labels, [("Sales", revenue), ("Operating Profit", [v * 0.2 for v in revenue]),
                    ("Net Profit", [v * 0.1 for v in revenue])])
        soup.find(id="profit-loss").replace_with(BeautifulSoup(new, "html.parser"))
        html = str(soup)
    return parse(html)


class Documents:
    def __init__(self, *, annual=True, calls=5, missing_date=False, bad_listing=False, annual_only=False,
                 qualitative=False, constant_currency=False, ebitda=False, analyst=False, no_capex=False,
                 book_capex=False, shifted=True, no_primary=False, cagr=False, pledged=False):
        self.fetches, self.downloads = [], []
        self.outputs, self.texts, self.promises = {}, {}, {}
        self.revision_text = REVISION
        self.options = locals().copy()
        base = company(cagr=cagr, pledged=pledged)
        call_docs = []
        ends = [date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30)]
        for index, end in enumerate(ends[:calls]):
            target = _quarter(_shift(end, 3))
            if annual_only:
                target = updated(target, start_on=date(2026, 4, 1), end_on=date(2027, 3, 31), kind="annual", label="FY27")
                target_label = "FY27"
            else:
                target_label = _fiscal_label(target.end_on, 3)
            metric = "ebitda_margin_pct" if ebitda else "revenue_growth_yoy"
            measure = "EBITDA margin" if ebitda else "revenue growth"
            qualifier = " in constant currency" if constant_currency else ""
            promise = f"We expect {measure} of 10% to 20% YoY{qualifier} in {target_label}."
            if qualitative:
                promise = f"We expect strong revenue growth YoY in {target_label}."
            header = (f"Test Company Limited\n{_fiscal_label(end, 3)} Earnings Conference Call\n"
                      + ("" if missing_date else f"Call date: {(_shift(end, 1).replace(day=20)).isoformat()}\n")
                      + f"Reported quarter ended: {end.isoformat()}\nConsolidated financial discussion.\nAnita Rao - Chief Financial Officer\n")
            speaker = "Analyst Person" if analyst else "Anita Rao"
            body = speaker + ":\n" + promise
            if index == 4 and shifted:
                body += "\n" + self.revision_text
            document = DocumentLink(document_id=f"call.{index}", ticker="TESTCO", kind="concall_transcript",
                url=f"https://issuer.example/call-{index}.pdf", listed_label=f"Call {index}",
                listed_month=_shift(end, 1).replace(day=1), embedded_notes="AI SUMMARY: invented 999% guidance; never use this.")
            call_docs.append(document)
            self.promises[document.document_id] = (promise, metric, target)
            self.texts[document.document_id] = (header, body)
            self.outputs[document.document_id] = self.extraction(document, header, body,
                bad_listing=bad_listing, no_primary=no_primary)
        annual_docs = []
        if annual:
            document = DocumentLink(document_id="annual.26", ticker="TESTCO", kind="annual_report",
                url="https://issuer.example/ar-2026.pdf", listed_label="Annual Report 2026", reporting_year=2026)
            annual_docs.append(document)
            header = "Test Company Limited\nAnnual Report 2025-2026\nConsolidated financial statements."
            text = "\n".join((OVERVIEW, SEGMENT, MOAT, DRIVER, CAPACITY, RISK1, RISK2, RISK3))
            if not no_capex:
                text += "\n" + ("Book asset additions for FY26 were INR 50 crore." if book_capex else CAPEX)
            self.texts[document.document_id] = (header, text)
            self.outputs[document.document_id] = self.extraction(document, header, text, no_primary=no_primary)
        self.company = updated(base, documents=DocumentCatalog(concall_transcripts=tuple(call_docs), annual_reports=tuple(annual_docs),
                               investor_presentations=(), selection_notes=()))

    @staticmethod
    def extraction(document, header, body, *, bad_listing=False, no_primary=False):
        return TranscriptExtraction(document=document, resolved_url=document.url,
            pdf_sha256=hashlib.sha256((header + body).encode()).hexdigest(), extracted_at=NOW,
            status="error" if no_primary else "ok", total_pages=2,
            pages=() if no_primary else (PdfPageText(page=1, text=header), PdfPageText(page=2, text=body)),
            chunks=(), truncated=False, listing_period_mismatch=bad_listing,
            error="Fixture unavailable" if no_primary else None)

    def fetch(self, ticker, **kwargs):
        self.fetches.append((ticker, kwargs))
        return self.company

    def fetch_transcript(self, document, **kwargs):
        self.downloads.append(document.document_id)
        return self.outputs[document.document_id]


class Models:
    def __init__(self, documents, *, bad_quote=False, bad_number=False, no_bull=False, disabled=False,
                 narrative_qa=False, bogus_qa=False):
        self.documents, self.calls = documents, []
        self.bad_quote, self.bad_number, self.no_bull, self.disabled = bad_quote, bad_number, no_bull, disabled
        self.narrative_qa, self.bogus_qa = narrative_qa, bogus_qa

    def available(self, route):
        return not self.disabled

    def complete(self, route, system, inputs, output):
        self.calls.append((route, inputs, output.__name__))
        if output.__name__ == "_Timing":
            result = {"call_on": None, "call_date_evidence": None, "reported_quarter_end": None,
                "reporting_period_evidence": None, "basis": None, "basis_evidence": None,
                "reporting_year": None, "annual_period_evidence": None, "management": []}
        elif output.__name__ == "_MapExtraction":
            info = inputs["metadata"]
            result = {"guidance": [], "business": [], "actuals": [], "revisions": []}
            if info["kind"] == "concall_transcript":
                promise, metric, target = self.documents.promises[info["document_id"]]
                block = next((b for b in inputs["sources"] if promise in b["text"]), None)
                if block:
                    ref = block["source_ref"]
                    proof = {"source_ref": ref, "quote": "Made-up quote" if self.bad_quote else promise}
                    speaker = "Analyst Person" if self.documents.options["analyst"] else "Anita Rao"
                    result["guidance"] = [{"metric": metric, "evidence": proof, "speaker": speaker,
                        "speaker_evidence": {"source_ref": ref, "quote": speaker + ":"},
                        "target_period": target.model_dump(mode="json"), "target_evidence": {"source_ref": ref, "quote": promise},
                        "basis": "consolidated", "measurement_basis": "constant_currency" if self.documents.options["constant_currency"] else "reported",
                        "entity_scope": "company", "unit": "percent", "lower_token": None if self.documents.options["qualitative"] else "999" if self.bad_number else "10",
                        "upper_token": None if self.documents.options["qualitative"] else "20"}]
                    if self.documents.revision_text in block["text"]:
                        result["revisions"] = [{"entity_scope": "company", "metric": "revenue_growth_yoy", "original_quarter_end": "2026-09-30",
                            "evidence": {"source_ref": ref, "quote": self.documents.revision_text}, "speaker": "Anita Rao",
                            "speaker_evidence": {"source_ref": ref, "quote": "Anita Rao:"}}]
            else:
                block = next((b for b in inputs["sources"] if OVERVIEW in b["text"]), None)
                if block:
                    ref = block["source_ref"]
                    for kind, quote, segment in (("overview", OVERVIEW, None), ("segment", SEGMENT, "controls"),
                                                  ("moat", MOAT, None), ("driver", DRIVER, None)):
                        result["business"].append({"kind": kind, "evidence": {"source_ref": ref, "quote": quote}, "segment_name": segment,
                            "mix_token": "60" if kind == "segment" else None,
                            "mix_period": {"start_on": "2025-04-01", "end_on": "2026-03-31", "kind": "annual", "label": "FY26"} if kind == "segment" else None})
                    if not self.documents.options["no_capex"]:
                        quote = "Book asset additions for FY26 were INR 50 crore." if self.documents.options["book_capex"] else CAPEX
                        result["actuals"] = [{"metric": "capex", "evidence": {"source_ref": ref, "quote": quote}, "number_token": "50",
                            "unit": "inr_crore", "period": {"start_on": "2025-04-01", "end_on": "2026-03-31", "kind": "annual", "label": "FY26"},
                            "period_evidence": {"source_ref": ref, "quote": quote}, "basis": "consolidated",
                            "measurement_basis": "book_additions" if self.documents.options["book_capex"] else "cash_outflow", "entity_scope": "company"}]
        elif output.__name__ == "_AuditReview":
            lines, links = [], []
            for quarter in inputs["quarters"]:
                if quarter["verdict"] == "BEAT":
                    text = "Revenue growth beat the disclosed range, supporting execution against this specific commitment."
                elif quarter["verdict"] == "DELIVERED":
                    text = "Revenue growth met the disclosed range, supporting execution against this specific commitment."
                elif quarter["verdict"] == "MISSED":
                    text = "Revenue growth missed the disclosed range, weakening evidence for execution against the stated commitment."
                else:
                    text = NOT_DISCLOSED
                if quarter["quarter_end"] == "2026-09-30" and inputs["revisions"]:
                    links.append({"original_guidance_id": quarter["comparisons"][0]["guidance_id"],
                                  "revision_id": inputs["revisions"][0]["revision_id"]})
                    text = "Management deferred the quarterly revenue-growth target, limiting confidence in its original timetable."
                lines.append({"quarter_end": quarter["quarter_end"], "text": text})
            result = {"commentary": lines, "goalpost_links": links}
        elif output.__name__ == "_Cases":
            block = next((b for b in inputs["sources"] if OVERVIEW in b["text"]), None)
            if self.no_bull or block is None:
                result = {"bull": [], "bear": []}
            else:
                def point(text, quote):
                    return {"text": text, "evidence": [{"source_ref": block["source_ref"], "quote": quote}], "fact_ids": [], "is_inference": True}
                result = {"bull": [point("Qualification requirements may support repeat business.", MOAT),
                                   point("A broader product range may diversify component demand.", DRIVER),
                                   point("Planned capacity may support shipments if commissioning follows.", CAPACITY)],
                          "bear": [point("Raw-material resets can pressure margins before customer repricing.", RISK1),
                                   point("Customer concentration increases exposure to individual buying decisions.", RISK2),
                                   point("Commissioning delays could disrupt planned shipments.", RISK3)]}
        elif output.__name__ == "_Answer":
            if self.narrative_qa or self.bogus_qa:
                block = next(b for b in inputs["sources"] if RISK1 in b["text"])
                result = {"claims": [{"text": "Raw-material prices pressure margins before customer resets." if not self.bogus_qa else "Margins will rise by 999 percent.",
                    "evidence": [{"source_ref": block["source_ref"], "quote": RISK1}], "fact_ids": [], "is_inference": False}]}
            else:
                fact = next(f for f in inputs["facts"] if f["metric"] in {"cfo_to_pat", "cash_conversion_pct", "revenue"})
                result = {"claims": [{"text": "This uncited prose must be replaced by deterministic fact display.",
                                      "evidence": [], "fact_ids": [fact["fact_id"]], "is_inference": False}]}
        else:
            raise AssertionError(output.__name__)
        return LLMCompletion(text=json.dumps(result), finish_reason="stop",
                             usage=LLMUsage(input_tokens=100, output_tokens=25))


class PullTests(unittest.TestCase):
    def setUp(self):
        self.cache = SQLiteCache(":memory:")
        self.addCleanup(self.cache.close)

    def engine(self, documents=None, transport=None, **kwargs):
        documents = documents or Documents()
        instance = PullEngine(self.cache, settings=kwargs.pop("settings", SETTINGS), clock=lambda: NOW,
            fundamentals_factory=lambda cache: documents, llm_transport=transport or Models(documents), **kwargs)
        self.addCleanup(instance.close)
        return instance

    def test_full_four_quarter_hero_and_transparent_score(self):
        documents = Documents()
        engine = self.engine(documents)
        report = engine.generate_report("testco.NS")
        self.assertIsInstance(report, CorePullReport)
        self.assertEqual([q.verdict for q in report.narrative_vs_numbers], ["BEAT", "DELIVERED", "MISSED", "GOALPOST_SHIFTED"])
        self.assertEqual(report.management_execution_score.score_out_of_100, 56.25)
        self.assertEqual(report.management_execution_score.assessed_quarters, 4)
        self.assertIn("Q2 FY26 -> Q3 FY26", report.narrative_vs_numbers[0].quarter_label)
        self.assertIn("+30.00% YoY", report.narrative_vs_numbers[0].actual_delivered)
        self.assertTrue(report.narrative_vs_numbers[-1].goalpost_shifts)
        self.assertGreaterEqual(documents.fetches[0][1]["document_limit"], 5)

    def test_all_eight_menu_items_and_exact_cases(self):
        report = self.engine().generate_report("TESTCO")
        self.assertTrue(report.business_snapshot.segments)
        self.assertEqual(report.business_snapshot.segments[0].revenue_mix.value, 60.0)
        self.assertTrue(report.business_snapshot.core_moat)
        self.assertTrue(report.financial_snapshot.key_ratios)
        self.assertTrue(report.trajectory.history)
        self.assertTrue(report.balance_sheet.facts)
        self.assertTrue(report.cash_quality.history)
        self.assertTrue(report.forward_guidance.statements)
        self.assertEqual(len(report.bull_vs_bear.bull), 3)
        self.assertEqual(len(report.bull_vs_bear.bear), 3)

    def test_tier_routing_and_repeat_report_cache(self):
        documents = Documents()
        models = Models(documents)
        engine = self.engine(documents, models)
        first = engine.generate_report("TESTCO")
        second = engine.generate_report("TESTCO")
        self.assertEqual(first.telemetry.tier1_calls, 6)
        self.assertEqual(first.telemetry.tier2_calls, 2)
        self.assertEqual(second.telemetry.tier1_calls, 0)
        self.assertEqual(second.telemetry.tier2_calls, 0)
        self.assertGreater(second.telemetry.cache_hits, 0)
        self.assertEqual(len(documents.fetches), 1)
        self.assertEqual(first.telemetry.estimated_cost_usd, 0.0)
        for route, _, model in models.calls:
            self.assertEqual(route.model, "gemini-3.8-flash" if model in {"_AuditReview", "_Cases"} else "gemini-3.5-flash-lite")

    def test_report_strict_round_trip(self):
        report = self.engine().generate_report("TESTCO")
        parsed = PullResearchReport.model_validate_json(report.model_dump_json(round_trip=True))
        self.assertEqual(parsed, report)

    def test_cash_capex_and_fcf_are_not_net_investing_cash_flow(self):
        report = self.engine().generate_report("TESTCO")
        latest = report.cash_quality.history[-1]
        self.assertEqual(latest.cfo.value, 90.0)
        self.assertEqual(latest.capex.value, 50.0)
        self.assertEqual(latest.free_cash_flow_inr_crore, 40.0)
        self.assertEqual(latest.cash_conversion, 0.9)

    def test_missing_capex_keeps_fcf_unavailable(self):
        report = self.engine(Documents(no_capex=True)).generate_report("TESTCO")
        self.assertIsNone(report.cash_quality.history[-1].free_cash_flow_inr_crore)
        self.assertIn(NOT_DISCLOSED, report.cash_quality.history[-1].capex.missing_reason)

    def test_book_asset_additions_are_not_cash_capex(self):
        report = self.engine(Documents(book_capex=True)).generate_report("TESTCO")
        self.assertIsNone(report.cash_quality.history[-1].free_cash_flow_inr_crore)

    def test_debt_equity_and_wc_measures_remain_distinct(self):
        report = self.engine().generate_report("TESTCO")
        self.assertEqual(report.trajectory_and_balance_sheet.debt_to_equity.value, 0.8)
        self.assertEqual(report.trajectory_and_balance_sheet.working_capital[-1].cycle_days, 10.0)
        self.assertTrue(any("41" in line and "+16" in line for line in report.balance_sheet.working_capital_trend))

    def test_absent_pledge_is_not_zero(self):
        report = self.engine().generate_report("TESTCO")
        self.assertTrue(any(g.field == "promoter_pledging_pct" for g in report.balance_sheet.data_gaps))
        self.assertFalse(any(f.fact.metric == "promoter_pledging_pct" for f in report.balance_sheet.facts))

    def test_cagr_exact_three_and_five_year_baselines(self):
        report = self.engine(Documents(cagr=True)).generate_report("TESTCO")
        revenue = [t for t in report.trajectory.cagr if t.metric == "revenue"]
        self.assertEqual({t.years for t in revenue}, {3, 5})
        for trend in revenue:
            self.assertAlmostEqual(trend.cagr_pct, 10.0, places=8)

    def test_qualitative_promises_are_not_invented_numeric_beats(self):
        report = self.engine(Documents(qualitative=True, shifted=False)).generate_report("TESTCO")
        self.assertTrue(all(q.verdict is None for q in report.narrative_vs_numbers))
        self.assertIsNone(report.management_execution_score.score_out_of_100)
        self.assertTrue(all(q.data_gaps for q in report.narrative_vs_numbers))

    def test_annual_guidance_is_not_scored_against_a_quarter(self):
        report = self.engine(Documents(annual_only=True, shifted=False)).generate_report("TESTCO")
        self.assertTrue(all(not q.comparisons for q in report.narrative_vs_numbers))
        self.assertTrue(report.forward_guidance.statements)
        self.assertTrue(all(g.target_period.kind == "annual" for g in report.forward_guidance.statements))

    def test_constant_currency_guidance_does_not_compare_to_reported_inr(self):
        report = self.engine(Documents(constant_currency=True, shifted=False)).generate_report("TESTCO")
        self.assertTrue(all(q.verdict is None for q in report.narrative_vs_numbers))
        self.assertTrue(all(c.actual is None for q in report.narrative_vs_numbers for c in q.comparisons))

    def test_operating_profit_proxy_cannot_score_issuer_ebitda_guidance(self):
        report = self.engine(Documents(ebitda=True, shifted=False)).generate_report("TESTCO")
        self.assertTrue(all(q.verdict is None for q in report.narrative_vs_numbers))
        self.assertTrue(any("proxy" in c.not_assessable_reason for q in report.narrative_vs_numbers for c in q.comparisons))

    def test_an_analyst_question_cannot_be_a_management_promise(self):
        report = self.engine(Documents(analyst=True, shifted=False)).generate_report("TESTCO")
        self.assertTrue(all(not q.comparisons for q in report.narrative_vs_numbers))
        self.assertTrue(any(i.stage == "primary_map" for i in report.issues))

    def test_bad_quote_and_number_are_rejected_before_cache(self):
        for option in ("bad_quote", "bad_number"):
            with self.subTest(option=option), SQLiteCache(":memory:") as cache:
                documents = Documents(shifted=False)
                models = Models(documents, **{option: True})
                with PullEngine(cache, settings=SETTINGS, clock=lambda: NOW,
                                fundamentals_factory=lambda c: documents, llm_transport=models) as engine:
                    report = engine.generate_report("TESTCO")
                self.assertTrue(all(not q.comparisons for q in report.narrative_vs_numbers))
                self.assertGreater(report.telemetry.failures, 0)

    def test_missing_call_date_is_not_filled_from_listing_month(self):
        report = self.engine(Documents(missing_date=True, shifted=False)).generate_report("TESTCO")
        self.assertTrue(all(c.call_on is None for c in report.document_coverage if c.kind == "concall_transcript"))
        self.assertTrue(all(q.verdict is None for q in report.narrative_vs_numbers))

    def test_mislabeled_or_unavailable_primary_pdf_is_excluded(self):
        for option in ("bad_listing", "no_primary"):
            documents = Documents(**{option: True})
            with self.subTest(option=option):
                report = self.engine(documents).generate_report("TESTCO", force_refresh=True)
                self.assertTrue(all(q.verdict is None for q in report.narrative_vs_numbers))
                self.assertEqual(len(report.sources), 1 if option == "no_primary" else 2)
                self.assertTrue(all(c.status == "unavailable" for c in report.document_coverage if c.kind == "concall_transcript"))

    def test_ai_notes_are_never_sent_to_models(self):
        documents = Documents()
        models = Models(documents)
        self.engine(documents, models).generate_report("TESTCO")
        self.assertNotIn("AI SUMMARY", json.dumps([inputs for _, inputs, _ in models.calls]))

    def test_no_llm_still_returns_financials_four_audit_slots_and_gaps(self):
        report = self.engine(llm_enabled=False).generate_report("TESTCO")
        self.assertEqual(len(report.narrative_vs_numbers), 4)
        self.assertTrue(report.financial_snapshot)
        self.assertTrue(report.cash_quality)
        self.assertIsNone(report.business_snapshot)
        self.assertIsNone(report.management_execution_score.score_out_of_100)
        self.assertEqual(report.telemetry.tier1_calls + report.telemetry.tier2_calls, 0)
        self.assertTrue(all(q.management_promise.startswith(("Unavailable:", "Unscored management commentary:"))
            for q in report.narrative_vs_numbers))

    def test_no_padding_bull_bear_when_six_points_are_not_supported(self):
        documents = Documents()
        report = self.engine(documents, Models(documents, no_bull=True)).generate_report("TESTCO")
        self.assertIsNone(report.bull_vs_bear)
        self.assertTrue(any(g.field == "bull_bear_case" for g in report.data_gaps))

    def test_one_assessed_quarter_does_not_invent_overall_score(self):
        report = self.engine(Documents(calls=1, shifted=False)).generate_report("TESTCO")
        self.assertEqual(report.management_execution_score.assessed_quarters, 1)
        self.assertIsNone(report.management_execution_score.score_out_of_100)

    def test_wrong_ticker_and_demo_financial_sources_are_rejected(self):
        documents = Documents()
        documents.company = updated(documents.company, ticker="OTHER")
        with self.assertRaises(GroundingError):
            self.engine(documents).generate_report("TESTCO")
        documents = Documents()
        documents.company = updated(documents.company, source=updated(documents.company.source, is_demo=True))
        with self.assertRaises(GroundingError):
            self.engine(documents).generate_report("TESTCO")

    def test_historical_cutoff_cannot_use_a_current_scrape(self):
        with self.assertRaises(GroundingError):
            self.engine().generate_report("TESTCO", as_of=date(2026, 9, 30))

    def test_qa_no_cache_means_no_network_or_model_calls(self):
        documents = Documents()
        models = Models(documents)
        answer = self.engine(documents, models).ask_primary_filings("TESTCO", "What is revenue?")
        self.assertEqual(answer.status, "no_cached_data")
        self.assertEqual(answer.answer, NOT_DISCLOSED)
        self.assertFalse(documents.fetches)
        self.assertFalse(models.calls)

    def test_qa_fact_only_output_cannot_smuggle_uncited_prose(self):
        engine = self.engine()
        engine.generate_report("TESTCO")
        answer = engine.ask_primary_filings("TESTCO", "What is cash conversion?")
        self.assertEqual(answer.status, "answered")
        self.assertNotIn("uncited prose", answer.answer)
        self.assertTrue(answer.citations)
        self.assertEqual(answer.telemetry.tier2_calls, 1)
        repeated = engine.ask_primary_filings("TESTCO", "What is cash conversion?")
        self.assertEqual(repeated.telemetry.tier2_calls, 0)

    def test_qa_primary_citations_include_exact_quote_and_page(self):
        documents = Documents()
        engine = self.engine(documents, Models(documents, narrative_qa=True))
        engine.generate_report("TESTCO")
        answer = engine.ask_primary_filings("TESTCO", "Why can raw materials pressure margins?")
        self.assertEqual(answer.status, "answered")
        self.assertIn("[FY26 AR]", answer.answer)
        self.assertEqual(answer.citations[0].quote, RISK1)
        self.assertEqual(answer.citations[0].citation.page, 2)

    def test_qa_unsupported_number_fails_grounding(self):
        documents = Documents()
        engine = self.engine(documents, Models(documents, bogus_qa=True))
        engine.generate_report("TESTCO")
        answer = engine.ask_primary_filings("TESTCO", "Why would margins improve?")
        self.assertEqual(answer.status, "model_unavailable")
        self.assertEqual(answer.answer, NOT_DISCLOSED)
        self.assertEqual(answer.telemetry.failures, 1)

    def test_qa_no_llm_can_return_deterministic_cash_facts(self):
        engine = self.engine(llm_enabled=False)
        engine.generate_report("TESTCO")
        answer = engine.ask_primary_filings("TESTCO", "Does profit turn into cash?")
        self.assertEqual(answer.status, "answered")
        self.assertTrue(answer.citations)
        self.assertEqual(answer.telemetry.tier2_calls, 0)

    def test_qa_no_llm_does_not_invent_causation(self):
        engine = self.engine(llm_enabled=False)
        engine.generate_report("TESTCO")
        answer = engine.ask_primary_filings("TESTCO", "Why did margins fall?")
        self.assertEqual(answer.status, "model_unavailable")
        self.assertEqual(answer.answer, NOT_DISCLOSED)

    def test_qa_never_crosses_ticker(self):
        engine = self.engine()
        engine.generate_report("TESTCO")
        answer = engine.ask_primary_filings("OTHER", "What is revenue?")
        self.assertEqual(answer.status, "no_cached_data")
        self.assertEqual(answer.ticker, "OTHER")

    def test_invalid_question_and_limits_fail_early(self):
        engine = self.engine()
        for question in ("", " ", "x" * 2401):
            with self.subTest(question=question[:5]), self.assertRaises(ValueError):
                engine.ask_primary_filings("TESTCO", question)
        with self.assertRaises(ValueError):
            self.engine(max_pdf_pages=26)

    def test_supported_number_units_are_normalized_in_python(self):
        self.assertEqual(_amount("500", "inr_million", "INR 500 million"), 50.0)
        self.assertEqual(_amount("2", "inr_billion", "INR 2 billion"), 200.0)
        with self.assertRaises(GroundingError):
            _amount("10", "percent", "Revenue is 100 crore")
        with self.assertRaises(GroundingError):
            _amount("500", "inr_million", "USD 500 million")
        self.assertEqual(_amount("-10", "percent", "Revenue growth target is −10%"), -10.0)

    def test_disclosed_metric_cannot_be_relabelled(self):
        with self.assertRaises(GroundingError):
            _metric_supported("revenue_growth_yoy", "We expect PAT growth of 10% YoY.")
        with self.assertRaises(GroundingError):
            _metric_supported("capex", "Revenue for FY26 was INR 500 crore.")

    def test_every_display_tag_resolves_to_citation_catalog(self):
        report = self.engine(Documents(cagr=True)).generate_report("TESTCO")
        available = {c.tag for c in report.citation_catalog}
        lines = [report.management_execution_score.summary]
        for quarter in report.narrative_vs_numbers:
            lines.extend((quarter.management_promise, quarter.actual_delivered, quarter.auditor_commentary))
        lines.extend(report.trajectory.margin_trends)
        lines.extend(t.statement for t in report.trajectory.cagr)
        lines.extend(report.cash_quality.observations)
        for text in lines:
            if text.startswith(NOT_DISCLOSED):
                continue
            tags = re.findall(r"\[([^\]]+)\]", text)
            with self.subTest(text=text[:70]):
                self.assertTrue(tags)
                self.assertTrue(set(tags).issubset(available), set(tags) - available)

    def test_document_catalog_can_request_a_fifth_call(self):
        soup = BeautifulSoup('<section id="documents"><div class="documents concalls"><h3>Concalls</h3><ul>' +
            "".join(f'<li><div>{label}</div><a href="https://issuer.example/call-{index}.pdf">Transcript</a></li>'
                    for index, label in enumerate(("Oct 2026", "Jul 2026", "Apr 2026", "Jan 2026", "Oct 2025", "Jul 2025"))) +
            '</ul></div></section>', "html.parser")
        catalog = parse_documents(soup, "TESTCO", base_url="https://www.screener.in/company/TESTCO/consolidated/", limit=6)
        self.assertEqual(len(catalog.concall_transcripts), 6)

    def test_legacy_adapter_without_document_limit_still_runs(self):
        documents = Documents(calls=4)
        class Legacy:
            def fetch(self, ticker, *, include_transcripts=False, force_refresh=False):
                return documents.company
            def fetch_transcript(self, document, **kwargs):
                return documents.outputs[document.document_id]
        with PullEngine(self.cache, settings=SETTINGS, clock=lambda: NOW, llm_transport=Models(documents),
                        fundamentals_factory=lambda cache: Legacy()) as engine:
            report = engine.generate_report("TESTCO")
        self.assertEqual(len(report.narrative_vs_numbers), 4)
        self.assertTrue(any(i.code == "LegacyCatalogLimit" for i in report.issues))

    def test_wrong_issuer_pdf_extraction_is_excluded(self):
        documents = Documents(calls=1, annual=False)
        extraction = documents.outputs["call.0"]
        documents.outputs["call.0"] = updated(extraction, document=updated(extraction.document, ticker="OTHER"))
        report = self.engine(documents).generate_report("TESTCO")
        self.assertEqual(len(report.sources), 1)
        self.assertEqual(report.document_coverage[0].status, "unavailable")

    def test_upward_guidance_revision_is_not_penalized_as_a_goalpost_shift(self):
        documents = Documents()
        document_id = "call.4"
        upgraded = "We raise our Q2 FY27 revenue growth guidance to a higher range."
        documents.revision_text = upgraded
        header, body = documents.texts[document_id]
        body = body.replace(REVISION, upgraded)
        documents.texts[document_id] = (header, body)
        extraction = documents.outputs[document_id]
        documents.outputs[document_id] = Documents.extraction(extraction.document, header, body)
        report = self.engine(documents).generate_report("TESTCO")
        self.assertEqual(report.narrative_vs_numbers[-1].verdict, "MISSED")
        self.assertFalse(report.narrative_vs_numbers[-1].goalpost_shifts)
        self.assertEqual(report.management_execution_score.score_out_of_100, 50.0)

    def test_mixed_metrics_do_not_attribute_a_pat_miss_to_revenue(self):
        documents = Documents(calls=1, annual=False)
        current = documents.outputs["call.0"]
        pat_promise = "We expect PAT growth of 10% to 20% YoY in Q3 FY26."
        header, body = documents.texts["call.0"]
        documents.outputs["call.0"] = Documents.extraction(current.document, header, body + "\n" + pat_promise)
        quarters = documents.company.quarters
        pat = quarters.row("pat")
        values = list(pat.values)
        index = next(i for i, c in enumerate(quarters.columns) if c.end_on == date(2025, 12, 31))
        values[index] = 10.0
        replacement = updated(pat, values=tuple(values), raw_values=tuple(str(v) for v in values))
        documents.company = updated(documents.company, quarters=updated(quarters,
            rows=tuple(replacement if row.metric == "pat" else row for row in quarters.rows)))
        class MixedModels(Models):
            def complete(self, route, system, inputs, output):
                if output.__name__ == "_AuditReview":
                    return LLMCompletion(text='{"commentary":[],"goalpost_links":[]}', finish_reason="stop")
                response = super().complete(route, system, inputs, output)
                if output.__name__ == "_MapExtraction" and inputs["metadata"]["document_id"] == "call.0":
                    payload = json.loads(response.text)
                    extra = json.loads(json.dumps(payload["guidance"][0]))
                    extra["metric"] = "pat_growth_yoy"
                    extra["evidence"]["quote"] = pat_promise
                    extra["target_evidence"]["quote"] = pat_promise
                    payload["guidance"].append(extra)
                    return updated(response, text=json.dumps(payload))
                return response
        report = self.engine(documents, MixedModels(documents)).generate_report("TESTCO")
        first = report.narrative_vs_numbers[0]
        self.assertEqual(first.verdict, "MISSED")
        self.assertIn("pat growth yoy target was missed", first.auditor_commentary)
        self.assertNotIn("revenue growth yoy target was missed", first.auditor_commentary)


if __name__ == "__main__":
    unittest.main()
