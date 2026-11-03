"""Deterministic comparison. No model ever computes here; it only narrates."""
from __future__ import annotations

from typing import Optional

from .models import GuidanceStatement, Metric, ResultsFiling


def not_comparable_reason(cur: ResultsFiling, prev: ResultsFiling) -> Optional[str]:
    """Return why two filings must not be compared, or None if like-for-like."""
    if cur.consolidated != prev.consolidated:
        return "accounting basis changed (consolidated vs standalone); compare like-for-like only"
    if cur.period_type != prev.period_type:
        return "period type differs (quarter vs year)"
    return None


def growth_pct(cur: ResultsFiling, prev: ResultsFiling, metric: Metric) -> Optional[float]:
    """Period-over-period growth in crore-normalised terms, or None when not comparable.

    Units normalise (lakh vs crore is arithmetic, not a basis change); a
    consolidated/standalone switch refuses the comparison. Both filings are
    read through the same metric field, so revenue from operations is never
    compared against total revenue (incl. other income)."""
    reason = not_comparable_reason(cur, prev)
    if reason is not None:
        return None
    cv, pv = cur.metric_value(metric), prev.metric_value(metric)
    if cv is None or pv is None:
        return None  # metric not stated in the filing; abstain rather than estimate
    c, p = cv.in_crore(), pv.in_crore()
    if p == 0:
        return None
    return (c - p) / abs(p) * 100.0


# Guidance metric strings that name a revenue definition explicitly.
_ALIASES = {
    "revenue from operations": Metric.REVENUE_FROM_OPERATIONS,
    "revenue_from_operations": Metric.REVENUE_FROM_OPERATIONS,
    "sales": Metric.REVENUE_FROM_OPERATIONS,
    "net sales": Metric.REVENUE_FROM_OPERATIONS,
    "total revenue": Metric.TOTAL_REVENUE,
    "total_revenue": Metric.TOTAL_REVENUE,
    "total income": Metric.TOTAL_REVENUE,
}
# Revenue strings that do not say which definition is meant: never guessed.
_AMBIGUOUS_REVENUE = {"revenue", "revenues", "topline", "top line", "top-line", "turnover"}
_REVENUE_METRICS = (Metric.REVENUE_FROM_OPERATIONS, Metric.TOTAL_REVENUE)


def _resolve_metric(text: str) -> Optional[Metric]:
    key = " ".join(text.lower().replace("-", " ").split()) if text.lower() not in _ALIASES else text.lower()
    if key in _ALIASES:
        return _ALIASES[key]
    for m in Metric:
        if key in (m.value, m.name.lower()):
            return m
    return None


class LedgerEntry(dict):
    """status: delivered | missed | qualitative | no_stated_guidance | not_comparable"""


def promised_vs_delivered(
    company: str,
    guidance: list[GuidanceStatement],
    filings: list[ResultsFiling],
    sources: Optional[list[str]] = None,
) -> list[LedgerEntry]:
    """The guidance ledger: what management promised vs what the filings delivered.

    Only verbatim stated guidance enters. A metric with no stated guidance
    yields an explicit abstention entry, never an invented target. Guidance
    whose metric string does not say which revenue definition it means is
    not_comparable, never mapped by guess. `sources` names the inspected
    sources for the no-guidance wording."""
    filings = sorted(filings, key=lambda f: f.period_end)
    entries: list[LedgerEntry] = []
    mine = [g for g in guidance if g.company == company]
    src = sources or sorted({g.source_type for g in mine if g.source_type}) or ["results filings"]
    scope = ", ".join(src)

    if not mine:
        entries.append(LedgerEntry(
            metric="all", metrics=[m.value for m in Metric], status="no_stated_guidance",
            note=f"Extraction found no stated guidance in the loaded source(s): {scope}. Add concall transcripts to compare.",
        ))
        return entries

    ambiguous = [g for g in mine if g.metric.lower().strip() in _AMBIGUOUS_REVENUE]
    for g in ambiguous:
        entries.append(LedgerEntry(
            metric=g.metric.lower().strip(), status="not_comparable",
            stated_target=g.stated_target, quote=g.quote, page=g.page,
            note=("Guidance says 'revenue' without specifying revenue from operations or total revenue "
                  "(incl. other income); definitions are not interchangeable, so no comparison was made."),
        ))

    for metric in Metric:
        stated = [g for g in mine if _resolve_metric(g.metric) is metric]
        if not stated:
            if ambiguous and metric in _REVENUE_METRICS:
                continue  # covered by the ambiguous-revenue entry above
            entries.append(LedgerEntry(
                metric=metric.value, status="no_stated_guidance",
                note=f"No stated guidance for this metric in the loaded source(s): {scope}. Add concall transcripts to compare.",
            ))
            continue
        g = stated[-1]
        if g.target_pct is None or len(filings) < 2:
            entries.append(LedgerEntry(
                metric=metric.value, status="qualitative",
                stated_target=g.stated_target, quote=g.quote, page=g.page,
                note="Qualitative guidance; quoted verbatim, no numeric comparison invented.",
            ))
            continue
        gap_months = (filings[-1].period_end.year - filings[-2].period_end.year) * 12 + \
                     (filings[-1].period_end.month - filings[-2].period_end.month)
        window = "qoq" if gap_months <= 4 else ("yoy" if gap_months <= 13 else "fy")
        if g.horizon and g.horizon != window:
            entries.append(LedgerEntry(
                metric=metric.value, status="not_comparable",
                stated_target=g.stated_target, quote=g.quote, page=g.page,
                note=f"Horizon mismatch: guidance is {g.horizon}, the filings give {window}; not compared.",
            ))
            continue
        actual = growth_pct(filings[-1], filings[-2], metric)
        if actual is None:
            entries.append(LedgerEntry(
                metric=metric.value, status="not_comparable",
                stated_target=g.stated_target, quote=g.quote, page=g.page,
                note=not_comparable_reason(filings[-1], filings[-2]),
            ))
            continue
        if g.bound == "at_most":
            delivered = actual <= g.target_pct
        else:  # at_least and point targets are met by reaching or exceeding them
            delivered = actual >= g.target_pct
        sign = {"at_least": ">=", "at_most": "<=", "point": ""}.get(g.bound, "")
        if g.bound == "range" and g.target_hi_pct is not None:
            target_display = f"{g.target_pct}-{g.target_hi_pct}%"
        else:
            target_display = f"{sign}{g.target_pct}%"
        entries.append(LedgerEntry(
            metric=metric.value, status="delivered" if delivered else "missed",
            stated_target=g.stated_target, quote=g.quote, page=g.page,
            target_pct=g.target_pct, bound=g.bound, target_display=target_display,
            actual_pct=round(actual, 1),
        ))
    return entries
