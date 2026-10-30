"""Shared product policy and explicit, inexpensive model routing (Python 3.11+).

Priority is a display policy, not a claim that every filing in a category is
material. A filing can override its category priority with a cited reason.
Percentages use percentage points: ``5.0`` means five percent, not ``0.05``.
No environment variables are read and no directories are created on import.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from enum import StrEnum
from functools import lru_cache
from types import MappingProxyType
from typing import Annotated, Literal, Mapping

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, model_validator

SCHEMA_VERSION = "1.1.0"
CACHE_SCHEMA_VERSION = 1
MARKET_TIMEZONE = "Asia/Kolkata"


class ConfigModel(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, validate_default=True,
        allow_inf_nan=False, revalidate_instances="always",
    )


class AnnouncementCategory(StrEnum):
    RESULTS = "results"
    M_AND_A = "M&A"
    FUND_RAISE = "fund_raise"
    MANAGEMENT_CHANGE = "management_change"
    LITIGATION = "litigation"
    INSIDER_TRADING = "insider_trading"
    EARNINGS_CALL = "earnings_call"
    INVESTOR_MEETINGS = "investor_meetings"
    CREDIT_RATING = "credit_rating"
    DIVIDEND = "dividend"
    BONUS_SPLIT = "bonus_split"
    CAPEX = "capex"
    PRESS_RELEASE = "press_release"
    POSTAL_BALLOT = "postal_ballot"
    AGM_EGM = "agm_egm"
    PLEDGING = "pledging"
    UNCLASSIFIED = "unclassified"


Priority = Annotated[int, Field(ge=1, le=5)]
NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CategoryPolicy(ConfigModel):
    label: NonEmpty
    priority: Priority
    collapse_by_default: bool = False


CATEGORY_POLICIES: Mapping[AnnouncementCategory, CategoryPolicy] = MappingProxyType({
    AnnouncementCategory.RESULTS: CategoryPolicy(label="Results", priority=5),
    AnnouncementCategory.M_AND_A: CategoryPolicy(label="M&A", priority=5),
    AnnouncementCategory.FUND_RAISE: CategoryPolicy(label="Capital actions", priority=4),
    AnnouncementCategory.MANAGEMENT_CHANGE: CategoryPolicy(label="Management", priority=5),
    AnnouncementCategory.LITIGATION: CategoryPolicy(label="Legal / regulatory", priority=5),
    AnnouncementCategory.INSIDER_TRADING: CategoryPolicy(label="Insider dealings", priority=3),
    AnnouncementCategory.EARNINGS_CALL: CategoryPolicy(label="Earnings call", priority=2),
    AnnouncementCategory.INVESTOR_MEETINGS: CategoryPolicy(label="Investor meetings", priority=2),
    AnnouncementCategory.CREDIT_RATING: CategoryPolicy(label="Credit rating", priority=4),
    AnnouncementCategory.DIVIDEND: CategoryPolicy(label="Dividend", priority=2),
    AnnouncementCategory.BONUS_SPLIT: CategoryPolicy(label="Bonus / split", priority=2),
    AnnouncementCategory.CAPEX: CategoryPolicy(label="Capital expenditure", priority=4),
    AnnouncementCategory.PRESS_RELEASE: CategoryPolicy(label="Company update", priority=3),
    AnnouncementCategory.POSTAL_BALLOT: CategoryPolicy(label="Postal ballot", priority=1, collapse_by_default=True),
    AnnouncementCategory.AGM_EGM: CategoryPolicy(label="AGM / EGM", priority=1, collapse_by_default=True),
    AnnouncementCategory.PLEDGING: CategoryPolicy(label="Promoter pledge", priority=4),
    AnnouncementCategory.UNCLASSIFIED: CategoryPolicy(label="Routine / other", priority=1, collapse_by_default=True),
})
ANNOUNCEMENT_CATEGORIES = tuple(category.value for category in AnnouncementCategory)
CATEGORY_PRIORITY_WEIGHTS: Mapping[str, int] = MappingProxyType({
    category.value: policy.priority for category, policy in CATEGORY_POLICIES.items()
})
ROUTINE_CATEGORIES = frozenset(
    category for category, policy in CATEGORY_POLICIES.items() if policy.collapse_by_default
)

DEFAULT_PORTFOLIO = (
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "TMPV", "SUNPHARMA", "LT", "ITC",
    "PERSISTENT", "DIXON", "POLYCAB", "TRENT", "MAXHEALTH", "KPITTECH", "KAYNES", "ZENTEC",
)
AD_HOC_DEMO_TICKERS = ("HAL", "BAJFINANCE", "CIPLA", "SUZLON", "TITAN")

# Exchange-confirmed rename of the SAME listed security. This does not alias
# the separately listed commercial-vehicle company TMCV or merge its accounts.
# Keep the user's original portfolio inputs while resolving the live symbol.
TICKER_RENAMES: Mapping[str, str] = MappingProxyType({"TATAMOTORS": "TMPV"})
TICKER_RENAME_SOURCES: Mapping[str, str] = MappingProxyType({
    "TATAMOTORS": "https://nsearchives.nseindia.com/content/circulars/FAOP70882.pdf",
})


def normalize_ticker(value: str) -> str:
    """Normalize an NSE equity symbol; the demo list is never an allowlist.

    ``M&M``, ``BAJAJ-AUTO``, and ``360ONE`` are valid. Yahoo's ``.NS`` suffix
    is accepted at the boundary; ``.BO`` and index symbols are not NSE equities.
    Only exchange-verified renames are resolved; other symbols are not guessed.
    """
    if not isinstance(value, str):
        raise ValueError("ticker must be a string")
    ticker = value.strip().upper()
    if ticker.endswith(".NS"):
        ticker = ticker[:-3]
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9&-]{0,31}", ticker):
        raise ValueError("expected an NSE equity symbol, optionally ending in .NS")
    return TICKER_RENAMES.get(ticker, ticker)


def strict_integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("expected an integer, not a boolean, float or numeric string")
    return value


class AlertThresholds(ConfigModel):
    price_move_pct: Annotated[float, Field(gt=0)] = 5.0
    volume_multiplier: Annotated[float, Field(gt=1)] = 2.0
    volume_lookback_sessions: Annotated[Literal[20], BeforeValidator(strict_integer)] = 20
    digest_window_hours: Annotated[int, Field(ge=1, le=24 * 90)] = 24
    material_min_priority: Priority = 4
    material_categories: tuple[AnnouncementCategory, ...] = (
        AnnouncementCategory.RESULTS, AnnouncementCategory.M_AND_A,
        AnnouncementCategory.FUND_RAISE, AnnouncementCategory.MANAGEMENT_CHANGE,
        AnnouncementCategory.LITIGATION, AnnouncementCategory.CREDIT_RATING,
        AnnouncementCategory.CAPEX, AnnouncementCategory.PLEDGING,
    )

    @model_validator(mode="after")
    def unique_categories(self) -> AlertThresholds:
        if len(self.material_categories) != len(set(self.material_categories)):
            raise ValueError("material_categories must not contain duplicates")
        return self


class Task(StrEnum):
    MARKET_METRICS = "market_metrics"
    FINANCIAL_METRICS = "financial_metrics"
    RULE_CLASSIFICATION = "rule_classification"
    REPORT_ASSEMBLY = "report_assembly"
    ANNOUNCEMENT_CLASSIFICATION = "announcement_classification"
    MANAGEMENT_BIO = "management_bio"
    GUIDANCE_EXTRACTION = "guidance_extraction"
    PRIMARY_FACT_EXTRACTION = "primary_fact_extraction"
    NARRATIVE_AUDIT = "narrative_audit"
    BULL_BEAR = "bull_bear"
    PRIMARY_QA = "primary_qa"


Tier = Annotated[Literal[0, 1, 2], BeforeValidator(strict_integer)]
Provider = Literal["openai", "google", "anthropic"]
TASK_TIERS: Mapping[Task, Tier] = MappingProxyType({
    Task.MARKET_METRICS: 0, Task.FINANCIAL_METRICS: 0,
    Task.RULE_CLASSIFICATION: 0, Task.REPORT_ASSEMBLY: 0,
    Task.ANNOUNCEMENT_CLASSIFICATION: 1, Task.MANAGEMENT_BIO: 1,
    Task.GUIDANCE_EXTRACTION: 1, Task.PRIMARY_FACT_EXTRACTION: 1,
    Task.NARRATIVE_AUDIT: 2, Task.BULL_BEAR: 2, Task.PRIMARY_QA: 2,
})
API_KEY_ENV: Mapping[str, str] = MappingProxyType({
    "openai": "OPENAI_API_KEY", "google": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
})


class TokenPricing(ConfigModel):
    """USD / million TEXT tokens, standard synchronous API, excluding tools.

    Output usage must include billed reasoning/thinking tokens, exactly once.
    A missing price is unknown, never an implicit free tier. Gemini explicit
    cache storage fees and non-text inputs need separate accounting later.
    """
    input_per_million_usd: Annotated[float, Field(ge=0)]
    output_per_million_usd: Annotated[float, Field(ge=0)]
    cached_input_per_million_usd: Annotated[float, Field(ge=0)] | None = None
    checked_on: date
    source_url: NonEmpty


# Verified 2026-10-29; prices are estimates, not a billing contract.
# https://developers.openai.com/api/docs/models/gpt-4o-mini
# https://developers.openai.com/api/docs/models/gpt-4o
# https://ai.google.dev/gemini-api/docs/pricing
_MINI_PRICE = TokenPricing(input_per_million_usd=0.15, output_per_million_usd=0.60,
    cached_input_per_million_usd=0.075, checked_on=date(2026, 10, 29),
    source_url="https://developers.openai.com/api/docs/models/gpt-4o-mini")
_FOUR_O_PRICE = TokenPricing(input_per_million_usd=2.50, output_per_million_usd=10.0,
    cached_input_per_million_usd=1.25, checked_on=date(2026, 10, 29),
    source_url="https://developers.openai.com/api/docs/models/gpt-4o")
_FLASH_PRICE = TokenPricing(input_per_million_usd=0.30, output_per_million_usd=2.50,
    cached_input_per_million_usd=0.03, checked_on=date(2026, 10, 29),
    source_url="https://ai.google.dev/gemini-api/docs/pricing")
# These are free-tier prices, conditional on an eligible billing-disabled
# Google AI Studio project. A model name/API key does not establish billing
# status. Keep paid/historical prices above for truthful old ledger estimates.
FREE_GEMINI_MODELS = frozenset({"gemini-3.5-flash-lite", "gemini-3.8-flash"})
DEFAULT_TIER1_MODEL = "gemini-3.5-flash-lite"
DEFAULT_TIER2_MODEL = "gemini-3.8-flash"
_GEMINI_FREE_PRICE = TokenPricing(input_per_million_usd=0.0, output_per_million_usd=0.0,
    cached_input_per_million_usd=0.0, checked_on=date(2026, 10, 29),
    source_url="https://ai.google.dev/gemini-api/docs/pricing")
TOKEN_PRICES: Mapping[tuple[str, str], TokenPricing] = MappingProxyType({
    ("openai", "gpt-4o-mini"): _MINI_PRICE,
    ("openai", "gpt-4o-mini-2024-07-18"): _MINI_PRICE,
    ("openai", "gpt-4o"): _FOUR_O_PRICE,
    ("openai", "gpt-4o-2024-08-06"): _FOUR_O_PRICE,
    ("openai", "gpt-4o-2024-11-20"): _FOUR_O_PRICE,
    ("google", "gemini-2.5-flash"): _FLASH_PRICE,
})


class ModelRoute(ConfigModel):
    provider: Provider
    model: NonEmpty
    max_output_tokens: Annotated[int, Field(ge=1, le=16_384)] = 2000
    timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 45.0
    max_retries: Annotated[int, Field(ge=0, le=3)] = 1
    # None lets an adapter omit temperature on models which do not support it.
    temperature: Annotated[float, Field(ge=0, le=2)] | None = None
    pricing_override: TokenPricing | None = None
    billing_tier: Literal["standard", "free"] = "standard"

    @model_validator(mode="after")
    def reject_retired_model(self) -> ModelRoute:
        if self.model.startswith("gemini-1.5"):
            raise ValueError("Gemini 1.5 Flash/Pro shut down 2025-09-29; use a supported Gemini model")
        if self.model.startswith("claude-3-7-sonnet"):
            raise ValueError("Claude 3.7 Sonnet retired 2026-02-19; configure a supported model")
        prefixes = {"openai": ("gpt-", "o1", "o3", "o4"), "google": ("gemini-",), "anthropic": ("claude-",)}
        if not self.model.startswith(prefixes[self.provider]):
            raise ValueError("model identifier does not match its provider")
        if self.billing_tier == "free":
            if self.provider != "google" or self.model not in FREE_GEMINI_MODELS:
                raise ValueError("free routes require an explicitly verified free-tier Gemini model")
            if self.pricing_override is not None and (
                self.pricing_override.input_per_million_usd != 0
                or self.pricing_override.output_per_million_usd != 0
                or self.pricing_override.cached_input_per_million_usd not in (None, 0)
            ):
                raise ValueError("a free route cannot have a nonzero pricing override")
        return self

    @property
    def pricing(self) -> TokenPricing | None:
        if self.billing_tier == "free":
            return _GEMINI_FREE_PRICE
        return self.pricing_override or TOKEN_PRICES.get((self.provider, self.model))

    @property
    def api_key_env(self) -> str:
        return API_KEY_ENV[self.provider]

    def ensure_usable(self, on_date: date | None = None) -> None:
        """Fail before a known shutdown; no network check or provider fallback."""
        today = on_date or datetime.now(timezone.utc).date()
        if self.provider == "openai" and self.model in {
            "gpt-4o", "gpt-4o-2024-08-06", "gpt-4o-2024-11-20",
            "gpt-4o-mini", "gpt-4o-mini-2024-07-18",
        } and today >= date(2027, 1, 14):
            raise ValueError("GPT-4o / mini API shutdown: 2027-01-14; update FINN's model configuration")


class LLMRouterConfig(ConfigModel):
    tier1: ModelRoute = Field(default_factory=lambda: ModelRoute(provider="google", model=DEFAULT_TIER1_MODEL,
        billing_tier="free", max_retries=0))
    tier2: ModelRoute = Field(default_factory=lambda: ModelRoute(provider="google", model=DEFAULT_TIER2_MODEL,
        billing_tier="free", max_output_tokens=6000, max_retries=3, timeout_seconds=150.0))
    # This is an operator assertion, not an API billing check. Without it the
    # PUSH client can reuse cached outputs, but makes no model API requests.
    free_tier_project_confirmed: bool = False
    # Local request-start spacing per model; this is not the project's quota.
    # Set from the active AI Studio limits, shared with all other API clients.
    gemini_min_request_interval_seconds: Annotated[float, Field(ge=0, le=60)] = 4.1
    rule_confidence_threshold: Annotated[float, Field(gt=0, le=1)] = 0.95
    concall_chunk_tokens: Annotated[int, Field(ge=256, le=8000)] = 1500
    concall_overlap_tokens: Annotated[int, Field(ge=0)] = 150
    max_reasoning_input_tokens: Annotated[int, Field(ge=1000, le=100_000)] = 24_000

    @model_validator(mode="after")
    def valid_chunk_overlap(self) -> LLMRouterConfig:
        if any(route.provider != "google" or route.billing_tier != "free"
               or route.model not in FREE_GEMINI_MODELS for route in (self.tier1, self.tier2)):
            raise ValueError("FINN is free-only: both tiers must use verified free-tier Gemini routes; no paid fallback")
        if self.concall_overlap_tokens >= self.concall_chunk_tokens:
            raise ValueError("chunk overlap must be smaller than chunk size")
        return self

    def route(self, task: Task | str) -> ModelRoute | None:
        tier = TASK_TIERS[Task(task)]
        if tier == 0:
            return None
        endpoint = self.tier1 if tier == 1 else self.tier2
        endpoint.ensure_usable()
        return endpoint


class CacheSettings(ConfigModel):
    path: NonEmpty = "finn_cache.db"
    busy_timeout_ms: Annotated[int, Field(ge=1, le=60_000)] = 5000
    max_payload_bytes: Annotated[int, Field(ge=1024, le=256 * 1024 * 1024)] = 32 * 1024 * 1024
    scrape_ttl_seconds: Annotated[int, Field(ge=1)] = 6 * 3600
    llm_ttl_seconds: Annotated[int, Field(ge=1)] = 30 * 24 * 3600


class Settings(ConfigModel):
    portfolio: tuple[str, ...] = DEFAULT_PORTFOLIO
    ad_hoc_demo_tickers: tuple[str, ...] = AD_HOC_DEMO_TICKERS
    thresholds: AlertThresholds = Field(default_factory=AlertThresholds)
    router: LLMRouterConfig = Field(default_factory=LLMRouterConfig)
    cache: CacheSettings = Field(default_factory=CacheSettings)

    @model_validator(mode="after")
    def valid_symbols(self) -> Settings:
        for field in ("portfolio", "ad_hoc_demo_tickers"):
            symbols = getattr(self, field)
            canonical = tuple(normalize_ticker(symbol) for symbol in symbols)
            if len(set(canonical)) != len(canonical):
                raise ValueError("ticker lists must not contain duplicates")
            if any(symbol != symbol.strip().upper() or symbol.endswith(".NS") for symbol in symbols):
                raise ValueError("configured ticker lists must use canonical uppercase NSE symbols")
            object.__setattr__(self, field, canonical)
        if not self.portfolio:
            raise ValueError("portfolio must not be empty")
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Load an optional full FINN_CONFIG_JSON, then an optional cache path.

        JSON is intentional: strict Pydantic still accepts JSON dates, enums,
        paths and arrays without accepting numeric strings as financial data.
        Secrets stay in provider-specific env vars and out of these models.
        """
        env = os.environ if environ is None else environ
        config = cls.model_validate_json(env.get("FINN_CONFIG_JSON", "{}"))
        if "FINN_CACHE_PATH" in env:
            data = config.model_dump(mode="json", round_trip=True)
            data["cache"]["path"] = env["FINN_CACHE_PATH"]
            config = cls.model_validate_json(json.dumps(data, allow_nan=False))
        if "FINN_GEMINI_FREE_TIER_CONFIRMED" in env:
            value = env["FINN_GEMINI_FREE_TIER_CONFIRMED"].strip().casefold()
            if value not in {"1", "0", "true", "false"}:
                raise ValueError("FINN_GEMINI_FREE_TIER_CONFIRMED must be 1/0 or true/false")
            data = config.model_dump(mode="json", round_trip=True)
            data["router"]["free_tier_project_confirmed"] = value in {"1", "true"}
            config = cls.model_validate_json(json.dumps(data, allow_nan=False))
        return config


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


DEFAULT_THRESHOLDS = AlertThresholds()
DEFAULT_LLM_ROUTER = LLMRouterConfig()
