from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, model_validator

# The 16 categories from the companion glossary (page 1). Guidance, not a constraint.
CATEGORIES = [
    "results", "M&A", "fund_raise", "management_change", "litigation",
    "insider_trading", "earnings_call", "investor_meetings", "credit_rating",
    "dividend", "bonus_split", "capex", "press_release", "postal_ballot",
    "agm_egm", "pledging", "unclassified",
]


class Unit(str, Enum):
    CRORE = "crore"
    LAKH = "lakh"


class MoneyValue(BaseModel):
    """A money amount with its accounting unit. 1 crore = 100 lakh."""

    amount: float
    unit: Unit = Unit.CRORE
    page: Optional[int] = None  # source-document page; None = not established (unverified)

    def in_crore(self) -> float:
        return self.amount / 100.0 if self.unit is Unit.LAKH else self.amount


class Metric(str, Enum):
    REVENUE_FROM_OPERATIONS = "revenue_from_operations"
    TOTAL_REVENUE = "total_revenue"  # includes other income
    EBITDA = "ebitda"
    PBT = "pbt"
    PAT = "pat"


class PeriodType(str, Enum):
    QUARTER = "quarter"
    YEAR = "year"


class ReportedNumber(BaseModel):
    metric: Metric
    value: MoneyValue
    consolidated: bool = True
    period_end: date
    period_type: PeriodType = PeriodType.QUARTER
    source_page: Optional[int] = None


class ResultsFiling(BaseModel):
    """Headline numbers from one results filing (brief page 2: Revenue, EBITDA, PBT, PAT)."""

    company: str
    period_end: date
    period_type: PeriodType = PeriodType.QUARTER
    consolidated: bool = True
    standard: Optional[str] = None  # e.g. "Ind-AS"
    revenue_from_operations: Optional[MoneyValue] = None  # "Sales" / revenue from operations
    total_revenue: Optional[MoneyValue] = None  # total income incl. other income
    ebitda: Optional[MoneyValue] = None  # None = not stated in the filing; abstain
    pbt: MoneyValue
    pat: MoneyValue
    source_page: Optional[int] = None

    @model_validator(mode="after")
    def _needs_a_revenue_definition(self) -> "ResultsFiling":
        if self.revenue_from_operations is None and self.total_revenue is None:
            raise ValueError("at least one of revenue_from_operations or total_revenue is required")
        return self

    def metric_value(self, metric: Metric) -> Optional[MoneyValue]:
        return getattr(self, metric.value)


class GuidanceStatement(BaseModel):
    """One verbatim guidance statement from a concall transcript.

    target_pct is filled by the extraction agent ONLY when management stated a
    number; it stays None for purely qualitative guidance, and the ledger then
    abstains instead of inventing a comparison. bound records the shape of what
    was said: "double-digit growth" is at_least 10, never a fabricated point 10.0."""

    company: str
    metric: str
    stated_target: str
    quote: str
    page: Optional[int] = None
    period: Optional[str] = None
    target_pct: Optional[float] = None
    bound: Literal["at_least", "at_most", "point", "range"] = "point"
    target_hi_pct: Optional[float] = None  # upper end when bound == "range"
    hedge: Optional[str] = None  # verbatim hedge word, e.g. "aspiration", "likely"
    horizon: Optional[str] = None  # "qoq", "yoy" or "fy", only when management stated it
    source_type: Optional[str] = None  # e.g. concall_transcript, investor_presentation


class Announcement(BaseModel):
    exchange: Literal["NSE", "BSE"]
    announcement_id: str
    company: str
    headline: str
    filed_at: datetime
    category: str = "unclassified"
    revision_of: Optional[str] = None
    body_text: str = ""


class Classification(BaseModel):
    category: str
    material: bool
    one_line_summary: str
