"""Validate financial/provenance boundaries and persistent cost accounting.

All documents, quotes and figures below are synthetic test fixtures. No network,
API key, finance feed, paid model call, or third-party test runner is needed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from backend.cache import CacheKey, SQLiteCache, content_hash, estimate_token_cost
from backend.config import (
    AD_HOC_DEMO_TICKERS,
    ANNOUNCEMENT_CATEGORIES,
    CATEGORY_POLICIES,
    DEFAULT_PORTFOLIO,
    TASK_TIERS,
    AlertThresholds,
    CacheSettings,
    LLMRouterConfig,
    ModelRoute,
    Settings,
    Task,
    normalize_ticker,
)
from backend.schemas import (
    BusinessSnapshot,
    CashFlowPeriod,
    CorporateAnnouncement,
    EODMarketMetric,
    EvidenceText,
    FinancialSnapshot,
    ManagementGuidance,
    NarrativeVsNumbersQuarter,
    NumericFact,
    PromiseDelivery,
    PullResearchReport,
    ResultsHeadlineTable,
    SourceDocument,
    TelemetryEvent,
    TrajectoryAndBalanceSheet,
    TriggeredAlert,
    WorkingCapitalPeriod,
    percentage_change,
)

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
CITATION = {"source_id": "results", "page": 1}


def load(model, data):
    return model.model_validate_json(json.dumps(data, allow_nan=False))


def source(kind="quarterly_results", source_id="results", published_on="2026-07-15"):
    return {
        "source_id": source_id, "ticker": "RELIANCE", "kind": kind,
        "title": "Synthetic regression fixture", "publisher": "Test issuer",
        "url": "https://issuer.example/fixture.pdf", "published_on": published_on,
        "retrieved_at": NOW.isoformat(), "content_hash": "a" * 64,
    }


def period(start="2026-04-01", end="2026-06-30", kind="quarter"):
    return {"start_on": start, "end_on": end, "kind": kind, "label": f"{start}/{end}"}


def fact(metric, value, unit="inr_crore", reporting_period=None):
    return {
        "metric": metric, "value": value, "unit": unit,
        "period": reporting_period or period(), "basis": "consolidated",
        "citations": [CITATION] if value is not None else [],
        "missing_reason": None if value is not None else "Not disclosed in this fixture",
    }


def snapshot(reporting_period=None, scale=1.0):
    p = reporting_period or period()
    return {
        "period": p, "basis": "consolidated",
        **{metric: fact(metric, value * scale, reporting_period=p)
           for metric, value in (("revenue", 100.0), ("ebitda", 20.0), ("pbt", 15.0), ("pat", 10.0))},
    }


def evidence(source_id="results", inference=False):
    return {"text": "Synthetic evidence for validation only.",
            "citations": [{"source_id": source_id, "page": 1}], "is_inference": inference}


def guidance():
    return {
        "guidance_id": "revenue-q1", "metric": "revenue", "exact_quote": "Test revenue guidance: 95 to 105.",
        "speaker": "Test CEO", "issued_on": "2026-04-20", "target_period": period(),
        "unit": "inr_crore", "basis": "consolidated", "numeric_range": {"lower": 95.0, "upper": 105.0},
        "citations": [{"source_id": "call", "page": 2}],
    }


def comparison():
    return {"guidance": guidance(), "actual": fact("revenue", 100.0),
            "verdict": "within_range", "explanation": evidence(inference=True)}


MODULES = ("business_snapshot", "financial_snapshot", "trajectory_and_balance_sheet",
           "cash_quality", "forward_guidance", "bull_bear_case")


def report():
    quarters = (
        period("2025-07-01", "2025-09-30"), period("2025-10-01", "2025-12-31"),
        period("2026-01-01", "2026-03-31"), period(),
    )
    return {
        "ticker": "RELIANCE", "company_name": "Test issuer", "as_of": "2026-10-29",
        "generated_at": NOW.isoformat(), "sources": [source()],
        **{name: None for name in MODULES},
        "narrative_vs_numbers": [{"quarter": q, "status": "insufficient_data", "data_gaps": [
            {"field": "guidance", "reason": "no_explicit_guidance", "detail": "No guidance supplied in test fixture."}
        ]} for q in quarters],
        "data_gaps": [{"field": name, "reason": "not_available", "detail": "Module omitted in test fixture."} for name in MODULES],
    }


def market(close=106.0, volume=250):
    return {
        "ticker": "RELIANCE", "session_date": "2026-10-29", "close_inr": close,
        "previous_close_inr": 100.0, "previous_session_date": "2026-10-28", "volume": volume,
        "prior_20d_volumes": [100] * 20, "volume_window_end": "2026-10-28",
        "price_basis": "split_adjusted", "source": source("market_data", published_on="2026-10-29"),
    }


class ConfigTests(unittest.TestCase):
    def test_exact_demo_and_reference_categories(self):
        self.assertEqual(len(DEFAULT_PORTFOLIO), 16)
        self.assertEqual(len(AD_HOC_DEMO_TICKERS), 5)
        self.assertEqual(len(set(ANNOUNCEMENT_CATEGORIES)), 17)
        self.assertIn("M&A", ANNOUNCEMENT_CATEGORIES)
        self.assertEqual({c.value for c, p in CATEGORY_POLICIES.items() if p.collapse_by_default},
                         {"unclassified", "postal_ballot", "agm_egm"})
        self.assertTrue(all(1 <= p.priority <= 5 for p in CATEGORY_POLICIES.values()))

    def test_symbols_generalize(self):
        for value, expected in ((" m&m.ns ", "M&M"), ("BAJAJ-AUTO", "BAJAJ-AUTO"), ("360ONE", "360ONE")):
            self.assertEqual(normalize_ticker(value), expected)
        for invalid in ("", "TCS.BO", "^NSEI", "A/B", "TCS;DROP TABLE", 123):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_ticker(invalid)

    def test_explicit_routing(self):
        router = LLMRouterConfig()
        self.assertIsNone(router.route(Task.FINANCIAL_METRICS))
        self.assertEqual(router.route(Task.GUIDANCE_EXTRACTION).model, "gemini-3.5-flash-lite")
        self.assertEqual(router.route(Task.NARRATIVE_AUDIT).model, "gemini-3.8-flash")
        self.assertEqual({t for t, tier in TASK_TIERS.items() if tier == 2},
                         {Task.NARRATIVE_AUDIT, Task.BULL_BEAR, Task.PRIMARY_QA})
        with self.assertRaises(ValueError):
            router.route("generic_chat")

    def test_model_lifecycle_and_unknown_pricing(self):
        with self.assertRaises(ValidationError):
            ModelRoute(provider="anthropic", model="claude-3-7-sonnet-20250219")
        route = ModelRoute(provider="openai", model="gpt-4o")
        route.ensure_usable(date(2026, 10, 29))
        with self.assertRaises(ValueError):
            route.ensure_usable(date(2027, 1, 14))
        self.assertIsNone(ModelRoute(provider="openai", model="gpt-custom-test").pricing)

    def test_env_is_explicit_and_strict(self):
        settings = Settings.from_env({"FINN_CONFIG_JSON": '{"thresholds":{"price_move_pct":3.0}}',
                                      "FINN_CACHE_PATH": "/tmp/finn-test.db"})
        self.assertEqual(settings.thresholds.price_move_pct, 3.0)
        self.assertEqual(settings.cache.path, "/tmp/finn-test.db")
        with self.assertRaises(ValidationError):
            Settings.from_env({"FINN_CONFIG_JSON": '{"thresholds":{"price_move_pct":"3"}}'})

    def test_invalid_thresholds_and_chunks(self):
        for data in ({"price_move_pct": 0}, {"volume_multiplier": 1}, {"volume_lookback_sessions": 19},
                     {"volume_lookback_sessions": 20.0}):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                load(AlertThresholds, data)
        with self.assertRaises(ValidationError):
            LLMRouterConfig(concall_chunk_tokens=300, concall_overlap_tokens=300)


class FinancialSchemaTests(unittest.TestCase):
    def test_strict_numbers_and_extras(self):
        for value in ("100", True):
            data = fact("revenue", value)
            with self.subTest(value=value), self.assertRaises(ValidationError):
                load(NumericFact, data)
        data = fact("revenue", 100.0)
        data["invented_field"] = "ignored?"
        with self.assertRaises(ValidationError):
            load(NumericFact, data)
        with self.assertRaises(ValidationError):
            NumericFact.model_validate_json(json.dumps({**fact("revenue", 100.0), "value": float("nan")}))

    def test_missing_number_has_a_reason_and_real_zero_is_available(self):
        self.assertEqual(load(NumericFact, fact("pat", 0.0)).value, 0)
        data = fact("pat", None)
        self.assertIsNone(load(NumericFact, data).value)
        data.pop("missing_reason")
        with self.assertRaises(ValidationError):
            load(NumericFact, data)

    def test_headlines_and_calendar_month_end(self):
        table = load(ResultsHeadlineTable, {
            "current": snapshot(), "previous_quarter": snapshot(period("2026-01-01", "2026-03-31"), 0.8),
            "year_ago": snapshot(period("2025-04-01", "2025-06-30"), 0.5),
        })
        self.assertAlmostEqual(table.headline_rows[0].qoq.value_pct, 25)
        self.assertAlmostEqual(table.headline_rows[0].yoy.value_pct, 100)
        self.assertEqual(table.current.ebitda_margin_pct, 20)
        self.assertEqual(table.current.pat_margin_pct, 10)
        restored = ResultsHeadlineTable.model_validate_json(table.model_dump_json(round_trip=True))
        self.assertEqual(restored, table)

    def test_missing_and_negative_growth_bases(self):
        for baseline, reason in ((None, "missing_value"), (0, "zero_base"), (-5, "negative_base")):
            result = percentage_change(10.0, baseline)
            self.assertIsNone(result.value_pct)
            self.assertEqual(result.unavailable_reason, reason)

    def test_no_ytd_or_mismatched_basis_in_headlines(self):
        base = {"current": snapshot(), "previous_quarter": None, "year_ago": None,
                "previous_quarter_missing_reason": "not available", "year_ago_missing_reason": "not available"}
        load(ResultsHeadlineTable, base)
        data = copy.deepcopy(base)
        data["current"] = snapshot(period("2026-01-01", "2026-06-30", "half_year"))
        with self.assertRaises(ValidationError):
            load(ResultsHeadlineTable, data)
        data = copy.deepcopy(base)
        data["previous_quarter"] = snapshot(period("2026-01-01", "2026-03-31"))
        data["previous_quarter_missing_reason"] = None
        data["previous_quarter"]["basis"] = "standalone"
        for metric in ("revenue", "ebitda", "pbt", "pat"):
            data["previous_quarter"][metric]["basis"] = "standalone"
        with self.assertRaises(ValidationError):
            load(ResultsHeadlineTable, data)

    def test_cash_metrics_and_capex_sign(self):
        p = period("2025-04-01", "2026-03-31", "annual")
        data = {"period": p, "basis": "consolidated", "cfo": fact("cfo", 80.0, reporting_period=p),
                "pat": fact("pat", 100.0, reporting_period=p), "capex": fact("capex", 30.0, reporting_period=p)}
        cash = load(CashFlowPeriod, data)
        self.assertEqual(cash.cash_conversion, 0.8)
        self.assertEqual(cash.free_cash_flow_inr_crore, 50)
        data["pat"]["value"] = -10.0
        self.assertIsNone(load(CashFlowPeriod, data).cash_conversion)
        data["capex"]["value"] = -30.0
        with self.assertRaises(ValidationError):
            load(CashFlowPeriod, data)

    def test_annual_cfo_cannot_be_compared_to_quarterly_pat(self):
        data = {"period": period(), "basis": "consolidated", "cfo": fact("cfo", 80.0),
                "pat": fact("pat", 100.0), "capex": fact("capex", 30.0)}
        data["cfo"]["period"] = period("2025-04-01", "2026-03-31", "annual")
        with self.assertRaises(ValidationError):
            load(CashFlowPeriod, data)

    def test_working_capital_cycle(self):
        data = {"period": period(), "basis": "consolidated",
                **{name: fact(name, value, "days") for name, value in
                   (("inventory_days", 40.0), ("receivable_days", 50.0), ("payable_days", 60.0))}}
        self.assertEqual(load(WorkingCapitalPeriod, data).cycle_days, 30)

    def test_sector_metric_can_be_explicitly_inapplicable(self):
        data = snapshot()
        data["ebitda"] = fact("ebitda", None)
        data["ebitda"]["missing_reason"] = "Not applicable for this financial institution"
        self.assertIsNone(load(FinancialSnapshot, data).ebitda_margin_pct)

    def test_working_capital_trend_and_chronology(self):
        periods = (period("2025-01-01", "2025-03-31"), period())
        history = [{"period": p, "basis": "consolidated", **{
            name: fact(name, value + (10.0 if name == "inventory_days" and index else 0.0), "days", p)
            for name, value in (("inventory_days", 40.0), ("receivable_days", 50.0), ("payable_days", 60.0))
        }} for index, p in enumerate(periods)]
        point = period("2026-06-30", "2026-06-30", "point_in_time")
        data = {"total_debt": fact("total_debt", 100.0, reporting_period=point),
                "cash_and_equivalents": fact("cash_and_equivalents", 20.0, reporting_period=point),
                "debt_to_equity": fact("debt_to_equity", 0.5, "ratio", point), "working_capital": history}
        self.assertEqual(load(TrajectoryAndBalanceSheet, data).working_capital_cycle_delta_days, (None, 10.0))
        data["working_capital"] = history[::-1]
        with self.assertRaises(ValidationError):
            load(TrajectoryAndBalanceSheet, data)


class AuditSchemaTests(unittest.TestCase):
    def test_numeric_guidance_verdict_is_deterministic(self):
        for value, verdict in ((90.0, "below_range"), (100.0, "within_range"), (110.0, "above_range")):
            data = comparison()
            data["actual"]["value"] = value
            data["verdict"] = verdict
            self.assertEqual(load(PromiseDelivery, data).verdict, verdict)
        data["verdict"] = "within_range"
        with self.assertRaises(ValidationError):
            load(PromiseDelivery, data)

    def test_hindsight_is_not_guidance(self):
        data = guidance()
        data["issued_on"] = "2026-07-20"
        with self.assertRaises(ValidationError):
            load(ManagementGuidance, data)

    def test_annual_guidance_and_different_metrics_cannot_be_scored(self):
        for field, value in (("metric", "pat"), ("basis", "standalone"), ("unit", "percent"),
                             ("period", period("2026-04-01", "2027-03-31", "annual"))):
            data = comparison()
            data["actual"][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                load(PromiseDelivery, data)

    def test_unassessable_is_explicit(self):
        data = comparison()
        data.update(verdict="not_assessable", actual=None)
        with self.assertRaises(ValidationError):
            load(PromiseDelivery, data)
        data["not_assessable_reason"] = "Annual guidance is not comparable to one quarter"
        self.assertEqual(load(PromiseDelivery, data).verdict, "not_assessable")

    def test_four_consecutive_quarters_and_explicit_missing_modules(self):
        data = report()
        self.assertEqual(len(load(PullResearchReport, data).narrative_vs_numbers), 4)
        data["narrative_vs_numbers"].pop()
        with self.assertRaises(ValidationError):
            load(PullResearchReport, data)
        data = report()
        data["narrative_vs_numbers"][1], data["narrative_vs_numbers"][2] = data["narrative_vs_numbers"][2], data["narrative_vs_numbers"][1]
        with self.assertRaises(ValidationError):
            load(PullResearchReport, data)
        data = report()
        data["data_gaps"] = []
        with self.assertRaises(ValidationError):
            load(PullResearchReport, data)

    def test_citation_resolution_and_primary_only_narratives(self):
        for source_id in ("missing", "screener"):
            data = report()
            data["business_snapshot"] = {"overview": evidence(source_id)}
            if source_id == "screener":
                data["sources"].append(source("screener_financials", "screener"))
            with self.subTest(source_id=source_id), self.assertRaises(ValidationError):
                load(PullResearchReport, data)

    def test_screener_is_allowed_for_numbers_and_labeled(self):
        data = report()
        data["sources"] = [source("screener_financials")]
        data["financial_snapshot"] = snapshot()
        result = load(PullResearchReport, data)
        self.assertEqual(result.financial_snapshot.revenue.value, 100)

    def test_professional_profiles_are_excluded_from_pull(self):
        data = report()
        data["sources"].append(source("professional_profile", "profile"))
        with self.assertRaises(ValidationError):
            load(PullResearchReport, data)

    def test_future_source_is_rejected(self):
        data = report()
        data["as_of"] = "2026-07-01"
        with self.assertRaises(ValidationError):
            load(PullResearchReport, data)

    def test_call_date_can_precede_transcript_publication(self):
        data = report()
        call = source("concall_transcript", "call", "2026-04-25")
        call["event_on"] = "2026-04-20"
        data["sources"].append(call)
        data["narrative_vs_numbers"][-1].update(status="complete", comparisons=[comparison()], data_gaps=[])
        self.assertEqual(load(PullResearchReport, data).narrative_vs_numbers[-1].status, "complete")
        data["sources"][-1]["event_on"] = "2026-04-21"
        with self.assertRaises(ValidationError):
            load(PullResearchReport, data)

    def test_cannot_claim_complete_without_an_assessed_comparison(self):
        data = {"quarter": period(), "status": "complete", "comparisons": []}
        with self.assertRaises(ValidationError):
            load(NarrativeVsNumbersQuarter, data)

    def test_purely_qualitative_guidance_has_no_artificial_numeric_unit(self):
        data = comparison()
        data["guidance"].update(numeric_range=None, unit=None)
        data.update(actual=None, verdict="qualitatively_supported")
        self.assertEqual(load(PromiseDelivery, data).verdict, "qualitatively_supported")


class PushSchemaTests(unittest.TestCase):
    def test_eod_math_excludes_current_volume(self):
        metric = load(EODMarketMetric, market())
        self.assertAlmostEqual(metric.price_change_pct, 6)
        self.assertEqual(metric.average_volume_20d, 100)
        self.assertEqual(metric.volume_multiple, 2.5)

    def test_volume_baseline_requires_twenty_previous_sessions(self):
        for count in (19, 21):
            data = market()
            data["prior_20d_volumes"] = [100] * count
            with self.subTest(count=count), self.assertRaises(ValidationError):
                load(EODMarketMetric, data)
        data = market()
        data["volume_window_end"] = "2026-10-29"
        with self.assertRaises(ValidationError):
            load(EODMarketMetric, data)

    def test_zero_volume_baseline_does_not_divide_by_zero(self):
        data = market()
        data["prior_20d_volumes"] = [0] * 20
        self.assertIsNone(load(EODMarketMetric, data).volume_multiple)

    def test_thresholds_are_inclusive_and_both_price_directions_work(self):
        for close in (106.0, 94.0):
            data = {"alert_id": "test", "ticker": "RELIANCE", "kind": "price_move", "priority": 3,
                    "title": "Test price move", "market_metric": market(close), "threshold": 5.0}
            load(TriggeredAlert, data)
        data["market_metric"] = market(105.0)
        load(TriggeredAlert, data)
        data.update(kind="volume_spike", market_metric=market(volume=250), threshold=2.0)
        load(TriggeredAlert, data)
        data["market_metric"] = market(volume=200)
        load(TriggeredAlert, data)
        data["market_metric"] = market(volume=199)
        with self.assertRaises(ValidationError):
            load(TriggeredAlert, data)

    def test_decimal_price_boundary_is_exact_and_inclusive(self):
        data = market(close=1.05)
        data["previous_close_inr"] = 1.0
        metric = load(EODMarketMetric, data)
        self.assertEqual(metric.price_change_pct, 5.0)
        load(TriggeredAlert, {"alert_id": "boundary", "ticker": "RELIANCE", "kind": "price_move",
            "priority": 3, "title": "Test boundary", "market_metric": data, "threshold": 5.0})

    def test_decimal_cash_flow_subtraction(self):
        data = {"period": period(), "basis": "consolidated", "cfo": fact("cfo", 80.1),
                "pat": fact("pat", 100.0), "capex": fact("capex", 30.1)}
        self.assertEqual(load(CashFlowPeriod, data).free_cash_flow_inr_crore, 50.0)

    def test_routine_confirmation_and_priority_override(self):
        data = {"announcement_id": "nse-123", "ticker": "RELIANCE", "exchange": "NSE",
                "title": "Test compliance notice", "published_at": NOW.isoformat(), "received_at": NOW.isoformat(),
                "category": "unclassified", "classification_tier": 0, "classification_confidence": 0.99,
                "classification_reason": "Exact compliance rule match", "filing": source("exchange_filing")}
        self.assertFalse(load(CorporateAnnouncement, data).collapse_in_digest)
        data["routine_confirmed"] = True
        self.assertTrue(load(CorporateAnnouncement, data).collapse_in_digest)
        data.update(priority_override=5, priority_override_reason=evidence())
        self.assertFalse(load(CorporateAnnouncement, data).collapse_in_digest)
        data.pop("priority_override_reason")
        with self.assertRaises(ValidationError):
            load(CorporateAnnouncement, data)

    def test_boolean_tier_and_naive_datetime_are_rejected(self):
        data = {"operation": "classify", "tier": True, "latency_ms": 0.0}
        with self.assertRaises(ValidationError):
            load(TelemetryEvent, data)
        data = source()
        data["retrieved_at"] = "2026-10-29T12:00:00"
        with self.assertRaises(ValidationError):
            load(SourceDocument, data)

    def test_management_bio_profiles_are_allowed_on_push(self):
        data = {"announcement_id": "management-123", "ticker": "RELIANCE", "exchange": "NSE",
                "title": "Test CEO change", "published_at": NOW.isoformat(), "received_at": NOW.isoformat(),
                "category": "management_change", "classification_tier": 1, "classification_confidence": 0.95,
                "classification_reason": "Explicit appointment in filing", "filing": source("exchange_filing"),
                "management_bio_enrichment": {
                    "incoming": [{"name": "Test incoming executive", "role": "CEO", "background": [evidence("profile")]}],
                    "outgoing": [{"name": "Test outgoing executive", "role": "CEO", "missing_background_reason": "Not available"}],
                    "sources": [source("professional_profile", "profile")],
                }}
        result = load(CorporateAnnouncement, data)
        self.assertEqual(len(result.management_bio_enrichment.incoming), 1)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "finn_cache.db"
        self.now = [1000.0]
        self.cache = SQLiteCache(self.path, clock=lambda: self.now[0])
        self.key = CacheKey.for_content("RELIANCE", "filing", {"document": "a"})

    def tearDown(self):
        self.cache.close()
        self.temp.cleanup()

    def test_json_null_bytes_unicode_and_persistence(self):
        for value in (None, {"label": "₹ हिन्दी", "value": 2.5}, b"%PDF\x00\xff", "<html>filing</html>"):
            with self.subTest(value=value):
                self.cache.put(self.key, value)
                result = self.cache.get(self.key)
                self.assertTrue(result.hit)
                self.assertEqual(result.value, value)
        self.cache.close()
        with SQLiteCache(self.path, clock=lambda: self.now[0]) as reopened:
            self.assertEqual(reopened.get(self.key).value, "<html>filing</html>")

    def test_content_identity_and_llm_invalidation(self):
        self.assertEqual(content_hash({"a": 1, "b": 2}), content_hash({"b": 2, "a": 1}))
        self.assertNotEqual(content_hash("hello"), content_hash(b"hello"))
        self.assertNotEqual(content_hash([1, 2]), content_hash([2, 1]))
        route = ModelRoute(provider="openai", model="gpt-4o-mini")
        kwargs = dict(ticker="RELIANCE", task=Task.GUIDANCE_EXTRACTION, inputs={"messages": ["full request"]},
                      route=route, prompt_version="v1", output_model=BusinessSnapshot)
        original = CacheKey.for_llm(**kwargs)
        variants = ({"prompt_version": "v2"}, {"schema_version": "v2"},
                    {"route": ModelRoute(provider="openai", model="gpt-4o")},
                    {"output_model": EvidenceText}, {"inputs": {"messages": ["changed request"]}})
        for change in variants:
            with self.subTest(change=change):
                self.assertNotEqual(original.content_hash, CacheKey.for_llm(**(kwargs | change)).content_hash)

    def test_ttl_exact_boundary_and_no_expiry(self):
        self.cache.put(self.key, {"a": 1}, ttl_seconds=10)
        self.now[0] = 1009.999
        self.assertTrue(self.cache.get(self.key).hit)
        self.now[0] = 1010.0
        self.assertEqual(self.cache.get(self.key).miss_reason, "expired")
        self.cache.put(self.key, None, ttl_seconds=None)
        self.now[0] = 10**9
        self.assertTrue(self.cache.get(self.key).hit)

    def test_model_round_trip_does_not_cache_computed_inputs(self):
        metric = load(EODMarketMetric, market())
        self.cache.put(self.key, metric)
        cached = self.cache.get(self.key, model=EODMarketMetric)
        self.assertTrue(cached.hit)
        self.assertEqual(cached.value.volume_multiple, 2.5)

    def test_schema_mismatch_is_a_miss(self):
        self.cache.put(self.key, {"not": "a market metric"})
        result = self.cache.get(self.key, model=EODMarketMetric)
        self.assertFalse(result.hit)
        self.assertEqual(result.miss_reason, "invalid_payload")
        self.assertEqual(self.cache.telemetry().cache_misses, 1)

    def test_checksum_corruption_is_evicted(self):
        self.cache.put(self.key, {"valid": True})
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE cache_entries SET payload=?", (b"broken",))
        self.assertEqual(self.cache.get(self.key).miss_reason, "checksum_mismatch")
        self.assertEqual(self.cache.get(self.key).miss_reason, "not_found")

    def test_invalid_json_cannot_hide_behind_a_valid_checksum(self):
        self.cache.put(self.key, {"valid": True})
        for invalid in (b"not json", b'{"value":NaN}'):
            self.cache.put(self.key, {"valid": True})
            with sqlite3.connect(self.path) as connection:
                connection.execute("UPDATE cache_entries SET payload=?,payload_sha256=?", (invalid, hashlib.sha256(invalid).hexdigest()))
            self.assertEqual(self.cache.get(self.key).miss_reason, "invalid_payload")

    def test_invalid_payloads_and_ttls_are_rejected_before_writes(self):
        for value in ({1: "non-string key"}, {"x": float("nan")}, object()):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                self.cache.put(self.key, value)
        for ttl in (0, -1, float("inf"), True):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.cache.put(self.key, {}, ttl_seconds=ttl)
        with SQLiteCache(":memory:", settings=CacheSettings(max_payload_bytes=1024)) as small:
            with self.assertRaises(ValueError):
                small.put(self.key, b"x" * 1025)

    def test_token_cost_discount_and_unknown_prices(self):
        route = ModelRoute(provider="openai", model="gpt-4o-mini")
        cost = estimate_token_cost(route.pricing, input_tokens=1000, cached_input_tokens=200, output_tokens=500)
        self.assertAlmostEqual(cost, 0.000435)
        self.assertIsNone(estimate_token_cost(None, input_tokens=1000, output_tokens=1))
        with self.assertRaises(ValueError):
            estimate_token_cost(route.pricing, input_tokens=1, cached_input_tokens=2, output_tokens=0)

    def test_local_hits_are_not_model_calls_and_retries_count(self):
        route = ModelRoute(provider="openai", model="gpt-4o-mini")
        self.cache.get(self.key)
        for _ in range(2):
            with self.cache.track("guidance", ticker="RELIANCE", tier=1, route=route) as usage:
                usage.set_tokens(input_tokens=1000, cached_input_tokens=200, output_tokens=500)
        with self.cache.track("math", ticker="RELIANCE", tier=0):
            pass
        self.cache.put(self.key, {"ok": True})
        self.cache.get(self.key)
        telemetry = self.cache.telemetry()
        self.assertEqual((telemetry.tier0_calls, telemetry.tier1_calls, telemetry.tier2_calls), (1, 2, 0))
        self.assertEqual((telemetry.cache_hits, telemetry.cache_misses), (1, 1))
        self.assertEqual(telemetry.input_tokens, 2000)
        self.assertAlmostEqual(telemetry.estimated_cost_usd, 0.00087)
        self.assertTrue(telemetry.cost_estimate_complete)

    def test_failed_call_records_unknown_cost_and_preserves_exception(self):
        route = ModelRoute(provider="openai", model="gpt-4o-mini")
        with self.assertRaisesRegex(RuntimeError, "do-not-store-this-message"):
            with self.cache.track("provider", ticker="RELIANCE", tier=1, route=route):
                raise RuntimeError("do-not-store-this-message")
        telemetry = self.cache.telemetry()
        self.assertEqual((telemetry.failures, telemetry.unpriced_llm_calls, telemetry.tier1_calls), (1, 1, 1))
        self.assertFalse(telemetry.cost_estimate_complete)
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT error_type,pricing_source FROM telemetry_events").fetchone()
        self.assertEqual(row, ("RuntimeError", None))

    def test_unknown_price_and_omitted_provider_usage_are_not_free(self):
        for model in ("gpt-custom-test", "gpt-4o-mini"):
            route = ModelRoute(provider="openai", model=model)
            with self.cache.track("provider", tier=1, route=route) as usage:
                if model == "gpt-custom-test":
                    usage.set_tokens(input_tokens=100, output_tokens=20)
        self.assertEqual(self.cache.telemetry().unpriced_llm_calls, 2)
        event = TelemetryEvent(operation="manual", tier=1, provider="openai", model="gpt-4o-mini",
                               provider_called=True, latency_ms=1.0)
        self.assertIsNone(event.estimated_cost_usd)

    def test_atomic_payload_and_ledger_rollback(self):
        invalid_cost = TelemetryEvent(operation="audit", ticker="RELIANCE", tier=2, provider="openai",
            model="gpt-4o", provider_called=True, latency_ms=1.0, estimated_cost_usd=1e20)
        with self.assertRaises(ValueError):
            self.cache.put(self.key, {"must": "rollback"}, event=invalid_cost)
        self.assertFalse(self.cache.get(self.key).hit)
        self.assertEqual(self.cache.telemetry().tier2_calls, 0)

    def test_event_ids_are_idempotent_and_filters_are_correct(self):
        event = TelemetryEvent(event_id="same", occurred_at=NOW, operation="math", ticker="RELIANCE",
                               request_id="r1", tier=0, latency_ms=2.0)
        self.assertTrue(self.cache.record_event(event))
        self.assertFalse(self.cache.record_event(event))
        self.assertEqual(self.cache.telemetry(request_id="r1", ticker="reliance.ns").events, 1)
        self.assertEqual(self.cache.telemetry(ticker="TCS").events, 0)
        self.assertEqual(self.cache.telemetry(since=NOW, until=NOW + timedelta(seconds=1)).events, 1)
        self.assertEqual(self.cache.telemetry(until=NOW).events, 0)
        with self.assertRaises(ValueError):
            self.cache.telemetry(since=datetime(2026, 1, 1))

    def test_purge_invalidate_and_telemetry_retention(self):
        self.cache.put(self.key, {}, ttl_seconds=1)
        other = CacheKey.for_content("TCS", "filing", {})
        self.cache.put(other, {}, ttl_seconds=None)
        self.now[0] += 2
        self.assertEqual(self.cache.purge_expired(), 1)
        self.assertEqual(self.cache.invalidate(ticker="RELIANCE"), 0)
        self.assertTrue(self.cache.get(other).hit)
        self.assertEqual(self.cache.invalidate(ticker="TCS", data_type="filing"), 1)
        self.assertEqual(self.cache.prune_telemetry(before=NOW + timedelta(days=1)), 1)

    def test_concurrent_managers_and_upserts(self):
        def worker(index):
            with SQLiteCache(self.path) as cache:
                for number in range(15):
                    key = CacheKey.for_content("TCS", f"worker.{index}", {"n": number})
                    cache.put(key, {"n": number})
                    self.assertEqual(cache.get(key).value, {"n": number})
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(worker, range(4)))
        self.assertEqual(self.cache.telemetry().cache_hits, 60)
        self.cache.put(self.key, {"version": 1})
        self.cache.put(self.key, {"version": 2})
        self.assertEqual(self.cache.get(self.key).value, {"version": 2})

    def test_in_memory_closed_and_future_schema(self):
        with SQLiteCache(":memory:") as cache:
            cache.put(self.key, {"a": 1})
            self.assertTrue(cache.get(self.key).hit)
        with self.assertRaises(RuntimeError):
            cache.get(self.key)
        self.cache.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA user_version=999")
        with self.assertRaises(RuntimeError):
            SQLiteCache(self.path)


if __name__ == "__main__":
    unittest.main()
