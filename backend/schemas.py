"""Strict, cited contracts for FINN PUSH, PULL and cost telemetry.

Use ``model_validate_json`` at API/LLM boundaries. Python-mode construction is
also strict: use date/datetime objects, enum instances and tuples. No missing
financial value becomes zero. INR amounts are in crore unless stated otherwise;
percentages are 0..100-style values, ratios are unscaled, capex is a positive
cash expenditure. Computed fields are deterministic and excluded by Pydantic's
``round_trip=True`` when caching models.

These contracts check provenance references and comparability. Actual quote
matching and issuer identity must be verified against fetched source bytes by
the ingestion/extraction layer; a schema cannot establish that a claim is true.
"""

from __future__ import annotations

import calendar
import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Annotated, Any, Iterator, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    computed_field,
    model_validator,
)

from .config import (
    CATEGORY_POLICIES,
    MARKET_TIMEZONE,
    SCHEMA_VERSION,
    AnnouncementCategory,
    Priority,
    Provider,
    Tier,
    normalize_ticker,
    strict_integer,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text must contain a non-whitespace character")
    return value


Text = Annotated[str, StringConstraints(min_length=1, max_length=1200), AfterValidator(_nonblank)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=180), AfterValidator(_nonblank)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]
Ticker = Annotated[str, BeforeValidator(normalize_ticker)]
ContentHash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
FiniteNumber = Annotated[float, Field(allow_inf_nan=False)]
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Count = Annotated[int, Field(ge=0, le=2**63 - 1)]
Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
FinancialBasis = Literal["consolidated", "standalone"]
Unit = Literal["inr_crore", "inr", "percent", "ratio", "days", "shares", "units"]
SourceKind = Literal[
    "annual_report", "concall_transcript", "quarterly_results", "investor_presentation",
    "exchange_filing", "screener_financials", "professional_profile", "market_data",
]
PRIMARY_DOCUMENT_KINDS = frozenset({
    "annual_report", "concall_transcript", "quarterly_results", "investor_presentation", "exchange_filing",
})


class StrictModel(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, validate_default=True,
        revalidate_instances="always", allow_inf_nan=False,
    )


class DataGap(StrictModel):
    field: Identifier
    reason: Literal[
        "not_disclosed", "not_available", "not_applicable", "insufficient_history",
        "not_comparable", "extraction_failed", "source_unavailable", "no_explicit_guidance",
    ]
    detail: Text


class SourceDocument(StrictModel):
    source_id: Identifier
    ticker: Ticker
    kind: SourceKind
    title: ShortText
    publisher: ShortText
    url: HttpUrl | None
    is_demo: bool = False
    published_on: date
    event_on: date | None = None  # concall date when the transcript is published later
    retrieved_at: AwareDatetime = Field(default_factory=utc_now)
    content_hash: ContentHash

    @model_validator(mode="after")
    def date_order(self) -> SourceDocument:
        # Dates printed on a document use the market's local calendar.
        if self.published_on > self.retrieved_at.astimezone(ZoneInfo(MARKET_TIMEZONE)).date():
            raise ValueError("source publication date is after retrieval")
        if self.event_on is not None and self.event_on > self.published_on:
            raise ValueError("source event date cannot be after publication")
        if self.url is None and not self.is_demo:
            raise ValueError("live source documents require a URL")
        if self.url is not None and (self.url.username or self.url.password):
            raise ValueError("source URLs must not contain credentials")
        return self


class Citation(StrictModel):
    source_id: Identifier
    page: Annotated[int, Field(ge=1)] | None = None  # 1-based physical PDF page
    section: ShortText | None = None
    chunk_id: Identifier | None = None

    @model_validator(mode="after")
    def addressable(self) -> Citation:
        if self.page is None and self.section is None and self.chunk_id is None:
            raise ValueError("citation needs a page, section, or stable chunk_id")
        return self


Citations = Annotated[tuple[Citation, ...], Field(min_length=1, max_length=12)]


class EvidenceText(StrictModel):
    text: Text
    citations: Citations
    is_inference: bool = False


class ReportingPeriod(StrictModel):
    start_on: date
    end_on: date
    kind: Literal["quarter", "half_year", "annual", "ttm", "point_in_time", "custom"]
    label: ShortText

    @model_validator(mode="after")
    def valid_period(self) -> ReportingPeriod:
        days = (self.end_on - self.start_on).days + 1
        if days < 1:
            raise ValueError("period start must not be after its end")
        if self.kind == "quarter" and not 80 <= days <= 100:
            raise ValueError("a quarter must contain 80..100 days; do not label YTD data as quarterly")
        bounds = {"half_year": (160, 200), "annual": (330, 400), "ttm": (330, 400)}
        if self.kind in bounds and not bounds[self.kind][0] <= days <= bounds[self.kind][1]:
            raise ValueError("period length does not match kind; use custom for a transitional fiscal period")
        if self.kind == "point_in_time" and days != 1:
            raise ValueError("point-in-time periods use the same start and end")
        return self


class NumericFact(StrictModel):
    metric: Identifier  # e.g. revenue, revenue_yoy_growth, debt_to_equity
    value: FiniteNumber | None
    unit: Unit
    period: ReportingPeriod
    basis: FinancialBasis
    citations: tuple[Citation, ...] = ()
    missing_reason: ShortText | None = None
    calculation: ShortText | None = None  # required by the adapter for derived input facts

    @model_validator(mode="after")
    def available_or_explained(self) -> NumericFact:
        if self.value is None:
            if self.missing_reason is None:
                raise ValueError("a missing numeric value needs missing_reason")
        elif not self.citations or self.missing_reason is not None:
            raise ValueError("a numeric value needs citations and must not have missing_reason")
        return self


class PercentageChange(StrictModel):
    value_pct: FiniteNumber | None
    unavailable_reason: Literal["missing_value", "zero_base", "negative_base", "numeric_overflow"] | None = None

    @model_validator(mode="after")
    def value_or_reason(self) -> PercentageChange:
        if (self.value_pct is None) != (self.unavailable_reason is not None):
            raise ValueError("percentage change needs either a value or an unavailable reason")
        return self


def percentage_change(current: float | None, baseline: float | None) -> PercentageChange:
    """Do not produce misleading growth rates for zero or loss-making bases."""
    if current is None or baseline is None:
        return PercentageChange(value_pct=None, unavailable_reason="missing_value")
    if baseline == 0:
        return PercentageChange(value_pct=None, unavailable_reason="zero_base")
    if baseline < 0:
        return PercentageChange(value_pct=None, unavailable_reason="negative_base")
    # Decimal-from-string prevents a true 5% move becoming 5.000000000000004%
    # and incorrectly crossing a strict threshold (e.g. INR 1.00 -> 1.05).
    with localcontext() as context:
        context.prec = 34
        value = float((Decimal(str(current)) - Decimal(str(baseline))) / Decimal(str(baseline)) * 100)
    if not math.isfinite(value):
        return PercentageChange(value_pct=None, unavailable_reason="numeric_overflow")
    return PercentageChange(value_pct=value)


def _ratio(numerator: float | None, denominator: float | None, scale: float = 1.0) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    with localcontext() as context:
        context.prec = 34
        value = float(Decimal(str(numerator)) / Decimal(str(denominator)) * Decimal(str(scale)))
    return value if math.isfinite(value) else None


def _period_identity(period: ReportingPeriod) -> tuple[date, date, str]:
    return period.start_on, period.end_on, period.kind


def _same_period(left: ReportingPeriod, right: ReportingPeriod) -> bool:
    return _period_identity(left) == _period_identity(right)


def _check_facts(facts: tuple[NumericFact, ...], period: ReportingPeriod, basis: FinancialBasis) -> None:
    if any(not _same_period(fact.period, period) or fact.basis != basis for fact in facts):
        raise ValueError("financial facts must use the declared period and reporting basis")


def _months_before(value: date, months: int) -> date:
    year, month_index = divmod(value.year * 12 + value.month - 1 - months, 12)
    month = month_index + 1
    # Preserve month-end for June 30 -> March 31 and September 30 -> June 30.
    last_day = calendar.monthrange(year, month)[1]
    day = last_day if value.day == calendar.monthrange(value.year, value.month)[1] else min(value.day, last_day)
    return date(year, month, day)


class FinancialSnapshot(StrictModel):
    period: ReportingPeriod
    basis: FinancialBasis
    revenue: NumericFact
    ebitda: NumericFact
    pbt: NumericFact
    pat: NumericFact
    key_ratios: Annotated[tuple[NumericFact, ...], Field(max_length=12)] = ()

    @model_validator(mode="after")
    def comparable_headlines(self) -> FinancialSnapshot:
        facts = (self.revenue, self.ebitda, self.pbt, self.pat)
        _check_facts(facts, self.period, self.basis)
        if tuple(f.metric for f in facts) != ("revenue", "ebitda", "pbt", "pat"):
            raise ValueError("headline metric names must match their fields")
        if any(f.unit != "inr_crore" for f in facts):
            raise ValueError("headline figures must be normalized to INR crore")
        if any(f.basis != self.basis for f in self.key_ratios):
            raise ValueError("key ratios must use the same reporting basis")
        return self

    @computed_field
    @property
    def ebitda_margin_pct(self) -> float | None:
        return _ratio(self.ebitda.value, self.revenue.value, 100.0)

    @computed_field
    @property
    def pat_margin_pct(self) -> float | None:
        return _ratio(self.pat.value, self.revenue.value, 100.0)


class ResultsHeadlineRow(StrictModel):
    metric: Literal["revenue", "ebitda", "pbt", "pat"]
    current_inr_crore: FiniteNumber | None
    previous_quarter_inr_crore: FiniteNumber | None
    year_ago_inr_crore: FiniteNumber | None
    qoq: PercentageChange
    yoy: PercentageChange


class ResultsHeadlineTable(StrictModel):
    current: FinancialSnapshot
    previous_quarter: FinancialSnapshot | None
    year_ago: FinancialSnapshot | None
    previous_quarter_missing_reason: ShortText | None = None
    year_ago_missing_reason: ShortText | None = None

    @model_validator(mode="after")
    def comparable_quarters(self) -> ResultsHeadlineTable:
        if self.current.period.kind != "quarter":
            raise ValueError("headline table must use standalone quarterly, not cumulative YTD, values")
        for name, snapshot, months, reason in (
            ("previous_quarter", self.previous_quarter, 3, self.previous_quarter_missing_reason),
            ("year_ago", self.year_ago, 12, self.year_ago_missing_reason),
        ):
            if snapshot is None:
                if reason is None:
                    raise ValueError(f"{name} needs a missing-data reason")
            elif (
                reason is not None or snapshot.basis != self.current.basis
                or snapshot.period.kind != "quarter"
                or snapshot.period.end_on != _months_before(self.current.period.end_on, months)
                or snapshot.period.start_on != _months_before(self.current.period.start_on, months)
            ):
                raise ValueError(f"{name} must be the comparable quarter on the same reporting basis")
        return self

    @computed_field
    @property
    def headline_rows(self) -> tuple[ResultsHeadlineRow, ...]:
        rows = []
        for metric in ("revenue", "ebitda", "pbt", "pat"):
            current = getattr(self.current, metric).value
            previous = getattr(self.previous_quarter, metric).value if self.previous_quarter else None
            year_ago = getattr(self.year_ago, metric).value if self.year_ago else None
            rows.append(ResultsHeadlineRow(
                metric=metric, current_inr_crore=current, previous_quarter_inr_crore=previous,
                year_ago_inr_crore=year_ago, qoq=percentage_change(current, previous),
                yoy=percentage_change(current, year_ago),
            ))
        return tuple(rows)


class ExecutiveBio(StrictModel):
    name: ShortText
    role: ShortText
    background: Annotated[tuple[EvidenceText, ...], Field(max_length=4)] = ()
    missing_background_reason: ShortText | None = None

    @model_validator(mode="after")
    def biography_or_gap(self) -> ExecutiveBio:
        if not self.background and self.missing_background_reason is None:
            raise ValueError("missing executive background must be explained")
        if self.background and self.missing_background_reason is not None:
            raise ValueError("available executive background cannot have a missing-data reason")
        return self


class ManagementBioEnrichment(StrictModel):
    incoming: Annotated[tuple[ExecutiveBio, ...], Field(max_length=8)] = ()
    outgoing: Annotated[tuple[ExecutiveBio, ...], Field(max_length=8)] = ()
    effective_on: date | None = None
    sources: tuple[SourceDocument, ...] = ()
    data_gaps: tuple[DataGap, ...] = ()

    @model_validator(mode="after")
    def at_least_one_executive(self) -> ManagementBioEnrichment:
        if not self.incoming and not self.outgoing and not self.data_gaps:
            raise ValueError("management enrichment needs an executive or an explicit data gap")
        return self


def _walk(value: Any) -> Iterator[BaseModel]:
    if isinstance(value, BaseModel):
        yield value
        for name in type(value).model_fields:
            yield from _walk(getattr(value, name))
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _walk(item)


def _validate_citations(root: BaseModel, sources: tuple[SourceDocument, ...], *, pull: bool) -> None:
    catalog = {source.source_id: source for source in sources}
    if len(catalog) != len(sources):
        raise ValueError("source_id values must be unique")
    nodes = tuple(_walk(root))
    for node in nodes:
        if isinstance(node, Citation) and node.source_id not in catalog:
            raise ValueError(f"unknown citation source_id: {node.source_id}")
    for node in nodes:
        if not pull:
            continue
        if isinstance(node, (EvidenceText, ManagementGuidance)):
            if any(catalog[c.source_id].kind not in PRIMARY_DOCUMENT_KINDS for c in node.citations):
                raise ValueError("PULL narrative claims and guidance require company/exchange primary documents")
        if isinstance(node, NumericFact):
            if any(catalog[c.source_id].kind not in PRIMARY_DOCUMENT_KINDS | {"screener_financials"} for c in node.citations):
                raise ValueError("PULL numeric facts require primary documents or labeled Screener financials")


class CorporateAnnouncement(StrictModel):
    announcement_id: Identifier  # stable exchange id or hash, never a random id per poll
    ticker: Ticker
    exchange: Literal["NSE", "BSE"]
    title: ShortText
    description: Annotated[str, StringConstraints(min_length=1, max_length=50_000)] | None = None
    attachment_url: HttpUrl | None = None
    published_at: AwareDatetime
    received_at: AwareDatetime = Field(default_factory=utc_now)
    category: AnnouncementCategory
    classification_tier: Annotated[Literal[0, 1], BeforeValidator(strict_integer)]
    classification_confidence: Confidence
    classification_reason: ShortText
    requires_llm_classification: bool = False
    routine_confirmed: bool = False  # ambiguous unclassified items stay visible
    filing: SourceDocument
    supporting_sources: tuple[SourceDocument, ...] = ()
    summary: EvidenceText | None = None
    priority_override: Priority | None = None
    priority_override_reason: EvidenceText | None = None
    results_headline_table: ResultsHeadlineTable | None = None
    management_bio_enrichment: ManagementBioEnrichment | None = None

    @model_validator(mode="after")
    def valid_enrichment(self) -> CorporateAnnouncement:
        if self.published_at > self.received_at:
            raise ValueError("announcement cannot be received before publication")
        if self.filing.ticker != self.ticker or self.filing.kind not in PRIMARY_DOCUMENT_KINDS:
            raise ValueError("announcement filing must be a primary document for this ticker")
        if self.routine_confirmed and not CATEGORY_POLICIES[self.category].collapse_by_default:
            raise ValueError("routine_confirmed is only valid for routine drawer categories")
        if self.requires_llm_classification and self.routine_confirmed:
            raise ValueError("ambiguous filings must stay visible until classification is resolved")
        if (self.priority_override is None) != (self.priority_override_reason is None):
            raise ValueError("a priority override requires a cited reason, and vice versa")
        if self.results_headline_table is not None and self.category != AnnouncementCategory.RESULTS:
            raise ValueError("results headline enrichment belongs only to results announcements")
        if self.management_bio_enrichment is not None and self.category != AnnouncementCategory.MANAGEMENT_CHANGE:
            raise ValueError("management bio enrichment belongs only to management changes")
        sources = (self.filing,) + self.supporting_sources
        if self.management_bio_enrichment:
            sources += self.management_bio_enrichment.sources
        if any(source.ticker != self.ticker for source in sources):
            raise ValueError("announcement sources must use the announcement ticker")
        _validate_citations(self, sources, pull=False)
        if self.results_headline_table:
            _validate_citations(self.results_headline_table, sources, pull=True)
        return self

    @computed_field
    @property
    def priority(self) -> int:
        return self.priority_override or CATEGORY_POLICIES[self.category].priority

    @computed_field
    @property
    def is_demo(self) -> bool:
        return self.filing.is_demo

    @computed_field
    @property
    def collapse_in_digest(self) -> bool:
        return self.routine_confirmed and CATEGORY_POLICIES[self.category].collapse_by_default and self.priority == 1


class EODMarketMetric(StrictModel):
    ticker: Ticker
    session_date: date
    close_inr: Positive
    previous_close_inr: Positive | None
    previous_session_date: date | None
    previous_close_missing_reason: ShortText | None = None
    close_5_sessions_ago_inr: Positive | None = None
    five_day_baseline_date: date | None = None
    five_day_missing_reason: ShortText | None = "Five-session baseline not supplied"
    provider_symbol: ShortText | None = None
    market_exchange: Literal["NSE", "BSE"] = "NSE"
    volume: Count
    # Exactly the preceding 20 trading sessions, excluding this session.
    prior_20d_volumes: Annotated[tuple[Count, ...], Field(min_length=20, max_length=20)] | None
    volume_window_end: date | None
    volume_history_missing_reason: ShortText | None = None
    price_basis: Literal["split_adjusted", "total_return_adjusted", "raw"]
    corporate_action_note: ShortText | None = None
    source: SourceDocument

    @model_validator(mode="after")
    def valid_baselines(self) -> EODMarketMetric:
        if self.source.ticker != self.ticker or self.source.kind != "market_data":
            raise ValueError("EOD source must be market data for this ticker")
        if self.close_5_sessions_ago_inr is None:
            if self.five_day_missing_reason is None or self.five_day_baseline_date is not None:
                raise ValueError("missing five-session close requires a reason and no baseline date")
        elif self.five_day_missing_reason is not None or self.five_day_baseline_date is None or self.five_day_baseline_date >= self.session_date:
            raise ValueError("five-session close requires an earlier date and no missing-data reason")
        if self.previous_close_inr is None:
            if self.previous_close_missing_reason is None or self.previous_session_date is not None:
                raise ValueError("missing previous close needs a reason and no baseline date")
        elif (
            self.previous_close_missing_reason is not None or self.previous_session_date is None
            or self.previous_session_date >= self.session_date
        ):
            raise ValueError("previous close needs an earlier session date and no missing-data reason")
        if self.prior_20d_volumes is None:
            if self.volume_history_missing_reason is None or self.volume_window_end is not None:
                raise ValueError("missing 20D volumes need a reason and no window end")
        elif (
            self.volume_history_missing_reason is not None or self.volume_window_end is None
            or self.volume_window_end >= self.session_date
        ):
            raise ValueError("20D baseline must end before this session and have no missing-data reason")
        if self.volume_window_end is not None and self.previous_session_date is not None and self.volume_window_end != self.previous_session_date:
            raise ValueError("the 20D baseline must end at the preceding session")
        return self

    @computed_field
    @property
    def price_change_pct(self) -> float | None:
        return percentage_change(self.close_inr, self.previous_close_inr).value_pct

    @computed_field
    @property
    def price_change_5d_pct(self) -> float | None:
        return percentage_change(self.close_inr, self.close_5_sessions_ago_inr).value_pct

    @computed_field
    @property
    def average_volume_20d(self) -> float | None:
        return sum(self.prior_20d_volumes) / 20 if self.prior_20d_volumes is not None else None

    @computed_field
    @property
    def volume_multiple(self) -> float | None:
        return _ratio(float(self.volume), self.average_volume_20d)

    @computed_field
    @property
    def volume_spike_ratio(self) -> float | None:
        return self.volume_multiple


class TriggeredAlert(StrictModel):
    alert_id: Identifier  # use ticker/session/kind/rule-version to deduplicate
    ticker: Ticker
    triggered_at: AwareDatetime = Field(default_factory=utc_now)
    kind: Literal["price_move", "volume_spike", "material_announcement"]
    priority: Priority
    title: ShortText
    market_metric: EODMarketMetric | None = None
    announcement: CorporateAnnouncement | None = None
    threshold: Positive | None = None  # percent or multiple, determined by kind

    @model_validator(mode="after")
    def actually_triggered(self) -> TriggeredAlert:
        if self.kind == "material_announcement":
            if self.announcement is None or self.market_metric is not None or self.threshold is not None:
                raise ValueError("material alerts require only an announcement payload")
            if self.announcement.ticker != self.ticker:
                raise ValueError("alert ticker does not match announcement")
            if self.triggered_at < self.announcement.published_at or self.priority != self.announcement.priority:
                raise ValueError("material alert time/priority must match its announcement")
        else:
            if self.market_metric is None or self.announcement is not None or self.threshold is None:
                raise ValueError("market alerts require only EOD data and a threshold")
            if self.market_metric.ticker != self.ticker:
                raise ValueError("alert ticker does not match EOD data")
            observed = self.market_metric.price_change_pct if self.kind == "price_move" else self.market_metric.volume_multiple
            if self.kind == "volume_spike" and self.threshold <= 1:
                raise ValueError("volume threshold must exceed one")
            if observed is None or (abs(observed) if self.kind == "price_move" else observed) < self.threshold:
                raise ValueError("observed metric must meet or exceed the configured threshold")
        return self


class BusinessSegment(StrictModel):
    name: ShortText
    description: EvidenceText
    revenue_mix: NumericFact | None = None

    @model_validator(mode="after")
    def valid_mix(self) -> BusinessSegment:
        if self.revenue_mix is not None:
            fact = self.revenue_mix
            if fact.unit != "percent" or (fact.value is not None and not 0 <= fact.value <= 100):
                raise ValueError("segment revenue mix must be a percentage in [0, 100]")
        return self


class BusinessSnapshot(StrictModel):
    overview: EvidenceText
    segments: Annotated[tuple[BusinessSegment, ...], Field(max_length=12)] = ()
    key_drivers: Annotated[tuple[EvidenceText, ...], Field(max_length=5)] = ()
    data_gaps: tuple[DataGap, ...] = ()


class WorkingCapitalPeriod(StrictModel):
    period: ReportingPeriod
    basis: FinancialBasis
    inventory_days: NumericFact
    receivable_days: NumericFact
    payable_days: NumericFact

    @model_validator(mode="after")
    def comparable_days(self) -> WorkingCapitalPeriod:
        facts = (self.inventory_days, self.receivable_days, self.payable_days)
        _check_facts(facts, self.period, self.basis)
        if tuple(f.metric for f in facts) != ("inventory_days", "receivable_days", "payable_days") or any(f.unit != "days" for f in facts):
            raise ValueError("working-capital inputs must identify inventory, receivable and payable days")
        return self

    @computed_field
    @property
    def cycle_days(self) -> float | None:
        values = (self.inventory_days.value, self.receivable_days.value, self.payable_days.value)
        if any(value is None for value in values):
            return None
        value = float(Decimal(str(values[0])) + Decimal(str(values[1])) - Decimal(str(values[2])))
        return value if math.isfinite(value) else None


class TrajectoryAndBalanceSheet(StrictModel):
    trends: Annotated[tuple[EvidenceText, ...], Field(max_length=6)] = ()
    historical_financials: Annotated[tuple[FinancialSnapshot, ...], Field(max_length=10)] = ()
    total_debt: NumericFact
    cash_and_equivalents: NumericFact
    debt_to_equity: NumericFact
    working_capital: Annotated[tuple[WorkingCapitalPeriod, ...], Field(max_length=10)] = ()
    funding_structure: Annotated[tuple[EvidenceText, ...], Field(max_length=4)] = ()
    contingent_liabilities: Annotated[tuple[EvidenceText, ...], Field(max_length=4)] = ()
    related_party_and_auditor_flags: Annotated[tuple[EvidenceText, ...], Field(max_length=5)] = ()
    data_gaps: tuple[DataGap, ...] = ()

    @model_validator(mode="after")
    def balance_sheet_units(self) -> TrajectoryAndBalanceSheet:
        for name, unit in (("total_debt", "inr_crore"), ("cash_and_equivalents", "inr_crore"), ("debt_to_equity", "ratio")):
            fact = getattr(self, name)
            if fact.metric != name or fact.unit != unit:
                raise ValueError(f"{name} has an incorrect metric or unit")
        _check_facts((self.cash_and_equivalents, self.debt_to_equity), self.total_debt.period, self.total_debt.basis)
        if self.total_debt.period.kind != "point_in_time":
            raise ValueError("balance-sheet facts need a point-in-time period")
        for history in (self.historical_financials, self.working_capital):
            if any(item.basis != self.total_debt.basis for item in history):
                raise ValueError("trajectory history must use the balance sheet's reporting basis")
            if any(left.period.end_on >= right.period.end_on for left, right in zip(history, history[1:])):
                raise ValueError("trajectory histories must be chronological without duplicate periods")
            if len({item.period.kind for item in history}) > 1:
                raise ValueError("compare the same period frequency within each trajectory history")
        return self

    @computed_field
    @property
    def working_capital_cycle_delta_days(self) -> tuple[float | None, ...]:
        """Aligned to working_capital; positive means a lengthening cycle."""
        if not self.working_capital:
            return ()
        changes: list[float | None] = [None]
        for previous, current in zip(self.working_capital, self.working_capital[1:]):
            if previous.cycle_days is None or current.cycle_days is None:
                changes.append(None)
            else:
                difference = float(Decimal(str(current.cycle_days)) - Decimal(str(previous.cycle_days)))
                changes.append(difference if math.isfinite(difference) else None)
        return tuple(changes)


class CashFlowPeriod(StrictModel):
    period: ReportingPeriod
    basis: FinancialBasis
    cfo: NumericFact
    pat: NumericFact
    capex: NumericFact

    @model_validator(mode="after")
    def comparable_cash_flows(self) -> CashFlowPeriod:
        facts = (self.cfo, self.pat, self.capex)
        _check_facts(facts, self.period, self.basis)
        if tuple(f.metric for f in facts) != ("cfo", "pat", "capex") or any(f.unit != "inr_crore" for f in facts):
            raise ValueError("cash-flow inputs must identify CFO, PAT and capex in INR crore")
        if self.period.kind == "point_in_time":
            raise ValueError("cash flows need a duration, not a balance-sheet date")
        if self.capex.value is not None and self.capex.value < 0:
            raise ValueError("normalize capex to positive capital expenditure before computing FCF")
        return self

    @computed_field
    @property
    def cash_conversion(self) -> float | None:
        # A zero/negative PAT denominator is not an interpretable conversion ratio.
        return _ratio(self.cfo.value, self.pat.value)

    @computed_field
    @property
    def free_cash_flow_inr_crore(self) -> float | None:
        if self.cfo.value is None or self.capex.value is None:
            return None
        value = float(Decimal(str(self.cfo.value)) - Decimal(str(self.capex.value)))
        return value if math.isfinite(value) else None


class CashQuality(StrictModel):
    history: Annotated[tuple[CashFlowPeriod, ...], Field(min_length=1, max_length=10)]
    interpretation: Annotated[tuple[EvidenceText, ...], Field(max_length=5)] = ()
    data_gaps: tuple[DataGap, ...] = ()

    @model_validator(mode="after")
    def comparable_history(self) -> CashQuality:
        if len({(item.basis, item.period.kind) for item in self.history}) > 1:
            raise ValueError("cash-flow history must use a consistent reporting basis and frequency")
        if any(left.period.end_on >= right.period.end_on for left, right in zip(self.history, self.history[1:])):
            raise ValueError("cash-flow history must be chronological without duplicate periods")
        return self


class GuidanceRange(StrictModel):
    lower: FiniteNumber | None
    upper: FiniteNumber | None

    @model_validator(mode="after")
    def ordered_bounds(self) -> GuidanceRange:
        if self.lower is None and self.upper is None:
            raise ValueError("guidance range requires at least one bound")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("guidance lower bound exceeds upper bound")
        return self


class ManagementGuidance(StrictModel):
    guidance_id: Identifier
    metric: Identifier
    exact_quote: Text  # verify verbatim against the source chunk before acceptance
    speaker: ShortText | None = None
    issued_on: date
    target_period: ReportingPeriod
    unit: Unit | None  # None is valid for purely qualitative guidance
    basis: FinancialBasis
    numeric_range: GuidanceRange | None = None
    citations: Citations

    @model_validator(mode="after")
    def not_retrospective(self) -> ManagementGuidance:
        if self.issued_on >= self.target_period.end_on:
            raise ValueError("forward guidance must have been issued before the target period ended")
        if self.numeric_range is not None and self.unit is None:
            raise ValueError("numeric guidance must declare its unit")
        return self


class PromiseDelivery(StrictModel):
    guidance: ManagementGuidance
    actual: NumericFact | None
    verdict: Literal[
        "within_range", "above_range", "below_range", "qualitatively_supported",
        "qualitatively_contradicted", "not_assessable",
    ]
    explanation: EvidenceText
    not_assessable_reason: ShortText | None = None

    @model_validator(mode="after")
    def comparable_and_honest(self) -> PromiseDelivery:
        if self.verdict == "not_assessable":
            if self.not_assessable_reason is None:
                raise ValueError("not_assessable needs an explicit reason")
            return self
        if self.not_assessable_reason is not None:
            raise ValueError("an assessed comparison cannot have not_assessable_reason")
        if not self.explanation.is_inference:
            raise ValueError("promise/delivery judgments must be labeled as inference")
        if self.actual is not None and (
            self.actual.metric != self.guidance.metric or self.actual.unit != self.guidance.unit
            or self.actual.basis != self.guidance.basis or not _same_period(self.actual.period, self.guidance.target_period)
        ):
            raise ValueError("compare the same metric, unit, reporting basis and target period")
        if self.verdict.startswith("qualitatively_"):
            if self.guidance.numeric_range is not None:
                raise ValueError("numeric guidance requires numeric comparison, not a qualitative verdict")
            return self
        actual = self.actual
        guidance = self.guidance
        if actual is None or actual.value is None or guidance.numeric_range is None:
            raise ValueError("numeric verdicts need an available actual and numeric guidance")
        if (
            actual.metric != guidance.metric or actual.unit != guidance.unit
            or actual.basis != guidance.basis or not _same_period(actual.period, guidance.target_period)
        ):
            raise ValueError("compare the same metric, unit, reporting basis and target period")
        bounds = guidance.numeric_range
        expected = "within_range"
        if bounds.lower is not None and actual.value < bounds.lower:
            expected = "below_range"
        elif bounds.upper is not None and actual.value > bounds.upper:
            expected = "above_range"
        if self.verdict != expected:
            raise ValueError(f"numeric guidance comparison must be {expected}")
        return self


class NarrativeVsNumbersQuarter(StrictModel):
    quarter: ReportingPeriod
    status: Literal["complete", "partial", "insufficient_data"]
    comparisons: Annotated[tuple[PromiseDelivery, ...], Field(max_length=10)] = ()
    findings: Annotated[tuple[EvidenceText, ...], Field(max_length=4)] = ()
    data_gaps: tuple[DataGap, ...] = ()

    @model_validator(mode="after")
    def honest_quarter(self) -> NarrativeVsNumbersQuarter:
        if self.quarter.kind != "quarter":
            raise ValueError("each audit slot must identify one quarter")
        if self.status != "complete" and not self.data_gaps:
            raise ValueError("partial or insufficient audit quarters require data gaps")
        if self.status == "complete" and (not self.comparisons or self.data_gaps or any(item.verdict == "not_assessable" for item in self.comparisons)):
            raise ValueError("a complete audit quarter needs comparisons and no unresolved gaps")
        if self.status == "insufficient_data" and any(item.verdict != "not_assessable" for item in self.comparisons):
            raise ValueError("a quarter with an assessed comparison is partial, not insufficient_data")
        for comparison in self.comparisons:
            if comparison.verdict != "not_assessable" and not _same_period(comparison.guidance.target_period, self.quarter):
                raise ValueError("annual or multi-quarter guidance cannot be scored against one quarter")
        ids = [item.guidance.guidance_id for item in self.comparisons]
        if len(ids) != len(set(ids)):
            raise ValueError("the same guidance cannot be counted twice in a quarter")
        return self


class ForwardGuidance(StrictModel):
    statements: Annotated[tuple[ManagementGuidance, ...], Field(max_length=12)] = ()
    data_gaps: tuple[DataGap, ...] = ()

    @model_validator(mode="after")
    def statements_or_gap(self) -> ForwardGuidance:
        if not self.statements and not self.data_gaps:
            raise ValueError("no explicit guidance must be represented as a data gap")
        return self


class BullBearCase(StrictModel):
    bull: Annotated[tuple[EvidenceText, ...], Field(min_length=1, max_length=5)]
    bear: Annotated[tuple[EvidenceText, ...], Field(min_length=1, max_length=5)]
    monitorables: Annotated[tuple[EvidenceText, ...], Field(max_length=5)] = ()

    @model_validator(mode="after")
    def distinguish_opinion(self) -> BullBearCase:
        if any(not claim.is_inference for claim in self.bull + self.bear):
            raise ValueError("bull and bear arguments must be labeled as inference")
        return self


class PullResearchReport(StrictModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    ticker: Ticker
    company_name: ShortText
    as_of: date  # report's information cutoff, not the newest accounting period
    generated_at: AwareDatetime = Field(default_factory=utc_now)
    sources: Annotated[tuple[SourceDocument, ...], Field(min_length=1, max_length=80)]
    business_snapshot: BusinessSnapshot | None
    financial_snapshot: FinancialSnapshot | None
    trajectory_and_balance_sheet: TrajectoryAndBalanceSheet | None
    cash_quality: CashQuality | None
    narrative_vs_numbers: Annotated[tuple[NarrativeVsNumbersQuarter, ...], Field(min_length=4, max_length=4)]
    forward_guidance: ForwardGuidance | None
    bull_bear_case: BullBearCase | None
    data_gaps: tuple[DataGap, ...] = ()

    @model_validator(mode="after")
    def primary_only_and_time_aligned(self) -> PullResearchReport:
        if self.as_of > self.generated_at.astimezone(ZoneInfo(MARKET_TIMEZONE)).date():
            raise ValueError("report cutoff cannot be after report generation")
        allowed = PRIMARY_DOCUMENT_KINDS | {"screener_financials"}
        for source in self.sources:
            if source.is_demo or source.kind not in allowed or source.ticker != self.ticker:
                raise ValueError("PULL sources must be primary/Screener financial documents for this ticker")
            if source.published_on > self.as_of:
                raise ValueError("source was published after the report's information cutoff")
        for name in (
            "business_snapshot", "financial_snapshot", "trajectory_and_balance_sheet",
            "cash_quality", "forward_guidance", "bull_bear_case",
        ):
            if getattr(self, name) is None and not any(gap.field == name for gap in self.data_gaps):
                raise ValueError(f"missing report module {name} requires an explicit data gap")
        quarters = tuple(item.quarter for item in self.narrative_vs_numbers)
        if any(quarter.end_on > self.as_of for quarter in quarters):
            raise ValueError("audit cannot include a quarter that has not ended")
        if any(left.end_on + timedelta(days=1) != right.start_on for left, right in zip(quarters, quarters[1:])):
            raise ValueError("audit needs exactly four consecutive quarters, oldest first")
        _validate_citations(self, self.sources, pull=True)
        catalog = {source.source_id: source for source in self.sources}
        for node in _walk(self):
            if isinstance(node, NumericFact) and node.period.end_on > self.as_of:
                raise ValueError("actual financial data cannot end after the report cutoff")
            if isinstance(node, NumericFact) and node.value is not None:
                if max(catalog[c.source_id].published_on for c in node.citations) < node.period.end_on:
                    raise ValueError("actual data requires a source published at or after its reporting period")
            if isinstance(node, ManagementGuidance):
                if node.issued_on > self.as_of:
                    raise ValueError("guidance was issued after the report cutoff")
                if any((catalog[c.source_id].event_on or catalog[c.source_id].published_on) != node.issued_on for c in node.citations):
                    raise ValueError("guidance issue date must match the cited call/publication date")
                if any(catalog[c.source_id].kind != "concall_transcript" for c in node.citations):
                    raise ValueError("this audit extracts management guidance from concall transcripts")
        return self


class TelemetryEvent(StrictModel):
    """One operation/attempt, not one whole report. Retries get separate events."""
    event_id: Identifier = Field(default_factory=lambda: uuid4().hex)
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    request_id: Identifier | None = None
    ticker: Ticker | None = None
    operation: Identifier
    tier: Tier | None = None  # None for scraping and cache lookups
    provider: Provider | None = None
    model: ShortText | None = None
    provider_called: bool = False
    cache_status: Literal["hit", "miss", "bypass"] = "bypass"
    latency_ms: NonNegative
    input_tokens: Count = 0  # total input, INCLUDING cached_input_tokens
    cached_input_tokens: Count = 0
    output_tokens: Count = 0  # includes billable reasoning/thinking tokens
    estimated_cost_usd: NonNegative | None = None
    pricing_source: ShortText | None = None
    status: Literal["success", "error"] = "success"
    error_type: Identifier | None = None  # class/code only; no prompt/secret leakage

    @model_validator(mode="before")
    @classmethod
    def default_local_cost(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("provider_called", False) is False and "estimated_cost_usd" not in data:
            return {**data, "estimated_cost_usd": 0.0}
        return data

    @model_validator(mode="after")
    def truthful_accounting(self) -> TelemetryEvent:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens are a subset of total input tokens")
        if self.provider_called:
            if self.tier not in (1, 2) or self.provider is None or self.model is None or self.cache_status == "hit":
                raise ValueError("provider calls require tier 1/2, provider/model, and cannot be local cache hits")
        elif self.input_tokens or self.output_tokens or self.cached_input_tokens or self.estimated_cost_usd != 0:
            raise ValueError("non-provider operations consume no LLM tokens or cost")
        if (self.status == "error") != (self.error_type is not None):
            raise ValueError("error status requires error_type; success cannot have error_type")
        return self


class EngineTelemetry(StrictModel):
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    events: Count = 0
    tier0_calls: Count = 0
    tier1_calls: Count = 0
    tier2_calls: Count = 0
    cache_hits: Count = 0
    cache_misses: Count = 0
    failures: Count = 0
    input_tokens: Count = 0
    cached_input_tokens: Count = 0
    output_tokens: Count = 0
    estimated_cost_usd: NonNegative = 0.0
    unpriced_llm_calls: Count = 0
    total_latency_ms: NonNegative = 0.0
    max_latency_ms: NonNegative = 0.0

    @computed_field
    @property
    def mean_latency_ms(self) -> float:
        return self.total_latency_ms / self.events if self.events else 0.0

    @computed_field
    @property
    def cache_hit_rate(self) -> float:
        lookups = self.cache_hits + self.cache_misses
        return self.cache_hits / lookups if lookups else 0.0

    @computed_field
    @property
    def cost_estimate_complete(self) -> bool:
        return self.unpriced_llm_calls == 0
