"""Pillar 2: cookie-warmed NSE JSON, conservative Tier-0 rules and demo fallback.

    python -m backend.data.announcements --demo
    python -m backend.data.announcements RELIANCE TCS
    python -m backend.data.announcements --write-seed /tmp/seed_announcements.json

The bundled/generated seeds are explicitly SYNTHETIC, have no fake attachment
URLs, and cannot be used as primary evidence in PullResearchReport. A successful
empty exchange response never causes fabricated filings to appear. Unknown or
conflicting classifications stay visible for later Tier-1 resolution.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import math
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Literal, Mapping
from urllib.parse import urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from pydantic import AwareDatetime, computed_field, model_validator
from requests.adapters import HTTPAdapter

from ..cache import CacheKey, SQLiteCache
from ..config import (
    CATEGORY_POLICIES,
    DEFAULT_PORTFOLIO,
    MARKET_TIMEZONE,
    AnnouncementCategory,
    get_settings,
    normalize_ticker,
)
from ..schemas import (
    Citation,
    CorporateAnnouncement,
    Count,
    EvidenceText,
    ExecutiveBio,
    FinancialSnapshot,
    ManagementBioEnrichment,
    NumericFact,
    ReportingPeriod,
    ResultsHeadlineTable,
    SourceDocument,
    StrictModel,
    Ticker,
    utc_now,
)

LOGGER = logging.getLogger(__name__)
NSE_HOME_URL = "https://www.nseindia.com"
NSE_API_URL = NSE_HOME_URL + "/api/corporate-announcements"
NSE_FILINGS_URL = NSE_HOME_URL + "/companies-listing/corporate-filings-announcements"
SEED_PATH = Path(__file__).with_name("seed_announcements.json")
ADAPTER_VERSION = "nse-v1"
CLASSIFIER_VERSION = "regex-v2"
LIVE_CACHE_TTL_SECONDS = 300
_IST = ZoneInfo(MARKET_TIMEZONE)
_MAX_BODY_BYTES = 8 * 1024 * 1024
_MAX_SEED_BYTES = 8 * 1024 * 1024
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
_CAT = AnnouncementCategory


class NSEFetchError(RuntimeError):
    """Expected source/network failure; safe to report a concise status."""


class NSEBlockedError(NSEFetchError):
    """Rate limiting, an access-denied response or an HTML challenge."""


class NSEPayloadError(NSEFetchError):
    """Exchange response could not be interpreted without inventing data."""


class SeedDataError(ValueError):
    """Malformed demo fixture; an application error, not a live exchange outage."""


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", unicodedata.normalize("NFKC", value)))).strip()


def _patterns(*expressions: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(expression, re.IGNORECASE) for expression in expressions)


_RULES: Mapping[AnnouncementCategory, tuple[re.Pattern[str], ...]] = {
    _CAT.RESULTS: _patterns(r"\b(?:financial|quarterly|annual|audited|unaudited)\s+results\b", r"\bearnings\s+(?:release|results)\b"),
    _CAT.M_AND_A: _patterns(r"\b(?:acquisitions?|mergers?|demergers?|amalgamation|joint\s+venture)\b",
        r"\b(?:invest(?:ment|ing)?\s+in|incorporation\s+of|sale\s+of\s+(?:a\s+)?stake\s+in)\s+(?:a\s+)?(?:wholly[- ]owned\s+)?subsidiary\b"),
    _CAT.FUND_RAISE: _patterns(r"\b(?:rights?\s+issue|qip|qualified\s+institutions?\s+placement|preferential\s+(?:issue|allotment)|buy[- ]?back|fund\s*rais(?:e|ing))\b",
        r"\b(?:issu(?:e|ance)|allotment|raising)\b.{0,100}\b(?:commercial\s+paper|debentures?|bonds?|equity\s+capital)\b"),
    _CAT.MANAGEMENT_CHANGE: _patterns(
        r"\bchanges?\s+in\s+(?:management|directors?)\b",
        r"\b(?:appoint\w*|resign\w*|retir\w*|cessation|re[- ]?designat\w*)\b.{0,130}\b(?:ceo|cfo|chief\s+(?:executive|financial)\s+officer|managing\s+director|whole[- ]time\s+director|independent\s+director|director|key\s+managerial\s+personnel)\b",
        r"\b(?:ceo|cfo|chief\s+(?:executive|financial)\s+officer|managing\s+director|independent\s+director)\b.{0,100}\b(?:appoint\w*|resign\w*|retir\w*|cessation)\b"),
    _CAT.LITIGATION: _patterns(r"\b(?:litigation|lawsuit|penalt(?:y|ies)|fines?|show[- ]cause|regulatory\s+order|insolvency|nclt|ibc\s+proceedings|warning\s+letter)\b"),
    _CAT.INSIDER_TRADING: _patterns(r"\b(?:insider\s+trading\b|prohibition\s+of\s+insider\b|sebi\s*\(?pit\b\)?|regulation\s+7\s*\(2\))",
        r"\b(?:promoter|insider)\b.{0,100}\b(?:acquir\w*|purchas\w*|bought|sold|sell\w*|dispos\w*)\b.{0,50}\bshares?\b"),
    _CAT.EARNINGS_CALL: _patterns(r"\b(?:earnings?\s*(?:conference\s*)?call|conference\s+call|concall|transcript|audio\s+recording|call\s+recording)\b"),
    _CAT.INVESTOR_MEETINGS: _patterns(r"\b(?:investor|analyst)\s*(?:/\s*(?:investor|analyst))?\s*(?:meet(?:ing)?s?|conference|interaction)\b",
        r"\b(?:one[- ]on[- ]one|group\s+meeting|investor\s+roadshow)\b"),
    _CAT.CREDIT_RATING: _patterns(r"\b(?:credit\s+rating|rating\s+(?:reaffirm|upgrade|downgrade)|rating\s+action)\b",
        r"\b(?:icra|crisil|care\s+ratings|india\s+ratings|fitch)\b.{0,100}\b(?:assign|reaffirm|upgrad|downgrad)\w*\b"),
    _CAT.DIVIDEND: _patterns(r"\bdividend\b"),
    _CAT.BONUS_SPLIT: _patterns(r"\b(?:bonus\s+(?:issue|shares?)|stock\s+split|share\s+split|sub[- ]?division\s+of\s+(?:equity\s+)?shares?)\b"),
    _CAT.CAPEX: _patterns(r"\b(?:capex|capital\s+expenditure|capacity\s+expansion|new\s+(?:plant|facility)|greenfield\s+(?:plant|project))\b"),
    _CAT.PRESS_RELEASE: _patterns(r"\b(?:press\s+release|order\s+win|new\s+product|product\s+launch|partnership|rumou?r\s+clarification|awarded\s+(?:a\s+)?contract)\b"),
    _CAT.POSTAL_BALLOT: _patterns(r"\bpostal\s+ballot\b"),
    _CAT.AGM_EGM: _patterns(r"\b(?:agm|egm|annual\s+general\s+meeting|extraordinary\s+general\s+meeting|extra[- ]ordinary\s+general\s+meeting)\b"),
    _CAT.PLEDGING: _patterns(r"\b(?:pledg\w*|encumbrances?|invocation\s+of\s+pledge)\b",
        r"\b(?:SAST|substantial acquisition of shares)\b.{0,100}\bregulation\s+31\s*\([123]\)"),
    _CAT.UNCLASSIFIED: _patterns(r"\b(?:esop|esps|compliance\s+certificate|reconciliation\s+of\s+share\s+capital|closure\s+of\s+trading\s+window|trading\s+window\s+closure|esg\s+report|sustainability\s+report)\b",
        r"\b(?:regulation\s+74\s*\(5\)|regulation\s+7\s*\(3\))",
        r"\bappoint\w*\b.{0,80}\b(?:statutory|secretarial|internal)\s+auditors?\b"),
}
_SPECIFIC_ORDER = (
    _CAT.LITIGATION, _CAT.MANAGEMENT_CHANGE, _CAT.M_AND_A, _CAT.RESULTS,
    _CAT.PLEDGING, _CAT.FUND_RAISE, _CAT.CREDIT_RATING, _CAT.CAPEX,
    _CAT.INSIDER_TRADING, _CAT.EARNINGS_CALL, _CAT.DIVIDEND, _CAT.BONUS_SPLIT,
    _CAT.POSTAL_BALLOT, _CAT.AGM_EGM, _CAT.INVESTOR_MEETINGS, _CAT.PRESS_RELEASE, _CAT.UNCLASSIFIED,
)
_ROUTINE = frozenset({_CAT.UNCLASSIFIED, _CAT.POSTAL_BALLOT, _CAT.AGM_EGM})


@dataclass(frozen=True, slots=True)
class RuleClassification:
    category: AnnouncementCategory
    confidence: float
    routine_confirmed: bool
    requires_llm: bool
    matched_categories: tuple[AnnouncementCategory, ...]
    reason: str


def classify_announcement(subject: str, description: str = "") -> RuleClassification:
    """All 17 reference categories; specific material content outranks routine labels.

    Rules are deterministic event classification, not a summary or an assertion
    of financial impact. Unmatched input is unclassified but NOT auto-hidden.
    """
    if not isinstance(subject, str) or not isinstance(description, str):
        raise ValueError("subject and description must be strings")
    subject = _text(subject)
    text = subject + " " + _text(description)
    matches = {category for category, rules in _RULES.items() if any(rule.search(text) for rule in rules)}
    subject_matches = {category for category, rules in _RULES.items() if any(rule.search(subject) for rule in rules)}
    # PIT regulations appear in routine trading-window notices. They do not
    # establish an insider transaction without actual share-dealing wording.
    window_notice = re.search(r"\b(?:closure\s+of\s+(?:the\s+)?trading\s+window|trading\s+window\s+closure)\b", text, re.I)
    dealing = re.search(r"\b(?:purchas\w*|bought|sold|sell\w*|dispos\w*|acquir\w*)\b.{0,60}\b(?:shares?|securities)\b", text, re.I)
    if window_notice and not dealing:
        matches.discard(_CAT.INSIDER_TRADING)
        subject_matches.discard(_CAT.INSIDER_TRADING)
        matches.add(_CAT.UNCLASSIFIED)
    # LODR 31 shareholding-pattern reports are not SAST pledge disclosures.
    if re.search(r"\bshareholding\s+pattern\b", text, re.I) and not re.search(r"\b(?:pledg\w*|encumbrances?)\b", text, re.I):
        matches.discard(_CAT.PLEDGING)
        matches.add(_CAT.UNCLASSIFIED)
    if _CAT.INSIDER_TRADING in matches and (_CAT.INSIDER_TRADING in subject_matches or not re.search(r"\b(?:merger|demerger|joint\s+venture|subsidiary|business\s+acquisition)\b", text, re.I)):
        matches.discard(_CAT.M_AND_A)  # a promoter buying personal shares is not M&A
    if _CAT.PLEDGING in matches:
        matches.discard(_CAT.INSIDER_TRADING)
    if _CAT.EARNINGS_CALL in matches and _CAT.RESULTS not in subject_matches:
        matches.discard(_CAT.RESULTS)  # transcript discussing results is not a new results release
        matches.discard(_CAT.INVESTOR_MEETINGS)
    if not matches:
        return RuleClassification(_CAT.UNCLASSIFIED, 0.0, False, True, (), "No reliable regex match; keep visible for Tier 1")
    material = {category for category in matches if CATEGORY_POLICIES[category].priority >= 4}
    subject_material = material & subject_matches
    candidates = subject_material if len(subject_material) == 1 else material or matches
    category = next(category for category in _SPECIFIC_ORDER if category in candidates)
    ambiguous = len(material) > 1
    # Generic press-release labels alone do not establish a meaningful subtype.
    generic_press = category == _CAT.PRESS_RELEASE and _text(text).casefold() in {"press release", "press release press release"}
    requires_llm = ambiguous or generic_press
    confidence = 0.70 if requires_llm else (0.99 if category in subject_matches else 0.96)
    routine = category in _ROUTINE and not requires_llm and not material
    ordered = tuple(category for category in _SPECIFIC_ORDER if category in matches)
    reason = f"Regex {CLASSIFIER_VERSION}: {category.value}" + ("; conflicting material signals need Tier 1" if ambiguous else "; explicit event wording")
    return RuleClassification(category, confidence, routine, requires_llm, ordered, reason)


def _period(end: date) -> dict[str, str]:
    start = date(end.year, ((end.month - 1) // 3) * 3 + 1, 1)
    return {"start_on": start.isoformat(), "end_on": end.isoformat(), "kind": "quarter", "label": f"Quarter ended {end.isoformat()}"}


def generate_seed_announcements(path: str | Path | None = None, *, anchor: date = date(2026, 10, 29)) -> dict[str, Any]:
    """Generate a reproducible rich fixture; same anchor gives identical bytes.

    Nothing here asserts that the named issuers actually made these disclosures.
    The loader rebases demo publication times to its explicit demo clock while
    preserving the accounting periods printed in this fixture.
    """
    quarter_end = date(anchor.year, ((anchor.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)
    previous_end = date(quarter_end.year, ((quarter_end.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)
    year_ago_end = date(quarter_end.year - 1, quarter_end.month, quarter_end.day)
    revenues = (260000, 65000, 85000, 42000, 110000, 13000, 67000, 19000, 3500, 12000, 7500, 5200, 2500, 1500, 1100, 450)
    records: list[dict[str, Any]] = []
    for index, (ticker, revenue) in enumerate(zip(DEFAULT_PORTFOLIO, revenues)):
        financials = {}
        for name, end, factor in (("current", quarter_end, 1.0), ("previous_quarter", previous_end, 0.94), ("year_ago", year_ago_end, 0.86)):
            value = round(revenue * factor, 2)
            financials[name] = {"period": _period(end), "revenue": value,
                "ebitda": None if ticker == "HDFCBANK" else round(value * (0.18 + index % 4 * 0.01), 2),
                "pbt": round(value * 0.14, 2), "pat": round(value * 0.105, 2)}
        records.append({"seed_id": f"{ticker.lower()}-results", "symbol": ticker,
            "desc": "Financial Results - Board approval of unaudited consolidated quarterly results",
            "attchmntText": f"SYNTHETIC FINN DEMO ONLY. Illustrative board approval for the quarter ended {quarter_end.isoformat()}. Revenue INR {revenue:,.2f} crore; PBT INR {revenue * 0.14:,.2f} crore; PAT INR {revenue * 0.105:,.2f} crore. Comparative quarter and year-ago figures are included in the demo headline table. These are invented figures, not issuer disclosures.",
            "minutes_before_anchor": 35 + index * 9, "demo_financials": financials})
    examples = (
        ("INFY", "management", "Appointment of Chief Financial Officer", "Illustrative CFO appointment effective next month. Incoming Demo Executive A previously led finance at Demo Manufacturing Group, with 22 years in treasury, controls and capital allocation. Outgoing Demo Executive B retires after a planned transition; a 30-day handover is included."),
        ("RELIANCE", "subsidiary", "Acquisition of a subsidiary stake", "Illustrative acquisition of a 75% stake in Demo Storage Platforms Private Limited for INR 1,250 crore in cash. The remaining 25% is retained by the fictional founders. Closing is subject to customary approvals; no relationship with promoters is assumed."),
        ("DIXON", "plant", "Capacity expansion - new manufacturing facility", "Illustrative board-approved capex of INR 1,500 crore for a new electronics plant in Noida. Planned capacity is 8 million units per year, with phased commissioning over 24 months. Funding is a mix of internal accruals and term loans."),
        ("KAYNES", "pledge", "Disclosure of promoter share pledge under Regulation 31(1)", "Illustrative creation of a pledge over 2.0% of outstanding equity held by a promoter entity. Total encumbered promoter holding rises from 3.0% to 5.0% of outstanding equity. The fictional lender is Demo Bank and the stated purpose is a promoter-level facility."),
        ("SUNPHARMA", "legal", "Regulatory order and monetary penalty", "Illustrative regulatory order imposing an INR 3.5 crore penalty. The company intends to pursue the available appellate remedy. Financial impact beyond the stated amount has not been quantified in this fictional filing."),
        ("POLYCAB", "funding", "Issuance of commercial paper", "Illustrative issuance of INR 500 crore commercial paper with a 90-day tenor for working-capital funding. The fictional allotment is on normal commercial terms; repayment and rating details are included in the simulated disclosure."),
        ("PERSISTENT", "insider", "Disclosure under SEBI PIT Regulation 7(2) - promoter acquisition of shares", "Illustrative promoter purchase of 100,000 equity shares through market transactions. This is a personal shareholding change, not a corporate acquisition or a subsidiary investment."),
        ("TCS", "call", "Earnings conference call transcript", "Transcript of an illustrative earnings conference call to discuss quarterly financial results, customer spending, utilization and operating margins. This is a transcript document, not a new results release."),
        ("TCS", "meetings", "Schedule of investor meetings", "Illustrative one-on-one investor meetings with Demo Long-Term Fund and Demo Capital on the next business day. No unpublished price-sensitive information is intended to be shared."),
        ("LT", "rating", "Credit Rating - reaffirmation", "Illustrative reaffirmation of long-term AAA / Stable and short-term A1+ credit ratings by Demo Ratings. There is no new issue of securities in this simulated rating update."),
        ("ITC", "dividend", "Declaration of interim dividend and record date", "Illustrative interim dividend of INR 6.50 per equity share. The record date is seven calendar days after this demo's anchor date and payment is proposed within 30 days."),
        ("TRENT", "split", "Board approves bonus issue", "Illustrative 1:1 bonus issue, subject to shareholder approval. Each eligible shareholder would receive one additional share for each share held; the record date will be announced separately."),
        ("LT", "orders", "Press Release - order win", "Illustrative order win of INR 2,450 crore for an industrial electrification project. The fictional execution schedule is 30 months. The release does not state the contract's expected margin."),
        ("MAXHEALTH", "ballot", "Postal Ballot Notice", "Illustrative notice of postal ballot for a registered-office administrative resolution. Electronic voting remains open for 30 days; the scrutinizer's report will be filed separately."),
        ("TMPV", "agm", "Notice of Annual General Meeting", "Illustrative AGM notice, attendance instructions and electronic voting procedures. This routine notice contains meeting logistics and no new financial result or material transaction."),
        ("KPITTECH", "esop", "ESOP allotment", "Illustrative allotment of 12,000 equity shares pursuant to employee stock-option exercises. The filing records a routine change in paid-up share capital under an existing employee plan."),
        ("KAYNES", "compliance", "Compliance Certificate under Regulation 74(5)", "Illustrative registrar certificate confirming dematerialisation and reconciliation compliance for the quarter. This is routine administrative paperwork."),
    )
    for index, (ticker, seed_id, subject, description) in enumerate(examples):
        record = {"seed_id": f"{ticker.lower()}-{seed_id}", "symbol": ticker, "desc": subject,
            "attchmntText": "SYNTHETIC FINN DEMO ONLY. " + description, "minutes_before_anchor": 25 + index * 11}
        if seed_id == "management":
            record["demo_executives"] = {"incoming": "Demo Executive A", "outgoing": "Demo Executive B"}
        records.append(record)
    payload = {"schema_version": 1, "is_demo": True, "accounting_anchor": anchor.isoformat(),
        "notice": "All filings, financial figures, deals and executive names are synthetic FINN demo fixtures, not statements about actual issuers.",
        "records": records}
    if path is not None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    return payload


def _json_loads(value: str | bytes) -> Any:
    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-finite JSON constant: {constant}")
    return json.loads(value, parse_constant=reject_constant)


def load_seed_announcements(path: str | Path | None = None, *, as_of: datetime | None = None) -> list[dict[str, Any]]:
    now = as_of or utc_now()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("seed as_of must be timezone-aware")
    chosen = Path(path) if path is not None else SEED_PATH
    try:
        if chosen.exists():
            if chosen.stat().st_size > _MAX_SEED_BYTES:
                raise SeedDataError("demo seed exceeds size limit")
            payload = _json_loads(chosen.read_bytes())
        elif path is not None:
            raise SeedDataError("requested demo seed file does not exist")
        else:
            payload = generate_seed_announcements()  # works on a read-only installation
        if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1 or payload.get("is_demo") is not True:
            raise SeedDataError("seed must declare schema_version=1 and is_demo=true")
        records = payload.get("records")
        if not isinstance(records, list) or not records:
            raise SeedDataError("seed needs a nonempty records array")
        rows = []
        ids = set()
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("seed_id"), str):
                raise SeedDataError("each seed record needs a string seed_id")
            if record["seed_id"] in ids:
                raise SeedDataError("seed IDs must be unique")
            ids.add(record["seed_id"])
            age = record.get("minutes_before_anchor")
            if type(age) is not int or age < 0:
                raise SeedDataError("seed ages must be nonnegative integer minutes")
            if not isinstance(record.get("desc"), str) or not isinstance(record.get("attchmntText"), str):
                raise SeedDataError("seed subject and description must be strings")
            normalize_ticker(record.get("symbol"))
            published = now - timedelta(minutes=age)
            rows.append({**record, "symbol": normalize_ticker(record["symbol"]), "seq_id": "demo-" + record["seed_id"],
                "an_dt": published.astimezone(_IST).strftime("%d-%b-%Y %H:%M:%S"),
                "attchmntFile": None, "is_demo": True})
        return rows
    except (OSError, ValueError, TypeError, OverflowError) as error:
        if isinstance(error, SeedDataError):
            raise
        raise SeedDataError(f"invalid seed fixture: {type(error).__name__}") from error


def _parse_date(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing exchange publication timestamp")
    text = value.strip()
    if not re.search(r"\d{1,2}:\d{2}", text):
        raise ValueError("exchange publication date needs a time, not an invented midnight")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for pattern in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError("unsupported exchange timestamp")
    return (parsed.replace(tzinfo=_IST) if parsed.tzinfo is None else parsed).astimezone(timezone.utc)


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _attachment(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or value.strip().lower() in {"na", "null", "-"}:
        return None
    url = urljoin("https://nsearchives.nseindia.com/", value.strip())
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or not (host == "nseindia.com" or host.endswith(".nseindia.com")):
        return None
    return url


def _demo_results(data: Mapping[str, Any], source_id: str) -> ResultsHeadlineTable:
    snapshots = {}
    for name in ("current", "previous_quarter", "year_ago"):
        values = data[name]
        period = ReportingPeriod.model_validate_json(json.dumps(values["period"]))
        facts = {}
        for metric in ("revenue", "ebitda", "pbt", "pat"):
            value = values[metric]
            facts[metric] = NumericFact(metric=metric, value=value, unit="inr_crore", period=period,
                basis="consolidated", citations=(Citation(source_id=source_id, section="Synthetic demo headline table"),) if value is not None else (),
                missing_reason=None if value is not None else "Not provided or not applicable in this synthetic fixture")
        snapshots[name] = FinancialSnapshot(period=period, basis="consolidated", **facts)
    return ResultsHeadlineTable(**snapshots)


def normalize_announcement(
    row: Mapping[str, Any], ticker: str, *, received_at: datetime,
    source_url: str = NSE_API_URL, is_demo: bool = False,
) -> CorporateAnnouncement:
    """Metadata normalization; no invented live numbers or PDF-derived biographies."""
    ticker = normalize_ticker(ticker)
    if normalize_ticker(row.get("symbol")) != ticker:
        raise ValueError("exchange row belongs to a different ticker")
    subject = row.get("desc") or row.get("subject")
    description = row.get("attchmntText") or row.get("description") or row.get("details") or subject
    if not isinstance(subject, str) or not isinstance(description, str) or not _text(subject):
        raise ValueError("exchange row needs textual subject/description")
    subject, description = _text(subject), _text(description)
    published = _parse_date(row.get("an_dt") or row.get("broadcastDateTime") or row.get("sort_date") or row.get("dt"))
    if published > received_at:
        raise ValueError("exchange publication timestamp is after retrieval")
    classification = classify_announcement(subject, description)
    canonical = json.dumps(dict(row), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    external_id = row.get("seq_id") or row.get("id")
    identity = str(external_id) if external_id is not None else f"{published.isoformat()}|{subject}|{row.get('attchmntFile', '')}"
    identifier = ("demo." if is_demo else "nse.") + hashlib.sha256(f"{ticker}|{identity}".encode()).hexdigest()[:32]
    filing = SourceDocument.model_validate_json(json.dumps({
        "source_id": identifier, "ticker": ticker, "kind": "exchange_filing", "is_demo": is_demo,
        "title": _short(("[DEMO] " if is_demo else "") + "Announcement metadata: " + subject, 180),
        "publisher": "FINN synthetic demo fixture" if is_demo else "National Stock Exchange of India",
        "url": None if is_demo else source_url, "published_on": published.astimezone(_IST).date().isoformat(),
        "retrieved_at": received_at.isoformat(), "content_hash": hashlib.sha256(canonical).hexdigest(),
    }))
    citation = Citation(source_id=identifier, section="Synthetic demo metadata" if is_demo else "NSE announcement metadata")
    enrichment = None
    if is_demo and row.get("demo_executives"):
        executives = row["demo_executives"]
        enrichment = ManagementBioEnrichment(
            incoming=(ExecutiveBio(name=executives["incoming"], role="Illustrative incoming CFO",
                background=(EvidenceText(text="Synthetic biography: 22 years in treasury, controls and capital allocation at Demo Manufacturing Group.", citations=(citation,)),)),),
            outgoing=(ExecutiveBio(name=executives["outgoing"], role="Illustrative outgoing CFO",
                missing_background_reason="No further biography in this synthetic fixture"),),
        )
    return CorporateAnnouncement(
        announcement_id=identifier, ticker=ticker, exchange="NSE",
        title=_short(("[DEMO] " if is_demo else "") + subject, 180), description=description,
        attachment_url=None if is_demo else _attachment(row.get("attchmntFile")),
        published_at=published, received_at=received_at, category=classification.category,
        classification_tier=0, classification_confidence=classification.confidence,
        classification_reason=classification.reason, requires_llm_classification=classification.requires_llm,
        routine_confirmed=classification.routine_confirmed, filing=filing,
        summary=EvidenceText(text=_short(description, 1200), citations=(citation,)),
        results_headline_table=_demo_results(row["demo_financials"], identifier) if is_demo and row.get("demo_financials") else None,
        management_bio_enrichment=enrichment,
    )


class AnnouncementBatch(StrictModel):
    ticker: Ticker
    source_mode: Literal["live", "demo", "unavailable"]
    cache_hit: bool
    fetched_at: AwareDatetime
    window_start: AwareDatetime
    window_end: AwareDatetime
    announcements: tuple[CorporateAnnouncement, ...]
    invalid_rows: Count = 0
    fallback_reason: str | None = None

    @model_validator(mode="after")
    def consistent_origin(self) -> AnnouncementBatch:
        if self.window_start >= self.window_end or self.fetched_at > self.window_end:
            raise ValueError("announcement batch has inconsistent time bounds")
        if any(item.ticker != self.ticker for item in self.announcements):
            raise ValueError("announcement batch mixes ticker identities")
        if self.source_mode == "live" and any(item.is_demo for item in self.announcements):
            raise ValueError("synthetic filings cannot be labeled live")
        if self.source_mode == "demo" and (not self.announcements or any(not item.is_demo for item in self.announcements)):
            raise ValueError("demo batches must contain exclusively synthetic filings")
        if self.source_mode == "unavailable" and self.announcements:
            raise ValueError("unavailable batches cannot contain filings")
        return self

    @computed_field
    @property
    def pending_tier1_count(self) -> int:
        return sum(item.requires_llm_classification for item in self.announcements)

    @computed_field
    @property
    def visible_count(self) -> int:
        return sum(not item.collapse_in_digest for item in self.announcements)


class NSEAnnouncementsClient:
    def __init__(
        self, cache: SQLiteCache | None = None, *, session: requests.Session | None = None,
        seed_path: str | Path | None = None, allow_seed_fallback: bool = True,
        connect_timeout_seconds: float = 4.0, read_timeout_seconds: float = 8.0,
        min_request_interval_seconds: float = 0.5, clock: Callable[[], datetime] = utc_now,
    ) -> None:
        for value in (connect_timeout_seconds, read_timeout_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("HTTP timeouts must be finite and positive")
        if isinstance(min_request_interval_seconds, bool) or not math.isfinite(min_request_interval_seconds) or min_request_interval_seconds < 0:
            raise ValueError("request interval must be finite and nonnegative")
        self.cache = cache if cache is not None else SQLiteCache(settings=get_settings().cache)
        self._owns_cache = cache is None
        self.session = session if session is not None else requests.Session()
        self._owns_session = session is None
        if self._owns_session:
            self.session.mount("https://", HTTPAdapter(max_retries=0, pool_connections=2, pool_maxsize=2))
        self.session.headers.update({"User-Agent": _UA, "Accept-Language": "en-IN,en;q=0.9",
            "Accept-Encoding": "gzip, deflate", "Accept": "application/json, text/plain, */*",
            "Referer": NSE_FILINGS_URL})
        self.seed_path = seed_path
        self.allow_seed_fallback = allow_seed_fallback
        self.timeout = (connect_timeout_seconds, read_timeout_seconds)
        self.request_interval = min_request_interval_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._primed_until = 0.0
        self._blocked_until = 0.0
        self._last_request = 0.0

    def __enter__(self) -> NSEAnnouncementsClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_session:
            self.session.close()
        if self._owns_cache:
            self.cache.close()

    def _request(self, url: str, *, params: Mapping[str, str] | None = None, homepage: bool = False) -> bytes:
        delay = self.request_interval - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        self._last_request = time.monotonic()
        headers = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"} if homepage else None
        try:
            response = self.session.get(url, params=params, headers=headers, timeout=self.timeout, stream=True)
            try:
                if response.status_code in (401, 403, 429):
                    retry_after = response.headers.get("Retry-After", "")
                    seconds = 300.0
                    try:
                        requested_wait = float(retry_after)
                        if math.isfinite(requested_wait) and requested_wait >= 0:
                            seconds = max(seconds, requested_wait)
                    except ValueError:
                        try:
                            retry_date = parsedate_to_datetime(retry_after)
                            seconds = max(seconds, (retry_date - self._clock()).total_seconds())
                        except (ValueError, TypeError, OverflowError):
                            pass
                    self._blocked_until = time.monotonic() + seconds
                    raise NSEBlockedError(f"NSE returned HTTP {response.status_code}; live requests paused")
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    size += len(chunk)
                    if size > _MAX_BODY_BYTES:
                        raise NSEPayloadError("NSE response exceeded the size limit")
                    chunks.append(chunk)
                body = b"".join(chunks)
                if not homepage and ("text/html" in response.headers.get("Content-Type", "").lower() or body.lstrip().startswith(b"<")):
                    self._blocked_until = time.monotonic() + 300
                    raise NSEBlockedError("NSE returned an HTML challenge instead of announcement JSON")
                return body
            finally:
                response.close()
        except requests.RequestException as error:
            self._blocked_until = time.monotonic() + 30
            raise NSEFetchError(f"NSE transport failure: {type(error).__name__}") from error

    def _live_body(self, ticker: str, params: Mapping[str, str]) -> bytes:
        with self._lock:
            if time.monotonic() < self._blocked_until:
                raise NSEBlockedError("NSE client is cooling down after an access/transport failure")
            with self.cache.track("announcements.http", ticker=ticker):
                if time.monotonic() >= self._primed_until:
                    self._request(NSE_HOME_URL, homepage=True)
                    self._primed_until = time.monotonic() + 1800
                return self._request(NSE_API_URL, params=params)

    @staticmethod
    def _rows(body: bytes) -> list[dict[str, Any]]:
        try:
            payload = _json_loads(body)
        except (ValueError, UnicodeError) as error:
            raise NSEPayloadError("NSE response was not valid JSON") from error
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            payload = payload["data"]
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise NSEPayloadError("expected an announcement array, not an exchange error object")
        return payload

    def _batch(
        self, ticker: str, rows: list[dict[str, Any]], *, now: datetime, fetched_at: datetime,
        since: datetime, source_url: str, demo: bool, cache_hit: bool, reason: str | None = None,
    ) -> AnnouncementBatch:
        normalized: dict[str, CorporateAnnouncement] = {}
        invalid = 0
        with self.cache.track("announcements.classify", ticker=ticker, tier=0):
            for row in rows:
                try:
                    item = normalize_announcement(row, ticker, received_at=fetched_at, source_url=source_url, is_demo=demo)
                except (ValueError, TypeError, KeyError):
                    invalid += 1
                    continue
                if since <= item.published_at <= now:
                    normalized[item.announcement_id] = item
        if rows and invalid == len(rows):
            if demo:
                raise SeedDataError("all demo rows failed normalization")
            raise NSEPayloadError("all NSE rows were invalid or belonged to another symbol")
        items = tuple(sorted(normalized.values(), key=lambda item: (-item.priority, -item.published_at.timestamp(), item.announcement_id)))
        return AnnouncementBatch(ticker=ticker, source_mode=("demo" if items else "unavailable") if demo else "live",
            cache_hit=cache_hit, fetched_at=fetched_at, window_start=since, window_end=now,
            announcements=items, invalid_rows=invalid, fallback_reason=reason)

    def _demo_batch(self, ticker: str, now: datetime, since: datetime, reason: str) -> AnnouncementBatch:
        rows = [row for row in load_seed_announcements(self.seed_path, as_of=now) if normalize_ticker(row["symbol"]) == ticker]
        return self._batch(ticker, rows, now=now, fetched_at=now, since=since,
            source_url=NSE_API_URL, demo=True, cache_hit=False, reason=reason)

    def fetch(self, ticker: str, *, window_hours: int = 24, force_refresh: bool = False, seed_only: bool = False) -> AnnouncementBatch:
        ticker = normalize_ticker(ticker)
        if type(window_hours) is not int or not 1 <= window_hours <= 24 * 90:
            raise ValueError("window_hours must be an integer in 1..2160")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        now = now.astimezone(timezone.utc)
        since = now - timedelta(hours=window_hours)
        if seed_only:
            return self._demo_batch(ticker, now, since, "Explicit synthetic demo mode")
        params = {"index": "equities", "symbol": ticker,
            "from_date": since.astimezone(_IST).strftime("%d-%m-%Y"),
            "to_date": now.astimezone(_IST).strftime("%d-%m-%Y")}
        source_url = NSE_API_URL + "?" + urlencode(params)
        request_key = CacheKey.for_content(ticker, "request.nse_announcements", params, context={"version": ADAPTER_VERSION})
        if not force_refresh:
            pointer = self.cache.get(request_key)
            if pointer.hit and isinstance(pointer.value, dict) and isinstance(pointer.value.get("content_hash"), str):
                try:
                    key = CacheKey(ticker, "nse.announcements_raw", pointer.value["content_hash"])
                    cached = self.cache.get(key)
                    if cached.hit and isinstance(cached.value, bytes) and cached.metadata:
                        fetched = _parse_date(cached.metadata["fetched_at"])
                        return self._batch(ticker, self._rows(cached.value), now=now, fetched_at=fetched, since=since,
                                           source_url=source_url, demo=False, cache_hit=True)
                except (ValueError, TypeError, KeyError, NSEPayloadError):
                    self.cache.delete(request_key)
        try:
            body = self._live_body(ticker, params)
            # A broadcast can arrive while the HTTP call is in flight. Use
            # response completion as retrieval/cutoff, not the request start.
            completed_at = self._clock()
            if completed_at.tzinfo is None or completed_at.utcoffset() is None:
                raise ValueError("clock must return a timezone-aware datetime")
            now = completed_at.astimezone(timezone.utc)
            since = now - timedelta(hours=window_hours)
            batch = self._batch(ticker, self._rows(body), now=now, fetched_at=now, since=since,
                                source_url=source_url, demo=False, cache_hit=False)
        except NSEFetchError as error:
            if not self.allow_seed_fallback:
                raise
            LOGGER.warning("NSE unavailable for %s (%s); serving explicitly labeled demo fallback", ticker, type(error).__name__)
            return self._demo_batch(ticker, now, since, str(error))
        key = CacheKey.for_content(ticker, "nse.announcements_raw", body, context={"version": ADAPTER_VERSION, "params": params})
        self.cache.put(key, body, ttl_seconds=LIVE_CACHE_TTL_SECONDS, metadata={"fetched_at": now.isoformat()})
        self.cache.put(request_key, {"content_hash": key.content_hash}, ttl_seconds=LIVE_CACHE_TTL_SECONDS)
        return batch

    def fetch_portfolio(self, tickers: tuple[str, ...] = DEFAULT_PORTFOLIO, **kwargs: Any) -> dict[str, AnnouncementBatch]:
        return {ticker: self.fetch(ticker, **kwargs) for ticker in dict.fromkeys(normalize_ticker(value) for value in tickers)}


def fetch_announcements(
    ticker: str, *, cache: SQLiteCache | None = None, window_hours: int = 24,
    force_refresh: bool = False, seed_only: bool = False, allow_seed_fallback: bool = True,
) -> AnnouncementBatch:
    with NSEAnnouncementsClient(cache, allow_seed_fallback=allow_seed_fallback) as client:
        return client.fetch(ticker, window_hours=window_hours, force_refresh=force_refresh, seed_only=seed_only)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--no-seed-fallback", action="store_true")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--write-seed", type=Path)
    args = parser.parse_args()
    if args.write_seed:
        payload = generate_seed_announcements(args.write_seed)
        print(f"Wrote {len(payload['records'])} synthetic demo records to {args.write_seed}")
        return
    try:
        with NSEAnnouncementsClient(allow_seed_fallback=not args.no_seed_fallback) as client:
            batches = client.fetch_portfolio(tuple(args.tickers) or DEFAULT_PORTFOLIO,
                seed_only=args.demo, force_refresh=args.refresh, window_hours=args.window_hours)
        print(json.dumps({ticker: batch.model_dump(mode="json") for ticker, batch in batches.items()}, indent=2, ensure_ascii=False, allow_nan=False))
    except (NSEFetchError, SeedDataError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
