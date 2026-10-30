"""Pillar 3: dynamic Screener tables, primary-document discovery and PDF text.

    python -m backend.data.fundamentals TCS
    python -m backend.data.fundamentals HAL --transcripts

No stock allowlist, LLM, synthetic fundamentals or login bypass. Financial
tables are explicitly Screener aggregates. Operating Profit is an EBITDA proxy
only when allowed and labeled; bank Financing Profit is never renamed EBITDA.
Screener's notes/AI summaries remain secondary, outside primary transcript text.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import ipaddress
import json
import math
import re
import socket
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Annotated, Any, Callable, Literal, Mapping
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import pymupdf as fitz
from bs4 import BeautifulSoup, Tag
from curl_cffi import requests as http
from pydantic import AwareDatetime, Field, HttpUrl, computed_field, model_validator

from ..cache import CacheKey, SQLiteCache
from ..config import MARKET_TIMEZONE, get_settings, normalize_ticker
from ..schemas import (
    Citation,
    DataGap,
    FinancialBasis,
    FinancialSnapshot,
    FiniteNumber,
    NumericFact,
    ReportingPeriod,
    ResultsHeadlineTable,
    SourceDocument,
    StrictModel,
    Ticker,
    Unit,
    utc_now,
)

SCREENER_ORIGIN = "https://www.screener.in"
PARSER_VERSION = "fundamentals-v2"
# Retain existing raw HTML/PDF pointers; current parsing always runs again.
# Parser-policy changes must not force a previously cached PDF onto a blocked
# exchange connection. This value matches the original raw-cache namespace.
RAW_CACHE_VERSION = "fundamentals-v1"
EXTRACTOR_VERSION = "transcript-v1"
ANNUAL_EXTRACTOR_VERSION = "annual-sections-v1"
SOURCE_POLICY_VERSION = "screener-schedules-annual-v1"
HTML_TTL_SECONDS = 6 * 3600
PDF_TTL_SECONDS = 30 * 24 * 3600
_IST = ZoneInfo(MARKET_TIMEZONE)
_MONTHS = {name.casefold(): index for index, name in enumerate(calendar.month_abbr) if name}
_SECTIONS = ("quarters", "profit-loss", "balance-sheet", "cash-flow", "ratios", "shareholding")


class FundamentalsError(RuntimeError):
    """Expected retrieval/parsing failure; no invented company data is returned."""


class SourceFetchError(FundamentalsError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class DocumentExtractionError(FundamentalsError):
    """A link was found but its PDF/text could not be safely extracted."""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9%]", "", text.casefold())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")[:80] or "unknown_row"


def _now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _months_before(end: date, count: int) -> date:
    year, month = divmod(end.year * 12 + end.month - 1 - count, 12)
    return _month_end(year, month + 1)


def parse_number(text: str) -> float | None:
    """Indian grouping, signed/parenthesized numbers, percentages and rupee labels.

    Inequalities, missing values and masked cells remain missing, never zero.
    Monetary scale conversion is separate because EPS and ratios are not crore.
    """
    value = _clean(text).replace("−", "-").replace("﹣", "-")
    if not value or value.casefold() in {"-", "--", "—", "–", "na", "n/a", "n.a.", "nil", "null", "none", "nm"}:
        return None
    if any(character in value for character in "<>≤≥"):
        return None
    negative = value.startswith("(") and value.endswith(")")
    if negative:
        value = value[1:-1].strip()
    value = re.sub(r"(?:₹|rs\.?|inr|%|crores?|cr\.?|lakhs?|lacs?|millions?|billions?)", "", value, flags=re.I)
    value = value.replace(",", "").replace(" ", "")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value):
        return None
    try:
        parsed = Decimal(value)
        if negative:
            parsed = -parsed
        result = float(parsed)
        return result if math.isfinite(result) else None
    except (InvalidOperation, OverflowError):
        return None


def _number(text: str, scale: Decimal = Decimal(1)) -> float | None:
    value = parse_number(text)
    if value is None:
        return None
    result = float(Decimal(str(value)) * scale)
    return result if math.isfinite(result) else None


def _divide(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    with localcontext() as context:
        context.prec = 34
        result = float(Decimal(str(numerator)) / Decimal(str(denominator)))
    return result if math.isfinite(result) else None


def _sum(values: tuple[float, ...]) -> float:
    return float(sum((Decimal(str(value)) for value in values), Decimal(0)))


class TableColumn(StrictModel):
    label: str
    end_on: date | None
    start_on: date | None
    kind: Literal["quarter", "half_year", "annual", "ttm", "point_in_time", "custom", "unknown"]

    def reporting_period(self) -> ReportingPeriod | None:
        if self.end_on is None or self.start_on is None or self.kind == "unknown":
            return None
        return ReportingPeriod(start_on=self.start_on, end_on=self.end_on, kind=self.kind, label=self.label)


class FinancialRow(StrictModel):
    metric: str
    source_label: str
    unit: Unit
    values: tuple[FiniteNumber | None, ...]
    raw_values: tuple[str, ...]
    source_id: str | None = None  # Separately downloaded schedule provenance.


class FinancialTable(StrictModel):
    section_id: str
    basis: FinancialBasis
    columns: tuple[TableColumn, ...]
    rows: tuple[FinancialRow, ...]
    monetary_unit_note: str

    @model_validator(mode="after")
    def aligned_cells(self) -> FinancialTable:
        if any(len(row.values) != len(self.columns) or len(row.raw_values) != len(self.columns) for row in self.rows):
            raise ValueError("financial table rows and period columns are misaligned")
        if len({row.metric for row in self.rows}) != len(self.rows):
            raise ValueError("financial table metric keys must be unique")
        return self

    def row(self, metric: str) -> FinancialRow | None:
        return next((row for row in self.rows if row.metric == metric), None)

    def at(self, metric: str, end_on: date) -> float | None:
        row = self.row(metric)
        index = next((i for i, column in enumerate(self.columns) if column.end_on == end_on), None)
        return row.values[index] if row is not None and index is not None else None


class HeaderRatio(StrictModel):
    metric: str
    label: str
    value: FiniteNumber | None
    unit: Unit
    raw_text: str


class CashConversionYear(StrictModel):
    period: ReportingPeriod
    cfo: NumericFact
    pat: NumericFact
    ebitda: NumericFact

    @computed_field
    @property
    def cfo_to_pat(self) -> float | None:
        return _divide(self.cfo.value, self.pat.value)

    @computed_field
    @property
    def cfo_to_ebitda(self) -> float | None:
        return _divide(self.cfo.value, self.ebitda.value)


class CashConversionSummary(StrictModel):
    years: tuple[date, ...]
    cumulative_cfo: FiniteNumber | None
    cumulative_pat: FiniteNumber | None
    cumulative_cfo_to_pat: FiniteNumber | None
    red_alert: bool
    unavailable_reason: str | None


class LeverageYear(StrictModel):
    period: ReportingPeriod
    debt: NumericFact
    share_capital: NumericFact
    reserves: NumericFact

    @computed_field
    @property
    def equity_inr_crore(self) -> float | None:
        if self.share_capital.value is None or self.reserves.value is None:
            return None
        return _sum((self.share_capital.value, self.reserves.value))

    @computed_field
    @property
    def debt_to_equity(self) -> float | None:
        return _divide(self.debt.value, self.equity_inr_crore)


class WorkingCapitalYear(StrictModel):
    period: ReportingPeriod
    debtor_days: FiniteNumber | None
    inventory_days: FiniteNumber | None
    payable_days: FiniteNumber | None
    reported_cash_conversion_cycle_days: FiniteNumber | None
    working_capital_days: FiniteNumber | None
    yoy_working_capital_change_days: FiniteNumber | None
    stretch_alert: bool

    @computed_field
    @property
    def computed_cash_conversion_cycle_days(self) -> float | None:
        if self.debtor_days is None or self.inventory_days is None or self.payable_days is None:
            return None
        return _sum((self.debtor_days, self.inventory_days, -self.payable_days))


class FinancialFlag(StrictModel):
    code: str
    severity: Literal["red", "warning"]
    period_end: date
    value: FiniteNumber
    threshold: FiniteNumber
    explanation: str


class DocumentLink(StrictModel):
    document_id: str
    ticker: Ticker
    kind: Literal["concall_transcript", "annual_report", "investor_presentation"]
    url: HttpUrl
    listed_label: str
    listed_month: date | None = None  # first day is a month sort key, NOT a call/publication date
    reporting_year: int | None = None
    alternate_urls: tuple[str, ...] = ()
    notes_url: HttpUrl | None = None
    embedded_notes: str | None = None
    notes_are_primary: Literal[False] = False


class DocumentCatalog(StrictModel):
    concall_transcripts: tuple[DocumentLink, ...]
    annual_reports: tuple[DocumentLink, ...]
    investor_presentations: tuple[DocumentLink, ...]
    selection_notes: tuple[str, ...]


class SecondaryConcallNotes(StrictModel):
    document_id: str
    url: HttpUrl | None
    text: str | None
    is_primary: Literal[False] = False
    status: Literal["available", "unavailable"]
    reason: str | None = None


class PdfPageText(StrictModel):
    page: Annotated[int, Field(ge=1)]
    text: str
    text_truncated: bool = False


class TranscriptChunk(StrictModel):
    chunk_id: str
    section: Literal["front_matter", "management_opening", "analyst_qa", "closing"]
    page_start: Annotated[int, Field(ge=1)]
    page_end: Annotated[int, Field(ge=1)]
    text: str


class TranscriptExtraction(StrictModel):
    document: DocumentLink
    resolved_url: HttpUrl | None
    pdf_sha256: str | None
    extracted_at: AwareDatetime
    status: Literal["ok", "partial", "needs_ocr", "error"]
    total_pages: int | None
    pages: tuple[PdfPageText, ...]
    chunks: tuple[TranscriptChunk, ...]
    truncated: bool
    cache_hit: bool = False
    error: str | None = None
    reported_quarter_end: date | None = None
    reported_period_evidence: str | None = None
    listing_period_mismatch: bool = False
    section_detection: Literal["heuristic"] = "heuristic"
    page_selection: Literal["first_pages", "annual_sections"] = "first_pages"

    @computed_field
    @property
    def full_text(self) -> str:
        return "\n\f\n".join(page.text for page in self.pages)


class CompanyFundamentals(StrictModel):
    ticker: Ticker
    company_name: str
    basis: FinancialBasis
    fetched_at: AwareDatetime
    cache_hit: bool
    source: SourceDocument
    schedule_sources: tuple[SourceDocument, ...] = ()
    header_ratios: tuple[HeaderRatio, ...]
    quarters: FinancialTable | None
    profit_loss: FinancialTable | None
    balance_sheet: FinancialTable | None
    cash_flow: FinancialTable | None
    ratios: FinancialTable | None
    shareholding: FinancialTable | None
    annual_shareholding: FinancialTable | None
    latest_quarter_headlines: ResultsHeadlineTable | None
    ebitda_basis: Literal["reported_ebitda", "operating_profit_proxy", "unavailable"]
    cash_conversion: tuple[CashConversionYear, ...]
    three_year_cash_conversion: CashConversionSummary
    leverage: tuple[LeverageYear, ...]
    working_capital: tuple[WorkingCapitalYear, ...]
    flags: tuple[FinancialFlag, ...]
    documents: DocumentCatalog
    transcripts: tuple[TranscriptExtraction, ...] = ()
    data_gaps: tuple[DataGap, ...] = ()
    notes: tuple[str, ...] = ()


# Aliases are based on row meaning, never a company ticker or fixed row index.
_ALIASES: dict[str, tuple[str, Unit]] = {}
def _alias(metric: str, unit: Unit, *labels: str) -> None:
    for label in labels:
        _ALIASES[_key(label)] = (metric, unit)


for _metric, _unit, _labels in (
    ("cash_and_equivalents", "inr_crore", ("Cash Equivalents", "Cash and Cash Equivalents", "Cash & Cash Equivalents")),
    ("cash_capex_outflow", "inr_crore", ("Fixed assets purchased", "Purchase of fixed assets")),
    ("revenue", "inr_crore", ("Sales", "Revenue", "Revenue from Operations", "Total Revenue", "Total Income")),
    ("operating_profit", "inr_crore", ("Operating Profit",)),
    ("ebitda", "inr_crore", ("EBITDA", "EBIDTA")),
    ("opm_pct", "percent", ("OPM %", "Operating Profit Margin %")),
    ("financing_profit", "inr_crore", ("Financing Profit",)),
    ("financing_margin_pct", "percent", ("Financing Margin %",)),
    ("other_income", "inr_crore", ("Other Income",)),
    ("interest", "inr_crore", ("Interest", "Finance Costs")),
    ("depreciation", "inr_crore", ("Depreciation", "Depreciation and Amortisation")),
    ("pbt", "inr_crore", ("Profit before tax", "Profit Before Taxation", "PBT")),
    ("pat", "inr_crore", ("Net Profit", "Profit after tax", "PAT")),
    ("tax_pct", "percent", ("Tax %",)),
    ("eps", "inr", ("EPS in Rs", "EPS", "Earnings Per Share")),
    ("equity_capital", "inr_crore", ("Equity Capital", "Share Capital", "Equity Share Capital")),
    ("reserves", "inr_crore", ("Reserves", "Other Equity")),
    ("debt", "inr_crore", ("Borrowings", "Debt", "Total Debt")),
    ("other_liabilities", "inr_crore", ("Other Liabilities",)),
    ("fixed_assets", "inr_crore", ("Fixed Assets",)),
    ("cwip", "inr_crore", ("CWIP", "Capital Work In Progress")),
    ("investments", "inr_crore", ("Investments",)),
    ("other_assets", "inr_crore", ("Other Assets",)),
    ("cfo", "inr_crore", ("Cash from Operating Activity", "Cash from Operating Activities", "Cash from Operations")),
    ("cfi", "inr_crore", ("Cash from Investing Activity", "Cash from Investing Activities")),
    ("cff", "inr_crore", ("Cash from Financing Activity", "Cash from Financing Activities")),
    ("debtor_days", "days", ("Debtor Days", "Receivable Days")),
    ("inventory_days", "days", ("Inventory Days",)),
    ("payable_days", "days", ("Days Payable", "Payable Days", "Creditor Days")),
    ("cash_conversion_cycle_days", "days", ("Cash Conversion Cycle", "Cash Conversion Cycle Days")),
    ("working_capital_days", "days", ("Working Capital Days",)),
    ("roce_pct", "percent", ("ROCE %", "ROCE")),
    ("promoters_pct", "percent", ("Promoters", "Promoter Holding", "Promoters %")),
    ("fiis_pct", "percent", ("FIIs", "FII", "Foreign Institutional Investors")),
    ("diis_pct", "percent", ("DIIs", "DII", "Domestic Institutional Investors")),
    ("public_pct", "percent", ("Public", "Public Shareholding")),
    ("promoter_pledging_pct", "percent", ("Promoter Pledging %", "Promoter Pledge %", "Pledged Percentage", "Pledged %", "Promoters Pledged", "Pledged Shares %")),
):
    _alias(_metric, _unit, *_labels)


def _header_date(label: str, node: Tag | None = None) -> date | None:
    key = node.get("data-date-key") if node is not None else None
    if isinstance(key, str):
        try:
            return date.fromisoformat(key)
        except ValueError:
            pass
    match = re.search(r"\b([A-Za-z]{3,9})\s+(20\d{2}|19\d{2})\b", label)
    if match and match[1][:3].casefold() in _MONTHS:
        return _month_end(int(match[2]), _MONTHS[match[1][:3].casefold()])
    return None


def _money_scale(section: Tag) -> tuple[Decimal, str]:
    sub = section.select_one("p.sub")
    text = _clean(sub.get_text(" ", strip=True)) if sub else ""
    if re.search(r"\b(?:lakh|lac)s?\b", text, re.I):
        return Decimal("0.01"), "Source INR lakh normalized to crore"
    if re.search(r"\bmillion", text, re.I):
        return Decimal("0.1"), "Source INR million normalized to crore"
    if re.search(r"\bbillion", text, re.I):
        return Decimal(100), "Source INR billion normalized to crore"
    return Decimal(1), "INR crore (Screener standard display); percentages/EPS/days use their own units"


def parse_financial_table(
    section: Tag | None, section_id: str, basis: FinancialBasis, *, limit: int = 15,
    annual_end_month: int | None = None, table_node: Tag | None = None,
) -> FinancialTable | None:
    if section is None:
        return None
    table = table_node if table_node is not None else (
        section.select_one("[data-reported-results] [data-result-table] table")
        or section.select_one("[data-result-table] table, table.data-table, table"))
    if table is None:
        return None
    header_row = table.select_one("thead tr") or table.find("tr")
    if header_row is None:
        return None
    headers = header_row.find_all(["th", "td"], recursive=False)[1:]
    if not headers:
        return None
    heading = section.find(["h2", "h3"])
    half_year = bool(heading and re.search(r"half[ -]?year", heading.get_text(), re.I))
    columns: list[TableColumn] = []
    previous_date = None
    for header in headers:
        label = _clean(header.get_text(" ", strip=True))
        end = _header_date(label, header)
        kind: str = "unknown"
        start = None
        if label.casefold() in {"ttm", "trailing twelve months"}:
            kind = "ttm"
        elif end is not None:
            if section_id in {"balance-sheet", "shareholding", "annual-shareholding"}:
                kind, start = "point_in_time", end
            elif section_id == "quarters":
                months = 6 if half_year else 3
                kind = "half_year" if half_year else "quarter"
                start = _months_before(end, months) + timedelta(days=1)
            else:
                kind, start = "annual", _months_before(end, 12) + timedelta(days=1)
                if previous_date is not None and (end - previous_date).days < 330:
                    start = previous_date + timedelta(days=1)
                    kind = "half_year" if 160 <= (end - start).days + 1 <= 200 else "custom"
                elif previous_date is not None and 400 < (end - previous_date).days < 700:
                    start, kind = previous_date + timedelta(days=1), "custom"
                if section_id == "cash-flow" and annual_end_month is not None and end.month != annual_end_month:
                    kind = "half_year" if end.month == ((annual_end_month + 5) % 12) + 1 else "custom"
                    start = _months_before(end, 6) + timedelta(days=1) if kind == "half_year" else start
                elif section_id == "cash-flow" and annual_end_month is not None:
                    # A preceding September YTD column does not make the next
                    # March full-year cash-flow statement a six-month period.
                    kind, start = "annual", _months_before(end, 12) + timedelta(days=1)
            previous_date = end
        columns.append(TableColumn(label=label, end_on=end, start_on=start, kind=kind))
    # Keep display/TTM cells, but never assign a fictional TTM period end.
    selected = list(range(len(columns)))[-limit:]
    scale, unit_note = _money_scale(section)
    rows = []
    seen = set()
    body = table.find("tbody") or table
    for tr in body.find_all("tr", recursive=False):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        label = _clean(cells[0].get_text(" ", strip=True)).rstrip("+ ")
        metric, unit = _ALIASES.get(_key(label), (_slug(label), "percent" if section_id in {"shareholding", "annual-shareholding"} or "%" in label else "inr_crore"))
        if metric in seen:
            continue
        seen.add(metric)
        if "shareholder" in label.casefold() and ("number" in label.casefold() or re.match(r"no\.?\s", label, re.I)):
            unit = "units"
        raw = [_clean(cell.get_text(" ", strip=True)) for cell in cells[1:]]
        raw += [""] * max(0, len(columns) - len(raw))
        values = tuple(_number(raw[index], scale if unit == "inr_crore" else Decimal(1)) for index in selected)
        rows.append(FinancialRow(metric=metric, source_label=label, unit=unit,
                                 values=values, raw_values=tuple(raw[index] for index in selected)))
    return FinancialTable(section_id=section_id, basis=basis, columns=tuple(columns[index] for index in selected),
                          rows=tuple(rows), monetary_unit_note=unit_note)


def _usable(table: FinancialTable | None) -> bool:
    return bool(table and any(row.metric in {"revenue", "pat", "operating_profit", "ebitda", "financing_profit"}
                              and any(value is not None for value in row.values) for row in table.rows))


def _without_future_columns(table: FinancialTable | None, observed_on: date) -> FinancialTable | None:
    if table is None:
        return None
    indices = [index for index, column in enumerate(table.columns) if column.end_on is None or column.end_on <= observed_on]
    if len(indices) == len(table.columns):
        return table
    return FinancialTable(section_id=table.section_id, basis=table.basis, monetary_unit_note=table.monetary_unit_note,
        columns=tuple(table.columns[index] for index in indices), rows=tuple(FinancialRow(
            metric=row.metric, source_label=row.source_label, unit=row.unit,
            values=tuple(row.values[index] for index in indices), raw_values=tuple(row.raw_values[index] for index in indices)) for row in table.rows))


def _fact(table: FinancialTable, metric: str, period: ReportingPeriod, source_id: str, *, amount: float | None = None,
          use_override: bool = False, calculation: str | None = None) -> NumericFact:
    row = table.row(metric)
    value = amount if use_override else table.at(metric, period.end_on)
    return NumericFact(metric=metric, value=value, unit="inr_crore", period=period, basis=table.basis,
        citations=(Citation(source_id=source_id, section=f"#{table.section_id}: {row.source_label if row else metric}; {period.label}"[:180]),) if value is not None else (),
        missing_reason=None if value is not None else f"{metric} not disclosed or not comparable in this table",
        calculation=calculation)


def _ebitda(table: FinancialTable, end: date, allow_proxy: bool) -> tuple[float | None, str]:
    explicit = table.at("ebitda", end)
    if explicit is not None:
        return explicit, "reported_ebitda"
    if allow_proxy and table.row("operating_profit") is not None and table.row("financing_profit") is None:
        return table.at("operating_profit", end), "operating_profit_proxy"
    return None, "unavailable"


def _financial_snapshot(table: FinancialTable, column: TableColumn, source_id: str, allow_proxy: bool) -> FinancialSnapshot:
    period = column.reporting_period()
    if period is None:
        raise ValueError("financial snapshot needs an explicit reporting period")
    ebitda, method = _ebitda(table, period.end_on, allow_proxy)
    ebitda_fact = _fact(table, "ebitda", period, source_id, amount=ebitda, use_override=True,
                        calculation="Screener Operating Profit used as EBITDA proxy; verify against primary filing" if method == "operating_profit_proxy" else None)
    if method == "operating_profit_proxy" and ebitda is not None:
        ebitda_fact = ebitda_fact.model_copy(update={"citations": (Citation(source_id=source_id, section=f"#{table.section_id}: Operating Profit; {column.label}"),)})
    pbt = _fact(table, "pbt", period, source_id)
    if pbt.value is None:
        inputs = tuple(table.at(metric, period.end_on) for metric in ("operating_profit", "other_income", "interest", "depreciation"))
        if all(value is not None for value in inputs):
            pbt = _fact(table, "pbt", period, source_id, amount=_sum((inputs[0], inputs[1], -inputs[2], -inputs[3])),
                        use_override=True, calculation="Operating Profit + Other Income - Interest - Depreciation")
            pbt = NumericFact.model_validate(pbt.model_copy(update={"citations": (
                Citation(source_id=source_id, section=f"#{table.section_id}: Operating Profit, Other Income, Interest, Depreciation; {column.label}"[:180]),)}))
    return FinancialSnapshot(period=period, basis=table.basis, revenue=_fact(table, "revenue", period, source_id),
                              ebitda=ebitda_fact, pbt=pbt, pat=_fact(table, "pat", period, source_id))


def compute_headlines(table: FinancialTable | None, source_id: str, *, observed_on: date,
                      allow_operating_profit_proxy: bool = True) -> tuple[ResultsHeadlineTable | None, str]:
    if table is None:
        return None, "unavailable"
    quarters = {column.end_on: column for column in table.columns if column.kind == "quarter" and column.end_on is not None and column.end_on <= observed_on}
    if not quarters:
        return None, "unavailable"
    end = max(quarters)
    previous, year_ago = quarters.get(_months_before(end, 3)), quarters.get(_months_before(end, 12))
    current = _financial_snapshot(table, quarters[end], source_id, allow_operating_profit_proxy)
    result = ResultsHeadlineTable(current=current,
        previous_quarter=_financial_snapshot(table, previous, source_id, allow_operating_profit_proxy) if previous else None,
        year_ago=_financial_snapshot(table, year_ago, source_id, allow_operating_profit_proxy) if year_ago else None,
        previous_quarter_missing_reason=None if previous else "Immediately preceding quarter is absent; older observations were not substituted",
        year_ago_missing_reason=None if year_ago else "Same quarter last year is absent; another period was not substituted")
    return result, _ebitda(table, end, allow_operating_profit_proxy)[1]


def compute_cash_conversion(cash_flow: FinancialTable | None, profit_loss: FinancialTable | None, source_id: str,
                            *, allow_operating_profit_proxy: bool = True) -> tuple[tuple[CashConversionYear, ...], CashConversionSummary]:
    unavailable = CashConversionSummary(years=(), cumulative_cfo=None, cumulative_pat=None,
        cumulative_cfo_to_pat=None, red_alert=False, unavailable_reason="Need three consecutive complete annual CFO/PAT periods")
    if cash_flow is None or profit_loss is None or cash_flow.basis != profit_loss.basis:
        return (), unavailable
    annual_pl = {column.end_on: column for column in profit_loss.columns if column.kind == "annual" and column.end_on is not None}
    observations = []
    for column in cash_flow.columns:
        if column.kind != "annual" or column.end_on not in annual_pl:
            continue
        period = column.reporting_period()
        pl_period = annual_pl[column.end_on].reporting_period()
        if period is None or pl_period is None or period.start_on != pl_period.start_on:
            continue
        snapshot = _financial_snapshot(profit_loss, annual_pl[column.end_on], source_id, allow_operating_profit_proxy)
        observations.append(CashConversionYear(period=period, cfo=_fact(cash_flow, "cfo", period, source_id),
                                                pat=snapshot.pat, ebitda=snapshot.ebitda))
    observations.sort(key=lambda item: item.period.end_on)
    recent = observations[-3:]
    if len(recent) != 3 or any(left.period.end_on != _months_before(right.period.end_on, 12) for left, right in zip(recent, recent[1:])):
        return tuple(observations), unavailable
    if any(item.cfo.value is None or item.pat.value is None for item in recent):
        return tuple(observations), unavailable
    cfo = _sum(tuple(item.cfo.value for item in recent))
    pat = _sum(tuple(item.pat.value for item in recent))
    ratio = _divide(cfo, pat)
    return tuple(observations), CashConversionSummary(years=tuple(item.period.end_on for item in recent),
        cumulative_cfo=cfo, cumulative_pat=pat, cumulative_cfo_to_pat=ratio,
        red_alert=pat > 0 and Decimal(str(cfo)) < Decimal("0.70") * Decimal(str(pat)),
        unavailable_reason="Cumulative PAT is zero or negative; conversion ratio is not meaningful" if pat <= 0 else None)


def compute_leverage(table: FinancialTable | None, source_id: str) -> tuple[LeverageYear, ...]:
    if table is None:
        return ()
    result = []
    for column in table.columns:
        period = column.reporting_period()
        if period is not None:
            result.append(LeverageYear(period=period, debt=_fact(table, "debt", period, source_id),
                share_capital=_fact(table, "equity_capital", period, source_id), reserves=_fact(table, "reserves", period, source_id)))
    return tuple(sorted(result, key=lambda item: item.period.end_on))


def compute_working_capital(table: FinancialTable | None) -> tuple[WorkingCapitalYear, ...]:
    if table is None:
        return ()
    result = []
    for column in table.columns:
        period = column.reporting_period()
        if period is None or column.kind != "annual":
            continue
        current = table.at("working_capital_days", period.end_on)
        previous = table.at("working_capital_days", _months_before(period.end_on, 12))
        change = _sum((current, -previous)) if current is not None and previous is not None else None
        result.append(WorkingCapitalYear(period=period, debtor_days=table.at("debtor_days", period.end_on),
            inventory_days=table.at("inventory_days", period.end_on), payable_days=table.at("payable_days", period.end_on),
            reported_cash_conversion_cycle_days=table.at("cash_conversion_cycle_days", period.end_on),
            working_capital_days=current, yoy_working_capital_change_days=change,
            stretch_alert=change is not None and Decimal(str(change)) > Decimal(15)))
    return tuple(sorted(result, key=lambda item: item.period.end_on))


def parse_header_ratios(soup: BeautifulSoup) -> tuple[HeaderRatio, ...]:
    section = soup.find(id="top-ratios")
    if section is None:
        return ()
    aliases = {"marketcap": ("market_cap", "inr_crore"), "currentprice": ("current_price", "inr"),
        "stockpe": ("stock_pe", "ratio"), "bookvalue": ("book_value", "inr"),
        "dividendyield": ("dividend_yield", "percent"), "roce": ("roce", "percent"), "roe": ("roe", "percent")}
    result = []
    for item in section.select("li"):
        name = item.select_one(".name")
        if name is None or _key(name.get_text()) not in aliases:
            continue
        metric, unit = aliases[_key(name.get_text())]
        value_node = item.select_one(".value") or item
        raw = _clean(value_node.get_text(" ", strip=True))
        number_node = value_node.select_one(".number")
        value = parse_number(number_node.get_text() if number_node else raw)
        result.append(HeaderRatio(metric=metric, label=_clean(name.get_text()), value=value, unit=unit, raw_text=raw))
    return tuple(result)


def _public_link(url: str, base_url: str) -> str | None:
    absolute = urljoin(base_url, url)
    parsed = urlsplit(absolute)
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or port not in (None, 443):
        return None
    host = parsed.hostname.casefold()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        pass
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def validate_public_url(url: str, *, trusted_hosts: set[str] | frozenset[str] = frozenset()) -> str:
    valid = _public_link(url, SCREENER_ORIGIN)
    if valid is None:
        raise SourceFetchError("document URL must use public HTTPS without credentials")
    host = urlsplit(valid).hostname
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        # Hosted HTTPS proxies can resolve source hosts remotely even when OS
        # DNS is unavailable. Allow only known origins or exact hosts observed
        # on the verified Screener page in that case. Positive private-address
        # DNS answers below are always rejected, including for trusted origins.
        known = {"www.screener.in", "screener.in", "www.bseindia.com", "bseindia.com",
                 "archives.nseindia.com", "nsearchives.nseindia.com", "www.nseindia.com"}
        if host in known | set(trusted_hosts):
            return valid
        raise SourceFetchError("document hostname could not be resolved") from error
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise SourceFetchError("private, local or reserved document hosts are not allowed")
    return valid


def _document_identity(url: str) -> str:
    parsed = urlsplit(url)
    filename = parse_qs(parsed.query).get("Pname", [parsed.path.rsplit("/", 1)[-1]])[0]
    if (parsed.hostname or "").endswith("bseindia.com") and re.fullmatch(r"[a-f0-9-]{36}\.pdf", filename, re.I):
        return "bse:" + filename.casefold()
    return url


def _listed_month(text: str) -> date | None:
    iso = re.search(r"\b((?:20|19)\d{2})-(0[1-9]|1[012])\b", text)
    if iso:
        return date(int(iso[1]), int(iso[2]), 1)
    match = re.search(r"\b([A-Za-z]{3,9})\s+(20\d{2}|19\d{2})\b", text)
    return date(int(match[2]), _MONTHS[match[1][:3].casefold()], 1) if match and match[1][:3].casefold() in _MONTHS else None


def _reporting_year(label: str) -> int | None:
    short_range = re.search(r"\b((?:19|20)\d{2})\s*[-/]\s*(\d{2})\b", label)
    if short_range:
        start = int(short_range[1])
        end = start // 100 * 100 + int(short_range[2])
        return end if end >= start else end + 100
    years = re.findall(r"\b(?:19|20)\d{2}\b", label)
    return max(map(int, years)) if years else None


def parse_documents(soup: BeautifulSoup, ticker: str, *, base_url: str, limit: int = 4) -> DocumentCatalog:
    section = soup.find(id="documents")
    selection_note = "Concall month is a listing/sort hint, not a verified call date or reported quarter. Verify period and issuer against PDF text."
    if section is None:
        return DocumentCatalog(concall_transcripts=(), annual_reports=(), investor_presentations=(), selection_notes=(selection_note, "No #documents section"))
    reports: list[DocumentLink] = []
    transcripts: list[DocumentLink] = []
    presentations: list[DocumentLink] = []
    seen = set()
    def add(kind: str, anchor: Tag, label: str, row: Tag, reporting_year: int | None = None) -> None:
        href = anchor.get("href")
        url = _public_link(href, base_url) if isinstance(href, str) else None
        if url is None:
            return
        identity = kind + "|" + _document_identity(url)
        if identity in seen:
            return
        seen.add(identity)
        month = _listed_month(label)
        notes_button = next((node for node in row.select("[data-url], a[href]") if re.search(r"\b(?:notes|summary)\b", node.get_text(" ", strip=True), re.I)), None)
        notes_href = notes_button.get("data-url") or notes_button.get("href") if notes_button is not None else None
        notes_url = _public_link(notes_href, base_url) if isinstance(notes_href, str) else None
        embedded = row.select_one("[data-concall-notes], .concall-notes, .concall-summary, [data-summary-text]")
        notes_text = _clean(embedded.get("data-summary-text") or embedded.get_text(" ", strip=True)) if embedded else None
        record = DocumentLink.model_validate_json(json.dumps({
            "document_id": kind + "." + hashlib.sha256(identity.encode()).hexdigest()[:24],
            "ticker": ticker, "kind": kind, "url": url, "listed_label": label,
            "listed_month": month.isoformat() if month else None, "reporting_year": reporting_year,
            "notes_url": notes_url, "embedded_notes": notes_text or None,
        }))
        {"annual_report": reports, "concall_transcript": transcripts, "investor_presentation": presentations}[kind].append(record)
    groups = section.select(".documents")
    if not groups:
        # Older/minimal pages can use bare h3 + ul groups without CSS classes.
        for heading in section.find_all(["h3", "h4"]):
            siblings = []
            for node in heading.next_siblings:
                if isinstance(node, Tag) and node.name in {"h3", "h4"}:
                    break
                siblings.append(str(node))
            fragment = BeautifulSoup("<div>" + str(heading) + "".join(siblings) + "</div>", "html.parser")
            groups.append(fragment.div)
    for group in groups:
        heading = group.find(["h3", "h4"])
        title = _clean(heading.get_text(" ", strip=True)).casefold() if heading else ""
        if "annual report" in title or "annual-reports" in group.get("class", []):
            for anchor in group.select("a[href]"):
                label = _clean(anchor.get_text(" ", strip=True))
                if re.search(r"annual\s+report|financial\s+year", label, re.I):
                    add("annual_report", anchor, label, anchor.find_parent("li") or group, _reporting_year(label))
        elif "concall" in title or "earning" in title or "concalls" in group.get("class", []):
            for row in group.select("li"):
                label_node = row.select_one(".nowrap, time")
                label = _clean(label_node.get_text(" ", strip=True)) if label_node else (_listed_month(row.get_text(" ", strip=True)) or "")
                label = label.isoformat()[:7] if isinstance(label, date) else str(label)
                for anchor in row.select("a[href]"):
                    text = _clean(anchor.get_text(" ", strip=True)).casefold()
                    if "transcript" in text:
                        add("concall_transcript", anchor, label, row)
                    elif text in {"ppt", "presentation", "investor presentation"} or "presentation" in text:
                        add("investor_presentation", anchor, label, row)
        elif "presentation" in title:
            for anchor in group.select("a[href]"):
                add("investor_presentation", anchor, _clean(anchor.get_text(" ", strip=True)), anchor.find_parent("li") or group)
    # Annual report duplicates across exchanges must not consume two year slots.
    reports.sort(key=lambda item: item.reporting_year or 0, reverse=True)
    annual_unique = []
    years_seen = set()
    for report in reports:
        key = report.reporting_year or report.document_id
        if key not in years_seen:
            years_seen.add(key)
            annual_unique.append(report)
    transcripts.sort(key=lambda item: item.listed_month or date.min, reverse=True)
    selected = []
    buckets: set[Any] = set()
    extra_candidates = []
    for document in transcripts:
        month = document.listed_month
        bucket = (month.year, (month.month - 1) // 3) if month else document.document_id
        if bucket in buckets:
            # A shared calendar quarter does not prove identical PDF content.
            # Keep another call independent rather than substituting it as a
            # purported mirror if the first PDF fails.
            extra_candidates.append(document)
        elif len(selected) < limit:
            buckets.add(bucket)
            selected.append(document)
    selected.extend(extra_candidates[:max(0, limit - len(selected))])
    presentations.sort(key=lambda item: item.listed_month or date.min, reverse=True)
    return DocumentCatalog(concall_transcripts=tuple(selected), annual_reports=tuple(annual_unique[:limit]),
        investor_presentations=tuple(presentations[:limit]), selection_notes=(selection_note,
        "One candidate per listing quarter is prioritized, then spare slots retain independent extra calls; a common listing quarter never proves mirror identity.",
        "Screener notes/AI summaries are secondary and excluded from primary PDF extraction."))


def parse_screener_html(
    html: str | bytes, ticker: str, *, source_url: str, fetched_at: datetime,
    basis: FinancialBasis = "consolidated", quarter_limit: int = 12,
    allow_operating_profit_proxy: bool = True, cache_hit: bool = False,
    document_limit: int = 4,
) -> CompanyFundamentals:
    ticker, fetched_at = normalize_ticker(ticker), _now(fetched_at)
    if type(quarter_limit) is not int or not 8 <= quarter_limit <= 12:
        raise ValueError("quarter_limit must be an integer from 8 to 12")
    if type(document_limit) is not int or not 1 <= document_limit <= 12:
        raise ValueError("document_limit must be an integer from 1 to 12")
    raw = html.encode() if isinstance(html, str) else html
    soup = BeautifulSoup(raw, "html.parser")
    company = soup.find("h1")
    if company is None or soup.select_one('form[action*="login"]') and not soup.find(id="quarters"):
        raise FundamentalsError("expected a public company page, not a login/challenge page")
    for anchor in soup.select('a[href*="nseindia.com"]'):
        match = re.search(r"\bNSE\s*:\s*([A-Z0-9&-]+)", anchor.get_text(" ", strip=True), re.I)
        if match and normalize_ticker(match[1]) != ticker:
            raise FundamentalsError("Screener page NSE identity does not match the requested ticker")
    declared = set()
    for section_id in _SECTIONS[:5]:
        section = soup.find(id=section_id)
        sub = section.select_one("p.sub") if section else None
        if sub:
            match = re.search(r"\b(consolidated|standalone)\b", sub.get_text(), re.I)
            if match:
                declared.add(match[1].casefold())
    if len(declared) > 1:
        raise FundamentalsError("financial sections mix consolidated and standalone reporting bases")
    basis = next(iter(declared)) if declared else basis
    observed = fetched_at.astimezone(_IST).date()
    source = SourceDocument.model_validate_json(json.dumps({
        "source_id": "screener." + hashlib.sha256(raw).hexdigest()[:24], "ticker": ticker,
        "kind": "screener_financials", "title": f"Financial table snapshot observed {observed.isoformat()}",
        "publisher": "Screener.in standardized financials", "url": source_url,
        "published_on": observed.isoformat(), "retrieved_at": fetched_at.isoformat(),
        "content_hash": hashlib.sha256(raw).hexdigest(),
    }))
    quarters = parse_financial_table(soup.find(id="quarters"), "quarters", basis, limit=quarter_limit)
    profit_loss = parse_financial_table(soup.find(id="profit-loss"), "profit-loss", basis)
    annual_ends = [column.end_on for column in profit_loss.columns if column.kind == "annual" and column.end_on] if profit_loss else []
    # The current fiscal year-end outranks the historical majority when an
    # issuer has moved, for example, from December to March.
    annual_month = max(annual_ends).month if annual_ends else None
    balance = parse_financial_table(soup.find(id="balance-sheet"), "balance-sheet", basis)
    cash = parse_financial_table(soup.find(id="cash-flow"), "cash-flow", basis, annual_end_month=annual_month)
    ratios = parse_financial_table(soup.find(id="ratios"), "ratios", basis)
    share_section = soup.find(id="shareholding")
    quarterly_share = share_section.select_one("#quarterly-shp table") if share_section else None
    annual_share = share_section.select_one("#yearly-shp table") if share_section else None
    shareholding = parse_financial_table(share_section, "shareholding", basis, limit=12, table_node=quarterly_share)
    annual_shareholding = parse_financial_table(share_section, "annual-shareholding", basis, table_node=annual_share) if annual_share else None
    quarters, profit_loss, balance, cash, ratios, shareholding, annual_shareholding = (
        _without_future_columns(table, observed) for table in
        (quarters, profit_loss, balance, cash, ratios, shareholding, annual_shareholding))
    if cash is not None and profit_loss is not None:
        # Exact annual P&L periods outrank the historical year-end-month mode:
        # a company may have changed from December to March. A transitional
        # 9/15-month P&L column stays non-annual and is never promoted here.
        annual_periods = {column.end_on: column for column in profit_loss.columns if column.kind == "annual"}
        aligned = tuple(TableColumn(label=column.label, end_on=column.end_on,
            start_on=annual_periods[column.end_on].start_on, kind="annual")
            if column.end_on in annual_periods else column for column in cash.columns)
        cash = FinancialTable(section_id=cash.section_id, basis=cash.basis, columns=aligned,
                              rows=cash.rows, monetary_unit_note=cash.monetary_unit_note)
    if not _usable(quarters) and not _usable(profit_loss):
        raise FundamentalsError("company page has no usable core financial tables")
    headlines, ebitda_basis = compute_headlines(quarters, source.source_id, observed_on=observed,
                                             allow_operating_profit_proxy=allow_operating_profit_proxy)
    cash_history, cumulative = compute_cash_conversion(cash, profit_loss, source.source_id,
                                                       allow_operating_profit_proxy=allow_operating_profit_proxy)
    leverage = compute_leverage(balance, source.source_id)
    working_capital = compute_working_capital(ratios)
    flags: list[FinancialFlag] = []
    if cumulative.red_alert:
        flags.append(FinancialFlag(code="cash_conversion_3y_below_70pct", severity="red", period_end=cumulative.years[-1],
            value=cumulative.cumulative_cfo_to_pat, threshold=0.70,
            explanation="Sum of CFO / sum of PAT over three consecutive matched full financial years is below 0.70"))
    if working_capital and working_capital[-1].stretch_alert:
        latest = working_capital[-1]
        flags.append(FinancialFlag(code="working_capital_days_stretch_gt15_yoy", severity="warning", period_end=latest.period.end_on,
            value=latest.yoy_working_capital_change_days, threshold=15.0,
            explanation="Reported Working Capital Days increased by more than 15 days versus the same financial year-end last year"))
    gaps = []
    for field, table in (("quarters", quarters), ("profit_loss", profit_loss), ("balance_sheet", balance),
                         ("cash_flow", cash), ("ratios", ratios), ("shareholding", shareholding)):
        if table is None or not table.rows:
            gaps.append(DataGap(field=field, reason="not_available", detail="Section absent or empty in the selected reporting basis"))
    if headlines is None:
        gaps.append(DataGap(field="latest_quarter_headlines", reason="not_comparable", detail="No completed quarterly periods; half-year/YTD data is not treated as quarterly"))
    if cumulative.unavailable_reason:
        gaps.append(DataGap(field="cash_conversion_3y", reason="not_comparable", detail=cumulative.unavailable_reason))
    if shareholding is None or shareholding.row("promoter_pledging_pct") is None:
        gaps.append(DataGap(field="promoter_pledging_pct", reason="not_disclosed", detail="No promoter pledging row in HTML; absence is not a zero pledge"))
    documents = parse_documents(soup, ticker, base_url=source_url, limit=document_limit)
    if len(documents.concall_transcripts) < 4:
        gaps.append(DataGap(field="concall_transcripts", reason="insufficient_history", detail=f"Only {len(documents.concall_transcripts)} distinct listing-quarter transcript candidates found"))
    return CompanyFundamentals(ticker=ticker, company_name=_clean(company.get_text(" ", strip=True)), basis=basis,
        fetched_at=fetched_at, cache_hit=cache_hit, source=source, header_ratios=parse_header_ratios(soup),
        quarters=quarters, profit_loss=profit_loss, balance_sheet=balance, cash_flow=cash, ratios=ratios,
        shareholding=shareholding, annual_shareholding=annual_shareholding, latest_quarter_headlines=headlines,
        ebitda_basis=ebitda_basis, cash_conversion=cash_history, three_year_cash_conversion=cumulative,
        leverage=leverage, working_capital=working_capital, flags=tuple(flags), documents=documents, data_gaps=tuple(gaps), notes=(
            "Source published_on denotes observation of this stored snapshot, not an inferred issuer filing date.",
            "Sales/Revenue and Net Profit preserve Screener definitions; Net Profit may be attributable to owners rather than total group PAT.",
            "EBITDA may be an explicitly labeled Operating Profit proxy. Financing Profit is never used as EBITDA.",
            "CFO/EBITDA here uses displayed CFO, not Screener's separately displayed before-tax CFO/EBITDA widget.",
            "Debt/equity uses Borrowings / (Equity Capital + Reserves); reported Borrowings can include lease liabilities.",
            "Cash conversion cycle and Working Capital Days remain distinct measures. Missing cells are not zero."))


_QA_START = re.compile(r"(?:\b(?:we\s+(?:will|shall|can|would|now)|let\s+us).{0,100}(?:open|begin|start|take).{0,100}(?:questions?|q\s*&\s*a|q\s*and\s*a)|^(?:question[ -]and[ -]answer(?:s|\s+session)?|questions?\s*(?:&|and)\s*answers?(?:\s+session)?|q\s*&\s*a(?:\s+session)?)\s*[:.-]?\s*$)", re.I)
_OPENING = re.compile(r"\b(?:opening\s+remarks|management\s+(?:presentation|commentary)|welcome\s+to.{0,100}(?:earnings|conference)\s+call|financial\s+highlights|good\s+(?:morning|evening|afternoon).{0,100}(?:everyone|everybody))\b", re.I)
_CLOSING = re.compile(r"\b(?:this\s+concludes\s+(?:the|our)\s+(?:conference|call)|we\s+(?:now\s+)?conclude\s+(?:the|this)\s+(?:conference|call))\b", re.I)


def _reported_quarter(pages: tuple[PdfPageText, ...], observed_on: date) -> tuple[date | None, str | None]:
    month = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    date_expression = rf"(?:{month}\s+\d{{1,2}}(?:st|nd|rd|th)?\s*,?\s*20\d{{2}}|\d{{1,2}}(?:st|nd|rd|th)?\s+{month}\s*,?\s*20\d{{2}})"
    pattern = re.compile(rf"\bquarter(?:\s+and\s+(?:(?:six|nine|twelve)[ -]months?|half[ -]year|year))?\s+ended\s*[:,]?\s*({date_expression})", re.I)
    candidates = []
    text = _clean(" ".join(page.text for page in pages[:5]))
    for match in pattern.finditer(text):
        pieces = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", match[1], flags=re.I).replace(",", "").split()
        try:
            if pieces[0][:3].casefold() in _MONTHS:
                value = date(int(pieces[2]), _MONTHS[pieces[0][:3].casefold()], int(pieces[1]))
            else:
                value = date(int(pieces[2]), _MONTHS[pieces[1][:3].casefold()], int(pieces[0]))
            if value <= observed_on:
                candidates.append((value, match[0]))
        except (ValueError, KeyError, IndexError):
            continue
    return max(candidates, key=lambda item: item[0]) if candidates else (None, None)


def chunk_transcript(pages: tuple[PdfPageText, ...], document_id: str, *, max_chars: int = 6000,
                     overlap_chars: int = 300) -> tuple[TranscriptChunk, ...]:
    if type(max_chars) is not int or not 500 <= max_chars <= 20_000 or type(overlap_chars) is not int or not 0 <= overlap_chars < max_chars:
        raise ValueError("chunk size must be 500..20000 characters; overlap must be smaller")
    groups: list[tuple[str, list[tuple[int, str]]]] = []
    section = "front_matter"
    previous_page = None
    for page in pages:
        page_gap = previous_page is not None and page.page != previous_page + 1
        previous_page = page.page
        if page_gap and groups:
            groups.append((section, []))
        for line in page.text.splitlines(keepends=True):
            if not line.endswith(("\n", "\r")):
                line += "\n"
            stripped = _clean(line)
            if _QA_START.search(stripped) and (section != "front_matter" or page.page > 1):
                section = "analyst_qa"
            elif section == "analyst_qa" and _CLOSING.search(stripped):
                section = "closing"
            elif section == "front_matter" and (_OPENING.search(stripped) or page.page > 1):
                section = "management_opening"
            if not groups or groups[-1][0] != section:
                groups.append((section, []))
            groups[-1][1].append((page.page, line))
    result = []
    for section, lines in groups:
        text = "".join(line for _, line in lines)
        spans = []
        offset = 0
        for number, line in lines:
            spans.append((offset, offset + len(line), number))
            offset += len(line)
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            if end < len(text):
                boundary = text.rfind("\n", start + max_chars // 2, end)
                if boundary >= 0:
                    end = boundary + 1
            segment = text[start:end]
            if segment.strip():
                included = [page for left, right, page in spans if left < end and right > start]
                digest = hashlib.sha256(segment.encode()).hexdigest()[:10]
                result.append(TranscriptChunk(chunk_id=f"{document_id}.c{len(result):03d}.{digest}", section=section,
                    page_start=min(included), page_end=max(included), text=segment))
            if end == len(text):
                break
            start = max(start + 1, end - overlap_chars)
    return tuple(result)


_ANNUAL_TOPICS = (
    r"management.{0,12}discussion|business\s+overview|operational\s+review",
    r"consolidated\s+(?:statement\s+of\s+profit|balance\s+sheet|financial\s+statements)",
    r"(?:statement\s+of\s+)?cash\s+flows?|purchase\s+of\s+(?:property|fixed\s+assets)",
    r"segment\s+(?:reporting|revenue|information|results)",
    r"contingent\s+liabilit\w*|capital\s+commitments",
    r"independent\s+auditor|key\s+audit\s+matters",
)


def _annual_page_indices(pdf: Any, max_pages: int) -> tuple[int, ...]:
    """Scan bounded text locally, then retain a diverse <=25-page evidence set.

    Keep cover pages for dates; section anchors plus adjacent pages preserve
    headings/table continuity. Unselected page text is never sent to an LLM.
    PDF page numbers remain the actual 1-based indices, including gaps.
    """
    count = min(len(pdf), max_pages)
    if len(pdf) <= max_pages:
        return tuple(range(count))
    selected = set(range(min(3, count)))
    ranked: list[list[tuple[int, int]]] = [[] for _ in _ANNUAL_TOPICS]
    for index in range(3, min(len(pdf), 1200)):
        text = pdf[index].get_text("text", sort=True)[:16_000]
        if re.search(r"\b(?:table\s+of\s+contents|contents|index)\b", text[:220], re.I):
            continue
        # Section headings outrank incidental references to a statement.
        for topic, expression in enumerate(_ANNUAL_TOPICS):
            hits = list(re.finditer(expression, text, re.I))
            if hits:
                score = min(len(hits), 4) + (5 if hits[0].start() < 700 else 0)
                ranked[topic].append((-score, index))
    for items in ranked:
        items.sort()
    # Round-robin coverage prevents many contingency notes crowding out MD&A.
    for rank in range(max_pages):
        for items in ranked:
            if rank < len(items) and len(selected) < count:
                selected.add(items[rank][1])
        if rank == 0:
            anchors = sorted(selected - {0, 1, 2})
            for index in anchors:
                if len(selected) < count and index + 1 < len(pdf):
                    selected.add(index + 1)
        if len(selected) >= count:
            break
    # Fill spare capacity without dropping previously selected late pages.
    for index in range(len(pdf)):
        if len(selected) >= count:
            break
        selected.add(index)
    return tuple(sorted(selected))


def extract_pdf_text(
    payload: bytes, document: DocumentLink, *, max_pages: int = 25, max_chunk_chars: int = 6000,
    overlap_chars: int = 300, extracted_at: datetime | None = None, resolved_url: str | None = None,
) -> TranscriptExtraction:
    """Retain <=25 pages: first pages for calls, selected sections for annuals."""
    if type(max_pages) is not int or not 1 <= max_pages <= 25:
        raise ValueError("max_pages must be an integer from 1 to 25")
    if not isinstance(payload, bytes) or len(payload) > 20 * 1024 * 1024 or not payload[:1024].lstrip().startswith(b"%PDF-"):
        raise DocumentExtractionError("expected a PDF no larger than 20 MiB")
    pages = []
    try:
        with fitz.open(stream=payload, filetype="pdf") as pdf:
            if pdf.is_encrypted and not pdf.authenticate(""):
                raise DocumentExtractionError("PDF is encrypted and requires a password")
            total = len(pdf)
            indices = (_annual_page_indices(pdf, max_pages) if document.kind == "annual_report"
                       else range(min(total, max_pages)))
            for index in indices:
                text = pdf[index].get_text("text", sort=True)
                pages.append(PdfPageText(page=index + 1, text=text[:80_000], text_truncated=len(text) > 80_000))
    except DocumentExtractionError:
        raise
    except Exception as error:
        raise DocumentExtractionError(f"PDF extraction failed: {type(error).__name__}") from error
    meaningful = sum(len(page.text.strip()) for page in pages)
    truncated = total > max_pages or any(page.text_truncated for page in pages)
    timestamp = _now(extracted_at or utc_now())
    reported_end, period_evidence = _reported_quarter(tuple(pages), timestamp.astimezone(_IST).date())
    month = document.listed_month
    mismatch = bool(month is not None and reported_end is not None and (
        reported_end > _month_end(month.year, month.month) or (month - reported_end).days > 180))
    status = "needs_ocr" if meaningful < 50 else ("partial" if truncated else "ok")
    if mismatch and status == "ok":
        status = "partial"
    return TranscriptExtraction(document=document, resolved_url=resolved_url or document.url,
        pdf_sha256=hashlib.sha256(payload).hexdigest(), extracted_at=timestamp, status=status,
        total_pages=total, pages=tuple(pages), page_selection="annual_sections" if document.kind == "annual_report" else "first_pages",
        chunks=chunk_transcript(tuple(pages), document.document_id,
        max_chars=max_chunk_chars, overlap_chars=overlap_chars) if meaningful >= 50 else (), truncated=truncated,
        reported_quarter_end=reported_end, reported_period_evidence=period_evidence, listing_period_mismatch=mismatch,
        error="No sufficient extractable text; scanned pages may require OCR" if meaningful < 50 else (
            "Listing month and explicit PDF reporting period disagree; do not score this as the listed quarter" if mismatch else None))


@dataclass(frozen=True, slots=True)
class _Resource:
    body: bytes
    url: str
    fetched_at: datetime
    cache_hit: bool


class FundamentalsClient:
    def __init__(self, cache: SQLiteCache | None = None, *, session: Any = None,
                 clock: Callable[[], datetime] = utc_now, timeout_seconds: float = 12.0,
                 url_validator: Callable[[str], str] | None = None,
                 allow_operating_profit_proxy: bool = True, quarter_limit: int = 12) -> None:
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout must be finite and positive")
        if type(quarter_limit) is not int or not 8 <= quarter_limit <= 12:
            raise ValueError("quarter_limit must be from 8 to 12")
        self.cache = cache if cache is not None else SQLiteCache(settings=get_settings().cache)
        self._owns_cache = cache is None
        self.session = session if session is not None else http.Session(impersonate="chrome")
        self._owns_session = session is None
        self.session.headers.update({"Accept-Language": "en-IN,en;q=0.9", "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.8"})
        self._discovered_hosts: set[str] = set()
        self._clock = clock
        self._validate_url = url_validator or (lambda url: validate_public_url(url, trusted_hosts=self._discovered_hosts))
        self.timeout_seconds = timeout_seconds
        self.allow_proxy, self.quarter_limit = allow_operating_profit_proxy, quarter_limit

    def __enter__(self) -> FundamentalsClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_session:
            self.session.close()
        if self._owns_cache:
            self.cache.close()

    def _download(self, ticker: str, url: str, *, max_bytes: int) -> _Resource:
        current = self._validate_url(url)
        with self.cache.track("fundamentals.http", ticker=ticker):
            for _ in range(5):
                try:
                    response = self.session.get(current, timeout=self.timeout_seconds, allow_redirects=False, stream=True)
                    try:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("Location")
                            if not location:
                                raise SourceFetchError("redirect has no destination")
                            current = self._validate_url(urljoin(current, location))
                            continue
                        if response.status_code != 200:
                            raise SourceFetchError(f"source returned HTTP {response.status_code}", response.status_code)
                        chunks, size = [], 0
                        for chunk in response.iter_content(chunk_size=64 * 1024):
                            size += len(chunk)
                            if size > max_bytes:
                                raise SourceFetchError("download exceeded the configured byte limit")
                            chunks.append(chunk)
                        return _Resource(b"".join(chunks), current, _now(self._clock()), False)
                    finally:
                        response.close()
                except SourceFetchError:
                    raise
                except Exception as error:
                    raise SourceFetchError(f"source transport failed: {type(error).__name__}") from error
        raise SourceFetchError("source exceeded the redirect limit")

    def _get(self, ticker: str, url: str, *, data_type: str, ttl: int, max_bytes: int,
             force_refresh: bool, context: Mapping[str, Any] | None = None) -> _Resource:
        url = _public_link(url, SCREENER_ORIGIN)
        if url is None:
            raise SourceFetchError("source URL must use public HTTPS without credentials")
        request_key = CacheKey.for_content(ticker, "request." + data_type, {"url": url, "context": context or {}, "parser": RAW_CACHE_VERSION})
        if not force_refresh:
            pointer = self.cache.get(request_key)
            if pointer.hit and isinstance(pointer.value, dict):
                try:
                    blob_key = CacheKey(ticker, data_type, pointer.value["content_hash"])
                    blob = self.cache.get(blob_key)
                    if blob.hit and isinstance(blob.value, bytes) and blob.metadata:
                        return _Resource(blob.value, blob.metadata["url"], _now(datetime.fromisoformat(blob.metadata["fetched_at"])), True)
                except (ValueError, TypeError, KeyError):
                    self.cache.delete(request_key)
        resource = self._download(ticker, url, max_bytes=max_bytes)
        key = CacheKey.for_content(ticker, data_type, resource.body, context={"url": url})
        self.cache.put(key, resource.body, ttl_seconds=ttl, metadata={"url": resource.url, "fetched_at": resource.fetched_at.isoformat()})
        self.cache.put(request_key, {"content_hash": key.content_hash}, ttl_seconds=ttl)
        return resource

    def _pdf_resource(self, ticker: str, url: str, *, force_refresh: bool, context: Mapping[str, Any]) -> _Resource:
        """Follow observed iframe/embed PDF links, e.g. an exchange PDF viewer.

        No archive URL or filename is guessed. Each actual redirect/download
        still passes public-host validation and the byte limit.
        """
        visited = set()
        for _ in range(3):
            if url in visited:
                raise DocumentExtractionError("PDF viewer linked back to itself")
            visited.add(url)
            resource = self._get(ticker, url, data_type="fundamentals.pdf", ttl=PDF_TTL_SECONDS,
                max_bytes=20 * 1024 * 1024, force_refresh=force_refresh, context=context)
            if resource.body[:1024].lstrip().startswith(b"%PDF-"):
                return resource
            soup = BeautifulSoup(resource.body, "html.parser")
            destination = None
            for node in soup.select("iframe[src], embed[src], object[data], a[href]"):
                target = node.get("src") or node.get("data") or node.get("href")
                candidate = _public_link(target, resource.url) if isinstance(target, str) else None
                if candidate and re.search(r"\.pdf(?:$|[?&#])", unquote(candidate), re.I):
                    destination = candidate
                    break
            if destination is None:
                raise DocumentExtractionError("document endpoint returned HTML/non-PDF content without an embedded PDF link")
            if urlsplit(resource.url).hostname in self._discovered_hosts:
                self._discovered_hosts.add(urlsplit(destination).hostname)
            url = destination
        raise DocumentExtractionError("PDF viewer nesting exceeded the limit")

    def _enrich_schedules(self, company: CompanyFundamentals, html_body: bytes, *, force_refresh: bool) -> CompanyFundamentals:
        soup = BeautifulSoup(html_body, "html.parser")
        # A peer-comparison row can name a different company: use only this
        # page's authoritative company-info, never the first data-row-company-id.
        info = soup.find(id="company-info")
        company_id = info.get("data-company-id") if info else None
        if not isinstance(company_id, str) or not company_id.isdigit():
            return company
        if info.has_attr("data-consolidated") != (company.basis == "consolidated"):
            return CompanyFundamentals.model_validate(company.model_copy(update={"notes": company.notes + (
                "Schedule expansion skipped: page identity/reporting basis is inconsistent.",)}))
        updates: dict[str, Any] = {}
        sources, gaps = list(company.schedule_sources), list(company.data_gaps)
        for section, parent, attribute, wanted in (
            ("balance-sheet", "Other Assets", "balance_sheet", "cash_and_equivalents"),
            ("cash-flow", "Cash from Investing Activity", "cash_flow", "cash_capex_outflow"),
        ):
            table: FinancialTable | None = getattr(company, attribute)
            if table is None or table.row(wanted) is not None:
                continue
            query = {"parent": parent, "section": section}
            if company.basis == "consolidated":
                query["consolidated"] = ""
            url = f"{SCREENER_ORIGIN}/api/company/{company_id}/schedules/?{urlencode(query)}"
            try:
                resource = self._get(company.ticker, url, data_type="fundamentals.schedule." + section,
                    ttl=HTML_TTL_SECONDS, max_bytes=512 * 1024, force_refresh=force_refresh)
                # Do not accept a login/redirect or a different company's URL.
                resolved = urlsplit(resource.url)
                params = parse_qs(resolved.query, keep_blank_values=True)
                if (resolved.hostname != "www.screener.in" or resolved.path != urlsplit(url).path
                        or any(params.get(k) != [v] for k, v in query.items())
                        or ("consolidated" in params) != (company.basis == "consolidated")):
                    raise SourceFetchError("schedule redirected away from its issuer/basis")
                payload = json.loads(resource.body)
                if not isinstance(payload, dict):
                    raise ValueError("schedule must be a JSON object")
                # Only exact, unambiguous cash/purchase labels are eligible.
                candidates = [(label, values) for label, values in payload.items()
                    if isinstance(label, str) and isinstance(values, dict)
                    and _ALIASES.get(_key(label), (None,))[0] == wanted]
                if len(candidates) != 1:
                    raise ValueError("schedule has no unique eligible cash row")
                label, values = candidates[0]
                raw = tuple(_clean(str(values.get(column.label, "")))
                    if isinstance(values.get(column.label, ""), (str, int, float))
                    and not isinstance(values.get(column.label, ""), bool) else "" for column in table.columns)
                scale, _ = _money_scale(soup.find(id=section))
                amounts = tuple(_number(cell, scale) for cell in raw)
                # Purchases in the investing cash-flow schedule are signed
                # outflows. A positive receipt is not silently turned into capex.
                amounts = tuple(value if value is None or (value <= 0 if wanted == "cash_capex_outflow" else value >= 0)
                                else None for value in amounts)
                if not any(value is not None for value in amounts):
                    raise ValueError("schedule has no usable, period-aligned cash values")
                digest = hashlib.sha256(resource.body).hexdigest()
                identity = hashlib.sha256((company.ticker + url + digest).encode()).hexdigest()[:24]
                source = SourceDocument(source_id="screener.schedule." + identity, ticker=company.ticker,
                    kind="screener_financials", title=f"{parent} schedule ({company.basis})",
                    publisher="Screener.in standardized financial schedule", url=resource.url,
                    published_on=resource.fetched_at.astimezone(_IST).date(), retrieved_at=resource.fetched_at,
                    content_hash=digest)
                row = FinancialRow(metric=wanted, source_label=_clean(label), unit="inr_crore", values=amounts,
                    raw_values=raw, source_id=source.source_id)
                updates[attribute] = FinancialTable.model_validate(table.model_copy(update={"rows": table.rows + (row,)}))
                sources.append(source)
            except (SourceFetchError, ValueError, TypeError) as error:
                gaps.append(DataGap(field=wanted, reason="source_unavailable",
                    detail=f"Optional {section} schedule unavailable or invalid ({type(error).__name__}); no cash value was inferred."))
        return CompanyFundamentals.model_validate(company.model_copy(update={**updates,
            "schedule_sources": tuple(sources), "data_gaps": tuple(gaps)}))

    def fetch(self, ticker: str, *, include_transcripts: bool = False, force_refresh: bool = False,
              max_pdf_pages: int = 25, document_limit: int = 4) -> CompanyFundamentals:
        if type(document_limit) is not int or not 1 <= document_limit <= 12:
            raise ValueError("document_limit must be an integer from 1 to 12")
        ticker = normalize_ticker(ticker)
        failures = []
        result = None
        consolidated_candidate = None
        winning_body, consolidated_body = None, None
        for suffix, requested_basis in (("consolidated/", "consolidated"), ("", "standalone")):
            url = f"{SCREENER_ORIGIN}/company/{quote(ticker, safe='')}/{suffix}"
            try:
                resource = self._get(ticker, url, data_type="fundamentals.html", ttl=HTML_TTL_SECONDS,
                    max_bytes=6 * 1024 * 1024, force_refresh=force_refresh)
            except SourceFetchError as error:
                if requested_basis == "consolidated" and error.status_code in (404, 410):
                    failures.append(str(error))
                    continue
                if requested_basis == "standalone" and consolidated_candidate is not None:
                    result, winning_body = consolidated_candidate, consolidated_body
                    failures.append("Standalone check unavailable: " + str(error))
                    break
                raise
            with self.cache.track("fundamentals.calculate", ticker=ticker, tier=0):
                try:
                    effective_basis = ("standalone" if "/company/" in urlsplit(resource.url).path
                        and "/consolidated" not in urlsplit(resource.url).path else requested_basis)
                    result = parse_screener_html(resource.body, ticker, source_url=resource.url, fetched_at=resource.fetched_at,
                        basis=effective_basis, quarter_limit=self.quarter_limit,
                        allow_operating_profit_proxy=self.allow_proxy, cache_hit=resource.cache_hit,
                        document_limit=document_limit)
                    winning_body = resource.body
                except FundamentalsError as error:
                    if requested_basis == "consolidated" and "no usable core financial tables" in str(error):
                        failures.append(str(error))
                        continue
                    if requested_basis == "standalone" and consolidated_candidate is not None:
                        result, winning_body = consolidated_candidate, consolidated_body
                        failures.append("Standalone check has no usable financials")
                        break
                    raise
            if requested_basis == "consolidated" and not _usable(result.quarters):
                # Some standalone-only issuers retain OLD consolidated annual
                # rows. Annual data alone must not prevent the quarterly fallback.
                consolidated_candidate = result
                consolidated_body = resource.body
                failures.append("Consolidated quarterly table is empty; checked standalone without mixing bases")
                continue
            break
        if result is None:
            raise FundamentalsError("No usable consolidated or standalone fundamentals: " + "; ".join(failures))
        if winning_body is not None:
            result = self._enrich_schedules(result, winning_body, force_refresh=force_refresh)
        if failures:
            label = "Standalone fallback: " if result.basis == "standalone" else "Consolidated snapshot retained: "
            result = CompanyFundamentals.model_validate(result.model_copy(update={"notes": result.notes + (label + "; ".join(failures),)}))
        for document in (*result.documents.concall_transcripts, *result.documents.annual_reports, *result.documents.investor_presentations):
            for url in (str(document.url), *document.alternate_urls):
                self._discovered_hosts.add(urlsplit(url).hostname)
        if include_transcripts:
            extracted = tuple(self.fetch_transcript(document, force_refresh=force_refresh, max_pages=max_pdf_pages)
                              for document in result.documents.concall_transcripts)
            result = CompanyFundamentals.model_validate(result.model_copy(update={"transcripts": extracted}))
        return result

    def fetch_transcript(self, document: DocumentLink, *, force_refresh: bool = False, max_pages: int = 25,
                         max_chunk_chars: int = 6000, overlap_chars: int = 300) -> TranscriptExtraction:
        if type(max_pages) is not int or not 1 <= max_pages <= 25:
            raise ValueError("max_pages must be an integer in 1..25")
        context = {"kind": document.kind, "label": document.listed_label, "document_id": document.document_id}
        errors = []
        for url in (str(document.url), *document.alternate_urls):
            try:
                resource = self._pdf_resource(document.ticker, url, force_refresh=force_refresh, context=context)
                key = CacheKey.for_content(document.ticker, "fundamentals.pdf_text", resource.body, context={
                    "version": ANNUAL_EXTRACTOR_VERSION if document.kind == "annual_report" else EXTRACTOR_VERSION,
                    "engine": fitz.VersionBind, "max_pages": max_pages,
                    "chunk_chars": max_chunk_chars, "overlap": overlap_chars, "document_id": document.document_id,
                    "listed_label": document.listed_label, "listed_month": document.listed_month.isoformat() if document.listed_month else None})
                if not force_refresh:
                    cached = self.cache.get(key, model=TranscriptExtraction)
                    if cached.hit:
                        return TranscriptExtraction.model_validate(cached.value.model_copy(update={"cache_hit": True, "document": document}))
                with self.cache.track("fundamentals.pdf_extract", ticker=document.ticker, tier=0):
                    result = extract_pdf_text(resource.body, document, max_pages=max_pages, max_chunk_chars=max_chunk_chars,
                        overlap_chars=overlap_chars, extracted_at=_now(self._clock()), resolved_url=resource.url)
            except (SourceFetchError, DocumentExtractionError) as error:
                errors.append(str(error))
                continue
            self.cache.put(key, result, ttl_seconds=PDF_TTL_SECONDS)
            return result
        return TranscriptExtraction(document=document, resolved_url=None, pdf_sha256=None, extracted_at=_now(self._clock()),
            status="error", total_pages=None, pages=(), chunks=(), truncated=False, error="; ".join(errors))

    def fetch_concall_notes(self, document: DocumentLink, *, force_refresh: bool = False) -> SecondaryConcallNotes:
        """Optional public Screener notes, explicitly outside primary-source text."""
        if document.embedded_notes:
            return SecondaryConcallNotes(document_id=document.document_id, url=document.notes_url,
                text=document.embedded_notes, status="available")
        if document.notes_url is None:
            return SecondaryConcallNotes(document_id=document.document_id, url=None, text=None,
                status="unavailable", reason="No public or embedded notes link was listed")
        try:
            resource = self._get(document.ticker, str(document.notes_url), data_type="fundamentals.secondary_notes",
                ttl=HTML_TTL_SECONDS, max_bytes=1024 * 1024, force_refresh=force_refresh)
            soup = BeautifulSoup(resource.body, "html.parser")
            if "/login" in urlsplit(resource.url).path or soup.select_one('form[action*="login"]'):
                raise SourceFetchError("Notes require login; authentication was not attempted")
            for node in soup.select("script, style, nav, header, footer"):
                node.decompose()
            content = soup.select_one(".concall-summary, .markdown, article, main") or soup.body or soup
            text = _clean(content.get_text(" ", strip=True))
            if len(text) < 40:
                raise SourceFetchError("No substantive public notes text was returned")
            return SecondaryConcallNotes(document_id=document.document_id, url=document.notes_url,
                text=text, status="available")
        except SourceFetchError as error:
            return SecondaryConcallNotes(document_id=document.document_id, url=document.notes_url, text=None,
                status="unavailable", reason=str(error))


def fetch_fundamentals(ticker: str, *, cache: SQLiteCache | None = None, include_transcripts: bool = False,
                       force_refresh: bool = False) -> CompanyFundamentals:
    with FundamentalsClient(cache) as client:
        return client.fetch(ticker, include_transcripts=include_transcripts, force_refresh=force_refresh)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker")
    parser.add_argument("--transcripts", action="store_true", help="download/extract the four selected transcript candidates")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--strict-ebitda", action="store_true", help="do not use Operating Profit as an EBITDA proxy")
    parser.add_argument("--max-pages", type=int, default=25)
    args = parser.parse_args()
    try:
        with FundamentalsClient(allow_operating_profit_proxy=not args.strict_ebitda) as client:
            result = client.fetch(args.ticker, include_transcripts=args.transcripts, force_refresh=args.refresh,
                                  max_pdf_pages=args.max_pages)
        print(result.model_dump_json(indent=2))
    except (FundamentalsError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
