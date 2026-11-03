"""Typed adapters for permitted public market data.

Source: Downstox free public API (https://downstox.com/api-docs) - free,
unauthenticated, read-only JSON endpoints documented for machine callers,
with an OpenAPI 3.1 description at /openapi.json. It is a third-party source,
not an official exchange feed: no published rate-limit or freshness guarantee,
and it is not SEBI-registered investment advice. Every model here therefore
carries provenance (source, source_url, fetched_at) and the upstream
freshness fields (quoted_at, stale, per-source ages/withheld flags) instead of
implying realtime official data. Fixtures make the whole layer testable
offline; network failures raise MarketDataError, never fabricated data.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import GuidanceStatement

BASE_URL = "https://downstox.com"
SOURCE_NAME = "downstox"


class MarketDataError(RuntimeError):
    """Raised when upstream data cannot be fetched or validated. Callers must
    surface this honestly (abstain) rather than invent numbers."""


class LiveQuote(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    ltp: float
    previous_close: float = Field(alias="previousClose")
    change_pct: float = Field(alias="changePct")
    open: float
    high: float
    low: float
    volume: int
    quoted_at: datetime = Field(alias="quotedAt")
    stale: bool


class Fundamentals(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    cmp: Optional[float] = None
    pe: Optional[float] = None
    roe: Optional[float] = None
    roce: Optional[float] = None
    div_yield: Optional[float] = Field(default=None, alias="divYield")
    mcap_cr: Optional[float] = Field(default=None, alias="mcap")
    updated_at: Optional[datetime] = Field(default=None, alias="updatedAt")


class StockSnapshot(BaseModel):
    symbol: str
    name: str
    exchange: str
    sector: Optional[str] = None
    live: Optional[LiveQuote] = None
    fundamentals: Optional[Fundamentals] = None
    fetched_at: datetime
    source: str = SOURCE_NAME
    source_url: str


class AnnouncementItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    exchange: str
    symbol: str
    company: str
    subject: str
    category: Optional[str] = None
    published_at: datetime = Field(alias="publishedAt")
    pdf_url: Optional[str] = Field(default=None, alias="pdfUrl")

    @field_validator("pdf_url")
    @classmethod
    def _https_only(cls, v: Optional[str]) -> Optional[str]:
        # Upstream content is untrusted: only plain https links survive; odd
        # schemes (javascript:, data:, file:) are dropped, never followed.
        if v is not None and not v.startswith("https://"):
            return None
        return v


class SourceHealth(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str
    status: str  # e.g. "ok" / "failed"
    newest_at: Optional[datetime] = Field(default=None, alias="newestAt")
    withheld: bool = False


class AnnouncementFeed(BaseModel):
    """Merged NSE+BSE announcement feed with per-source health, so callers can
    abstain honestly when one exchange's feed failed instead of implying 'no
    announcements'."""
    items: list[AnnouncementItem]
    generated_at: datetime
    sources: list[SourceHealth]

    def degraded_sources(self) -> list[SourceHealth]:
        return [s for s in self.sources if s.status != "ok" or s.withheld]

    def coverage_note(self) -> str:
        bad = self.degraded_sources()
        if not bad:
            return "Both exchange feeds healthy at fetch time."
        parts = ", ".join(f"{s.name.upper()} {s.status}" + (" (withheld)" if s.withheld else "") for s in bad)
        return f"Partial coverage: {parts}. Absence of an item here does not prove no filing exists."


class PromiseTarget(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    raw: str
    parsed: bool = False
    comparator: Optional[str] = None  # range | at_least | at_most | point | approx | ...
    value: Optional[float] = None
    lo: Optional[float] = None
    hi: Optional[float] = None
    unit: Optional[str] = None
    hedge: Optional[str] = None
    basis: Optional[str] = None


class PromiseHorizon(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    raw: str = ""
    kind: Optional[str] = None
    scoreable: bool = False
    reason: Optional[str] = None


class ManagementPromise(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str
    symbol: str
    company: str
    quote: str
    metric: str
    target_raw: str = Field(alias="target")
    timeframe: str = ""
    said_on: datetime = Field(alias="saidOn")
    source_url: str = Field(alias="sourceUrl")
    status: str
    norm_target: Optional[PromiseTarget] = None
    norm_horizon: Optional[PromiseHorizon] = None

    @classmethod
    def from_upstream(cls, raw: dict) -> "ManagementPromise":
        norm = raw.get("norm") or {}
        return cls(
            **{k: v for k, v in raw.items() if k not in ("norm", "normVersion")},
            norm_target=PromiseTarget(**norm["target"]) if norm.get("target") else None,
            norm_horizon=PromiseHorizon(**norm["horizon"]) if norm.get("horizon") else None,
        )


class MarketStatus(BaseModel):
    open: bool
    state: str
    reason: str = ""
    last_trading_day: Optional[str] = None
    next_trading_day: Optional[str] = None
    fetched_at: datetime
    source: str = SOURCE_NAME


class DownstoxClient:
    """Read-only client. fixtures_dir swaps the network for recorded samples,
    keeping tests and offline demos deterministic."""

    def __init__(self, base_url: str = BASE_URL, fixtures_dir: Optional[Path] = None,
                 timeout: float = 20.0, http: Optional[httpx.Client] = None):
        self.base_url = base_url.rstrip("/")
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else None
        self.timeout = timeout
        self._http = http

    def _get(self, path: str, fixture: str) -> dict:
        if self.fixtures_dir is not None:
            f = self.fixtures_dir / fixture
            if not f.exists():
                raise MarketDataError(f"no fixture recorded for {fixture}")
            return json.loads(f.read_text())
        url = f"{self.base_url}{path}"
        try:
            http = self._http or httpx
            resp = http.get(url, timeout=self.timeout)
        except Exception as exc:  # network/DNS/TLS failure
            raise MarketDataError(f"upstream unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise MarketDataError(f"upstream HTTP {resp.status_code} for {url}")
        if len(resp.content) > 5_000_000:  # untrusted third-party payload: hard cap
            raise MarketDataError(f"upstream response too large for {url}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise MarketDataError(f"upstream returned non-JSON for {url}") from exc
        if not payload.get("success", False):
            raise MarketDataError(f"upstream reported failure for {url}")
        return payload

    def get_raw(self, path: str, fixture: str) -> dict:
        """Verbatim upstream JSON for endpoints without a typed model (UI proxy)."""
        return self._get(path, fixture)

    def get_stock(self, symbol: str) -> StockSnapshot:
        sym = symbol.upper()
        payload = self._get(f"/api/stocks/{sym}", f"downstox-stock-{sym}.json")
        stock = payload["stock"]
        return StockSnapshot(
            symbol=stock["symbol"], name=stock["name"], exchange=stock["exchange"],
            sector=payload.get("sector"),
            live=LiveQuote(**payload["live"]) if payload.get("live") else None,
            fundamentals=Fundamentals(**payload["fundamentals"]) if payload.get("fundamentals") else None,
            fetched_at=datetime.now(timezone.utc),
            source_url=f"{self.base_url}/api/stocks/{sym}",
        )

    def get_announcements(self, symbol: Optional[str] = None, limit: int = 20) -> AnnouncementFeed:
        if symbol:
            payload = self._get(f"/api/announcements/?symbol={symbol.upper()}&limit={limit}",
                                f"downstox-ann-{symbol.upper()}.json")
        else:
            payload = self._get(f"/api/announcements/?limit={limit}", "downstox-ann-feed.json")
        ages = payload.get("sourceAges", {})
        health = [
            SourceHealth(name=name, status=status,
                         newestAt=ages.get(name, {}).get("newestAt"),
                         withheld=ages.get(name, {}).get("withheld", False))
            for name, status in (payload.get("sources") or {}).items()
        ]
        return AnnouncementFeed(
            items=[AnnouncementItem(**a) for a in payload.get("announcements", [])],
            generated_at=datetime.fromisoformat(payload["generatedAt"].replace("Z", "+00:00")),
            sources=health,
        )

    def get_promises(self, symbol: str) -> list[ManagementPromise]:
        sym = symbol.upper()
        payload = self._get(f"/api/promises/?symbol={sym}", f"downstox-promises-{sym}.json")
        return [ManagementPromise.from_upstream(p) for p in payload.get("promises", [])]

    def get_market_status(self) -> MarketStatus:
        payload = self._get("/api/holidays/", "downstox-holidays.json")
        ms = payload["marketStatus"]
        return MarketStatus(
            open=ms["open"], state=ms["state"], reason=ms.get("reason", ""),
            last_trading_day=(ms.get("lastTradingDay") or {}).get("label"),
            next_trading_day=(ms.get("nextTradingDay") or {}).get("label"),
            fetched_at=datetime.now(timezone.utc),
        )


_BOUND_MAP = {"at_least": "at_least", "at_most": "at_most", "point": "point",
              "approx": "point", "range": "range"}


def promise_to_guidance(p: ManagementPromise) -> GuidanceStatement:
    """Map an upstream management statement onto the ledger's GuidanceStatement,
    preserving bound semantics (a range stays a range), the verbatim quote,
    hedge wording and source URL. Unparsed targets abstain (target_pct None)."""
    t = p.norm_target
    bound, pct, hi = "point", None, None
    if t and t.parsed:
        bound = _BOUND_MAP.get(t.comparator or "", "point")
        if bound == "range":
            pct, hi = t.lo, t.hi
        else:
            pct = t.value
    return GuidanceStatement(
        company=p.company,
        metric=p.metric,
        stated_target=t.raw if t else p.target_raw,
        quote=p.quote,
        page=None,
        period=p.timeframe or None,
        target_pct=pct,
        bound=bound,  # type: ignore[arg-type]
        target_hi_pct=hi,
        hedge=(t.hedge if t else None),
        horizon=p.timeframe or None,
        source_type=f"filing_excerpt ({p.source_url})",
    )


def freshness_label(quoted_at: datetime, as_of: datetime, market_open: bool) -> str:
    """Market-aware age label against an injected clock (deterministic in tests).
    Upstream 'stale' flags are advisory; callers reason from quoted_at."""
    age = (as_of - quoted_at).total_seconds()
    if age < 0:
        return "clock-skew (quoted in the future - treat with suspicion)"
    if market_open and age <= 900:
        return "live (this session)"
    if age <= 900:
        return "recent (market closed)"
    if age <= 86400:
        return f"delayed ({int(age // 3600)}h old)"
    return f"stale ({int(age // 86400)}d old - last trading session or earlier)"
