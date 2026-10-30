"""Live, credential-safe backend checks, with optional Gemini canary checks.

    python -m backend.validate --tickers RELIANCE TCS HDFCBANK HAL --llm
    python -m backend.validate --llm --output validation.json

Default symbols cover the full portfolio plus the ad-hoc demo list. Source
checks use isolated in-memory caching. No synthetic filing can pass as live.
The LLM canary is explicitly a protocol test, never company research. A model
route passes only after a real complete, grounded response and reported usage.
"""
from __future__ import annotations

import argparse
import math
import random
import time
from contextlib import ExitStack
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

import requests
from pydantic import Field, ValidationError

from .cache import SQLiteCache
from .config import TICKER_RENAME_SOURCES, ModelRoute, Settings, get_settings, normalize_ticker
from .data.announcements import NSEAnnouncementsClient
from .data.fundamentals import CompanyFundamentals, FundamentalsClient
from .data.market_data import TRADING_SESSIONS, MarketDataClient, MarketDataError, evaluate_eod_triggers
from .engines.push_engine import GeminiLLMClient, LLMCompletion, LLMHTTPError, describe_llm_error
from .schemas import EngineTelemetry, EODMarketMetric, StrictModel, utc_now


class ValidationCheck(StrictModel):
    component: str
    ticker: str | None = None
    status: Literal["passed", "warning", "failed", "unverified"]
    detail: str
    observations: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(StrictModel):
    checked_at: datetime
    requested_tickers: tuple[str, ...]
    resolved_tickers: tuple[str, ...]
    checks: tuple[ValidationCheck, ...]
    telemetry: EngineTelemetry
    live_llm_requested: bool
    note: str = ("Passed source parsing does not prove every disclosure is available. Model canaries verify access, "
        "request/response schema, copying, routing and usage; they do not certify all research-language entailment.")


class _Probe(StrictModel):
    nonce: str
    exact_quote: str
    value: float
    period_end: date


class _ScopedCache:
    def __init__(self, base: SQLiteCache, request_id: str):
        self.base, self.request_id = base, request_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("request_id", self.request_id)
        return self.base.get(*args, **kwargs)

    def track(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("request_id", self.request_id)
        return self.base.track(*args, **kwargs)


def _equal(actual: float | None, expected: Decimal | None, label: str) -> None:
    if expected is None:
        if actual is not None:
            raise ValueError(label + " should be unavailable")
    elif actual is None or not math.isclose(actual, float(expected), rel_tol=1e-11, abs_tol=1e-9):
        raise ValueError(label + " disagrees with independent Decimal calculation")


def audit_market(metric: EODMarketMetric) -> dict[str, Any]:
    metric = EODMarketMetric.model_validate(metric)
    close = Decimal(str(metric.close_inr))
    for label, base, actual in (("1D", metric.previous_close_inr, metric.price_change_pct),
                              ("5D", metric.close_5_sessions_ago_inr, metric.price_change_5d_pct)):
        expected = (close / Decimal(str(base)) - 1) * 100 if base is not None else None
        _equal(actual, expected, label)
    volumes = metric.prior_20d_volumes
    average = sum(map(Decimal, volumes), Decimal(0)) / 20 if volumes is not None else None
    _equal(metric.average_volume_20d, average, "20D volume")
    _equal(metric.volume_spike_ratio, Decimal(metric.volume) / average if average else None, "volume multiple")
    alerts = evaluate_eod_triggers(metric, 3.0, 2.0)
    expected_count = int(metric.price_change_pct is not None and abs(metric.price_change_pct) >= 3)
    expected_count += int(metric.volume_spike_ratio is not None and metric.volume_spike_ratio >= 2)
    if len(alerts) != expected_count:
        raise ValueError("Threshold evaluation disagrees with observed metrics")
    return {"session_date": str(metric.session_date), "provider_symbol": metric.provider_symbol,
            "close_inr": metric.close_inr, "change_1d_pct": metric.price_change_pct,
            "change_5d_pct": metric.price_change_5d_pct, "average_volume_20d": metric.average_volume_20d,
            "volume_spike_ratio": metric.volume_spike_ratio, "alerts": len(alerts)}


def audit_fundamentals(company: CompanyFundamentals) -> dict[str, Any]:
    company = CompanyFundamentals.model_validate(company)
    headline = company.latest_quarter_headlines
    if headline:
        for row in headline.headline_rows:
            for label, base, actual in (("QoQ", row.previous_quarter_inr_crore, row.qoq.value_pct),
                                       ("YoY", row.year_ago_inr_crore, row.yoy.value_pct)):
                expected = ((Decimal(str(row.current_inr_crore)) / Decimal(str(base)) - 1) * 100
                    if row.current_inr_crore is not None and base is not None and base > 0 else None)
                _equal(actual, expected, row.metric + " " + label)
        snapshot = headline.current
        revenue = Decimal(str(snapshot.revenue.value)) if snapshot.revenue.value is not None else None
        for metric, actual in ((snapshot.ebitda.value, snapshot.ebitda_margin_pct), (snapshot.pat.value, snapshot.pat_margin_pct)):
            _equal(actual, Decimal(str(metric)) / revenue * 100 if metric is not None and revenue and revenue > 0 else None, "margin")
    for year in company.cash_conversion:
        cfo, pat, ebitda = year.cfo.value, year.pat.value, year.ebitda.value
        for label, base, actual in (("CFO/PAT", pat, year.cfo_to_pat), ("CFO/EBITDA", ebitda, year.cfo_to_ebitda)):
            _equal(actual, Decimal(str(cfo)) / Decimal(str(base)) if cfo is not None and base is not None and base > 0 else None, label)
    summary = company.three_year_cash_conversion
    if summary.cumulative_cfo_to_pat is not None:
        _equal(summary.cumulative_cfo_to_pat, Decimal(str(summary.cumulative_cfo)) / Decimal(str(summary.cumulative_pat)), "3Y CFO/PAT")
        if summary.red_alert != (Decimal(str(summary.cumulative_cfo)) < Decimal("0.7") * Decimal(str(summary.cumulative_pat))):
            raise ValueError("Three-year cash warning has the wrong threshold")
    for year in company.leverage:
        debt, capital, reserves = year.debt.value, year.share_capital.value, year.reserves.value
        equity = Decimal(str(capital)) + Decimal(str(reserves)) if capital is not None and reserves is not None else None
        _equal(year.debt_to_equity, Decimal(str(debt)) / equity if debt is not None and equity and equity > 0 else None, "D/E")
    return {"basis": company.basis, "quarter_columns": len(company.quarters.columns) if company.quarters else 0,
        "headline_quarter": str(headline.current.period.end_on) if headline else None,
        "ebitda_basis": company.ebitda_basis, "cash_years": len(company.cash_conversion),
        "concall_links": len(company.documents.concall_transcripts), "annual_links": len(company.documents.annual_reports),
        "source_url": str(company.source.url), "reported_gaps": len(company.data_gaps)}


def _safe_error(error: Exception) -> str:
    if isinstance(error, LLMHTTPError):
        return describe_llm_error(error)
    if isinstance(error, MarketDataError):
        # MarketDataClient constructs bounded symbol/error-class diagnostics;
        # it never puts response bodies, request headers or credentials here.
        return str(error)[:500]
    if isinstance(error, ValidationError):
        return "; ".join(".".join(map(str, item["loc"])) + ": " + item["type"]
            for item in error.errors(include_input=False, include_url=False)[:3])
    if type(error) is ValueError:
        return str(error)[:300]
    return type(error).__name__ + "; inspect source availability or the bounded validation checks."


def _canary_contract(tier: int, nonce: str, pull_contracts: bool) -> tuple[str, dict[str, Any], type[StrictModel], Callable[[str], None]]:
    if not pull_contracts:
        quote = "PROTOCOL CANARY ONLY: value 125.5 for the period ended 2026-06-30. This is not company research."
        inputs = {"nonce": nonce, "exact_quote": quote, "value": 125.5, "period_end": "2026-06-30"}
        def verify(text: str) -> None:
            probe = _Probe.model_validate_json(text)
            if (probe.nonce, probe.exact_quote, probe.value, probe.period_end) != (nonce, quote, 125.5, date(2026, 6, 30)):
                raise ValueError("Canary output failed exact grounding")
        return ("Copy only the supplied nonce, exact_quote, value and period_end into the schema. Do not paraphrase or infer. This is a protocol canary.",
                inputs, _Probe, verify)
    from .engines.pull_engine import _Cases, _MapExtraction
    quote = "SYNTHETIC PROTOCOL CANARY " + nonce + ": this is a schema copying test, not company research."
    inputs = {"source_ref": nonce, "exact_quote": quote}
    if tier == 1:
        def verify_map(text: str) -> None:
            probe = _MapExtraction.model_validate_json(text)
            if (len(probe.business) != 1 or probe.guidance or probe.actuals or probe.revisions or probe.rejections):
                raise ValueError("Canary map output failed exact grounding")
            item = probe.business[0]
            if (item.kind != "overview" or item.evidence.source_ref != nonce or item.evidence.quote != quote
                    or any(value is not None for value in (item.segment_name, item.mix_token, item.mix_period))):
                raise ValueError("Canary map output failed exact grounding")
        return ("Synthetic protocol test only. Return exactly ONE business item of kind overview, with evidence copying source_ref and exact_quote verbatim. "
                "All other business fields are null. guidance, actuals, revisions and rejections are empty arrays. Do not add company facts.",
                inputs, _MapExtraction, verify_map)
    def verify_cases(text: str) -> None:
        probe = _Cases.model_validate_json(text)
        if len(probe.bull) != 1 or len(probe.bear) != 1:
            raise ValueError("Canary cases output failed exact grounding")
        for claim in probe.bull + probe.bear:
            if (claim.text != quote or claim.fact_ids or claim.is_inference or len(claim.evidence) != 1
                    or claim.evidence[0].source_ref != nonce or claim.evidence[0].quote != quote):
                raise ValueError("Canary cases output failed exact grounding")
    return ("Synthetic protocol test only, not investment analysis. Return ONE bull item and ONE bear item, both copying exact_quote into text verbatim. "
            "For both, is_inference is false, fact_ids is empty, and evidence contains ONE item copying source_ref and exact_quote into quote verbatim.",
            inputs, _Cases, verify_cases)


def _validate_canary(transport: Any, scoped: _ScopedCache, route: ModelRoute,
                     tier: int, ticker: str, *, pull_contracts: bool = False) -> ValidationCheck:
    """Honor the route's explicit retry budget and record every API attempt."""
    nonce = uuid4().hex
    instruction, inputs, output_model, verify = _canary_contract(tier, nonce, pull_contracts)
    attempts: list[dict[str, Any]] = []
    observations: dict[str, Any] = {"model": route.model, "timeout_seconds": route.timeout_seconds,
        "max_retries": route.max_retries, "api_calls": 0, "attempts": attempts,
        "contract_model": output_model.__name__, "probe_kind": "pull_contract" if pull_contracts else "copy"}
    last_error: Exception | None = None
    for attempt in range(route.max_retries + 1):
        started = time.perf_counter()
        try:
            with scoped.track("validate.gemini", ticker=ticker, tier=tier, route=route) as usage:
                response = LLMCompletion.model_validate(transport.complete(route, instruction, inputs, output_model))
                if response.usage:
                    usage.set_tokens(**response.usage.model_dump())
                if response.finish_reason != "stop" or response.text is None:
                    raise ValueError("Incomplete canary response")
                verify(response.text)
            attempts.append({"attempt": attempt + 1, "status": "passed",
                "latency_ms": (time.perf_counter() - started) * 1000})
            observations.update(api_calls=len(attempts), usage_reported=response.usage is not None)
            return ValidationCheck(component=f"gemini_tier_{tier}",
                status="passed" if response.usage else "warning",
                detail=("Credentialed request completed; actual PULL output schema and exact source copying verified."
                        if pull_contracts else "Credentialed request completed; schema, nonce, exact text, date and numeric copying verified."),
                observations=observations)
        except Exception as error:
            last_error = error
            retryable = (isinstance(error, LLMHTTPError) and error.status_code in {500, 502, 503, 504}
                         or isinstance(error, (requests.Timeout, requests.ConnectionError, TimeoutError)))
            record: dict[str, Any] = {"attempt": attempt + 1, "status": "failed",
                "error": _safe_error(error), "retryable": retryable,
                "latency_ms": (time.perf_counter() - started) * 1000}
            attempts.append(record)
            observations["api_calls"] = len(attempts)
            if not retryable or attempt >= route.max_retries:
                break
            # SDK retries remain disabled. Explicit backoff is bounded, jittered
            # and separately counted. Quota/auth/schema failures never retry.
            delay = max(2.0 ** attempt, min(30.0, max(0.0, getattr(error, "retry_after", 0))))
            delay += random.uniform(0.0, 0.5)
            record["retry_delay_seconds"] = delay
            time.sleep(delay)
    observations["retry_budget_exhausted"] = bool(attempts[-1]["retryable"])
    return ValidationCheck(component=f"gemini_tier_{tier}", status="failed",
        detail=_safe_error(last_error) if last_error is not None else "Canary did not complete",
        observations=observations)


def validate_backend(tickers: tuple[str, ...] | None = None, *, settings: Settings | None = None,
                     live_llm: bool = False, documents: bool = True, market: bool = True,
                     announcements: bool = True, timeout_seconds: float = 12.0,
                     cache: SQLiteCache | None = None, clock: Callable[[], datetime] = utc_now,
                     fundamentals_factory: Callable[[Any], Any] | None = None,
                     market_factory: Callable[[Any], Any] | None = None,
                     announcements_factory: Callable[[Any], Any] | None = None,
                     llm_transport: Any = None, pull_contracts: bool = False) -> ValidationReport:
    settings = settings or get_settings()
    requested = settings.portfolio + settings.ad_hoc_demo_tickers if tickers is None else tickers
    if isinstance(requested, (str, bytes)):
        raise ValueError("tickers must be a tuple of symbols")
    symbols = tuple(dict.fromkeys(normalize_ticker(ticker) for ticker in requested))
    if not symbols or isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Supply symbols and a finite positive timeout")
    checks = []
    with ExitStack() as resources:
        base = cache if cache is not None else resources.enter_context(SQLiteCache(":memory:"))
        scoped = _ScopedCache(base, "validate." + uuid4().hex)
        def make(factory: Callable[[Any], Any] | None, default: Callable[[Any], Any]) -> Any:
            client = (factory or default)(scoped)
            close = getattr(client, "close", None)
            if close:
                resources.callback(close)
            return client
        fundamentals = make(fundamentals_factory, lambda c: FundamentalsClient(c, clock=clock, timeout_seconds=timeout_seconds))
        market_client = make(market_factory, lambda c: MarketDataClient(c, clock=clock, timeout_seconds=timeout_seconds)) if market else None
        nse = make(announcements_factory, lambda c: NSEAnnouncementsClient(c, clock=clock,
            connect_timeout_seconds=min(timeout_seconds, 4), read_timeout_seconds=timeout_seconds)) if announcements else None
        for original in requested:
            plain = original.strip().upper().removesuffix(".NS")
            if plain in TICKER_RENAME_SOURCES:
                checks.append(ValidationCheck(component="symbol_resolution", ticker=normalize_ticker(original), status="warning",
                    detail=f"{plain} resolves to the exchange-confirmed current symbol; TMCV is a separate security.",
                    observations={"requested": original, "source": TICKER_RENAME_SOURCES[plain]}))
        for ticker in symbols:
            if market_client is not None:
                try:
                    snapshot = market_client.fetch_snapshot(ticker)
                    if snapshot.metrics.ticker != ticker:
                        raise ValueError("Market issuer mismatch")
                    observations = audit_market(snapshot.metrics)
                    observations["bars"] = len(snapshot.bars)
                    observations["required_bars"] = TRADING_SESSIONS
                    observations["missing_metrics"] = [name for name, value in (
                        ("change_1d_pct", snapshot.metrics.price_change_pct),
                        ("change_5d_pct", snapshot.metrics.price_change_5d_pct),
                        ("average_volume_20d", snapshot.metrics.average_volume_20d),
                        ("volume_spike_ratio", snapshot.metrics.volume_spike_ratio)) if value is None]
                    complete = len(snapshot.bars) >= TRADING_SESSIONS and not observations["missing_metrics"]
                    checks.append(ValidationCheck(component="market", ticker=ticker,
                        status="passed" if complete else "unverified",
                        detail=("Completed bars, independent metrics and inclusive alert thresholds agree." if complete else
                            "Incomplete market history or unavailable EOD metrics; available arithmetic agrees, but the full market check cannot pass."),
                        observations=observations))
                except Exception as error:
                    checks.append(ValidationCheck(component="market", ticker=ticker, status="unverified", detail=_safe_error(error)))
            if nse is not None:
                try:
                    batch = nse.fetch(ticker)
                    checks.append(ValidationCheck(component="announcements", ticker=ticker,
                        status="passed" if batch.source_mode == "live" and not batch.invalid_rows else "warning",
                        detail="Live exchange payload normalized." if batch.source_mode == "live" else "Live NSE unavailable; fallback source mode is explicitly labeled.",
                        observations={"source_mode": batch.source_mode, "filings": len(batch.announcements), "invalid_rows": batch.invalid_rows,
                                      "fallback_reason": batch.fallback_reason}))
                except Exception as error:
                    checks.append(ValidationCheck(component="announcements", ticker=ticker, status="unverified", detail=_safe_error(error)))
            try:
                company = CompanyFundamentals.model_validate(fundamentals.fetch(ticker, include_transcripts=False))
                if company.ticker != ticker:
                    raise ValueError("Issuer mismatch")
                observations = audit_fundamentals(company)
                checks.append(ValidationCheck(component="fundamentals", ticker=ticker,
                    status="passed" if company.latest_quarter_headlines else "warning",
                    detail="Live tables parsed; independent growth, margins, cash conversion and leverage agree.", observations=observations))
            except Exception as error:
                checks.append(ValidationCheck(component="fundamentals", ticker=ticker, status="unverified", detail=_safe_error(error)))
                continue
            if documents:
                selected = company.documents.concall_transcripts[:1] + company.documents.annual_reports[:1]
                if not selected:
                    checks.append(ValidationCheck(component="primary_documents", ticker=ticker, status="warning", detail="No primary PDF links were listed."))
                for document in selected:
                    try:
                        extraction = fundamentals.fetch_transcript(document, max_pages=3)
                        usable = extraction.status in {"ok", "partial"} and bool(extraction.pages) and not extraction.listing_period_mismatch
                        status = "passed" if usable else "unverified" if extraction.status == "error" else "warning"
                        checks.append(ValidationCheck(component=document.kind, ticker=ticker, status=status,
                            detail=("Primary PDF text extracted; this is a three-page availability check, not a full research review."
                                    if usable else "Primary PDF text was unavailable or inconsistent; this document is not verified."),
                            observations={"url": str(document.url), "pages_checked": len(extraction.pages), "total_pages": extraction.total_pages,
                                          "status": extraction.status, "listing_mismatch": extraction.listing_period_mismatch}))
                    except Exception as error:
                        checks.append(ValidationCheck(component=document.kind, ticker=ticker, status="unverified", detail=_safe_error(error)))
        transport = llm_transport or GeminiLLMClient(free_tier_project_confirmed=settings.router.free_tier_project_confirmed,
            min_request_interval_seconds=settings.router.gemini_min_request_interval_seconds)
        if llm_transport is None:
            resources.callback(transport.close)
        for tier, route in ((1, settings.router.tier1), (2, settings.router.tier2)):
            component = f"gemini_tier_{tier}"
            if not live_llm or not settings.router.free_tier_project_confirmed or not transport.available(route):
                checks.append(ValidationCheck(component=component, status="unverified",
                    detail="No credentialed canary executed. Use --llm with GEMINI_API_KEY and a confirmed billing-disabled project.",
                    observations={"model": route.model, "api_calls": 0}))
                continue
            checks.append(_validate_canary(transport, scoped, route, tier, symbols[0], pull_contracts=pull_contracts))
        return ValidationReport(checked_at=clock(), requested_tickers=tuple(requested), resolved_tickers=symbols,
            checks=tuple(checks), telemetry=base.telemetry(request_id=scoped.request_id), live_llm_requested=live_llm)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="+")
    parser.add_argument("--llm", action="store_true", help="check each available free model tier with explicit route-bounded retries")
    parser.add_argument("--pull-contracts", action="store_true", help="test real PULL mapping/case output schemas instead of the small copying schema; requires --llm")
    parser.add_argument("--skip-documents", action="store_true")
    parser.add_argument("--skip-market", action="store_true")
    parser.add_argument("--skip-announcements", action="store_true")
    parser.add_argument("--timeout", type=float, default=12)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.pull_contracts and not args.llm:
        parser.error("--pull-contracts requires --llm")
    try:
        report = validate_backend(tuple(args.tickers) if args.tickers else None, live_llm=args.llm,
            documents=not args.skip_documents, market=not args.skip_market, announcements=not args.skip_announcements,
            timeout_seconds=args.timeout, pull_contracts=args.pull_contracts)
        serialized = report.model_dump_json(indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized + "\n", encoding="utf-8")
        else:
            print(serialized)
        if any(c.status == "failed" or c.status == "unverified" and (args.llm or not c.component.startswith("gemini_")) for c in report.checks):
            parser.exit(2)
    except (ValueError, OSError) as error:
        parser.exit(1, type(error).__name__ + ": validation could not complete.\n")


if __name__ == "__main__":
    main()
