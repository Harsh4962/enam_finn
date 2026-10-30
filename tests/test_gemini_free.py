"""Real legacy SDK messages + fixture responses; no credentialed API requests."""

from __future__ import annotations

import json
import os
import unittest
import warnings
from datetime import date
from unittest.mock import patch

from pydantic import ValidationError

from backend.cache import SQLiteCache
from backend.config import (
    DEFAULT_TIER1_MODEL,
    DEFAULT_TIER2_MODEL,
    FREE_GEMINI_MODELS,
    LLMRouterConfig,
    ModelRoute,
    Settings,
    Task,
    TokenPricing,
)
from backend.engines.push_engine import (
    GeminiLLMClient,
    LLMCompletion,
    LLMHTTPError,
    ProviderUnavailable,
    _Takeaway,
)
from tests.test_push_engine import FakeLLM, engine, item, material_payload

with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    import google.generativeai as genai
from google.api_core.exceptions import InternalServerError, PermissionDenied, ResourceExhausted


def sdk_response(payload=None, *, reason=1, prompt=100, visible=20, total=150, cached=10,
                 candidates=True, usage=True):
    data = {"candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(payload or material_payload())}]},
                            "finish_reason": reason}] if candidates else []}
    if usage:
        data["usage_metadata"] = {"prompt_token_count": prompt, "candidates_token_count": visible,
                                  "total_token_count": total, "cached_content_token_count": cached}
    return genai.protos.GenerateContentResponse(**data)


class FixtureSDK:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if not self.outputs:
            raise AssertionError("unexpected SDK request")
        response = self.outputs.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(sdk, *, confirmed=True, api_key="fixture-key"):
    return GeminiLLMClient(api_key=api_key, free_tier_project_confirmed=confirmed, sdk_client=sdk)


def settings(*, confirmed=True, retries=0):
    return Settings(router=LLMRouterConfig(free_tier_project_confirmed=confirmed,
        tier1=ModelRoute(provider="google", model=DEFAULT_TIER1_MODEL, billing_tier="free", max_retries=retries)))


class ConfigurationTests(unittest.TestCase):
    def test_current_defaults_use_google_free_routes(self):
        router = LLMRouterConfig()
        self.assertEqual(router.tier1.model, "gemini-3.5-flash-lite")
        self.assertEqual(router.tier2.model, "gemini-3.8-flash")
        self.assertFalse(router.free_tier_project_confirmed)
        self.assertEqual(router.tier1.max_retries, 0)
        self.assertEqual(router.tier1.timeout_seconds, 45.0)
        self.assertEqual(router.tier2.max_retries, 3)
        self.assertEqual(router.tier2.timeout_seconds, 150.0)
        for route in (router.tier1, router.tier2):
            self.assertEqual(route.provider, "google")
            self.assertEqual(route.billing_tier, "free")
            self.assertEqual(route.pricing.input_per_million_usd, 0.0)
            self.assertEqual(route.pricing.output_per_million_usd, 0.0)
            self.assertEqual(route.pricing.cached_input_per_million_usd, 0.0)

    def test_retired_15_models_are_rejected(self):
        for model in ("gemini-1.5-flash", "gemini-1.5-pro", "gemini-1.5-flash-002"):
            with self.subTest(model=model), self.assertRaisesRegex(ValidationError, "shut down"):
                ModelRoute(provider="google", model=model)

    def test_paid_provider_cannot_be_configured_as_active_route(self):
        for provider, model in (("openai", "gpt-4o-mini"), ("anthropic", "claude-sonnet-4-5")):
            with self.subTest(provider=provider), self.assertRaisesRegex(ValidationError, "free-only"):
                LLMRouterConfig(tier1=ModelRoute(provider=provider, model=model))

    def test_current_gemini_paid_billing_route_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "free-only"):
            LLMRouterConfig(tier1=ModelRoute(provider="google", model=DEFAULT_TIER1_MODEL))

    def test_unknown_or_paid_only_models_cannot_be_marked_free(self):
        for model in ("gemini-4-argon", "gemini-custom", "gemini-2.5-pro"):
            with self.subTest(model=model), self.assertRaises(ValidationError):
                ModelRoute(provider="google", model=model, billing_tier="free")

    def test_nonzero_pricing_override_cannot_be_marked_free(self):
        price = TokenPricing(input_per_million_usd=1.0, output_per_million_usd=1.0,
                             checked_on=date(2026, 10, 29), source_url="https://ai.google.dev/gemini-api/docs/pricing")
        with self.assertRaises(ValidationError):
            ModelRoute(provider="google", model=DEFAULT_TIER1_MODEL, billing_tier="free", pricing_override=price)

    def test_confirmed_env_flag_does_not_serialize_api_key(self):
        result = Settings.from_env({"FINN_GEMINI_FREE_TIER_CONFIRMED": "1", "GEMINI_API_KEY": "fixture-secret"})
        self.assertTrue(result.router.free_tier_project_confirmed)
        self.assertNotIn("fixture-secret", result.model_dump_json())
        self.assertFalse(Settings.from_env({"FINN_GEMINI_FREE_TIER_CONFIRMED": "false"}).router.free_tier_project_confirmed)

    def test_confirmed_env_flag_is_strict(self):
        for value in ("yes", "paid", "", "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings.from_env({"FINN_GEMINI_FREE_TIER_CONFIRMED": value})

    def test_task_routing_remains_three_tiers(self):
        router = LLMRouterConfig()
        self.assertIsNone(router.route(Task.FINANCIAL_METRICS))
        self.assertEqual(router.route(Task.MANAGEMENT_BIO).model, DEFAULT_TIER1_MODEL)
        self.assertEqual(router.route(Task.NARRATIVE_AUDIT).model, DEFAULT_TIER2_MODEL)
        self.assertEqual(router.route(Task.BULL_BEAR).model, DEFAULT_TIER2_MODEL)


class SDKTests(unittest.TestCase):
    def test_request_uses_actual_sdk_protos_and_json_mime(self):
        sdk = FixtureSDK([sdk_response()])
        transport = client(sdk)
        result = transport.complete(LLMRouterConfig().tier1, "source-only", {"source": "disclosure"}, _Takeaway)
        request = sdk.calls[0]["request"]
        self.assertIsInstance(request, genai.protos.GenerateContentRequest)
        self.assertEqual(request.model, "models/" + DEFAULT_TIER1_MODEL)
        self.assertEqual(request.generation_config.response_mime_type, "application/json")
        self.assertEqual(request.generation_config.candidate_count, 0)
        self.assertEqual(request.generation_config.response_schema.type_, genai.protos.Type.OBJECT)
        self.assertFalse(request.tools)
        self.assertFalse(request.cached_content)
        self.assertIsNone(sdk.calls[0]["retry"])
        self.assertEqual(sdk.calls[0]["timeout"], 45.0)
        body = json.loads(request.contents[0].parts[0].text)
        self.assertFalse(body["output_json_schema"]["additionalProperties"])
        self.assertIn("evidence", body["output_json_schema"]["properties"])
        self.assertEqual(body["inputs"], {"source": "disclosure"})
        self.assertNotIn("fixture-key", request.contents[0].parts[0].text)
        self.assertEqual(result.finish_reason, "stop")

    def test_thinking_tokens_are_recovered_without_double_counting(self):
        sdk = FixtureSDK([sdk_response(prompt=100, visible=20, total=150, cached=10)])
        result = client(sdk).complete(LLMRouterConfig().tier1, "", {}, _Takeaway)
        self.assertEqual(result.usage.input_tokens, 100)
        self.assertEqual(result.usage.output_tokens, 50)  # 20 visible + 30 thinking
        self.assertEqual(result.usage.cached_input_tokens, 10)

    def test_missing_or_inconsistent_usage_stays_unknown(self):
        sdk = FixtureSDK([sdk_response(usage=False), sdk_response(prompt=100, visible=20, total=110)])
        transport = client(sdk)
        self.assertIsNone(transport.complete(LLMRouterConfig().tier1, "", {}, _Takeaway).usage)
        self.assertIsNone(transport.complete(LLMRouterConfig().tier1, "", {}, _Takeaway).usage)

    def test_blocked_prompt_preserves_available_usage(self):
        sdk = FixtureSDK([sdk_response(candidates=False, visible=0, total=100)])
        result = client(sdk).complete(LLMRouterConfig().tier1, "", {}, _Takeaway)
        self.assertEqual(result.finish_reason, "blocked")
        self.assertIsNone(result.text)
        self.assertEqual(result.usage.input_tokens, 100)

    def test_max_tokens_is_not_a_complete_json_result(self):
        sdk = FixtureSDK([sdk_response(reason=2)])
        result = client(sdk).complete(LLMRouterConfig().tier1, "", {}, _Takeaway)
        self.assertEqual(result.finish_reason, "length")

    def test_tier2_model_uses_the_same_free_sdk_adapter(self):
        sdk = FixtureSDK([sdk_response()])
        client(sdk).complete(LLMRouterConfig().tier2, "", {}, _Takeaway)
        self.assertEqual(sdk.calls[0]["request"].model, "models/" + DEFAULT_TIER2_MODEL)
        self.assertEqual(sdk.calls[0]["request"].generation_config.max_output_tokens, 6000)

    def test_no_project_confirmation_means_no_sdk_call(self):
        sdk = FixtureSDK([])
        transport = client(sdk, confirmed=False)
        self.assertFalse(transport.available(LLMRouterConfig().tier1))
        with self.assertRaises(ProviderUnavailable):
            transport.complete(LLMRouterConfig().tier1, "", {}, _Takeaway)
        self.assertFalse(sdk.calls)

    def test_missing_api_key_does_not_use_adc(self):
        sdk = FixtureSDK([])
        with patch.dict(os.environ, {}, clear=True):
            transport = client(sdk, api_key=None)
            self.assertFalse(transport.available(LLMRouterConfig().tier1))
        self.assertFalse(sdk.calls)

    def test_confirmation_parameter_does_not_accept_truthy_strings(self):
        for value in ("false", "true", 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                GeminiLLMClient(free_tier_project_confirmed=value)

    def test_old_paid_model_cannot_call_adapter(self):
        sdk = FixtureSDK([])
        route = ModelRoute(provider="openai", model="gpt-4o-mini")
        with self.assertRaises(ProviderUnavailable):
            client(sdk).complete(route, "", {}, _Takeaway)
        self.assertFalse(sdk.calls)

    def test_quota_error_starts_cooldown_without_upgrade(self):
        sdk = FixtureSDK([ResourceExhausted("fixture quota")])
        transport = client(sdk)
        with self.assertRaises(LLMHTTPError) as raised:
            transport.complete(LLMRouterConfig().tier1, "", {}, _Takeaway)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertFalse(transport.available(LLMRouterConfig().tier1))
        self.assertEqual(len(sdk.calls), 1)

    def test_permission_error_is_sanitized(self):
        sdk = FixtureSDK([PermissionDenied("sensitive fixture-key should not be emitted")])
        with self.assertRaises(LLMHTTPError) as raised:
            client(sdk).complete(LLMRouterConfig().tier1, "", {}, _Takeaway)
        self.assertEqual(raised.exception.status_code, 403)
        self.assertNotIn("fixture-key", str(raised.exception))

    def test_clients_have_explicit_keys_and_fixed_developer_api_endpoint(self):
        created = []
        class Constructor:
            def __init__(self, **kwargs):
                created.append(kwargs)
                self.transport = type("Transport", (), {"close": lambda self: None})()
        with patch("google.ai.generativelanguage.GenerativeServiceClient", Constructor):
            first = GeminiLLMClient(api_key="first-key", free_tier_project_confirmed=True)
            second = GeminiLLMClient(api_key="second-key", free_tier_project_confirmed=True)
            try:
                self.assertTrue(first.available(LLMRouterConfig().tier1))
                self.assertTrue(second.available(LLMRouterConfig().tier1))
            finally:
                first.close()
                second.close()
        self.assertEqual([c["client_options"]["api_key"] for c in created], ["first-key", "second-key"])
        self.assertTrue(all(c["client_options"]["api_endpoint"] == "generativelanguage.googleapis.com" for c in created))
        self.assertTrue(all(c["transport"] == "rest" for c in created))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.cache = SQLiteCache(":memory:")
        self.addCleanup(self.cache.close)

    def test_full_gemini_path_counts_tokens_and_zero_estimated_cost(self):
        sdk = FixtureSDK([sdk_response()])
        with engine(self.cache, [item()], client(sdk)) as pipeline:
            first = pipeline.get_push_feed(["TESTCO"], include_market=False)
            second = pipeline.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual(first.telemetry.tier1_calls, 1)
        self.assertEqual(first.telemetry.input_tokens, 100)
        self.assertEqual(first.telemetry.output_tokens, 50)
        self.assertEqual(first.telemetry.estimated_cost_usd, 0.0)
        self.assertTrue(first.telemetry.cost_estimate_complete)
        self.assertEqual(second.telemetry.tier1_calls, 0)
        self.assertEqual(second.telemetry.cache_hits, 1)
        self.assertEqual(len(sdk.calls), 1)

    def test_unconfirmed_project_keeps_source_cards_without_any_model_attempt(self):
        llm = FakeLLM([material_payload()])
        with engine(self.cache, [item()], llm, settings=settings(confirmed=False)) as pipeline:
            feed = pipeline.get_push_feed(["TESTCO"], include_market=False)
        self.assertTrue(feed.priority_digest)
        self.assertEqual(feed.telemetry.tier1_calls, 0)
        self.assertFalse(llm.calls)

    def test_cached_outputs_can_be_read_after_confirmation_is_removed(self):
        with engine(self.cache, [item()], FakeLLM([material_payload()]), settings=settings()) as pipeline:
            pipeline.get_push_feed(["TESTCO"], include_market=False)
        llm = FakeLLM([])
        with engine(self.cache, [item()], llm, settings=settings(confirmed=False)) as pipeline:
            feed = pipeline.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual(feed.telemetry.cache_hits, 1)
        self.assertEqual(feed.telemetry.tier1_calls, 0)
        self.assertFalse(llm.calls)

    def test_quota_exhaustion_is_not_retried_even_if_retries_are_configured(self):
        sdk = FixtureSDK([ResourceExhausted("fixture quota")])
        rows = [item(identity="a"), item(identity="b", description="Another capacity expansion with new plant details.")]
        with engine(self.cache, rows, client(sdk), settings=settings(retries=1)) as pipeline:
            feed = pipeline.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual(len(sdk.calls), 1)
        self.assertEqual(feed.telemetry.tier1_calls, 1)
        self.assertEqual(len(feed.priority_digest), 2)
        self.assertEqual(feed.telemetry.estimated_cost_usd, 0.0)

    def test_explicit_server_retry_is_counted_once_per_attempt(self):
        sdk = FixtureSDK([InternalServerError("fixture"), sdk_response()])
        with engine(self.cache, [item()], client(sdk), settings=settings(retries=1)) as pipeline:
            with patch("backend.engines.push_engine.time.sleep"):
                feed = pipeline.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual(feed.telemetry.tier1_calls, 2)
        self.assertEqual(len(sdk.calls), 2)
        self.assertTrue(all(c["retry"] is None for c in sdk.calls))

    def test_incomplete_response_tokens_count_but_output_is_not_cached(self):
        sdk = FixtureSDK([sdk_response(reason=2), sdk_response()])
        with engine(self.cache, [item()], client(sdk)) as pipeline:
            first = pipeline.get_push_feed(["TESTCO"], include_market=False)
            second = pipeline.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual(first.telemetry.output_tokens, 50)
        self.assertEqual(first.telemetry.failures, 1)
        self.assertEqual(second.telemetry.tier1_calls, 1)
        self.assertEqual(len(sdk.calls), 2)

    def test_public_completion_contract_is_provider_neutral(self):
        result = LLMCompletion(text='{"text":null,"evidence":null}', finish_reason="stop")
        self.assertNotIn("choices", result.model_dump())
        self.assertEqual(set(FREE_GEMINI_MODELS), {DEFAULT_TIER1_MODEL, DEFAULT_TIER2_MODEL})


if __name__ == "__main__":
    unittest.main()
