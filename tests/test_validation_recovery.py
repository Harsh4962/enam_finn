"""Regression tests for live-validation retries and incomplete market history."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import requests
from google.ai import generativelanguage as api

from backend.cache import CacheKey, SQLiteCache
from backend.config import LLMRouterConfig, Settings
from backend.data.market_data import MarketDataClient, MarketDataError
from backend.engines.push_engine import GeminiLLMClient, LLMCompletion, LLMHTTPError, LLMUsage
from backend.validate import validate_backend
from tests.test_data_plumbing import FakeTickerFactory, frame
from tests.test_pull_engine import NOW, Documents


class ScriptedCanary:
    def __init__(self, tier2=(), tier1=()):
        self.scripts = {1: list(tier1), 2: list(tier2)}
        self.calls = []

    def available(self, route):
        return True

    def complete(self, route, system, inputs, model):
        tier = 1 if route.model == LLMRouterConfig().tier1.model else 2
        self.calls.append((tier, route, dict(inputs)))
        action = self.scripts[tier].pop(0) if self.scripts[tier] else None
        if isinstance(action, Exception):
            raise action
        if action == "wrong_copy":
            return LLMCompletion(text=json.dumps({**inputs, "value": 999}), finish_reason="stop")
        if action == "bad_schema":
            return LLMCompletion(text='{"nonce": 123}', finish_reason="stop")
        if action == "truncated":
            return LLMCompletion(text=json.dumps(inputs), finish_reason="length")
        return LLMCompletion(text=json.dumps(inputs), finish_reason="stop",
            usage=None if action == "missing_usage" else LLMUsage(input_tokens=10, output_tokens=8))


class ValidationRecoveryTests(unittest.TestCase):
    def run_validation(self, transport, **kwargs):
        settings = Settings(router=LLMRouterConfig(free_tier_project_confirmed=True))
        with patch("backend.validate.time.sleep") as sleep, patch("backend.validate.random.uniform", return_value=0):
            result = validate_backend(("TESTCO",), settings=settings, live_llm=True,
                documents=False, announcements=False, market=False,
                fundamentals_factory=lambda cache: Documents(), llm_transport=transport,
                clock=lambda: NOW, **kwargs)
        return result, sleep

    @staticmethod
    def tier2(result):
        return next(check for check in result.checks if check.component == "gemini_tier_2")

    def test_503_then_success_uses_configured_timeout_and_records_both_attempts(self):
        transport = ScriptedCanary(tier2=[LLMHTTPError(503)])
        result, sleep = self.run_validation(transport)
        check = self.tier2(result)
        self.assertEqual(check.status, "passed")
        self.assertEqual(check.observations["api_calls"], 2)
        self.assertEqual(check.observations["timeout_seconds"], 150.0)
        self.assertEqual(check.observations["max_retries"], 3)
        self.assertEqual([item["status"] for item in check.observations["attempts"]], ["failed", "passed"])
        self.assertEqual(result.telemetry.tier2_calls, 2)
        self.assertEqual(result.telemetry.tier1_calls, 1)
        self.assertEqual(result.telemetry.failures, 1)
        self.assertEqual(transport.calls[1][2], transport.calls[2][2])
        self.assertTrue(all(route.timeout_seconds == 150.0 for tier, route, _ in transport.calls if tier == 2))
        sleep.assert_called_once_with(1.0)

    def test_four_503_failures_exhaust_exactly_three_retries(self):
        result, sleep = self.run_validation(ScriptedCanary(tier2=[LLMHTTPError(503) for _ in range(4)]))
        check = self.tier2(result)
        self.assertEqual(check.status, "failed")
        self.assertEqual(check.detail, "Gemini HTTP 503")
        self.assertEqual(check.observations["api_calls"], 4)
        self.assertTrue(check.observations["retry_budget_exhausted"])
        self.assertEqual(result.telemetry.tier2_calls, 4)
        self.assertEqual(result.telemetry.failures, 4)
        self.assertEqual([args[0][0] for args in sleep.call_args_list], [1.0, 2.0, 4.0])
        self.assertFalse(result.telemetry.cost_estimate_complete)

    def test_timeout_and_503_are_retried_without_repeating_tier1(self):
        result, sleep = self.run_validation(ScriptedCanary(tier2=[requests.ReadTimeout("private"), LLMHTTPError(503)]))
        self.assertEqual(self.tier2(result).status, "passed")
        self.assertEqual(result.telemetry.tier2_calls, 3)
        self.assertEqual(result.telemetry.tier1_calls, 1)
        self.assertEqual(sleep.call_count, 2)
        self.assertNotIn("private", result.model_dump_json())

    def test_auth_quota_and_client_errors_never_retry(self):
        for code in (400, 401, 403, 404, 429):
            with self.subTest(code=code):
                result, sleep = self.run_validation(ScriptedCanary(tier2=[LLMHTTPError(code)]))
                check = self.tier2(result)
                self.assertEqual(check.status, "failed")
                self.assertEqual(check.observations["api_calls"], 1)
                self.assertFalse(check.observations["retry_budget_exhausted"])
                self.assertEqual(result.telemetry.tier2_calls, 1)
                sleep.assert_not_called()

    def test_schema_copying_and_truncation_failures_never_retry(self):
        for action in ("wrong_copy", "bad_schema", "truncated"):
            with self.subTest(action=action):
                result, sleep = self.run_validation(ScriptedCanary(tier2=[action]))
                check = self.tier2(result)
                self.assertEqual(check.status, "failed")
                self.assertEqual(check.observations["api_calls"], 1)
                sleep.assert_not_called()

    def test_unexpected_error_is_sanitized_and_not_retried(self):
        result, sleep = self.run_validation(ScriptedCanary(tier2=[RuntimeError("fixture-secret-key")]))
        self.assertEqual(self.tier2(result).status, "failed")
        self.assertEqual(result.telemetry.tier2_calls, 1)
        self.assertNotIn("fixture-secret-key", result.model_dump_json())
        sleep.assert_not_called()

    def test_success_without_usage_is_warning_and_not_retried(self):
        result, sleep = self.run_validation(ScriptedCanary(tier2=["missing_usage"]))
        self.assertEqual(self.tier2(result).status, "warning")
        self.assertEqual(result.telemetry.tier2_calls, 1)
        self.assertFalse(result.telemetry.cost_estimate_complete)
        sleep.assert_not_called()

    def test_tier1_still_has_no_retries(self):
        result, sleep = self.run_validation(ScriptedCanary(tier1=[LLMHTTPError(503)]))
        check = next(check for check in result.checks if check.component == "gemini_tier_1")
        self.assertEqual(check.observations["api_calls"], 1)
        self.assertEqual(check.observations["timeout_seconds"], 45.0)
        self.assertEqual(check.observations["max_retries"], 0)
        self.assertEqual(result.telemetry.tier1_calls, 1)
        self.assertEqual(self.tier2(result).status, "passed")
        sleep.assert_not_called()

    def test_actual_sdk_http_503_then_success_has_no_hidden_retries_or_sampling_fields(self):
        sdk = api.GenerativeServiceClient(transport="rest", client_options={
            "api_key": "fixture-key", "api_endpoint": "generativelanguage.googleapis.com"})
        transport = GeminiLLMClient(api_key="fixture-key", free_tier_project_confirmed=True, sdk_client=sdk)
        captured = []
        def request(session, method, url, **kwargs):
            body = json.loads(kwargs["data"])
            captured.append((url, body, kwargs["timeout"]))
            response = requests.Response()
            response.request = requests.Request(method, url).prepare()
            response.headers["Content-Type"] = "application/json"
            tier2_calls = sum("gemini-3.8-flash:" in item[0] for item in captured)
            if "gemini-3.8-flash:" in url and tier2_calls == 1:
                response.status_code = 503
                payload = {"error": {"code": 503, "status": "UNAVAILABLE", "message": "Synthetic overload"}}
            else:
                response.status_code = 200
                inputs = json.loads(body["contents"][0]["parts"][0]["text"])["inputs"]
                payload = {"candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(inputs)}]},
                    "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 10,
                    "candidatesTokenCount": 8, "totalTokenCount": 18}}
            response._content = json.dumps(payload).encode()
            return response
        try:
            with patch("requests.sessions.Session.request", request):
                result, _ = self.run_validation(transport)
            self.assertEqual(self.tier2(result).status, "passed", self.tier2(result).model_dump())
            self.assertEqual(result.telemetry.tier2_calls, 2)
            self.assertEqual(len(captured), 3)
            for url, body, timeout in captured:
                self.assertEqual(set(body["generationConfig"]), {"maxOutputTokens", "responseMimeType", "responseSchema"})
                expected = 150.0 if "gemini-3.8-flash:" in url else 45.0
                self.assertEqual(timeout, expected)
        finally:
            sdk.transport.close()


class MarketValidationCoverageTests(unittest.TestCase):
    def check_frame(self, history):
        factory = FakeTickerFactory({"TESTCO.NS": history})
        with SQLiteCache(":memory:") as cache:
            report = validate_backend(("TESTCO",), settings=Settings(), live_llm=False,
                documents=False, announcements=False, fundamentals_factory=lambda c: Documents(),
                market_factory=lambda c: MarketDataClient(c, ticker_factory=factory, clock=lambda: NOW),
                cache=cache, clock=lambda: NOW)
        return next(check for check in report.checks if check.component == "market")

    def test_one_bar_cannot_pass(self):
        check = self.check_frame(frame(1))
        self.assertEqual(check.status, "unverified")
        self.assertEqual(check.observations["bars"], 1)
        self.assertEqual(check.observations["required_bars"], 35)
        self.assertEqual(len(check.observations["missing_metrics"]), 4)

    def test_21_bars_with_valid_metrics_still_discloses_short_history(self):
        check = self.check_frame(frame(21))
        self.assertEqual(check.status, "unverified")
        self.assertEqual(check.observations["missing_metrics"], [])
        self.assertEqual(check.observations["bars"], 21)

    def test_complete_history_passes(self):
        check = self.check_frame(frame(45))
        self.assertEqual(check.status, "passed")
        self.assertEqual(check.observations["bars"], 35)
        self.assertEqual(check.observations["missing_metrics"], [])

    def test_missing_volume_does_not_pass_despite_35_bars(self):
        history = frame(35)
        history.loc[history.index[-3], "Volume"] = float("nan")
        check = self.check_frame(history)
        self.assertEqual(check.status, "unverified")
        self.assertIn("volume_spike_ratio", check.observations["missing_metrics"])

    def test_zero_volume_baseline_is_not_invented_as_a_valid_multiple(self):
        history = frame(35)
        history.loc[history.index[:-1], "Volume"] = 0
        check = self.check_frame(history)
        self.assertEqual(check.status, "unverified")
        self.assertEqual(check.observations["average_volume_20d"], 0.0)
        self.assertIsNone(check.observations["volume_spike_ratio"])

    def test_market_source_failure_includes_bounded_symbol_diagnostics(self):
        class Missing:
            def fetch_snapshot(self, ticker):
                raise MarketDataError("No usable EOD data from NSE/BSE Yahoo symbols: TESTCO.NS: MarketDataError; TESTCO.BO: MarketDataError")
        report = validate_backend(("TESTCO",), settings=Settings(), documents=False, announcements=False,
            fundamentals_factory=lambda c: Documents(), market_factory=lambda c: Missing(), clock=lambda: NOW)
        check = next(check for check in report.checks if check.component == "market")
        self.assertEqual(check.status, "unverified")
        self.assertIn("TESTCO.NS", check.detail)
        self.assertIn("TESTCO.BO", check.detail)

    def test_range_query_recovers_history_when_explicit_dates_would_return_one_bar(self):
        class DateSensitiveTicker:
            def history(self, **kwargs):
                return frame(1) if "start" in kwargs or "end" in kwargs else frame(45)
        with SQLiteCache(":memory:") as cache, MarketDataClient(cache,
                ticker_factory=lambda symbol: DateSensitiveTicker(), clock=lambda: NOW) as client:
            snapshot = client.fetch_snapshot("TESTCO")
        self.assertEqual(len(snapshot.bars), 35)
        self.assertIsNotNone(snapshot.metrics.price_change_pct)
        self.assertIsNotNone(snapshot.metrics.price_change_5d_pct)
        self.assertIsNotNone(snapshot.metrics.volume_spike_ratio)

    def test_old_incomplete_history_cache_is_not_reused(self):
        factory = FakeTickerFactory({"TESTCO.NS": frame(45)})
        with SQLiteCache(":memory:") as cache, MarketDataClient(cache,
                ticker_factory=factory, clock=lambda: NOW) as client:
            old = client._snapshot("TESTCO", "TESTCO.NS", client._bars(frame(1), NOW.date()), NOW)
            payload_key = CacheKey.for_content("TESTCO", "market.eod_snapshot", old)
            cache.put(payload_key, old, ttl_seconds=21600)
            pointer_key = CacheKey.for_content("TESTCO", "request.market_eod", {
                "version": "market-v1", "completed_through": NOW.date().isoformat(),
                "sessions": 35, "auto_adjust": False, "repair": False})
            cache.put(pointer_key, {"content_hash": payload_key.content_hash}, ttl_seconds=21600)
            recovered = client.fetch_snapshot("TESTCO")
        self.assertEqual(len(recovered.bars), 35)
        self.assertEqual(len(factory.calls), 1)


if __name__ == "__main__":
    unittest.main()
