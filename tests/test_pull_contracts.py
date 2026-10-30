"""Production PULL schema, rejected-contract and unavailable-evidence regressions.

All company text and intercepted HTTP responses in this file are synthetic.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import requests
from google.ai import generativelanguage as api
from google.api_core.exceptions import InvalidArgument
from pydantic import ValidationError

from backend.cache import SQLiteCache
from backend.config import LLMRouterConfig, Settings
from backend.engines.pull_engine import PullEngine, _Cases, _MapExtraction, _Timing
from backend.engines.push_engine import (
    GeminiLLMClient,
    LLMCompletion,
    LLMHTTPError,
    LLMUsage,
    _provider_schema,
    describe_llm_error,
)
from backend.validate import validate_backend
from tests.test_gemini_free import FixtureSDK
from tests.test_pull_engine import NOW, SETTINGS, Documents, Models


def contract_payload(model, inputs):
    evidence = {"source_ref": inputs["source_ref"], "quote": inputs["exact_quote"]}
    if model is _MapExtraction:
        return {"guidance": [], "actuals": [], "revisions": [], "business": [
            {"kind": "overview", "evidence": evidence, "segment_name": None, "mix_token": None, "mix_period": None}]}
    if model is _Cases:
        claim = {"text": inputs["exact_quote"], "evidence": [evidence], "fact_ids": [], "is_inference": False}
        return {"bull": [claim], "bear": [claim]}
    raise AssertionError("Unexpected contract model")


class ContractTransport:
    def __init__(self, *, wrong_map=False, map_error=None):
        self.calls = []
        self.wrong_map = wrong_map
        self.map_error = map_error

    def available(self, route):
        return True

    def complete(self, route, system, inputs, model):
        self.calls.append((route, model))
        if model is _MapExtraction and self.map_error:
            raise self.map_error
        payload = contract_payload(model, inputs)
        if model is _MapExtraction and self.wrong_map:
            payload["business"][0]["evidence"]["quote"] = "Unrelated synthetic quote"
        return LLMCompletion(text=json.dumps(payload), finish_reason="stop", usage=LLMUsage(input_tokens=12, output_tokens=15))


class ProviderProjectionTests(unittest.TestCase):
    def test_map_remote_contract_has_fixed_keys_without_local_diagnostics(self):
        schema = _provider_schema(_MapExtraction, api)
        self.assertEqual(set(schema.properties), {"guidance", "business", "actuals", "revisions"})
        def check(node):
            if node.type_ == api.Type.OBJECT:
                self.assertEqual(set(node.required), set(node.properties))
                for child in node.properties.values():
                    check(child)
            if node.type_ == api.Type.ARRAY:
                self.assertEqual(node.max_items, 0)
                self.assertEqual(node.min_items, 0)
                check(node.items)
        check(schema)
        self.assertTrue(schema.properties["guidance"].items.properties["basis"].nullable)

    def test_local_array_limits_and_extra_field_rules_stay_strict(self):
        row = {"kind": "overview", "evidence": {"source_ref": "synthetic", "quote": "Synthetic test"}}
        with self.assertRaises(ValidationError):
            _MapExtraction.model_validate_json(json.dumps({"business": [row] * 9}))
        with self.assertRaises(ValidationError):
            _MapExtraction.model_validate_json(json.dumps({"business": [{**row, "unexpected": True}]}))

    def test_other_provider_contracts_keep_existing_optional_field_policy(self):
        timing = _provider_schema(_Timing, api)
        self.assertEqual(list(timing.required), [])
        self.assertEqual(timing.properties["management"].max_items, 12)

    def test_provider_schema_error_is_classified_without_echoing_text_or_key(self):
        sdk = FixtureSDK([InvalidArgument("GenerationConfig.response_schema: constraint has too many states. fixture-secret-key")])
        transport = GeminiLLMClient(api_key="fixture-secret-key", free_tier_project_confirmed=True, sdk_client=sdk)
        with self.assertRaises(LLMHTTPError) as caught:
            transport.complete(LLMRouterConfig().tier1, "Synthetic only", {}, _MapExtraction)
        self.assertEqual(caught.exception.provider_reason, "schema_complexity")
        message = describe_llm_error(caught.exception)
        self.assertIn("response schema", message)
        self.assertNotIn("fixture-secret-key", message)

    def test_unknown_provider_error_reason_cannot_enter_report(self):
        error = LLMHTTPError(400, provider_reason="raw fixture-secret-key")
        self.assertIsNone(error.provider_reason)
        self.assertNotIn("fixture-secret-key", describe_llm_error(error))


class PullFailureSemanticsTests(unittest.TestCase):
    def test_400_stops_duplicate_map_contract_calls_and_preserves_other_routes(self):
        documents = Documents()
        class RejectMap(Models):
            def complete(self, route, system, inputs, output):
                if output is _MapExtraction:
                    self.calls.append((route, inputs, output.__name__))
                    raise LLMHTTPError(400, provider_reason="schema_complexity")
                return super().complete(route, system, inputs, output)
        transport = RejectMap(documents)
        with SQLiteCache(":memory:") as cache, PullEngine(cache, settings=SETTINGS, clock=lambda: NOW,
                fundamentals_factory=lambda c: documents, llm_transport=transport) as engine:
            report = engine.generate_report("TESTCO")
            self.assertEqual(sum(name == "_MapExtraction" for _, _, name in transport.calls), 1)
            self.assertTrue(any(name == "_Cases" for _, _, name in transport.calls))
            self.assertEqual(sum(c.mapped_blocks for c in report.document_coverage), 0)
            self.assertEqual(sum(c.mapped_pages for c in report.document_coverage), 0)
            self.assertTrue(all(q.management_promise.startswith(("Unavailable:", "Unscored management commentary:")) for q in report.narrative_vs_numbers))
            self.assertTrue(all(any(g.field == "management_promise" and g.reason == ("not_comparable" if q.qualitative_commentary else "not_available") for g in q.data_gaps)
                for q in report.narrative_vs_numbers))
            self.assertEqual(report.forward_guidance.data_gaps[0].reason, "not_comparable" if report.management_commentary else "not_available")
            self.assertIsNone(report.management_execution_score.score_out_of_100)
            self.assertTrue(report.management_execution_score.summary.startswith("Not assessed:"))
            self.assertTrue(any("schema's complexity" in issue.detail for issue in report.issues))
            engine.generate_report("TESTCO")
            self.assertEqual(sum(name == "_MapExtraction" for _, _, name in transport.calls), 2)

    def test_reviewed_empty_calls_remain_distinct_from_failed_extraction(self):
        documents = Documents()
        class EmptyMap(Models):
            def complete(self, route, system, inputs, output):
                if output is _MapExtraction:
                    self.calls.append((route, inputs, output.__name__))
                    return LLMCompletion(text='{"guidance":[],"business":[],"actuals":[],"revisions":[]}',
                        finish_reason="stop", usage=LLMUsage(input_tokens=12, output_tokens=8))
                return super().complete(route, system, inputs, output)
        with SQLiteCache(":memory:") as cache, PullEngine(cache, settings=SETTINGS, clock=lambda: NOW,
                fundamentals_factory=lambda c: documents, llm_transport=EmptyMap(documents)) as engine:
            report = engine.generate_report("TESTCO")
        self.assertGreater(sum(c.mapped_blocks for c in report.document_coverage), 0)
        self.assertGreater(sum(c.mapped_pages for c in report.document_coverage), 0)
        self.assertTrue(all(q.management_promise.startswith("Unscored management commentary:") if q.qualitative_commentary
            else q.management_promise == "Not disclosed in primary filings" for q in report.narrative_vs_numbers))


class PullContractCanaryTests(unittest.TestCase):
    @staticmethod
    def run_contracts(transport):
        return validate_backend(("TESTCO",), settings=Settings(router=LLMRouterConfig(free_tier_project_confirmed=True)),
            live_llm=True, pull_contracts=True, market=False, announcements=False, documents=False,
            fundamentals_factory=lambda c: Documents(), llm_transport=transport, clock=lambda: NOW)

    def test_canaries_use_actual_mapping_and_cases_contracts(self):
        transport = ContractTransport()
        report = self.run_contracts(transport)
        self.assertEqual([model for _, model in transport.calls], [_MapExtraction, _Cases])
        checks = [item for item in report.checks if item.component.startswith("gemini_")]
        self.assertEqual([item.status for item in checks], ["passed", "passed"])
        self.assertEqual([item.observations["contract_model"] for item in checks], ["_MapExtraction", "_Cases"])
        self.assertEqual(report.telemetry.tier1_calls, 1)
        self.assertEqual(report.telemetry.tier2_calls, 1)

    def test_map_canary_rejects_unsupported_quote(self):
        report = self.run_contracts(ContractTransport(wrong_map=True))
        check = next(item for item in report.checks if item.component == "gemini_tier_1")
        self.assertEqual(check.status, "failed")
        self.assertIn("exact grounding", check.detail)

    def test_rejected_map_canary_does_not_block_independent_tier2_probe(self):
        report = self.run_contracts(ContractTransport(map_error=LLMHTTPError(400, provider_reason="response_schema")))
        checks = [item for item in report.checks if item.component.startswith("gemini_")]
        self.assertEqual([item.status for item in checks], ["failed", "passed"])
        self.assertIn("response schema", checks[0].detail)
        self.assertEqual(report.telemetry.tier1_calls, 1)

    def test_actual_http_payload_uses_projected_map_schema_and_unchanged_generation_fields(self):
        sdk = api.GenerativeServiceClient(transport="rest", client_options={
            "api_key": "fixture-key", "api_endpoint": "generativelanguage.googleapis.com"})
        transport = GeminiLLMClient(api_key="fixture-key", free_tier_project_confirmed=True, sdk_client=sdk)
        captured = []
        def request(session, method, url, **kwargs):
            body = json.loads(kwargs["data"])
            captured.append(body)
            inputs = json.loads(body["contents"][0]["parts"][0]["text"])["inputs"]
            model = _MapExtraction if "flash-lite:" in url else _Cases
            response = requests.Response()
            response.status_code = 200
            response.headers["Content-Type"] = "application/json"
            response._content = json.dumps({"candidates": [{"content": {"role": "model", "parts": [
                {"text": json.dumps(contract_payload(model, inputs))}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 15, "totalTokenCount": 27}}).encode()
            return response
        try:
            with patch("requests.sessions.Session.request", request):
                report = self.run_contracts(transport)
            self.assertTrue(all(item.status == "passed" for item in report.checks if item.component.startswith("gemini_")))
            self.assertEqual(len(captured), 2)
            for body in captured:
                self.assertEqual(set(body["generationConfig"]), {"maxOutputTokens", "responseMimeType", "responseSchema"})
            wire = captured[0]["generationConfig"]["responseSchema"]
            self.assertNotIn("rejections", wire["properties"])
            self.assertEqual(set(wire["required"]), set(wire["properties"]))
            self.assertNotIn("maxItems", json.dumps(wire))
        finally:
            sdk.transport.close()


if __name__ == "__main__":
    unittest.main()
