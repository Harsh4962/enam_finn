"""Pillar 1: completed daily Yahoo bars, NSE first and BSE fallback.

    python -m backend.data.market_data RELIANCE --price-threshold 5 --volume-threshold 2

Fetch a three-month buffer, then retain the last 35 actual observations. Do not
invent trading dates or fill missing prices/volume with zero. Cache a request
pointer and its content-addressed snapshot for six hours. The request key
changes when today's EOD becomes eligible, even if an intraday cache is fresh.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, Callable, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from pydantic import Field, model_validator

from ..cache import CacheKey, SQLiteCache
from ..config import MARKET_TIMEZONE, AlertThresholds, get_settings, normalize_ticker
from ..schemas import Count, EODMarketMetric, Positive, ShortText, SourceDocument, StrictModel, TriggeredAlert, utc_now

ADAPTER_VERSION = "market-v2"
CACHE_TTL_SECONDS = 6 * 60 * 60
TRADING_SESSIONS = 35
HISTORY_PERIOD = "3mo"
_IST = ZoneInfo(MARKET_TIMEZONE)
# Conservative availability time, after the normal 15:30 close and closing /
# trade-modification window. Override for exchange special sessions if needed.
DEFAULT_EOD_READY_TIME = time(16, 15)


class MarketDataError(RuntimeError):
    """Both exchange symbols failed or required source data was invalid."""


class DailyMarketBar(StrictModel):
    session_date: date
    close_inr: Positive | None
    volume: Count | None
    dividend_inr: Annotated[float, Field(ge=0)] = 0.0
    stock_split_ratio: Annotated[float, Field(ge=0)] = 0.0


class MarketDataSnapshot(StrictModel):
    metrics: EODMarketMetric
    bars: Annotated[tuple[DailyMarketBar, ...], Field(min_length=1, max_length=35)]
    provider_symbol: ShortText
    market_exchange: Literal["NSE", "BSE"]
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def consistent_snapshot(self) -> MarketDataSnapshot:
        if self.metrics.session_date != self.bars[-1].session_date:
            raise ValueError("metrics must describe the last retained daily bar")
        if any(a.session_date >= b.session_date for a, b in zip(self.bars, self.bars[1:])):
            raise ValueError("daily bars must be chronological and unique")
        return self


def _aware_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _positive_or_missing(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _volume_or_missing(value: Any) -> int | None:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number != number.to_integral_value() or number > 2**63 - 1:
            return None
        return int(number)
    except (ValueError, TypeError, ArithmeticError):
        return None


def _action(value: Any) -> float:
    # Yahoo omits these columns/values when there is no action; unlike missing
    # volume, an absent optional action does not mean zero trading activity.
    return _positive_or_missing(value) or 0.0


class MarketDataClient:
    def __init__(
        self, cache: SQLiteCache | None = None, *,
        ticker_factory: Callable[[str], Any] = yf.Ticker,
        clock: Callable[[], datetime] = utc_now,
        timeout_seconds: float = 12.0,
        eod_ready_time: time = DEFAULT_EOD_READY_TIME,
    ) -> None:
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self.cache = cache if cache is not None else SQLiteCache(settings=get_settings().cache)
        self._owns_cache = cache is None
        self._ticker_factory = ticker_factory
        self._clock = clock
        self.timeout_seconds = timeout_seconds
        self.eod_ready_time = eod_ready_time

    def __enter__(self) -> MarketDataClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_cache:
            self.cache.close()

    def _completed_through(self, now: datetime) -> date:
        local = now.astimezone(_IST)
        return local.date() if local.time() >= self.eod_ready_time else local.date() - timedelta(days=1)

    def _bars(self, frame: pd.DataFrame, completed_through: date) -> tuple[DailyMarketBar, ...]:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            raise MarketDataError("Yahoo returned no daily bars")
        if isinstance(frame.columns, pd.MultiIndex) or not {"Close", "Volume"}.issubset(frame.columns):
            raise MarketDataError("expected single-symbol Yahoo Close and Volume columns")
        if not isinstance(frame.index, pd.DatetimeIndex):
            raise MarketDataError("Yahoo daily bars need a DatetimeIndex")
        by_date: dict[date, DailyMarketBar] = {}
        for stamp, row in frame.sort_index().iterrows():
            if pd.isna(stamp):
                continue
            session_date = stamp.tz_convert(_IST).date() if stamp.tzinfo is not None else stamp.date()
            if session_date > completed_through:
                continue
            by_date[session_date] = DailyMarketBar(
                session_date=session_date, close_inr=_positive_or_missing(row["Close"]),
                volume=_volume_or_missing(row["Volume"]), dividend_inr=_action(row.get("Dividends")),
                stock_split_ratio=_action(row.get("Stock Splits")),
            )
        bars = tuple(by_date[day] for day in sorted(by_date))[-TRADING_SESSIONS:]
        if not bars or bars[-1].close_inr is None or bars[-1].volume is None:
            raise MarketDataError("latest completed bar lacks a valid close or volume")
        return bars

    def _snapshot(self, ticker: str, symbol: str, bars: tuple[DailyMarketBar, ...], now: datetime) -> MarketDataSnapshot:
        latest = bars[-1]
        previous = bars[-2] if len(bars) >= 2 else None
        five_ago = bars[-6] if len(bars) >= 6 else None
        previous_valid = previous is not None and previous.close_inr is not None
        five_valid = five_ago is not None and five_ago.close_inr is not None
        volume_bars = bars[-21:-1] if len(bars) >= 21 else ()
        valid_volume_window = len(volume_bars) == 20 and all(bar.volume is not None for bar in volume_bars)
        raw = [bar.model_dump(mode="json", round_trip=True) for bar in bars]
        serialized = json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        exchange = "NSE" if symbol.endswith(".NS") else "BSE"
        source = SourceDocument.model_validate_json(json.dumps({
            "source_id": "yahoo." + hashlib.sha256(serialized).hexdigest()[:32],
            "ticker": ticker, "kind": "market_data", "title": f"Yahoo daily EOD table: {symbol}",
            "publisher": "Yahoo Finance via yfinance", "url": f"https://finance.yahoo.com/quote/{quote(symbol, safe='')}/history/",
            "published_on": latest.session_date.isoformat(), "retrieved_at": now.isoformat(),
            "content_hash": hashlib.sha256(serialized).hexdigest(),
        }))
        has_split = any(bar.stock_split_ratio for bar in bars[-21:])
        metrics = EODMarketMetric(
            ticker=ticker, session_date=latest.session_date, close_inr=latest.close_inr,
            previous_close_inr=previous.close_inr if previous_valid else None,
            previous_session_date=previous.session_date if previous_valid else None,
            previous_close_missing_reason=None if previous_valid else "Previous session close unavailable",
            close_5_sessions_ago_inr=five_ago.close_inr if five_valid else None,
            five_day_baseline_date=five_ago.session_date if five_valid else None,
            five_day_missing_reason=None if five_valid else "Need six valid session closes for a five-session comparison",
            provider_symbol=symbol, market_exchange=exchange, volume=latest.volume,
            prior_20d_volumes=tuple(bar.volume for bar in volume_bars) if valid_volume_window else None,
            volume_window_end=previous.session_date if valid_volume_window else None,
            volume_history_missing_reason=None if valid_volume_window else "Need all preceding 20 session volumes; missing values are not zero",
            price_basis="raw", corporate_action_note=(
                "Split reported in the 20-session window; verify price/volume comparability."
                if has_split else "Yahoo Close with auto_adjust=False; no FINN dividend or split repair."
            ), source=source,
        )
        notes = (f"Only {len(bars)} completed sessions available; no synthetic history filled",) if len(bars) < 35 else ()
        return MarketDataSnapshot(metrics=metrics, bars=bars, provider_symbol=symbol, market_exchange=exchange, notes=notes)

    def fetch_snapshot(self, ticker: str, *, force_refresh: bool = False) -> MarketDataSnapshot:
        ticker = normalize_ticker(ticker)
        now = _aware_now(self._clock())
        completed_through = self._completed_through(now)
        request_key = CacheKey.for_content(ticker, "request.market_eod", {
            "version": ADAPTER_VERSION, "completed_through": completed_through.isoformat(),
            "sessions": TRADING_SESSIONS, "period": HISTORY_PERIOD, "auto_adjust": False, "repair": False,
        })
        if not force_refresh:
            pointer = self.cache.get(request_key)
            if pointer.hit and isinstance(pointer.value, dict) and isinstance(pointer.value.get("content_hash"), str):
                try:
                    stored_key = CacheKey(ticker, "market.eod_snapshot", pointer.value["content_hash"])
                    stored = self.cache.get(stored_key, model=MarketDataSnapshot)
                    if stored.hit:
                        return stored.value
                except ValueError:
                    self.cache.delete(request_key)
        failures: list[str] = []
        for suffix in (".NS", ".BO"):
            symbol = ticker + suffix
            try:
                with self.cache.track("market.fetch", ticker=ticker):
                    try:
                        frame = self._ticker_factory(symbol).history(
                            # The supported range query avoids empty/truncated
                            # explicit-date responses observed for NSE symbols.
                            # Completed-session filtering still happens locally.
                            period=HISTORY_PERIOD, interval="1d", auto_adjust=False, back_adjust=False,
                            actions=True, repair=False, keepna=True, timeout=self.timeout_seconds,
                        )
                    except Exception as error:
                        raise MarketDataError(f"Yahoo download failed: {type(error).__name__}") from error
                with self.cache.track("market.metrics", ticker=ticker, tier=0):
                    bars = self._bars(frame, completed_through)
                    snapshot = self._snapshot(ticker, symbol, bars, now)
            except (MarketDataError, ValueError, TypeError, KeyError) as error:
                reason = str(error) if isinstance(error, MarketDataError) else type(error).__name__
                failures.append(f"{symbol}: {reason}")
                continue
            # Cache/storage failures are intentionally outside the provider
            # fallback handler; a broken database is not a missing exchange.
            key = CacheKey.for_content(ticker, "market.eod_snapshot", snapshot,
                                      context={"version": ADAPTER_VERSION, "symbol": symbol})
            self.cache.put(key, snapshot, ttl_seconds=CACHE_TTL_SECONDS, metadata={"provider_symbol": symbol})
            self.cache.put(request_key, {"content_hash": key.content_hash}, ttl_seconds=CACHE_TTL_SECONDS)
            return snapshot
        raise MarketDataError("No usable EOD data from NSE/BSE Yahoo symbols: " + "; ".join(failures))

    def fetch(self, ticker: str, *, force_refresh: bool = False) -> EODMarketMetric:
        return self.fetch_snapshot(ticker, force_refresh=force_refresh).metrics


def fetch_market_data(ticker: str, *, cache: SQLiteCache | None = None, force_refresh: bool = False) -> EODMarketMetric:
    with MarketDataClient(cache) as client:
        return client.fetch(ticker, force_refresh=force_refresh)


def evaluate_eod_triggers(
    ticker_metrics: EODMarketMetric, price_threshold_pct: float = 5.0,
    volume_multiplier_threshold: float = 2.0,
) -> list[TriggeredAlert]:
    """Inclusive boundaries: abs(1D change) >= X and current/prior-20D >= K."""
    ticker_metrics = EODMarketMetric.model_validate(ticker_metrics)
    thresholds = AlertThresholds(price_move_pct=price_threshold_pct, volume_multiplier=volume_multiplier_threshold)
    alerts = []
    for kind, value, threshold, title in (
        ("price_move", ticker_metrics.price_change_pct, thresholds.price_move_pct, "EOD price move"),
        ("volume_spike", ticker_metrics.volume_spike_ratio, thresholds.volume_multiplier, "EOD volume spike"),
    ):
        observed = abs(value) if kind == "price_move" and value is not None else value
        if observed is None or observed < threshold:
            continue
        identity = f"{ticker_metrics.ticker}|{ticker_metrics.session_date}|{kind}|{threshold}|gte-v1"
        alerts.append(TriggeredAlert(
            alert_id="eod." + hashlib.sha256(identity.encode()).hexdigest()[:32],
            ticker=ticker_metrics.ticker, kind=kind, priority=3, title=f"{ticker_metrics.ticker}: {title}",
            market_metric=ticker_metrics, threshold=threshold,
        ))
    return alerts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker")
    parser.add_argument("--price-threshold", type=float, default=5.0)
    parser.add_argument("--volume-threshold", type=float, default=2.0)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    try:
        with MarketDataClient() as client:
            snapshot = client.fetch_snapshot(args.ticker, force_refresh=args.refresh)
        alerts = evaluate_eod_triggers(snapshot.metrics, args.price_threshold, args.volume_threshold)
        print(json.dumps({"snapshot": snapshot.model_dump(mode="json"),
                          "alerts": [a.model_dump(mode="json") for a in alerts]}, indent=2, allow_nan=False))
    except (MarketDataError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
