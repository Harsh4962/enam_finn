"""Cross-module regression tests and real SDK-to-HTTP serialization checks."""
from __future__ import annotations

import json
import unittest
from datetime import date
from unittest.mock import patch

from bs4 import BeautifulSoup
from google.ai import generativelanguage as api
from pydantic import ValidationError
from requests import Response

from backend.cache import CacheKey, SQLiteCache
from backend.config import DEFAULT_PORTFOLIO, LLMRouterConfig, Settings, normalize_ticker
from backend.data.announcements import classify_announcement, load_seed_announcements
from backend.data.fundamentals import FundamentalsClient, parse_documents
from backend.engines.pull_engine import PullEngine, _Answer, _AuditReview, _Cases, _MapExtraction, _Timing
from backend.engines.push_engine import (
    GeminiLLMClient,
    LLMCompletion,
    LLMUsage,
    _BioResponse,
    _Classification,
    _ExecutiveExtraction,
    _provider_schema,
    _ResultsTakeaway,
    _Takeaway,
    announced_quarter_end,
)
from backend.validate import audit_fundamentals, validate_backend
from tests.test_data_plumbing import FakeResponse, FakeSession
from tests.test_fundamentals import company_html, parse, table
from tests.test_pull_engine import NOW, RISK1, SETTINGS, Documents, Models


class RequestTests(unittest.TestCase):
    def test_every_production_llm_schema_compiles_to_remote_schema(self):
        for model in (_Takeaway, _Classification, _ExecutiveExtraction, _BioResponse, _ResultsTakeaway,
                      _MapExtraction, _Timing, _AuditReview, _Cases, _Answer):
            with self.subTest(model=model.__name__):
                compiled = _provider_schema(model, api)
                self.assertEqual(compiled.type_, api.Type.OBJECT)
                local_fields = getattr(model, "provider_schema_local_fields", frozenset())
                self.assertEqual(set(compiled.properties), set(model.model_fields) - local_fields)

    def test_nullable_nested_schema_and_canonical_enums_are_sent(self):
        takeaway = _provider_schema(_Takeaway, api)
        self.assertTrue(takeaway.properties["text"].nullable)
        self.assertTrue(takeaway.properties["evidence"].nullable)
        self.assertIn("source_ref", takeaway.properties["evidence"].properties)
        category = _provider_schema(_Classification, api).properties["category"]
        self.assertIn("M&A", category.enum)
        self.assertEqual(len(category.enum), 17)

    def test_actual_rest_transport_serializes_both_model_requests(self):
        routes = LLMRouterConfig()
        for route in (routes.tier1, routes.tier2):
            with self.subTest(model=route.model):
                sdk = api.GenerativeServiceClient(transport="rest", client_options={
                    "api_key": "fixture-not-a-real-key", "api_endpoint": "generativelanguage.googleapis.com"})
                transport = GeminiLLMClient(api_key="fixture-not-a-real-key", free_tier_project_confirmed=True, sdk_client=sdk)
                captured = []
                def request(session, method, url, **kwargs):
                    captured.append((method, url, json.loads(kwargs["data"])))
                    response = Response()
                    response.status_code = 200
                    response.headers["Content-Type"] = "application/json"
                    response._content = json.dumps({"candidates": [{"content": {"role": "model", "parts": [
                        {"text": '{"text":null,"evidence":null}'}]}, "finishReason": "STOP"}],
                        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 8, "totalTokenCount": 18}}).encode()
                    return response
                try:
                    with patch("requests.sessions.Session.request", request):
                        result = transport.complete(route, "Only supplied disclosures.", {}, _Takeaway)
                    self.assertEqual(result.finish_reason, "stop")
                    self.assertEqual(result.usage.output_tokens, 8)
                    self.assertEqual(len(captured), 1)
                    method, url, body = captured[0]
                    self.assertEqual(method, "POST")
                    self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/models/" + route.model + ":generateContent")
                    config = body["generationConfig"]
                    self.assertEqual(set(config), {"maxOutputTokens", "responseMimeType", "responseSchema"})
                    self.assertNotIn("temperature", config)
                    self.assertNotIn("candidateCount", config)
                    self.assertIn("properties", config["responseSchema"])
                    self.assertNotIn("fixture-not-a-real-key", json.dumps(body))
                finally:
                    sdk.transport.close()


class ParsingAuditTests(unittest.TestCase):
    def test_empty_quarters_falls_back_even_with_old_consolidated_annuals(self):
        soup = BeautifulSoup(company_html(), "html.parser")
        soup.find(id="quarters").replace_with(BeautifulSoup('<section id="quarters"><h2>Quarterly Results</h2></section>', "html.parser"))
        standalone = company_html(basis="Standalone")
        session = FakeSession([FakeResponse(body=str(soup).encode()), FakeResponse(body=standalone.encode())])
        with SQLiteCache(":memory:") as cache, FundamentalsClient(cache, session=session, clock=lambda: NOW,
                url_validator=lambda url: url) as client:
            company = client.fetch("TESTCO")
        self.assertEqual(company.basis, "standalone")
        self.assertIsNotNone(company.latest_quarter_headlines)
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(any("Standalone fallback" in note for note in company.notes))

    def test_unavailable_standalone_preserves_available_consolidated_annuals(self):
        soup = BeautifulSoup(company_html(), "html.parser")
        soup.find(id="quarters").decompose()
        session = FakeSession([FakeResponse(body=str(soup).encode()), FakeResponse(status=403)])
        with SQLiteCache(":memory:") as cache, FundamentalsClient(cache, session=session, clock=lambda: NOW,
                url_validator=lambda url: url) as client:
            company = client.fetch("TESTCO")
        self.assertEqual(company.basis, "consolidated")
        self.assertIsNone(company.latest_quarter_headlines)
        self.assertTrue(company.profit_loss.rows)
        self.assertTrue(any("Consolidated snapshot retained" in note for note in company.notes))

    def test_quarter_date_does_not_capture_following_board_date(self):
        self.assertEqual(announced_quarter_end("Results for the quarter ended June 30, 2026 approved on July 17, 2026."), date(2026, 6, 30))

    def test_quarter_date_does_not_capture_following_record_date(self):
        self.assertEqual(announced_quarter_end("Quarter and year ended 31.03.2026; dividend record date 15.06.2026."), date(2026, 3, 31))

    def test_filing_date_without_reporting_phrase_remains_unknown(self):
        self.assertIsNone(announced_quarter_end("Board meeting date July 17, 2026."))

    def test_current_portfolio_and_all_adapters_resolve_verified_rename(self):
        self.assertEqual(normalize_ticker("tatamotors.ns"), "TMPV")
        self.assertEqual(CacheKey.for_content("TATAMOTORS", "test", {}).ticker, "TMPV")
        self.assertIn("TMPV", DEFAULT_PORTFOLIO)
        self.assertEqual(len(DEFAULT_PORTFOLIO), 16)
        self.assertEqual(Settings(portfolio=("TATAMOTORS",)).portfolio, ("TMPV",))
        self.assertEqual(normalize_ticker("TMCV"), "TMCV")

    def test_alias_and_current_symbol_are_duplicate_configuration(self):
        with self.assertRaises(ValidationError):
            Settings(portfolio=("TATAMOTORS", "TMPV"))

    def test_seed_loader_keeps_all_tickers_canonical(self):
        self.assertEqual({row["symbol"] for row in load_seed_announcements(as_of=NOW)}, set(DEFAULT_PORTFOLIO))

    def test_pit_window_notice_is_routine_without_a_share_transaction(self):
        result = classify_announcement("Closure of Trading Window", "Pursuant to SEBI Prohibition of Insider Trading Regulations, trading window closure applies.")
        self.assertEqual(result.category.value, "unclassified")
        self.assertTrue(result.routine_confirmed)
        self.assertFalse(result.requires_llm)

    def test_actual_insider_transaction_stays_visible(self):
        result = classify_announcement("Insider trading - promoter shares", "A promoter purchased 50,000 equity shares on the exchange.")
        self.assertEqual(result.category.value, "insider_trading")
        self.assertFalse(result.routine_confirmed)

    def test_lodr_shareholding_pattern_is_not_a_pledge(self):
        result = classify_announcement("Shareholding Pattern", "Report under Regulation 31(1)(b) of SEBI LODR Regulations.")
        self.assertEqual(result.category.value, "unclassified")
        self.assertTrue(result.routine_confirmed)

    def test_explicit_pledge_stays_material(self):
        result = classify_announcement("Disclosure under SAST Regulation 31(1)", "Creation of promoter pledge over equity shares.")
        self.assertEqual(result.category.value, "pledging")
        self.assertFalse(result.routine_confirmed)

    def test_latest_full_fiscal_year_outranks_historical_majority(self):
        html = company_html()
        labels = ["Dec " + str(year) for year in range(2017, 2024)] + ["Mar 2025", "Mar 2026"]
        soup = BeautifulSoup(html, "html.parser")
        replacement = table("profit-loss", labels, [("Sales", [1000] * len(labels)),
            ("Operating Profit", [200] * len(labels)), ("Net Profit", [100] * len(labels))])
        soup.find(id="profit-loss").replace_with(BeautifulSoup(replacement, "html.parser"))
        company = parse(str(soup))
        self.assertEqual(PullEngine._fiscal_month(company), 3)
        self.assertEqual(company.cash_conversion[-1].period.end_on, date(2026, 3, 31))

    def test_same_quarter_calls_are_independent_not_assumed_mirrors(self):
        html = '<section id="documents"><div class="documents concalls"><h3>Concalls</h3><ul>'
        for month, identity in (("Oct 2026", 1), ("Jul 2026", 2), ("Aug 2026", 3), ("Apr 2026", 4), ("Jan 2026", 5)):
            html += f'<li><span class="nowrap">{month}</span><a href="https://issuer.example/transcript.pdf?id={identity}">Transcript</a></li>'
        html += '</ul></div></section>'
        docs = parse_documents(BeautifulSoup(html, "html.parser"), "TESTCO", base_url="https://issuer.example", limit=6)
        self.assertEqual(len(docs.concall_transcripts), 5)
        self.assertTrue(all(not item.alternate_urls for item in docs.concall_transcripts))
        self.assertEqual(len({str(item.url) for item in docs.concall_transcripts}), 5)

    def test_independent_math_checks_match_complete_synthetic_financials(self):
        observations = audit_fundamentals(parse())
        self.assertEqual(observations["cash_years"], 3)
        self.assertEqual(observations["basis"], "consolidated")


class ValidationCommandTests(unittest.TestCase):
    def test_no_llm_requested_never_calls_transport(self):
        documents = Documents()
        class Forbidden:
            def available(self, route):
                raise AssertionError("Not requested")
            def complete(self, *args):
                raise AssertionError("Not requested")
        result = validate_backend(("TESTCO",), settings=SETTINGS, market=False, announcements=False,
            documents=False, fundamentals_factory=lambda cache: documents, llm_transport=Forbidden(), clock=lambda: NOW)
        self.assertEqual(result.telemetry.tier1_calls + result.telemetry.tier2_calls, 0)
        self.assertEqual([c.status for c in result.checks if c.component.startswith("gemini_")], ["unverified", "unverified"])


class GroundingAuditTests(unittest.TestCase):
    def test_segment_revision_cannot_penalize_company_guidance(self):
        documents = Documents()
        class SegmentRevision(Models):
            def complete(self, route, system, inputs, output):
                response = super().complete(route, system, inputs, output)
                if output.__name__ == "_MapExtraction":
                    payload = json.loads(response.text)
                    for item in payload["revisions"]:
                        item["entity_scope"] = "segment"
                    return LLMCompletion(text=json.dumps(payload), finish_reason="stop", usage=response.usage)
                return response
        with SQLiteCache(":memory:") as cache, PullEngine(cache, settings=SETTINGS, clock=lambda: NOW,
                fundamentals_factory=lambda c: documents, llm_transport=SegmentRevision(documents)) as engine:
            report = engine.generate_report("TESTCO")
        self.assertFalse(any(q.goalpost_shifts for q in report.narrative_vs_numbers))
        self.assertEqual(report.management_execution_score.score_out_of_100, 50.0)

    def test_factual_qa_cannot_smuggle_unrelated_nonnumeric_paraphrase(self):
        documents = Documents()
        class InventedParaphrase(Models):
            def complete(self, route, system, inputs, output):
                response = super().complete(route, system, inputs, output)
                if output.__name__ == "_Answer":
                    payload = json.loads(response.text)
                    payload["claims"][0]["text"] = "The company manufactures semiconductors."
                    return LLMCompletion(text=json.dumps(payload), finish_reason="stop", usage=response.usage)
                return response
        with SQLiteCache(":memory:") as cache, PullEngine(cache, settings=SETTINGS, clock=lambda: NOW,
                fundamentals_factory=lambda c: documents, llm_transport=InventedParaphrase(documents, narrative_qa=True)) as engine:
            engine.generate_report("TESTCO")
            answer = engine.ask_primary_filings("TESTCO", "What is disclosed about raw-material costs?")
        self.assertIn(RISK1, answer.answer)
        self.assertNotIn("semiconductors", answer.answer)

    def test_fact_period_year_cannot_be_used_as_a_financial_amount(self):
        from backend.engines.pull_engine import GroundingError, _Claim, _Quote
        documents = Documents()
        with SQLiteCache(":memory:") as cache, PullEngine(cache, settings=SETTINGS, clock=lambda: NOW,
                fundamentals_factory=lambda c: documents, llm_transport=Models(documents)) as engine:
            report = engine.generate_report("TESTCO")
            corpus = engine._load("TESTCO", NOW.date(), False)
            block = next(b for b in corpus.blocks if RISK1 in b.text)
            fact = next(f for f in report.quantitative_facts if f.fact.metric == "revenue")
            claim = _Claim(text="Revenue is 2026 crore.", is_inference=True,
                evidence=(_Quote(source_ref=block.ref, quote=RISK1),), fact_ids=(fact.fact_id,))
            with self.assertRaises(GroundingError):
                engine._verify_claim(claim, corpus, {fact.fact_id: fact}, primary_required=True)
    def test_two_live_canaries_require_real_schema_and_exact_grounding(self):
        documents = Documents()
        class Canary:
            def __init__(self):self.calls = []
            def available(self, route):return True
            def complete(self, route, system, inputs, model):
                self.calls.append(route.model)
                return LLMCompletion(text=json.dumps(inputs), finish_reason="stop", usage=LLMUsage(input_tokens=10, output_tokens=8))
        canary = Canary()
        result = validate_backend(("TESTCO",), settings=SETTINGS, live_llm=True, market=False, announcements=False,
            documents=False, fundamentals_factory=lambda cache: documents, llm_transport=canary, clock=lambda: NOW)
        self.assertEqual(len(canary.calls), 2)
        self.assertEqual(result.telemetry.tier1_calls, 1)
        self.assertEqual(result.telemetry.tier2_calls, 1)
        self.assertTrue(all(c.status == "passed" for c in result.checks if c.component.startswith("gemini_")))

    def test_hallucinated_canary_is_failed_and_credential_is_never_echoed(self):
        documents = Documents()
        class Wrong:
            def available(self, route):return True
            def complete(self, route, system, inputs, model):
                return LLMCompletion(text=json.dumps({**inputs, "value": 999}), finish_reason="stop")
        result = validate_backend(("TESTCO",), settings=SETTINGS, live_llm=True, market=False, announcements=False,
            documents=False, fundamentals_factory=lambda cache: documents, llm_transport=Wrong(), clock=lambda: NOW)
        self.assertEqual([c.status for c in result.checks if c.component.startswith("gemini_")], ["failed", "failed"])
        self.assertFalse(result.telemetry.cost_estimate_complete)

    def test_missing_confirmation_never_calls_transport(self):
        documents = Documents()
        class Forbidden:
            def available(self, route):raise AssertionError("Not confirmed")
        result = validate_backend(("TESTCO",), settings=Settings(), live_llm=True, market=False, announcements=False,
            documents=False, fundamentals_factory=lambda cache: documents, llm_transport=Forbidden(), clock=lambda: NOW)
        self.assertEqual(result.telemetry.tier1_calls + result.telemetry.tier2_calls, 0)


if __name__ == "__main__":
    unittest.main()
