# FINN proposed-backend-fix review — 1.5.4

Reviewed on 30 October 2026 against the current 1.5.3 backend and the complete
attached `finn_backend_fixes_and_code_updates.md`.

The guide identifies real coverage and usability gaps, but several supplied
replacements would remove safeguards or overwrite fixes already present. The
integrated release adopts the useful changes with the evidence boundaries below.
It does not certify a complete live Gemini research report.

| Proposal | Finding and integrated behavior |
| --- | --- |
| Raise Tier-1 output/timeout/retries to 4096/60/2 | Not applied as a default. The supplied run shows quota/grounding failures, not `MAX_TOKENS`, and both production schemas passed. Both existing tier routes stay unchanged. Route overrides remain available through `FINN_CONFIG_JSON`; truncation still fails closed. |
| Per-model cooldown | Already fixed in 1.5.3 when structured provider details prove model scope. Retained. A project/unknown quota still blocks both models; treating every 429 as model-only is unsafe. |
| Pace requests at 4.1 seconds | Added as configurable per-model request-start spacing, protected against concurrent reservations within the client. The 4.1-second default is a conservative local setting, not a verified 15-RPM account entitlement. Separate processes/clients, TPM and daily limits remain outside this pacer. |
| Retry every 429 after 15 seconds | Rejected. A 429 can be daily, project-wide or token exhaustion, and 15 seconds is not a guaranteed reset. Existing structured retry durations/cooldown scope remain; no automatic quota retries, key rotation, model switching or paid fallback. |
| Select later annual-report pages | Added. Keep covers, scan at most 1,200 pages locally, rank diverse financial/MD&A topics and retain <=25 pages. Actual physical page numbers and truncation stay visible. Spare capacity is filled without dropping previously selected late pages. Chunks do not cross unselected page gaps. Concall extraction stays bounded to its first pages. |
| Expand Screener cash/investing schedules | Added after live endpoint verification. Use the page's authoritative `company-info`, not a peer table's company ID; preserve consolidated/standalone query scope, validate redirects, align periods, cache responses and register independent URL/hash sources. Optional failures leave explicit gaps while core tables remain usable. |
| Alias Bank Balances and generic Capital Expenditure | Rejected. Bank balances can differ from cash equivalents, and reported capex can mean book additions. Cash balances require exact cash-equivalent labels. FCF uses signed purchases from the investing cash-flow schedule or independently proved cash capex; investments, asset sales and net CFI are excluded. Conflicting PDF/schedule cash values produce a gap, not a selected winner. |
| Quote normalization | Added case/whitespace/PDF punctuation and soft-hyphen normalization. Words and financial numbers must still match in the exact declared source block. |
| Automatically locate a quote in an adjacent block | Rejected. Silent source reassignment breaks citation/speaker provenance and can cross documents. An incorrect source reference remains a grounding rejection. |
| Accept rounded figures | Added exact one/two-decimal rounding at the precision actually printed in the narrative. No arbitrary whole-number rounding, sign changes or fabricated zero from a small nonzero amount. This numerical check does not formally prove an inference's meaning or units. |
| Ignore fiscal/technology labels in numeric checks | The existing standalone-number regex already excludes joined labels such as Q1 and 5G; this was not the reported root cause. Standalone years are not added to financial evidence. Existing date/metric/basis checks remain separate. |
| Allow Rs./Cr./Mr. abbreviations | Added. Abbreviations/decimal points no longer create false sentence boundaries; genuinely separate sentences and newline-separated prose are still rejected. |
| Ignore section headings as speaker labels | Added for a narrow list of non-speaker headings. Moderator/operator/questioner boundaries are retained; removing those as the proposal suggests could carry the preceding CFO into an analyst premise. Management roles now also recognize heads of investor relations and additional executive titles. |
| Default every document's basis to the table basis | Rejected. A mixed standalone/consolidated cover must remain unresolved at document level. A quoted declaration or same-block statement heading can prove basis for that particular excerpt; no basis is carried across unreviewed pages. |
| Cache fully filtered map batches | Added with explicit `accepted_items`, `rejected_items`, `rejected_only_blocks`, per-item diagnostics and Tier-0 validation-error telemetry. Invalid items are discarded. Processed-but-rejected evidence cannot establish that a promise was absent or create an execution verdict/score. |
| Annual timing filter | Already present in 1.5.3; retained. Annuals do not require call dates, quarterly call periods or management speaker rosters. |
| Better overview/segment selection | Existing company-overview guard retained; valid annual overviews are preferred, management opening passages get a bounded ranking boost, trailing business/segment/division labels are deduplicated, and call business facts require verified management speech. No product KPI or analyst premise becomes a company profile. |
| Populate citation quotes | Added exact validated map quotes where available, otherwise a literal bounded excerpt from that registered PDF block. Table references retain source/row/period citations. An excerpt is supporting source text, not a formal entailment certificate. |
| Historical growth at scrape-window edges | Added the proposed skip when the prior comparison period is outside the scraped window. That boundary is no longer represented as issuer non-disclosure. Current-quarter accounting formulas are unchanged. |

## Verification performed

- All **380 offline tests** pass, including **27 new regression tests** covering
  pacing, strict source references, precision-aware rounding, speaker boundaries,
  same-block basis proof, schedule scope/provenance/cache failures, conflicting
  capex, primary QA, annual page gaps and filtered-result caching.
- Ruff passes over all backend and tests. Actual legacy SDK request/response
  tests remain in the suite, including structured production schemas, error
  handling, explicit retry counting and no deprecated sampling overrides.
- AST comparisons confirm both route definitions, the provider-schema compiler
  and ten production map/timing/case models are unchanged. Tier 2 still has
  `max_retries=3` and `timeout_seconds=150.0`.
- Live RELIANCE, TCS and HAL cash/FCF panel checks passed with zero LLM calls;
  independent Decimal arithmetic agreed and all citations resolved to registered
  sources. These were financial-panel checks, not complete company reports.
- The BSE annual URL in the uploaded RELIANCE report returned **HTTP 403** here.
  Annual page selection was checked separately on an accessible issuer-hosted
  105-page financial-report PDF. It retained 25 physical pages, including six
  financial pages beyond page 25. This was a different PDF from the uploaded run.
- Annual-policy invalidation re-extracts from the cached raw PDF without another
  download. No cache database deletion is required. Concall parser keys and
  compatible model-request contexts are retained.

Evidence, source URLs and fingerprints are recorded in
`verification/proposed_fixes_1_5_4.json`.

## Remaining limits and rerun

No Gemini API key is present in this runtime, so full credentialed report
generation is **unverified**. Previously supplied passing contract probes prove
provider/schema access for that run; they do not prove every research inference.
Quota exhaustion, blocked source downloads, missing OCR and incomplete selected
page/chunk coverage can still produce a partial report. Current Yahoo EOD
availability was not retested by this patch.

Update the existing project files while retaining `finn_cache.db`:

```bash
python -m pip install -e .
python -m unittest discover -s tests -q
python -m backend.engines.pull_engine RELIANCE \
  --max-tier1-calls 12 --max-tier2-calls 8 > reliance_report_v154.json
```

These budgets cap work; they do not force that many calls. Do not delete raw
source caches merely to apply this version. A null credibility score remains
correct unless comparable dated company-wide promises and subsequent actuals
are proved. Literal management commentary stays explicitly unscored.

Rate-limit source checked: https://ai.google.dev/gemini-api/docs/rate-limits
