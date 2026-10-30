"""Model-scoped quota and literal, unscored primary-commentary regressions.

All text and HTTP responses in these tests are synthetic.
"""
from __future__ import annotations

import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import requests
from google.ai import generativelanguage as api
from google.api_core.exceptions import ResourceExhausted

from backend.cache import SQLiteCache
from backend.config import LLMRouterConfig, Task
from backend.data.fundamentals import DocumentLink
from backend.engines.pull_engine import (
    DocumentCoverage,
    GroundingError,
    PrimaryBlock,
    PullEngine,
    _BusinessDraft,
    _company_overview,
    _MapExtraction,
    _Models,
    _quarter,
    _Quote,
)
from backend.engines.push_engine import GeminiLLMClient, LLMCompletion, LLMHTTPError, LLMUsage, _quota_info, _Takeaway
from backend.schemas import Citation
from tests.test_gemini_free import FixtureSDK, sdk_response
from tests.test_pull_engine import NOW, SETTINGS, Documents, Models, updated


def quota_details(model=None, *, project=False):
    violation = {"quotaId": "GenerateRequestsPerMinutePerProject" if project else "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                 "quotaDimensions": {"model": model} if model else {}}
    return [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [violation]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "60s"}]


class QuotaScopeTests(unittest.TestCase):
    def test_model_quota_keeps_independent_tier2_available(self):
        routes = LLMRouterConfig()
        sdk = FixtureSDK([ResourceExhausted("Synthetic quota", details=quota_details(routes.tier1.model)), sdk_response()])
        client = GeminiLLMClient(api_key="fixture-key", free_tier_project_confirmed=True, sdk_client=sdk)
        with self.assertRaises(LLMHTTPError) as caught:
            client.complete(routes.tier1, "Synthetic", {}, _Takeaway)
        self.assertEqual(caught.exception.quota_scope, "model")
        self.assertEqual(caught.exception.retry_after, 60.0)
        self.assertFalse(client.available(routes.tier1))
        self.assertTrue(client.available(routes.tier2))
        self.assertEqual(client.complete(routes.tier2, "Synthetic", {}, _Takeaway).finish_reason, "stop")
        self.assertEqual(len(sdk.calls), 2)

    def test_project_quota_blocks_both_models(self):
        routes = LLMRouterConfig()
        sdk = FixtureSDK([ResourceExhausted("Synthetic project quota", details=quota_details(project=True))])
        client = GeminiLLMClient(api_key="fixture-key", free_tier_project_confirmed=True, sdk_client=sdk)
        with self.assertRaises(LLMHTTPError) as caught:
            client.complete(routes.tier1, "Synthetic", {}, _Takeaway)
        self.assertEqual(caught.exception.quota_scope, "project")
        self.assertFalse(client.available(routes.tier2))
        self.assertIn("project", client.unavailable_reason(routes.tier2))

    def test_unknown_quota_scope_remains_conservative(self):
        routes = LLMRouterConfig()
        sdk = FixtureSDK([ResourceExhausted("Synthetic unknown quota")])
        client = GeminiLLMClient(api_key="fixture-key", free_tier_project_confirmed=True, sdk_client=sdk)
        with self.assertRaises(LLMHTTPError) as caught:
            client.complete(routes.tier1, "Synthetic", {}, _Takeaway)
        self.assertEqual(caught.exception.quota_scope, "unknown")
        self.assertFalse(client.available(routes.tier2))
        self.assertEqual(len(sdk.calls), 1)

    def test_mixed_model_and_project_quotas_are_project_scoped(self):
        model = LLMRouterConfig().tier1.model
        details = quota_details(model) + quota_details(project=True)
        self.assertEqual(_quota_info(ResourceExhausted("Synthetic", details=details), model), ("project", 60.0))

    def test_unreadable_quota_details_fail_closed_without_masking_error(self):
        error = ResourceExhausted("Synthetic", details=[object(), {"violations": 17}])
        self.assertEqual(_quota_info(error, LLMRouterConfig().tier1.model), ("unknown", 0.0))

    def test_model_cooldown_expires_without_retrying_429(self):
        routes = LLMRouterConfig()
        sdk = FixtureSDK([ResourceExhausted("Synthetic", details=quota_details(routes.tier1.model))])
        client = GeminiLLMClient(api_key="fixture-key", free_tier_project_confirmed=True, sdk_client=sdk)
        with patch("backend.engines.push_engine.time.monotonic", return_value=10.0):
            with self.assertRaises(LLMHTTPError):
                client.complete(routes.tier1, "Synthetic", {}, _Takeaway)
        with patch("backend.engines.push_engine.time.monotonic", return_value=71.0):
            self.assertTrue(client.available(routes.tier1))
        self.assertEqual(len(sdk.calls), 1)

    def test_actual_rest_error_retains_scope_and_permits_other_model(self):
        routes = LLMRouterConfig()
        sdk = api.GenerativeServiceClient(transport="rest", client_options={
            "api_key": "fixture-key", "api_endpoint": "generativelanguage.googleapis.com"})
        client = GeminiLLMClient(api_key="fixture-key", free_tier_project_confirmed=True, sdk_client=sdk)
        requests_made = []
        def request(session, method, url, **kwargs):
            requests_made.append(url)
            response = requests.Response()
            response.request = requests.Request(method, url).prepare()
            response.headers["Content-Type"] = "application/json"
            if "flash-lite:" in url:
                response.status_code = 429
                payload = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Synthetic quota",
                    "details": quota_details(routes.tier1.model)}}
            else:
                response.status_code = 200
                payload = {"candidates": [{"content": {"role": "model", "parts": [{"text": '{"text":null,"evidence":null}'}]},
                    "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 8, "totalTokenCount": 18}}
            response._content = json.dumps(payload).encode()
            return response
        try:
            with patch("requests.sessions.Session.request", request):
                with self.assertRaises(LLMHTTPError) as caught:
                    client.complete(routes.tier1, "Synthetic", {}, _Takeaway)
                self.assertEqual(caught.exception.quota_scope, "model")
                self.assertTrue(client.available(routes.tier2))
                self.assertEqual(client.complete(routes.tier2, "Synthetic", {}, _Takeaway).finish_reason, "stop")
            self.assertEqual(len(requests_made), 2)
        finally:
            sdk.transport.close()


def commentary_corpus(texts, *, stale_context=False):
    document_id, source_id = "call.synthetic", "source.synthetic"
    blocks = tuple(PrimaryBlock(ref="block." + str(i), text=text,
        citation=Citation(source_id=source_id, page=i + 1, chunk_id="block." + str(i)),
        tag="Q1FY27 Concall", kind="concall_transcript", document_id=document_id,
        speaker_at_start="Anita Rao" if stale_context else None,
        speaker_source_ref="block.0" if stale_context else None,
        speaker_quote="Anita Rao:" if stale_context else None) for i, text in enumerate(texts))
    info = DocumentCoverage(document_id=document_id, kind="concall_transcript", status="ready", source_id=source_id,
        call_on=date(2026, 7, 17), reporting_quarter_end=date(2026, 6, 30), management_names=("Anita Rao",), note="Synthetic")
    return SimpleNamespace(blocks=blocks, coverage=(info,))


class LiteralCommentaryTests(unittest.TestCase):
    def test_named_manager_quotes_are_preserved_and_questioner_is_excluded(self):
        corpus = commentary_corpus(["Anita Rao:\nWe will expand capacity during the coming year.\n"
            "Questioner (Analyst Person, Synthetic Research)\nWe expect revenue to double next quarter.\n"
            "Anita Rao:\nWe plan to grow production over the next two years."])
        result = PullEngine._management_commentary(corpus)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(item.speaker == "Anita Rao" for item in result))
        self.assertTrue(all(item.assessment == "unscored_commentary" for item in result))
        self.assertFalse(any("double" in item.statement.text for item in result))

    def test_stale_cached_manager_does_not_override_questioner_boundary(self):
        corpus = commentary_corpus(["Anita Rao:\nHistorical results.\nQuestioner (Analyst Person, Synthetic Research)",
            "We expect revenue growth next quarter."], stale_context=True)
        self.assertEqual(PullEngine._management_commentary(corpus), ())
        with self.assertRaises(GroundingError):
            PullEngine._speaker("Anita Rao", _Quote(source_ref="block.0", quote="Anita Rao:"),
                _Quote(source_ref="block.1", quote="We expect revenue growth next quarter."), corpus.blocks, corpus.coverage[0])

    def test_wrapped_questioner_heading_clears_management_context(self):
        corpus = commentary_corpus(["Anita Rao:\nHistorical results.\nQuestioner (Analyst Person,\n"
            "Synthetic Research)\nWe expect revenue growth next quarter."])
        self.assertEqual(PullEngine._management_commentary(corpus), ())

    def test_cross_page_manager_quote_cites_body_and_speaker_anchor(self):
        corpus = commentary_corpus(["Anita Rao:\nOpening remarks.", "We plan to expand manufacturing capacity next year."])
        result = PullEngine._management_commentary(corpus)
        self.assertEqual(len(result), 1)
        self.assertEqual([citation.page for citation in result[0].statement.citations], [2, 1])

    def test_presentation_agenda_is_not_an_operating_commitment(self):
        corpus = commentary_corpus(["Anita Rao:\nWe will discuss revenue growth and margin performance."])
        self.assertEqual(PullEngine._management_commentary(corpus), ())

    def test_overlapping_prospective_fragments_are_not_duplicate_cards(self):
        text = "Anita Rao:\nWe will grow revenue " + "through production expansion " * 10 + "and we plan to expand capacity further."
        result = PullEngine._management_commentary(commentary_corpus([text]))
        self.assertEqual(len(result), 1)

    def test_unscored_commentary_never_creates_execution_verdict_without_llm(self):
        documents = Documents()
        with SQLiteCache(":memory:") as cache, PullEngine(cache, settings=SETTINGS, clock=lambda: NOW,
                fundamentals_factory=lambda c: documents, llm_transport=Models(documents), llm_enabled=False) as engine:
            report = engine.generate_report("TESTCO")
        self.assertTrue(report.management_commentary)
        self.assertTrue(any(quarter.qualitative_commentary for quarter in report.narrative_vs_numbers))
        self.assertTrue(all(quarter.verdict is None and not quarter.promise_scores for quarter in report.narrative_vs_numbers))
        self.assertIsNone(report.management_execution_score.score_out_of_100)
        self.assertEqual(report.telemetry.tier1_calls + report.telemetry.tier2_calls, 0)

    def test_call_before_required_audit_window_is_not_sent_for_model_mapping(self):
        documents = Documents()
        old = DocumentLink(document_id="call.old", ticker="TESTCO", kind="concall_transcript",
            url="https://issuer.example/old-call.pdf", listed_label="Old call", listed_month=date(2025, 7, 1))
        header = ("Test Company Limited\nQ1 FY26 Earnings Conference Call\nCall date: 2025-07-20\n"
            "Reported quarter ended: 2025-06-30\nConsolidated financial discussion.\nAnita Rao - Chief Financial Officer\n")
        promise = "We expect revenue growth of 10% to 20% YoY in Q2 FY26."
        documents.outputs[old.document_id] = documents.extraction(old, header, "Anita Rao:\n" + promise)
        documents.promises[old.document_id] = (promise, "revenue_growth_yoy", _quarter(date(2025, 9, 30)))
        documents.company = updated(documents.company, documents=updated(documents.company.documents,
            concall_transcripts=documents.company.documents.concall_transcripts + (old,)))
        transport = Models(documents)
        with SQLiteCache(":memory:") as cache, PullEngine(cache, settings=SETTINGS, clock=lambda: NOW,
                fundamentals_factory=lambda c: documents, llm_transport=transport) as engine:
            report = engine.generate_report("TESTCO")
        self.assertFalse(any(name == "_MapExtraction" and inputs["metadata"]["document_id"] == old.document_id
            for _, inputs, name in transport.calls))
        coverage = next(item for item in report.document_coverage if item.document_id == old.document_id)
        self.assertIn("outside the four-quarter audit", coverage.note)
        self.assertEqual(coverage.mapped_blocks, 0)
        self.assertEqual(report.management_execution_score.assessed_quarters, 4)


class BusinessOverviewTests(unittest.TestCase):
    def test_product_kpi_fragment_is_not_company_overview(self):
        self.assertFalse(_company_overview("ARPU for the mobile offer increased during this synthetic quarter.", "Test Company Ltd"))
        self.assertTrue(_company_overview("The company manufactures industrial controls.", "Test Company Ltd"))

    def test_cited_activity_list_preserves_segments_without_promoting_kpi_fragment(self):
        arpu = "ARPU for the mobile offer increased during this synthetic quarter."
        segment = "The Retail business sells household products."
        corpus = commentary_corpus([arpu + "\n" + segment])
        corpus.company = SimpleNamespace(company_name="Test Company Ltd")
        drafts = (
            _BusinessDraft(kind="overview", evidence=_Quote(source_ref="block.0", quote=arpu)),
            _BusinessDraft(kind="segment", segment_name="Retail", evidence=_Quote(source_ref="block.0", quote=segment)),
            _BusinessDraft(kind="segment", segment_name="Retail business", evidence=_Quote(source_ref="block.0", quote=segment)),
        )
        panel = PullEngine._business(object.__new__(PullEngine), corpus, SimpleNamespace(business=drafts, coverage=corpus.coverage))
        self.assertIsNotNone(panel)
        self.assertNotIn("ARPU", panel.overview.text)
        self.assertEqual(len(panel.segments), 1)
        self.assertTrue(panel.overview.citations)
        self.assertTrue(any(gap.field == "company_wide_overview" and gap.reason == "not_available" for gap in panel.data_gaps))

    def test_new_evidence_validation_regenerates_only_rejected_cached_output(self):
        class SourceCopy:
            def __init__(self):
                self.calls = 0
            def available(self, route):
                return True
            def complete(self, route, system, inputs, model):
                self.calls += 1
                quote = "Old unsupported synthetic quote" if self.calls == 1 else inputs["source"]
                return LLMCompletion(text=json.dumps({"business": [{"kind": "overview",
                    "evidence": {"source_ref": "synthetic", "quote": quote}}]}), finish_reason="stop",
                    usage=LLMUsage(input_tokens=10, output_tokens=10))
        transport = SourceCopy()
        inputs = {"source": "The company manufactures synthetic controls."}
        def verify(result):
            if result.business[0].evidence.quote != inputs["source"]:
                raise GroundingError("Unsupported cached quote")
        with SQLiteCache(":memory:") as cache:
            runner = _Models(cache, transport, SETTINGS, enabled=True, max_tier1=4, max_tier2=2)
            runner.call("TESTCO", Task.PRIMARY_FACT_EXTRACTION, _MapExtraction,
                "Synthetic cache test", inputs, lambda result: None)
            result = runner.call("TESTCO", Task.PRIMARY_FACT_EXTRACTION, _MapExtraction,
                "Synthetic cache test", inputs, verify)
            self.assertEqual(result.business[0].evidence.quote, inputs["source"])
            runner.call("TESTCO", Task.PRIMARY_FACT_EXTRACTION, _MapExtraction,
                "Synthetic cache test", inputs, verify)
            self.assertEqual(transport.calls, 2)


if __name__ == "__main__":
    unittest.main()
