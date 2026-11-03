"""Small single-purpose Pydantic AI agents with typed outputs.

Live runs use FINN_MODEL (default google-gla:gemini-3.8-flash on the free tier);
tests and the offline demo swap in TestModel or the fixture classifier, so no
API key is needed to prove the pipeline."""
from __future__ import annotations

import os

from pydantic_ai import Agent

from .models import CATEGORIES, Classification, GuidanceStatement, ResultsFiling
from .budget import guarded_model

CLASSIFIER_INSTRUCTIONS = (
    "Classify one NSE/BSE corporate announcement into exactly one category: "
    + ", ".join(CATEGORIES)
    + ". Judge materiality for an investor (routine paperwork is not material). "
    "Write a one-line summary grounded only in the announcement text."
)

RESULTS_INSTRUCTIONS = (
    "Extract the headline numbers from this results filing: revenue_from_operations "
    "(labeled Sales / Revenue from operations), total_revenue (labeled Total Revenue / "
    "Total income, including other income), EBITDA, PBT, PAT. Report the two revenue "
    "figures as DISTINCT values exactly as labeled in the filing and never merge them "
    "into one 'revenue'; leave a definition None when the filing does not state it. "
    "Every figure must include the page it came from (page). "
    "Record the accounting unit exactly as stated (crore vs lakh), whether figures are "
    "consolidated or standalone, and the period end. Use only the filing text; if a "
    "number is not stated, leave it out rather than estimating."
)

GUIDANCE_INSTRUCTIONS = (
    "Extract only verbatim forward guidance management stated in this concall "
    "transcript: metric, stated target, exact quote, page, period. Fill target_pct "
    "only when management stated a number. Never infer or invent a target."
)


def default_model() -> str:
    return os.environ.get("FINN_MODEL", "google:gemini-3.8-flash")


def with_fallback(model: str):
    """Fallback providers are disabled: unpriced models cannot run under the
    shared spend cap, so any FINN_FALLBACK_MODELS config fails closed."""
    fallbacks = [m.strip() for m in os.environ.get("FINN_FALLBACK_MODELS", "").split(",") if m.strip()]
    if not fallbacks:
        return model
    from .budget import BudgetExceeded

    raise BudgetExceeded(
        f"Live model blocked: FINN_FALLBACK_MODELS={fallbacks!r} disabled under the budget guard")


def build_classifier(model=None) -> Agent:
    return Agent(guarded_model(model or with_fallback(default_model())), output_type=Classification,
                 instructions=CLASSIFIER_INSTRUCTIONS)


def build_results_extractor(model=None) -> Agent:
    return Agent(guarded_model(model or with_fallback(default_model())), output_type=ResultsFiling,
                 instructions=RESULTS_INSTRUCTIONS)


def build_guidance_extractor(model=None) -> Agent:
    return Agent(guarded_model(model or with_fallback(default_model())), output_type=list[GuidanceStatement],
                 instructions=GUIDANCE_INSTRUCTIONS)
