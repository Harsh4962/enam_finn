"""Regression tests for real issuer layouts and partially invalid model output.

Financial/disclosure fixtures here are synthetic; no test needs credentials.
The issuer PDF layout was separately checked against a downloaded primary PDF.
"""
from __future__ import annotations

import json
import unittest
from datetime import date

from backend.cache import SQLiteCache
from backend.data.fundamentals import PdfPageText
from backend.engines.pull_engine import (
    GroundingError,
    PullEngine,
    _quarter_mentions,
    _Quote,
)
from backend.engines.push_engine import LLMCompletion
from tests.test_pull_engine import NOW, SETTINGS, Documents, Models, updated


class MutatedModels(Models):
    def __init__(self, documents, change):
        super().__init__(documents)
        self.change = change

    def complete(self, route, system, inputs, output):
        response = super().complete(route, system, inputs, output)
        if output.__name__ == "_MapExtraction":
            payload = json.loads(response.text)
            self.change(payload, inputs)
            return updated(response, text=json.dumps(payload))
        return response


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.cache = SQLiteCache(":memory:")
        self.addCleanup(self.cache.close)

    def engine(self, documents, models=None, **kwargs):
        engine = PullEngine(self.cache, settings=SETTINGS, clock=lambda: NOW,
            fundamentals_factory=lambda c: documents, llm_transport=models or Models(documents), **kwargs)
        self.addCleanup(engine.close)
        return engine

    def timing(self, header, *, model=None):
        documents = Documents(calls=1, annual=False)
        extraction = documents.outputs["call.0"]
        extraction = updated(extraction, pages=(PdfPageText(page=1, text=header),))
        engine = self.engine(documents, model)
        blocks = engine._blocks(extraction, "primary.layout", "Concall")
        return engine._timing("TESTCO", extraction, blocks, 3, NOW.date(), False), engine

    def test_fiscal_range_titles_resolve_end_year_without_extra_quarter(self):
        for title, expected in (("Q1 FY2026 - 2027", date(2026, 6, 30)),
                                ("Q4 2025 - 2026", date(2026, 3, 31)),
                                ("Q3 FY2025-26", date(2025, 12, 31))):
            with self.subTest(title=title):
                self.assertEqual(_quarter_mentions(title, 3), {expected})

    def test_adjacent_title_date_comma_roles_and_honorifics_need_no_llm(self):
        timing, engine = self.timing("Test Company Q4 2025 - 2026\nMedia & Analyst Call Transcript\n"
            "24 April 2026 20:02 IST\nCall Participants:\n•\nSh Arjun Mehta, CFO, Test Company\n"
            "•\nMs Nina Sharma, Head of Strategy, Test Company\n")
        self.assertEqual(timing.call_on, date(2026, 4, 24))
        self.assertEqual(timing.reported_quarter_end, date(2026, 3, 31))
        self.assertEqual(len(timing.management), 2)
        self.assertEqual(engine.transport.calls, [])

    def test_reported_quarter_date_below_title_is_not_event_date(self):
        timing, _ = self.timing("Q3 FY26 Earnings Conference Call\nReported quarter ended: 2025-12-31\n"
            "Arjun Mehta - Chief Financial Officer\n")
        self.assertIsNone(timing.call_on)
        self.assertEqual(timing.reported_quarter_end, date(2025, 12, 31))

    def test_cover_letter_date_does_not_override_explicit_meeting_date(self):
        timing, _ = self.timing("July 19, 2026\nTranscript of results for the quarter ended June 30, 2026, "
            "at the analyst meet held on July 17, 2026.\n"
            "Q1 FY2026 - 2027\nMedia & Analyst Call Transcript\n17 July 2026 19:52 IST\n"
            "Arjun Mehta, CFO, Test Company\n")
        self.assertEqual(timing.call_on, date(2026, 7, 17))
        self.assertEqual(timing.reported_quarter_end, date(2026, 6, 30))

    def test_mixed_basis_is_not_inherited_from_one_segment_paragraph(self):
        timing, _ = self.timing("Q4 2025-2026\nMedia & Analyst Call Transcript\n24 April 2026\n"
            "Consolidated and Standalone financial results.\nStandalone financial discussion.\n"
            "Arjun Mehta, CFO, Test Company\n")
        self.assertIsNone(timing.basis)

    def test_null_model_metadata_cannot_erase_verified_deterministic_fields(self):
        timing, _ = self.timing("Q4 2025-2026\nMedia & Analyst Call Transcript\n24 April 2026\n")
        self.assertEqual(timing.call_on, date(2026, 4, 24))
        self.assertEqual(timing.reported_quarter_end, date(2026, 3, 31))
        self.assertFalse(timing.management)

    def test_bad_guidance_quote_does_not_discard_valid_guidance_sibling(self):
        documents = Documents(calls=1, annual=False, shifted=False)
        def change(payload, inputs):
            if payload["guidance"]:
                bad = json.loads(json.dumps(payload["guidance"][0]))
                bad["evidence"]["quote"] = "This exact quote was never disclosed."
                payload["guidance"].append(bad)
        report = self.engine(documents, MutatedModels(documents, change)).generate_report("TESTCO")
        self.assertEqual(len(report.narrative_vs_numbers[0].comparisons), 1)
        self.assertEqual(report.narrative_vs_numbers[0].verdict, "BEAT")
        self.assertTrue(any("guidance[1]" in issue.detail and "Quote does not match" in issue.detail for issue in report.issues))

    def test_malformed_item_does_not_discard_valid_typed_sibling(self):
        documents = Documents(calls=1, annual=False, shifted=False)
        def change(payload, inputs):
            if payload["guidance"]:
                bad = json.loads(json.dumps(payload["guidance"][0]))
                del bad["target_period"]
                payload["guidance"].append(bad)
        report = self.engine(documents, MutatedModels(documents, change)).generate_report("TESTCO")
        self.assertEqual(report.narrative_vs_numbers[0].verdict, "BEAT")
        self.assertTrue(any(i.code == "ValidationError" and "target_period: missing" in i.detail for i in report.issues))

    def test_bad_actual_does_not_discard_valid_annual_business_quotes(self):
        documents = Documents(calls=0)
        def change(payload, inputs):
            for item in payload["actuals"]:
                item["number_token"] = "999"
        report = self.engine(documents, MutatedModels(documents, change)).generate_report("TESTCO")
        self.assertIsNotNone(report.business_snapshot)
        self.assertTrue(any("Number token is absent" in i.detail for i in report.issues))
        self.assertIsNone(report.cash_quality.history[-1].free_cash_flow_inr_crore)

    def test_partial_success_and_its_diagnostics_are_cached_without_new_calls(self):
        documents = Documents(calls=1, shifted=False)
        def change(payload, inputs):
            if payload["business"]:
                payload["business"].append({"kind": "overview", "evidence": {"source_ref": "unknown.block", "quote": "Not a source."}})
        engine = self.engine(documents, MutatedModels(documents, change))
        first = engine.generate_report("TESTCO")
        second = engine.generate_report("TESTCO")
        self.assertIsNotNone(second.business_snapshot)
        self.assertTrue(any("business[4]" in i.detail for i in first.issues))
        self.assertTrue(any("business[4]" in i.detail for i in second.issues))
        self.assertEqual(second.telemetry.tier1_calls + second.telemetry.tier2_calls, 0)

    def test_segment_guidance_cannot_score_against_group_numbers(self):
        documents = Documents(calls=1, annual=False, shifted=False)
        def change(payload, inputs):
            for item in payload["guidance"]:
                item["entity_scope"] = "segment"
        report = self.engine(documents, MutatedModels(documents, change)).generate_report("TESTCO")
        first = report.narrative_vs_numbers[0]
        self.assertIsNone(first.verdict)
        self.assertTrue(first.comparisons)
        self.assertIn("entity scope", first.comparisons[0].not_assessable_reason)

    def test_unknown_scope_cannot_be_assumed_to_be_group(self):
        documents = Documents(calls=1, annual=False, shifted=False)
        def change(payload, inputs):
            for item in payload["guidance"]:
                item.pop("entity_scope", None)
        report = self.engine(documents, MutatedModels(documents, change)).generate_report("TESTCO")
        self.assertIsNone(report.narrative_vs_numbers[0].verdict)

    def test_segment_cash_capex_cannot_supply_group_fcf(self):
        documents = Documents(calls=0)
        def change(payload, inputs):
            for item in payload["actuals"]:
                item["entity_scope"] = "segment"
        report = self.engine(documents, MutatedModels(documents, change)).generate_report("TESTCO")
        self.assertIsNotNone(report.business_snapshot)
        self.assertIsNone(report.cash_quality.history[-1].free_cash_flow_inr_crore)

    def test_timestamped_speaker_heading_accepts_only_active_manager(self):
        documents = Documents(calls=1, annual=False)
        engine = self.engine(documents)
        corpus = engine._load("TESTCO", NOW.date(), False)
        info = corpus.coverage[0]
        template = corpus.blocks[-1]
        heading = "Sh Anita Rao 00:00:01 – 00:09:45 (Group Performance)"
        quote = documents.promises["call.0"][0]
        block = updated(template, text=heading + "\n" + quote)
        engine._speaker("Anita Rao", _Quote(source_ref=block.ref, quote=heading),
                        _Quote(source_ref=block.ref, quote=quote), (block,), info)
        analyst = updated(block, text=heading + "\nAnalyst Person:\n" + quote)
        with self.assertRaises(GroundingError):
            engine._speaker("Anita Rao", _Quote(source_ref=block.ref, quote=heading),
                            _Quote(source_ref=block.ref, quote=quote), (analyst,), info)

    def test_continued_page_has_cited_prior_turn_and_new_analyst_resets_it(self):
        documents = Documents(calls=1, annual=False)
        extraction = documents.outputs["call.0"]
        header, _ = documents.texts["call.0"]
        quote = documents.promises["call.0"][0]
        heading = "Anita Rao 00:00:01 – 00:09:45 (Group Performance)"
        pages = (PdfPageText(page=1, text=header), PdfPageText(page=2, text=heading + "\nOpening remarks."),
                 PdfPageText(page=3, text=quote + "\nAnalyst Person:\nQuestion."))
        documents.outputs["call.0"] = updated(extraction, pages=pages, total_pages=3)
        engine = self.engine(documents)
        corpus = engine._load("TESTCO", NOW.date(), False)
        first, continued = corpus.blocks[-2:]
        self.assertEqual(continued.speaker_source_ref, first.ref)
        self.assertEqual(continued.speaker_at_start, "Anita Rao")
        proof = _Quote(source_ref=first.ref, quote=heading)
        engine._speaker("Anita Rao", proof, _Quote(source_ref=continued.ref, quote=quote), corpus.blocks, corpus.coverage[0])
        with self.assertRaises(GroundingError):
            engine._speaker("Anita Rao", proof, _Quote(source_ref=continued.ref, quote="Question."), corpus.blocks, corpus.coverage[0])

    def test_bad_metadata_field_does_not_erase_other_verified_fields(self):
        documents = Documents(calls=1, annual=False)
        class PartialTiming(Models):
            def complete(self, route, system, inputs, output):
                if output.__name__ == "_Timing":
                    ref = inputs["sources"][0]["source_ref"]
                    return LLMCompletion(finish_reason="stop", text=json.dumps({"basis": "consolidated",
                        "basis_evidence": {"source_ref": ref, "quote": "This quote is not present"}}))
                return super().complete(route, system, inputs, output)
        timing, engine = self.timing("Q4 2025-2026\nMedia & Analyst Call Transcript\n24 April 2026\n",
                                     model=PartialTiming(documents))
        self.assertEqual(timing.call_on, date(2026, 4, 24))
        self.assertEqual(timing.reported_quarter_end, date(2026, 3, 31))
        self.assertIsNone(timing.basis)
        self.assertTrue(any(i.stage == "document_timing.basis" and "Quote does not match" in i.detail for i in engine._issues))


if __name__ == "__main__":
    unittest.main()
