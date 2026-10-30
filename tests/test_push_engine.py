"""Offline PUSH contracts; all people, disclosures and prices are synthetic."""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pymupdf as fitz
from pydantic import ValidationError

from backend.cache import SQLiteCache
from backend.config import AnnouncementCategory as Cat
from backend.config import LLMRouterConfig, ModelRoute, Settings, Task
from backend.data.announcements import AnnouncementBatch, NSEAnnouncementsClient, normalize_announcement
from backend.engines.push_engine import (
    EvidenceError,
    ExecutiveSearch,
    FilingTextClient,
    GeminiLLMClient,
    LLMCompletion,
    LLMHTTPError,
    LLMUsage,
    PushEngine,
    PushFeed,
    SearchOutcome,
    announced_quarter_end,
)
from backend.schemas import EODMarketMetric, SourceDocument, TelemetryEvent
from tests.test_data_plumbing import FakeResponse, FakeSession
from tests.test_fundamentals import company_html, parse

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
SETTINGS = Settings(router=LLMRouterConfig(free_tier_project_confirmed=True))
INCOMING = "The Board appointed Anita Rao as Chief Financial Officer effective November 1, 2026."
OUTGOING = "Ravi Shah will retire as Chief Financial Officer effective October 31, 2026."
CAREER = "Anita Rao previously led treasury at Acme Components and has experience in controls and capital allocation."
DEPARTING = "Ravi Shah previously served as finance director at Acme Engineering."


def item(subject="Capacity expansion", description="The Board approved INR 500 crore capex for a new plant, funded from internal accruals.",
         *, ticker="TESTCO", minutes=30, identity="one", attachment=False):
    published = NOW - timedelta(minutes=minutes)
    return normalize_announcement({"symbol": ticker, "seq_id": identity, "desc": subject,
        "attchmntText": description, "an_dt": published.isoformat(),
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/test.pdf" if attachment else None},
        ticker, received_at=max(NOW, published))


def executive_payload(outgoing=False):
    people = [{"name": "Anita Rao", "designation": "CFO", "direction": "incoming",
        "effective_on": "2026-11-01", "identity": {"source_ref": "filing.metadata", "quote": INCOMING},
        "effective_date": {"source_ref": "filing.metadata", "quote": INCOMING}}]
    if outgoing:
        people.append({"name": "Ravi Shah", "designation": "CFO", "direction": "outgoing",
            "effective_on": "2026-10-31", "identity": {"source_ref": "filing.metadata", "quote": OUTGOING},
            "effective_date": {"source_ref": "filing.metadata", "quote": OUTGOING}})
    return {"executives": people}


def bio_payload(outgoing=False):
    pills = [{"executive_index": 0,
        "background": "Anita Rao previously led treasury at Acme Components, with experience in controls and capital allocation.",
        "why_it_matters": "The CFO transition puts funding discipline and financial reporting continuity in focus.",
        "evidence": [{"source_ref": "filing.metadata", "quote": CAREER}], "missing_reason": None}]
    if outgoing:
        pills.append({"executive_index": 1, "background": "Ravi Shah previously served as finance director at Acme Engineering.",
            "why_it_matters": "His retirement makes the disclosed finance handover relevant to reporting continuity.",
            "evidence": [{"source_ref": "filing.metadata", "quote": DEPARTING}], "missing_reason": None})
    return {"pills": pills}


def material_payload(quote="The Board approved INR 500 crore capex for a new plant, funded from internal accruals."):
    return {"text": "Internally funded plant expansion commits INR 500 crore, making commissioning execution and cash deployment the key follow-through.",
            "evidence": {"source_ref": "filing.metadata", "quote": quote}}


class FakeLLM:
    def __init__(self, outputs=(), *, available=True, usage=True):
        self.outputs, self.calls, self.enabled, self.usage = list(outputs), [], available, usage

    def available(self, route):
        return self.enabled

    def complete(self, route, system, inputs, model):
        self.calls.append((route, system, inputs, model))
        if not self.outputs:
            raise AssertionError("unexpected LLM request")
        result = self.outputs.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, LLMCompletion):
            return result
        return LLMCompletion(text=json.dumps(result), finish_reason="stop",
            usage=LLMUsage(input_tokens=100, output_tokens=20, cached_input_tokens=10) if self.usage else None)


class FakeAnnouncements:
    def __init__(self, cache, items, *, error=None):
        self.cache, self.items, self.calls, self.error = cache, items, [], error

    def fetch(self, ticker, **kwargs):
        self.calls.append((ticker, kwargs))
        if self.error:
            raise self.error
        with self.cache.track("fixture.classify", ticker=ticker, tier=0):
            data = tuple(i for i in self.items if i.ticker == ticker)
        return AnnouncementBatch(ticker=ticker, source_mode="live", cache_hit=False, fetched_at=NOW,
            window_start=NOW - timedelta(hours=90), window_end=NOW, announcements=data)


class FakeFundamentals:
    def __init__(self, cache, company=None, *, error=None):
        self.cache, self.company, self.error, self.calls = cache, company, error, []

    def fetch(self, ticker, **kwargs):
        self.calls.append((ticker, kwargs))
        if self.error:
            raise self.error
        return self.company or parse()


class FakeMarket:
    def __init__(self, cache, metric=None, error=None):
        self.cache, self.metric, self.error = cache, metric, error

    def fetch(self, ticker, **kwargs):
        if self.error:
            raise self.error
        return self.metric


class EmptySearch:
    def __init__(self, cache):
        self.calls = []

    def search(self, name, company, ticker, **kwargs):
        self.calls.append((name, company, ticker))
        return SearchOutcome(query=name + " " + company, hits=(), errors=("ddgs.TimeoutError",))


def engine(cache, items=(), llm=None, *, company=None, market=None, **kwargs):
    return PushEngine(cache, settings=kwargs.pop("settings", SETTINGS), clock=lambda: NOW,
        llm_transport=llm or FakeLLM(),
        announcements_factory=kwargs.pop("announcements_factory", lambda c: FakeAnnouncements(c, items)),
        fundamentals_factory=kwargs.pop("fundamentals_factory", lambda c: FakeFundamentals(c, company)),
        market_factory=kwargs.pop("market_factory", lambda c: FakeMarket(c, market)),
        search_factory=kwargs.pop("search_factory", EmptySearch), **kwargs)


def metric(*, close=103.0, volume=200, session=date(2026, 10, 29)):
    source = SourceDocument(source_id="market.test", ticker="TESTCO", kind="market_data", title="Synthetic market fixture",
        publisher="Test fixture", url="https://finance.yahoo.com/quote/TESTCO.NS/", published_on=session,
        retrieved_at=NOW, content_hash="a" * 64)
    return EODMarketMetric(ticker="TESTCO", session_date=session, close_inr=close,
        previous_close_inr=100.0, previous_session_date=session - timedelta(days=1), volume=volume,
        prior_20d_volumes=(100,) * 20, volume_window_end=session - timedelta(days=1), price_basis="raw", source=source)


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.cache = SQLiteCache(":memory:")
        self.addCleanup(self.cache.close)

    def test_routine_drawer_costs_zero_tokens(self):
        rows = [item("Compliance Certificate under Regulation 74(5)", "Routine dematerialisation certificate", identity="a"),
                item("Postal Ballot Notice", "Registered-office administrative resolution", identity="b"),
                item("Annual General Meeting", "AGM voting and attendance instructions", identity="c")]
        llm = FakeLLM()
        with engine(self.cache, rows, llm) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
            self.assertEqual(feed.routine_drawer.count, 3)
            self.assertFalse(feed.priority_digest)
            self.assertFalse(feed.triggered_alerts)
            self.assertEqual(feed.telemetry.tier1_calls, 0)
            self.assertEqual(feed.telemetry.input_tokens, 0)
            self.assertFalse(llm.calls)
            self.assertFalse(client.search.calls)

    def test_unknown_unclassified_is_visible_without_llm(self):
        row = item("Update", "Further details of an unidentified event will follow.")
        with engine(self.cache, [row]) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual(len(feed.review_queue), 1)
        self.assertEqual(feed.routine_drawer.count, 0)
        self.assertEqual(feed.telemetry.tier1_calls, 0)

    def test_material_takeaway_and_request_exact_cache_usage(self):
        llm = FakeLLM([material_payload()])
        self.cache.record_event(TelemetryEvent(operation="unrelated", tier=0, latency_ms=1.0, request_id="another.request"))
        with engine(self.cache, [item()], llm) as client:
            first = client.get_push_feed(["TESTCO"], include_market=False)
            second = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual(first.telemetry.tier1_calls, 1)
        self.assertEqual(first.telemetry.input_tokens, 100)
        self.assertEqual(first.telemetry.cached_input_tokens, 10)
        self.assertEqual(first.telemetry.output_tokens, 20)
        self.assertEqual(first.telemetry.tier0_calls, 3)
        self.assertEqual(first.telemetry.tier2_calls, 0)
        self.assertAlmostEqual(first.telemetry.estimated_cost_usd, 0.0, places=9)
        self.assertEqual(second.telemetry.tier1_calls, 0)
        self.assertEqual(second.telemetry.estimated_cost_usd, 0.0)
        self.assertEqual(second.telemetry.cache_hits, 1)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(first.triggered_alerts[0].alert_id, second.triggered_alerts[0].alert_id)
        self.assertLessEqual(len(first.priority_digest[0].takeaway.text.split()), 22)

    def test_empty_watches_disable_category_alerts(self):
        with engine(self.cache, [item()], FakeLLM([material_payload()])) as client:
            feed = client.get_push_feed(["TESTCO"], watched_categories=[], include_market=False)
        self.assertTrue(feed.priority_digest)
        self.assertFalse(feed.triggered_alerts)

    def test_inclusive_price_and_volume_thresholds(self):
        with engine(self.cache, market=metric()) as client:
            feed = client.get_push_feed(["TESTCO"])
        self.assertEqual({a.kind for a in feed.triggered_alerts}, {"price_move", "volume_spike"})
        self.assertEqual(feed.telemetry.tier1_calls, 0)

    def test_negative_price_boundary(self):
        with engine(self.cache, market=metric(close=97.0, volume=100)) as client:
            feed = client.get_push_feed(["TESTCO"])
        self.assertEqual([a.kind for a in feed.triggered_alerts], ["price_move"])

    def test_old_market_event_does_not_become_new_alert(self):
        with engine(self.cache, market=metric(session=date(2026, 10, 23))) as client:
            feed = client.get_push_feed(["TESTCO"])
        self.assertFalse(feed.triggered_alerts)
        self.assertTrue(any(i.code == "OutsideWindow" for i in feed.issues))

    def test_market_failure_does_not_hide_corporate_cards(self):
        with engine(self.cache, [item()], FakeLLM([material_payload()]),
                    market_factory=lambda c: FakeMarket(c, error=TimeoutError())) as client:
            feed = client.get_push_feed(["TESTCO"])
        self.assertEqual(len(feed.priority_digest), 1)
        self.assertEqual(len(feed.triggered_alerts), 1)
        self.assertTrue(any(i.stage == "market" for i in feed.issues))

    def test_announcement_failure_does_not_hide_market_alerts(self):
        with engine(self.cache, market=metric(), announcements_factory=lambda c: FakeAnnouncements(c, (), error=TimeoutError())) as client:
            feed = client.get_push_feed(["TESTCO"])
        self.assertEqual(len(feed.triggered_alerts), 2)
        self.assertEqual(feed.source_modes["TESTCO"], "unavailable")

    def test_window_and_id_deduplication(self):
        good = item()
        rows = [good, good, item(minutes=24 * 60 + 1, identity="old"), item(minutes=-1, identity="future")]
        with engine(self.cache, rows, FakeLLM([material_payload()])) as client:
            feed = client.get_push_feed([" testco.NS ", "TESTCO"], include_market=False)
            self.assertEqual(len(client.announcements.calls), 1)
        self.assertEqual(feed.tickers, ("TESTCO",))
        self.assertEqual(len(feed.priority_digest), 1)

    def test_unknown_ticker_not_restricted_to_portfolio(self):
        with engine(self.cache, [item("Schedule of investor meetings", "Meeting logistics", ticker="M&M")]) as client:
            feed = client.get_push_feed(["m&m.NS"], include_market=False)
        self.assertEqual(feed.priority_digest[0].ticker, "M&M")
        self.assertEqual(feed.telemetry.tier1_calls, 0)

    def test_missing_credentials_keep_deterministic_card(self):
        with engine(self.cache, [item()], FakeLLM(available=False)) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual(feed.telemetry.tier1_calls, 0)
        self.assertEqual(feed.priority_digest[0].enrichment_status, "partial")
        self.assertTrue(feed.priority_digest[0].data_gaps)

    def test_missing_usage_is_unpriced_not_free(self):
        with engine(self.cache, [item()], FakeLLM([material_payload()], usage=False)) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual(feed.telemetry.tier1_calls, 1)
        self.assertEqual(feed.telemetry.unpriced_llm_calls, 1)
        self.assertFalse(feed.telemetry.cost_estimate_complete)

    def test_invalid_quote_consumes_tokens_but_is_not_cached(self):
        bad = material_payload("This quote was never present.")
        llm = FakeLLM([bad, material_payload()])
        with engine(self.cache, [item()], llm) as client:
            first = client.get_push_feed(["TESTCO"], include_market=False)
            second = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual(first.telemetry.failures, 1)
        self.assertEqual(first.telemetry.output_tokens, 20)
        self.assertEqual(second.telemetry.tier1_calls, 1)
        self.assertEqual(second.priority_digest[0].enrichment_status, "enriched")

    def test_overlong_takeaway_is_rejected(self):
        bad = material_payload()
        bad["text"] = " ".join(["word"] * 23)
        with engine(self.cache, [item()], FakeLLM([bad])) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertTrue(feed.telemetry.failures)
        self.assertLessEqual(len(feed.priority_digest[0].takeaway.text.split()), 22)

    def test_unsupported_number_in_takeaway_is_rejected(self):
        bad = material_payload()
        bad["text"] = "The company commits INR 999 crore to expansion."
        with engine(self.cache, [item()], FakeLLM([bad])) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertTrue(feed.telemetry.failures)
        self.assertNotIn("999", feed.priority_digest[0].takeaway.text)

    def test_budget_limits_actual_calls(self):
        rows = [item(identity="a"), item(identity="b", description="The Board approved INR 500 crore capex for a new plant, funded from internal accruals. Additional detail.")]
        with engine(self.cache, rows, FakeLLM([material_payload()]), max_llm_calls=1) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual(len(feed.priority_digest), 2)
        self.assertEqual(feed.telemetry.tier1_calls, 1)

    def test_demo_is_rich_but_does_not_generate_live_alerts_or_llm_calls(self):
        with engine(self.cache, announcements_factory=lambda c: NSEAnnouncementsClient(c, clock=lambda: NOW)) as client:
            feed = client.get_push_feed(["INFY"], seed_only=True, include_market=False)
            opted = client.get_push_feed(["INFY"], seed_only=True, include_market=False, include_demo_alerts=True)
        self.assertEqual(len(feed.priority_digest), 2)
        self.assertTrue(any(c.management_bio_enrichment for c in feed.priority_digest))
        self.assertTrue(any(c.results_headline_table for c in feed.priority_digest))
        self.assertFalse(feed.triggered_alerts)
        self.assertEqual(len(opted.triggered_alerts), 2)
        self.assertEqual(feed.telemetry.tier1_calls, 0)
        self.assertTrue(all(c.enrichment_status == "demo" for c in feed.priority_digest))

    def test_priority_rank_is_stable_and_policy_based(self):
        rows = [item("Schedule of investor meetings", "No unpublished information", identity="meeting"), item()]
        with engine(self.cache, rows, FakeLLM([material_payload()])) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual([c.priority_rank for c in feed.priority_digest], [1, 2])
        self.assertEqual([c.priority for c in feed.priority_digest], [4, 2])

    def test_invalid_parameters_fail_before_io(self):
        with engine(self.cache) as client:
            for kwargs in ({"tickers": "TESTCO"}, {"tickers": []}, {"window_hours": 0},
                           {"price_threshold_pct": 0.0}, {"volume_multiplier": 1.0},
                           {"watched_categories": ["postal_ballot"]}):
                with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, ValidationError)):
                    client.get_push_feed(**({"tickers": ["TESTCO"], "include_market": False} | kwargs))
            self.assertFalse(client.announcements.calls)

    def test_strict_feed_round_trip(self):
        with engine(self.cache, [item()], FakeLLM([material_payload()])) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        again = PushFeed.model_validate_json(feed.model_dump_json(round_trip=True))
        self.assertEqual(feed, again)

    def test_tier2_cannot_be_routed_here(self):
        with engine(self.cache) as client:
            with self.assertRaises(ValueError):
                client.llm.call("TESTCO", Task.NARRATIVE_AUDIT, PushFeed, "", {})

    def test_retry_counts_each_actual_provider_attempt(self):
        settings = Settings(router=LLMRouterConfig(tier1=ModelRoute(provider="google", model="gemini-3.5-flash-lite", billing_tier="free", max_retries=1), free_tier_project_confirmed=True))
        with engine(self.cache, [item()], FakeLLM([LLMHTTPError(503), material_payload()]), settings=settings) as client:
            with patch("backend.engines.push_engine.time.sleep"):
                feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual(feed.telemetry.tier1_calls, 2)
        self.assertEqual(feed.telemetry.failures, 1)
        self.assertEqual(feed.telemetry.unpriced_llm_calls, 1)

    def test_401_is_not_retried(self):
        settings = Settings(router=LLMRouterConfig(tier1=ModelRoute(provider="google", model="gemini-3.5-flash-lite", billing_tier="free", max_retries=1), free_tier_project_confirmed=True))
        with engine(self.cache, [item()], FakeLLM([LLMHTTPError(401)]), settings=settings) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual(feed.telemetry.tier1_calls, 1)


class ResultsTests(unittest.TestCase):
    def setUp(self):
        self.cache = SQLiteCache(":memory:")
        self.addCleanup(self.cache.close)

    def feed(self, row, company=None, answer=None):
        llm = FakeLLM([answer or {"text": "Operating Profit proxy margin was flat QoQ; the table does not establish the drivers.", "operating_leverage": "flat"}])
        with engine(self.cache, [row], llm, company=company) as client:
            return client.get_push_feed(["TESTCO"], include_market=False), llm

    def test_exact_quarter_table_and_deltas(self):
        feed, _ = self.feed(item("Financial Results", "Board approved consolidated results for the quarter ended September 30, 2026."))
        card = feed.priority_digest[0]
        self.assertEqual(card.results_context.alignment, "matched")
        self.assertEqual(card.results_headline_table.current.period.end_on, date(2026, 9, 30))
        self.assertEqual(card.results_headline_table.headline_rows[0].qoq.value_pct, 100.0)
        self.assertEqual(card.results_context.ebitda_basis, "operating_profit_proxy")
        self.assertEqual(card.results_context.operating_leverage, "flat")

    def test_historical_filing_does_not_take_latest_quarter(self):
        feed, _ = self.feed(item("Financial Results", "Results for the quarter ended 30 June 2026."))
        self.assertEqual(feed.priority_digest[0].results_headline_table.current.period.end_on, date(2026, 6, 30))

    def test_new_quarter_not_yet_in_screener_stays_missing(self):
        company = parse(company_html(quarterly_labels=["Sep 2025", "Dec 2025", "Mar 2026", "Jun 2026"]))
        feed, llm = self.feed(item("Financial Results", "Results for the quarter ended 30 September 2026."), company)
        self.assertIsNone(feed.priority_digest[0].results_headline_table)
        self.assertEqual(feed.priority_digest[0].results_context.alignment, "unavailable")
        self.assertFalse(llm.calls)

    def test_standalone_filing_does_not_borrow_consolidated_numbers(self):
        feed, llm = self.feed(item("Financial Results", "Standalone results for the quarter ended 30 September 2026."))
        self.assertIsNone(feed.priority_digest[0].results_headline_table)
        self.assertFalse(llm.calls)

    def test_unknown_quarter_is_labeled_context_only(self):
        feed, _ = self.feed(item("Financial Results", "Board approved the consolidated unaudited financial results."))
        self.assertEqual(feed.priority_digest[0].results_context.alignment, "latest_available_context")

    def test_future_reporting_quarter_is_not_delivered(self):
        feed, llm = self.feed(item("Financial Results", "Results for the quarter ending December 31, 2026."))
        self.assertIsNone(feed.priority_digest[0].results_headline_table)
        self.assertFalse(llm.calls)

    def test_scheduled_board_meeting_does_not_become_results(self):
        feed, llm = self.feed(item("Financial Results", "Board meeting intimation to consider and approve unaudited financial results for the quarter ended September 30, 2026."))
        self.assertIsNone(feed.priority_digest[0].results_headline_table)
        self.assertFalse(llm.calls)

    def test_annual_only_is_not_relabelled_quarterly(self):
        feed, llm = self.feed(item("Annual Financial Results", "Audited results for the year ended March 31, 2026."))
        self.assertIsNone(feed.priority_digest[0].results_headline_table)
        self.assertFalse(llm.calls)

    def test_wrong_operating_leverage_verdict_is_rejected(self):
        answer = {"text": "Operating Profit proxy margin expanded QoQ.", "operating_leverage": "expanded"}
        feed, _ = self.feed(item("Financial Results", "Results for the quarter ended September 30, 2026."), answer=answer)
        self.assertTrue(feed.telemetry.failures)
        self.assertIn("flat", feed.priority_digest[0].takeaway.text)

    def test_dates_are_explicit_and_not_filing_dates(self):
        self.assertEqual(announced_quarter_end("Quarter ended 2026-09-30"), date(2026, 9, 30))
        self.assertEqual(announced_quarter_end("Quarter and half-year ended 30.09.2026"), date(2026, 9, 30))
        self.assertIsNone(announced_quarter_end("Board meeting held on October 29, 2026"))


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.cache = SQLiteCache(":memory:")
        self.addCleanup(self.cache.close)

    def test_per_person_dates_and_cited_two_sentence_bios(self):
        row = item("Appointment and retirement of Chief Financial Officer", " ".join((INCOMING, OUTGOING, CAREER, DEPARTING)))
        llm = FakeLLM([executive_payload(True), bio_payload(True)])
        with engine(self.cache, [row], llm) as client:
            first = client.get_push_feed(["TESTCO"], include_market=False)
            second = client.get_push_feed(["TESTCO"], include_market=False)
            self.assertEqual(client.search.calls[0][1], "Test Company Limited")
        card = first.priority_digest[0]
        self.assertEqual(first.telemetry.tier1_calls, 2)
        self.assertEqual(second.telemetry.tier1_calls, 0)
        self.assertEqual([e.effective_on for e in card.executive_transitions], [date(2026, 11, 1), date(2026, 10, 31)])
        self.assertIsNone(card.management_bio_enrichment.effective_on)
        self.assertEqual(card.management_bio_enrichment.incoming[0].name, "Anita Rao")
        self.assertTrue(card.management_bio_enrichment.outgoing[0].background)
        self.assertTrue(card.management_bio_enrichment.incoming[0].background[0].is_inference)

    def test_common_date_populated_when_all_dates_match(self):
        row = item("Appointment of CFO", INCOMING + " " + CAREER)
        with engine(self.cache, [row], FakeLLM([executive_payload(), bio_payload()])) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual(feed.priority_digest[0].management_bio_enrichment.effective_on, date(2026, 11, 1))

    def test_hallucinated_name_is_not_exposed(self):
        draft = executive_payload()
        draft["executives"][0]["name"] = "Invented Person"
        row = item("Appointment of CFO", INCOMING)
        with engine(self.cache, [row], FakeLLM([draft])) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertFalse(feed.priority_digest[0].executive_transitions)
        self.assertFalse(feed.priority_digest[0].management_bio_enrichment.incoming)
        self.assertTrue(feed.telemetry.failures)

    def test_effective_date_requires_matching_quote(self):
        draft = executive_payload()
        draft["executives"][0]["effective_on"] = "2026-12-01"
        row = item("Appointment of CFO", INCOMING)
        with engine(self.cache, [row], FakeLLM([draft])) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertFalse(feed.priority_digest[0].executive_transitions)

    def test_failed_bio_retains_identity_and_dates(self):
        row = item("Appointment of CFO", INCOMING)
        with engine(self.cache, [row], FakeLLM([executive_payload(), EvidenceError("No biography")])) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        card = feed.priority_digest[0]
        self.assertEqual(card.executive_transitions[0].name, "Anita Rao")
        self.assertEqual(card.executive_transitions[0].effective_on, date(2026, 11, 1))
        self.assertTrue(card.management_bio_enrichment.incoming[0].missing_background_reason)

    def test_no_names_never_launches_profile_search(self):
        with engine(self.cache, [item("Appointment of CFO", "A CFO transition will be announced separately.")],
                    FakeLLM([{"executives": []}])) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
            self.assertFalse(client.search.calls)
        self.assertTrue(feed.priority_digest[0].data_gaps)

    def test_no_career_details_yields_gap_not_generic_padding(self):
        payload = {"pills": [{"executive_index": 0, "background": None, "why_it_matters": None,
                              "evidence": [], "missing_reason": "Prior roles and expertise were not disclosed"}]}
        with engine(self.cache, [item("Appointment of CFO", INCOMING)], FakeLLM([executive_payload(), payload])) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertFalse(feed.priority_digest[0].management_bio_enrichment.incoming[0].background)

    def test_hallucinated_career_duration_is_rejected(self):
        draft = bio_payload()
        draft["pills"][0]["background"] = "Anita Rao has 99 years of treasury experience at Acme Components."
        with engine(self.cache, [item("Appointment of CFO", INCOMING + " " + CAREER)],
                    FakeLLM([executive_payload(), draft])) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertFalse(feed.priority_digest[0].management_bio_enrichment.incoming[0].background)
        self.assertTrue(feed.telemetry.failures)

    def test_another_executives_career_is_not_attached(self):
        draft = bio_payload()
        draft["pills"][0]["evidence"][0]["quote"] = DEPARTING
        row = item("Appointment of CFO", INCOMING + " " + DEPARTING)
        with engine(self.cache, [row], FakeLLM([executive_payload(), draft])) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertFalse(feed.priority_digest[0].management_bio_enrichment.incoming[0].background)

    def test_search_snippet_reaches_bio_prompt_and_citation_catalog(self):
        class ProfileLLM(FakeLLM):
            def complete(self, route, system, inputs, model):
                if model.__name__ == "_BioResponse":
                    block = next(b for b in inputs["sources"] if b["source_ref"].startswith("search."))
                    draft = bio_payload()
                    draft["pills"][0]["evidence"][0]["source_ref"] = block["source_ref"]
                    self.outputs.append(draft)
                return super().complete(route, system, inputs, model)
        rows = [{"title": "Anita Rao - Test Company Limited", "href": "https://www.linkedin.com/in/anita-rao/", "body": CAREER}]
        llm = ProfileLLM([executive_payload()])
        with engine(self.cache, [item("Appointment of CFO", INCOMING)], llm,
                    search_factory=lambda c: ExecutiveSearch(c, clock=lambda: NOW, environ={},
                        ddgs_factory=ClientTests.ddgs_factory(rows))) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        bio = feed.priority_digest[0].management_bio_enrichment
        self.assertEqual(len(bio.sources), 1)
        self.assertEqual(bio.sources[0].kind, "professional_profile")
        self.assertTrue(any(c.source_id == bio.sources[0].source_id for c in bio.incoming[0].background[0].citations))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.cache = SQLiteCache(":memory:")
        self.addCleanup(self.cache.close)

    @staticmethod
    def ddgs_factory(rows):
        class Client:
            calls = []

            def __init__(self, **kwargs):
                self.options = kwargs

            def text(self, query, **kwargs):
                self.calls.append((query, kwargs))
                return rows
        return Client

    def test_profile_search_requires_name_company_and_approved_domain(self):
        factory = self.ddgs_factory([
            {"title": "Anita Rao - Test Company Limited", "href": "https://www.linkedin.com/in/anita-rao/", "body": CAREER},
            {"title": "Anita Rao - Other Company", "href": "https://www.linkedin.com/in/different-anita/", "body": "Other company profile"},
            {"title": "Anita Rao - Test Company Limited", "href": "https://news.example/story/", "body": CAREER},
            {"title": "Anita Rao - Test Company Limited", "href": "https://www.linkedin.com/company/test/", "body": CAREER},
        ])
        search = ExecutiveSearch(self.cache, clock=lambda: NOW, environ={}, ddgs_factory=factory)
        self.addCleanup(search.close)
        result = search.search("Anita Rao", "Test Company Limited", "TESTCO")
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.hits[0].source.kind, "professional_profile")
        self.assertEqual(result.hits[0].evidence_kind, "search_snippet")
        self.assertIn('"Anita Rao"', result.query)
        self.assertEqual(factory.calls[0][1]["backend"], "duckduckgo")

    def test_issuer_domain_can_be_explicitly_added(self):
        rows = [{"title": "Anita Rao - Test Company Limited", "href": "https://testcompany.example/management/anita", "body": CAREER}]
        search = ExecutiveSearch(self.cache, clock=lambda: NOW, environ={},
            ddgs_factory=self.ddgs_factory(rows), company_domains={"TESTCO": ("testcompany.example",)})
        self.addCleanup(search.close)
        self.assertEqual(len(search.search("Anita Rao", "Test Company Limited", "TESTCO").hits), 1)

    def test_partial_name_and_partial_company_context_are_rejected(self):
        rows = [
            {"title": "Anita Raothakur - Acme Financial Services", "href": "https://www.linkedin.com/in/other-a/", "body": "Treasury background"},
            {"title": "Anita Rao - Other Financial Services", "href": "https://www.linkedin.com/in/other-b/", "body": "Treasury background"},
        ]
        search = ExecutiveSearch(self.cache, clock=lambda: NOW, environ={}, ddgs_factory=self.ddgs_factory(rows))
        self.addCleanup(search.close)
        self.assertFalse(search.search("Anita Rao", "Acme Financial Services Limited", "TESTCO").hits)

    def test_search_cache_avoids_network_and_paid_keys_are_ignored(self):
        class ForbiddenPaidSession:
            def post(self, *args, **kwargs):
                raise AssertionError("No paid search is authorized")
        rows = [{"title": "Anita Rao - Test Company Limited", "href": "https://www.linkedin.com/in/anita-rao/", "body": CAREER}]
        factory = self.ddgs_factory(rows)
        search = ExecutiveSearch(self.cache, clock=lambda: NOW, session=ForbiddenPaidSession(),
            environ={"TAVILY_API_KEY": "synthetic-secret", "SERPER_API_KEY": "synthetic-secret"}, ddgs_factory=factory)
        first = search.search("Anita Rao", "Test Company Limited", "TESTCO")
        second = search.search("Anita Rao", "Test Company Limited", "TESTCO")
        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(first.paid_api_attempts, 0)
        self.assertEqual(first.estimated_search_api_cost_usd, 0.0)
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.attempted_providers, ("ddgs",))
        self.assertNotIn("synthetic-secret", first.model_dump_json())

    def test_paid_search_provider_cannot_be_called_directly(self):
        from backend.engines.push_engine import ProviderUnavailable
        search = ExecutiveSearch(self.cache, clock=lambda: NOW)
        self.addCleanup(search.close)
        for provider in ("tavily", "serper"):
            with self.subTest(provider=provider), self.assertRaises(ProviderUnavailable):
                search._search(provider, "Anita Rao")

    def test_empty_search_is_cached_without_llm(self):
        factory = self.ddgs_factory([])
        search = ExecutiveSearch(self.cache, clock=lambda: NOW, environ={}, ddgs_factory=factory)
        self.addCleanup(search.close)
        first = search.search("Anita Rao", "Test Company Limited", "TESTCO")
        second = search.search("Anita Rao", "Test Company Limited", "TESTCO")
        self.assertFalse(first.hits)
        self.assertTrue(second.cache_hit)
        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(self.cache.telemetry().tier1_calls, 0)

    def test_private_and_credential_urls_do_not_become_profile_evidence(self):
        rows = [{"title": "Anita Rao Test Company Limited", "href": url, "body": CAREER}
                for url in ("https://127.0.0.1/a", "https://secret@www.linkedin.com/in/a", "http://www.linkedin.com/in/a")]
        search = ExecutiveSearch(self.cache, clock=lambda: NOW, environ={}, ddgs_factory=self.ddgs_factory(rows))
        self.addCleanup(search.close)
        self.assertFalse(search.search("Anita Rao", "Test Company Limited", "TESTCO").hits)

    def test_filing_pdf_pages_citations_and_cache(self):
        with fitz.open() as doc:
            for index in range(4):
                page = doc.new_page()
                page.insert_text((72, 72), f"Page {index + 1}. " + INCOMING + " " + CAREER)
            pdf = doc.tobytes()
        session = FakeSession([FakeResponse(body=pdf, content_type="application/pdf")])
        reader = FilingTextClient(self.cache, session=session, clock=lambda: NOW, max_pages=2, url_validator=lambda url: url)
        row = item("Appointment of CFO", "See attached profile", attachment=True)
        first, second = reader.read(row), reader.read(row)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(first.blocks), 3)  # metadata + two pages
        self.assertEqual(first.blocks[-1].citation.page, 2)
        self.assertEqual(first.sources[0].content_hash, second.sources[0].content_hash)
        self.assertTrue(first.gaps)
        self.assertEqual(self.cache.telemetry().tier0_calls, 1)

    def test_html_attachment_is_not_treated_as_filing(self):
        session = FakeSession([FakeResponse(body=b"<html>Access denied</html>", content_type="text/html")])
        reader = FilingTextClient(self.cache, session=session, clock=lambda: NOW, url_validator=lambda url: url)
        with self.assertRaises(Exception):
            reader.read(item("Appointment of CFO", "See attached", attachment=True))

    def test_redirect_is_validated_before_requesting_destination(self):
        redirect = FakeResponse(status=302)
        redirect.headers["Location"] = "http://127.0.0.1/private"
        session = FakeSession([redirect])
        reader = FilingTextClient(self.cache, session=session, clock=lambda: NOW, url_validator=lambda url: url)
        with self.assertRaises(EvidenceError):
            reader.read(item("Appointment of CFO", "Attached", attachment=True))
        self.assertEqual(len(session.calls), 1)

    def test_default_transport_is_gemini(self):
        with PushEngine(self.cache, settings=SETTINGS, clock=lambda: NOW) as client:
            self.assertIsInstance(client.llm.transport, GeminiLLMClient)
            self.assertEqual(client.settings.router.tier1.model, "gemini-3.5-flash-lite")
            self.assertEqual(client.settings.router.tier2.model, "gemini-3.8-flash")


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.cache = SQLiteCache(":memory:")
        self.addCleanup(self.cache.close)

    def test_ambiguous_material_event_gets_tier1_then_takeaway(self):
        description = "The Board approved an acquisition of a subsidiary and the quarterly financial results."
        row = item("Outcome of Board meeting", description)
        self.assertTrue(row.requires_llm_classification)
        classification = {"category": "M&A", "confidence": 0.99,
            "evidence": {"source_ref": "filing.metadata", "quote": description}}
        takeaway = {"text": "Subsidiary acquisition changes the group perimeter; transaction economics remain undisclosed.",
            "evidence": {"source_ref": "filing.metadata", "quote": description}}
        with engine(self.cache, [row], FakeLLM([classification, takeaway])) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertEqual(feed.telemetry.tier1_calls, 2)
        self.assertEqual(feed.priority_digest[0].category, Cat.M_AND_A)
        self.assertEqual(feed.priority_digest[0].announcement.classification_tier, 1)
        self.assertEqual(len(feed.triggered_alerts), 1)

    def test_uncertain_classification_cannot_trigger_category_alert(self):
        description = "An acquisition and quarterly financial results were discussed."
        row = item("Board outcome", description)
        classification = {"category": "M&A", "confidence": 0.7,
            "evidence": {"source_ref": "filing.metadata", "quote": description}}
        with engine(self.cache, [row], FakeLLM([classification])) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertFalse(feed.triggered_alerts)
        self.assertTrue(feed.priority_digest[0].announcement.requires_llm_classification)

    def test_scheduled_results_and_future_period_do_not_trigger_results_alerts(self):
        rows = [item("Financial Results", "Board meeting intimation to consider and approve financial results for the quarter ended September 30, 2026.", identity="scheduled"),
                item("Financial Results", "Quarter ending December 31, 2026.", identity="future")]
        with engine(self.cache, rows) as client:
            feed = client.get_push_feed(["TESTCO"], include_market=False)
        self.assertFalse(feed.triggered_alerts)
        self.assertEqual(len(feed.priority_digest), 2)


if __name__ == "__main__":
    unittest.main()
