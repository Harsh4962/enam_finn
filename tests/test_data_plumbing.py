"""Offline source-boundary tests. Every quote, bar and filing here is synthetic."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import requests
from pydantic import ValidationError

from backend.cache import SQLiteCache
from backend.config import DEFAULT_PORTFOLIO, AnnouncementCategory
from backend.data.announcements import (
    NSE_API_URL,
    NSE_HOME_URL,
    NSEAnnouncementsClient,
    NSEBlockedError,
    SeedDataError,
    classify_announcement,
    generate_seed_announcements,
    load_seed_announcements,
    normalize_announcement,
)
from backend.data.market_data import MarketDataClient, MarketDataError, evaluate_eod_triggers
from backend.schemas import PullResearchReport, SourceDocument
from tests.test_foundation import report as report_fixture

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)  # 17:30 IST, after EOD readiness


def frame(count=45, latest_volume=250):
    index = pd.bdate_range(end="2026-10-29", periods=count, tz="Asia/Kolkata")
    return pd.DataFrame({"Close": [100.0 + i for i in range(count)],
                         "Volume": [100] * (count - 1) + [latest_volume]}, index=index)


class FakeTickerFactory:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, symbol):
        factory = self
        class Ticker:
            def history(self, **kwargs):
                factory.calls.append((symbol, kwargs))
                value = factory.responses[symbol]
                if isinstance(value, Exception):
                    raise value
                return value.copy()
        return Ticker()


class FakeResponse:
    def __init__(self, data=None, *, status=200, body=None, content_type="application/json", retry_after=None):
        self.status_code = status
        self.body = body if body is not None else json.dumps(data).encode()
        self.headers = {"Content-Type": content_type}
        if retry_after is not None:
            self.headers["Retry-After"] = retry_after
        self.closed = False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected additional HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


def live_row(**changes):
    return {"symbol": "RELIANCE", "seq_id": "12345", "desc": "Financial Results",
        "attchmntText": "The board approved unaudited financial results for the quarter ended September 30, 2026.",
        "an_dt": "29-Oct-2026 16:45:00", "attchmntFile": "https://nsearchives.nseindia.com/corporate/test-results.pdf", **changes}


def homepage():
    return FakeResponse(body=b"<html>Exchange homepage</html>", content_type="text/html")


class MarketDataTests(unittest.TestCase):
    def setUp(self):
        self.now = [NOW]
        self.cache = SQLiteCache(":memory:", clock=lambda: self.now[0].timestamp())

    def tearDown(self):
        self.cache.close()

    def client(self, factory):
        return MarketDataClient(self.cache, ticker_factory=factory, clock=lambda: self.now[0])

    def test_last_35_sessions_and_correct_1d_5d_volume_math(self):
        factory = FakeTickerFactory({"RELIANCE.NS": frame()})
        snapshot = self.client(factory).fetch_snapshot(" reliance.ns ")
        metric = snapshot.metrics
        self.assertEqual(len(snapshot.bars), 35)
        self.assertEqual(metric.close_inr, 144.0)
        self.assertEqual(metric.previous_close_inr, 143.0)
        self.assertAlmostEqual(metric.price_change_pct, 100 / 143)
        self.assertAlmostEqual(metric.price_change_5d_pct, 500 / 139)
        self.assertEqual(metric.average_volume_20d, 100)
        self.assertEqual(metric.volume_spike_ratio, 2.5)
        self.assertEqual(metric.market_exchange, "NSE")
        self.assertFalse(factory.calls[0][1]["auto_adjust"])

    def test_nse_then_bse_fallback_records_actual_exchange(self):
        factory = FakeTickerFactory({"M&M.NS": ValueError("unavailable"), "M&M.BO": frame()})
        metric = self.client(factory).fetch("M&M")
        self.assertEqual([symbol for symbol, _ in factory.calls], ["M&M.NS", "M&M.BO"])
        self.assertEqual(metric.provider_symbol, "M&M.BO")
        self.assertEqual(metric.market_exchange, "BSE")

    def test_empty_primary_history_uses_fallback(self):
        factory = FakeTickerFactory({"TCS.NS": pd.DataFrame(), "TCS.BO": frame()})
        self.assertEqual(self.client(factory).fetch("TCS").market_exchange, "BSE")

    def test_both_sources_fail_without_fabricated_market_data(self):
        factory = FakeTickerFactory({"TCS.NS": RuntimeError("blocked"), "TCS.BO": RuntimeError("blocked")})
        with self.assertRaises(MarketDataError):
            self.client(factory).fetch("TCS")

    def test_today_is_excluded_before_eod_readiness(self):
        self.now[0] = NOW.replace(hour=9)  # 14:30 IST
        factory = FakeTickerFactory({"TCS.NS": frame()})
        metric = self.client(factory).fetch("TCS")
        self.assertEqual(metric.session_date.isoformat(), "2026-10-28")
        self.assertEqual(metric.close_inr, 143)

    def test_cache_rolls_at_eod_even_within_six_hours(self):
        self.now[0] = NOW.replace(hour=9)
        factory = FakeTickerFactory({"TCS.NS": frame()})
        client = self.client(factory)
        self.assertEqual(client.fetch("TCS").session_date.isoformat(), "2026-10-28")
        self.now[0] = NOW
        self.assertEqual(client.fetch("TCS").session_date.isoformat(), "2026-10-29")
        self.assertEqual(len(factory.calls), 2)

    def test_cache_hits_six_hour_expiry_and_force_refresh(self):
        factory = FakeTickerFactory({"TCS.NS": frame()})
        client = self.client(factory)
        client.fetch("TCS")
        client.fetch("TCS")
        self.assertEqual(len(factory.calls), 1)
        self.now[0] += timedelta(hours=6)
        client.fetch("TCS")
        self.assertEqual(len(factory.calls), 2)
        client.fetch("TCS", force_refresh=True)
        self.assertEqual(len(factory.calls), 3)

    def test_short_listing_keeps_missing_baselines_explicit(self):
        factory = FakeTickerFactory({"KAYNES.NS": frame(count=3)})
        snapshot = self.client(factory).fetch_snapshot("KAYNES")
        self.assertEqual(len(snapshot.bars), 3)
        self.assertIsNone(snapshot.metrics.price_change_5d_pct)
        self.assertIsNone(snapshot.metrics.volume_spike_ratio)
        self.assertTrue(snapshot.notes)

    def test_missing_volume_is_not_zero_filled_or_removed_from_baseline(self):
        history = frame()
        history.loc[history.index[-3], "Volume"] = float("nan")
        factory = FakeTickerFactory({"TCS.NS": history})
        metric = self.client(factory).fetch("TCS")
        self.assertIsNone(metric.average_volume_20d)
        self.assertIsNotNone(metric.volume_history_missing_reason)

    def test_invalid_latest_bar_does_not_silently_use_older_day(self):
        history = frame()
        history.loc[history.index[-1], "Volume"] = float("nan")
        factory = FakeTickerFactory({"TCS.NS": history, "TCS.BO": pd.DataFrame()})
        with self.assertRaises(MarketDataError):
            self.client(factory).fetch("TCS")

    def test_zero_volume_baseline_has_no_spike(self):
        history = frame()
        history.loc[history.index[-21:-1], "Volume"] = 0
        factory = FakeTickerFactory({"TCS.NS": history})
        metric = self.client(factory).fetch("TCS")
        self.assertEqual(metric.average_volume_20d, 0)
        self.assertIsNone(metric.volume_spike_ratio)
        self.assertEqual(evaluate_eod_triggers(metric), [])

    def test_trigger_boundaries_both_directions_and_stable_ids(self):
        for latest in (105.0, 95.0):
            history = frame(latest_volume=200)
            history.loc[history.index[-2], "Close"] = 100.0
            history.loc[history.index[-1], "Close"] = latest
            factory = FakeTickerFactory({"TCS.NS": history})
            metric = self.client(factory).fetch("TCS", force_refresh=True)
            alerts = evaluate_eod_triggers(metric, 5.0, 2.0)
            self.assertEqual({a.kind for a in alerts}, {"price_move", "volume_spike"})
            self.assertEqual([a.alert_id for a in alerts], [a.alert_id for a in evaluate_eod_triggers(metric, 5.0, 2.0)])
            self.assertEqual(evaluate_eod_triggers(metric, 5.01, 2.01), [])

    def test_no_llm_calls_or_model_cost(self):
        factory = FakeTickerFactory({"TCS.NS": frame()})
        self.client(factory).fetch("TCS")
        telemetry = self.cache.telemetry()
        self.assertEqual((telemetry.tier0_calls, telemetry.tier1_calls, telemetry.tier2_calls), (1, 0, 0))
        self.assertEqual(telemetry.estimated_cost_usd, 0)

    def test_borrowed_cache_survives_client_context(self):
        factory = FakeTickerFactory({"TCS.NS": frame()})
        with self.client(factory) as client:
            client.fetch("TCS")
        self.assertGreater(self.cache.telemetry().events, 0)


class ClassifierTests(unittest.TestCase):
    def test_all_seventeen_categories(self):
        examples = {
            "results": ("Financial Results", "Board approved quarterly financial results."),
            "M&A": ("Acquisition", "Company acquired a 75% stake in a subsidiary."),
            "fund_raise": ("Fund Raising", "Board approved a QIP."),
            "management_change": ("Appointment of CFO", "New chief financial officer appointed."),
            "litigation": ("Regulatory order", "Monetary penalty imposed by a regulator."),
            "insider_trading": ("Disclosures under Regulation 7(2)", "Disclosure submitted in the prescribed format."),
            "earnings_call": ("Concall transcript", "Transcript of the earnings conference call."),
            "investor_meetings": ("Investor meetings", "One-on-one meeting with a fund."),
            "credit_rating": ("Credit Rating", "ICRA reaffirmed the rating."),
            "dividend": ("Dividend", "Interim dividend declared."),
            "bonus_split": ("Stock Split", "Sub-division of equity shares."),
            "capex": ("Capacity expansion", "Board approved a new plant."),
            "press_release": ("Press Release", "Company announced a new product launch."),
            "postal_ballot": ("Postal Ballot Notice", "Electronic voting instructions."),
            "agm_egm": ("Notice of Annual General Meeting", "Meeting and voting logistics."),
            "pledging": ("Promoter pledge", "Creation of encumbrance on promoter shares."),
            "unclassified": ("Compliance Certificate", "Certificate under Regulation 74(5)."),
        }
        self.assertEqual(len(examples), 17)
        for category, (subject, description) in examples.items():
            with self.subTest(category=category):
                self.assertEqual(classify_announcement(subject, description).category.value, category)

    def test_insider_acquisition_is_not_corporate_m_and_a(self):
        result = classify_announcement("SEBI PIT - promoter acquisition of shares",
            "Personal shareholding change, not a business acquisition or investment in a subsidiary.")
        self.assertEqual(result.category, AnnouncementCategory.INSIDER_TRADING)

    def test_material_content_cannot_be_hidden_by_a_routine_subject(self):
        result = classify_announcement("Postal Ballot Notice", "Approval of acquisition of a wholly-owned subsidiary.")
        self.assertEqual(result.category, AnnouncementCategory.M_AND_A)
        self.assertFalse(result.routine_confirmed)

    def test_ambiguous_and_unknown_items_need_tier1_and_remain_visible(self):
        unknown = classify_announcement("General update", "Please see the attachment.")
        self.assertEqual(unknown.category, AnnouncementCategory.UNCLASSIFIED)
        self.assertTrue(unknown.requires_llm)
        self.assertFalse(unknown.routine_confirmed)
        ambiguous = classify_announcement("Outcome of board meeting", "Financial results approved and appointment of a CFO confirmed.")
        self.assertTrue(ambiguous.requires_llm)
        self.assertFalse(ambiguous.routine_confirmed)

    def test_earnings_call_discussing_results_stays_a_call(self):
        result = classify_announcement("Earnings call transcript", "Conference call discussing quarterly financial results.")
        self.assertEqual(result.category, AnnouncementCategory.EARNINGS_CALL)

    def test_merger_related_court_order_still_needs_semantic_resolution(self):
        result = classify_announcement("NCLT order", "Order approving a merger with the subsidiary.")
        self.assertTrue(result.requires_llm)
        self.assertFalse(result.routine_confirmed)
        self.assertIn(AnnouncementCategory.M_AND_A, result.matched_categories)

    def test_all_three_routine_categories_are_confirmed(self):
        for subject in ("Compliance certificate", "Postal Ballot", "AGM notice"):
            with self.subTest(subject=subject):
                self.assertTrue(classify_announcement(subject).routine_confirmed)


class NSEClientTests(unittest.TestCase):
    def setUp(self):
        self.now = [NOW]
        self.cache = SQLiteCache(":memory:", clock=lambda: self.now[0].timestamp())

    def tearDown(self):
        self.cache.close()

    def client(self, session, **kwargs):
        return NSEAnnouncementsClient(self.cache, session=session, clock=lambda: self.now[0],
                                      min_request_interval_seconds=0, **kwargs)

    def test_homepage_warmup_headers_params_and_live_metadata(self):
        warmup, response = homepage(), FakeResponse([live_row()])
        session = FakeSession([warmup, response])
        batch = self.client(session).fetch("reliance.ns")
        self.assertEqual([url for url, _ in session.calls], [NSE_HOME_URL, NSE_API_URL])
        self.assertIn("Mozilla/5.0", session.headers["User-Agent"])
        self.assertIn("application/json", session.headers["Accept"])
        self.assertEqual(session.calls[1][1]["params"]["symbol"], "RELIANCE")
        self.assertEqual(batch.source_mode, "live")
        self.assertFalse(batch.cache_hit)
        self.assertFalse(batch.announcements[0].is_demo)
        self.assertEqual(batch.announcements[0].published_at.hour, 11)  # 16:45 IST -> 11:15 UTC
        self.assertTrue(str(batch.announcements[0].attachment_url).startswith("https://nsearchives.nseindia.com/"))
        self.assertIsNone(batch.announcements[0].results_headline_table)  # no invented PDF extraction
        self.assertTrue(warmup.closed and response.closed)

    def test_live_payload_cache_reuses_response_and_original_fetch_time(self):
        session = FakeSession([homepage(), FakeResponse([live_row()])])
        client = self.client(session)
        first = client.fetch("RELIANCE")
        self.now[0] += timedelta(minutes=1)
        second = client.fetch("RELIANCE")
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.fetched_at, second.fetched_at)
        self.assertEqual(first.announcements[0].announcement_id, second.announcements[0].announcement_id)

    def test_empty_live_list_remains_empty_and_live(self):
        session = FakeSession([homepage(), FakeResponse([])])
        batch = self.client(session).fetch("RELIANCE")
        self.assertEqual((batch.source_mode, batch.announcements, batch.fallback_reason), ("live", (), None))

    def test_broadcast_during_http_request_is_not_mistaken_for_future_data(self):
        session = FakeSession([homepage(), FakeResponse([live_row(an_dt="29-Oct-2026 17:30:30")])])
        original_get = session.get
        def advancing_get(url, **kwargs):
            response = original_get(url, **kwargs)
            if url == NSE_API_URL:
                self.now[0] += timedelta(minutes=1)
            return response
        session.get = advancing_get
        batch = self.client(session).fetch("RELIANCE")
        self.assertEqual(batch.source_mode, "live")
        self.assertEqual(len(batch.announcements), 1)
        self.assertEqual(batch.invalid_rows, 0)

    def test_blocked_homepage_falls_back_and_cooldown_skips_more_requests(self):
        session = FakeSession([FakeResponse(status=403, body=b"Access denied")])
        client = self.client(session)
        with self.assertLogs("backend.data.announcements", level="WARNING"):
            batches = client.fetch_portfolio()
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(batches), 16)
        self.assertTrue(all(batch.source_mode == "demo" for batch in batches.values()))
        self.assertTrue(all(item.is_demo and item.filing.url is None for batch in batches.values() for item in batch.announcements))

    def test_429_and_html_challenge_use_honest_demo_mode(self):
        responses = (FakeResponse(status=429, retry_after="60"),
                     FakeResponse(body=b"<html>challenge</html>", content_type="text/html"))
        for response in responses:
            with self.subTest(status=response.status_code):
                session = FakeSession([homepage(), response])
                with self.assertLogs("backend.data.announcements", level="WARNING"):
                    batch = self.client(session).fetch("RELIANCE", force_refresh=True)
                self.assertEqual(batch.source_mode, "demo")
                self.assertIsNotNone(batch.fallback_reason)

    def test_network_timeout_is_handled_without_a_retry_storm(self):
        session = FakeSession([requests.ReadTimeout("network timeout")])
        client = self.client(session)
        with self.assertLogs("backend.data.announcements", level="WARNING"):
            client.fetch("RELIANCE")
            client.fetch("TCS")
        self.assertEqual(len(session.calls), 1)

    def test_demo_fallback_can_be_disabled(self):
        session = FakeSession([FakeResponse(status=403)])
        with self.assertRaises(NSEBlockedError):
            self.client(session, allow_seed_fallback=False).fetch("RELIANCE")

    def test_seed_only_never_calls_http_and_has_results_and_bio(self):
        session = FakeSession([])
        batches = self.client(session).fetch_portfolio(seed_only=True)
        self.assertEqual(len(session.calls), 0)
        self.assertEqual(sum(len(batch.announcements) for batch in batches.values()), 33)
        categories = {item.category.value for batch in batches.values() for item in batch.announcements}
        self.assertEqual(len(categories), 17)
        self.assertTrue(any(item.results_headline_table for batch in batches.values() for item in batch.announcements))
        self.assertTrue(any(item.management_bio_enrichment for batch in batches.values() for item in batch.announcements))
        self.assertTrue(all(item.collapse_in_digest for batch in batches.values() for item in batch.announcements
                            if item.category.value in {"unclassified", "postal_ballot", "agm_egm"}))

    def test_unknown_demo_ticker_is_unavailable_not_a_fake_generic_company(self):
        batch = self.client(FakeSession([])).fetch("HAL", seed_only=True)
        self.assertEqual(batch.source_mode, "unavailable")
        self.assertEqual(batch.announcements, ())

    def test_deduplication_and_partial_bad_rows(self):
        row = live_row()
        bad = live_row(seq_id="bad", an_dt="not a timestamp")
        session = FakeSession([homepage(), FakeResponse([row, row, bad])])
        batch = self.client(session).fetch("RELIANCE")
        self.assertEqual(len(batch.announcements), 1)
        self.assertEqual(batch.invalid_rows, 1)

    def test_an_old_but_valid_row_does_not_trigger_demo_fallback(self):
        session = FakeSession([homepage(), FakeResponse([live_row(an_dt="25-Oct-2026 16:45:00")])])
        batch = self.client(session).fetch("RELIANCE")
        self.assertEqual(batch.source_mode, "live")
        self.assertEqual(batch.announcements, ())
        self.assertEqual(batch.invalid_rows, 0)

    def test_error_object_and_invalid_json_trigger_labeled_fallback(self):
        for response in (FakeResponse({"error": "denied"}), FakeResponse(body=b"not JSON")):
            with self.subTest(body=response.body):
                session = FakeSession([homepage(), response])
                with self.assertLogs("backend.data.announcements", level="WARNING"):
                    batch = self.client(session).fetch("RELIANCE", force_refresh=True)
                self.assertEqual(batch.source_mode, "demo")

    def test_demo_is_never_saved_under_the_live_request_key(self):
        with self.assertLogs("backend.data.announcements", level="WARNING"):
            self.client(FakeSession([FakeResponse(status=403)])).fetch("RELIANCE")
        live_session = FakeSession([homepage(), FakeResponse([live_row()])])
        batch = self.client(live_session).fetch("RELIANCE")
        self.assertEqual(batch.source_mode, "live")
        self.assertEqual(len(live_session.calls), 2)

    def test_source_limits_and_untrusted_attachment_links(self):
        session = FakeSession([homepage(), FakeResponse([live_row()])])
        with patch("backend.data.announcements._MAX_BODY_BYTES", 100), self.assertLogs("backend.data.announcements", level="WARNING"):
            self.assertEqual(self.client(session).fetch("RELIANCE").source_mode, "demo")
        item = normalize_announcement(live_row(attchmntFile="javascript:alert(1)"), "RELIANCE", received_at=NOW)
        self.assertIsNone(item.attachment_url)

    def test_no_llm_tokens_in_either_live_or_seed_path(self):
        client = self.client(FakeSession([homepage(), FakeResponse([live_row()])]))
        client.fetch("RELIANCE")
        client.fetch("TCS", seed_only=True)
        telemetry = self.cache.telemetry()
        self.assertEqual((telemetry.tier1_calls, telemetry.tier2_calls, telemetry.estimated_cost_usd), (0, 0, 0))


class SeedTests(unittest.TestCase):
    def test_generator_and_loader_are_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seed.json"
            first = generate_seed_announcements(path)
            first_bytes = path.read_bytes()
            self.assertEqual(first, generate_seed_announcements(path))
            self.assertEqual(first_bytes, path.read_bytes())
            self.assertEqual(load_seed_announcements(path, as_of=NOW), load_seed_announcements(path, as_of=NOW))
            self.assertEqual({row["symbol"] for row in load_seed_announcements(path, as_of=NOW)}, set(DEFAULT_PORTFOLIO))

    def test_default_seed_can_be_generated_without_writing_to_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "not-created.json"
            with patch("backend.data.announcements.SEED_PATH", missing):
                self.assertEqual(len(load_seed_announcements(as_of=NOW)), 33)
            self.assertFalse(missing.exists())

    def test_bad_custom_seed_is_an_explicit_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seed.json"
            path.write_text('{"schema_version":1,"records":[]}', encoding="utf-8")
            with self.assertRaises(SeedDataError):
                load_seed_announcements(path, as_of=NOW)

    def test_synthetic_sources_cannot_enter_pull_research(self):
        data = report_fixture()
        data["sources"][0].update(is_demo=True, url=None)
        with self.assertRaises(ValidationError):
            PullResearchReport.model_validate_json(json.dumps(data))
        data["sources"][0]["is_demo"] = False
        with self.assertRaises(ValidationError):
            SourceDocument.model_validate_json(json.dumps(data["sources"][0]))


if __name__ == "__main__":
    unittest.main()
