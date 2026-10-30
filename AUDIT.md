# FINN backend audit — 1.5.0 with follow-ups through 1.5.4

Audit date: 29 October 2026. Scope: all eight original backend modules, all tests,
both supplied ENAM PDFs, the new validation command, live source probes, and the
actual legacy Google SDK's serialized HTTP requests.

## Proposed-fix verification — 1.5.4, 30 October 2026

Reviewed the complete attached `finn_backend_fixes_and_code_updates.md` against
1.5.3. Useful proposals are integrated with narrower accounting/provenance
rules. The review and evidence are in `PROPOSED_FIX_REVIEW.md` and
`verification/proposed_fixes_1_5_4.json`.

Added configurable per-model request pacing, selected annual-report sections,
public cash/investing schedules with independent source records, quote typography
normalization, precision-aware rounding, abbreviation-aware sentence checks,
broader management-role recognition, prioritized valid business overviews and
scrape-boundary growth handling. Rejected map items are recorded/cached rather
than aborting their siblings; counts and Tier-0 validation error events remain
visible. All-rejected/partially rejected call extracts do not establish absence
of guidance. Invalid items never enter execution scoring.

Rejected the proposed unconditional model-only cooldown, automatic 15-second
429 retry, implicit table-basis fallback, wrong-block quote reassignment,
generic bank-balance/capex aliases, unrestricted integer rounding and removal
of moderator/operator speaker boundaries. Existing conditional quota isolation,
annual timing filtering and business-overview guards were retained.

No evidence of a `MAX_TOKENS` failure appeared in the supplied run. Both route
defaults (Tier 1: 2,000 tokens / 45 s / zero retries; Tier 2: 6,000 tokens / 150 s /
three retries), the sampling-field fixes and accepted provider schema/compiler
remain unchanged. AST comparisons cover ten production output models and the
provider-schema function. SDK retries remain disabled; 429 never triggers a
hidden request, key rotation or paid fallback.

Verification: 380 offline tests including 27 new regressions; Ruff passes. Live
RELIANCE, TCS and HAL cash/FCF panels agree with independent Decimal arithmetic
and register schedule citations. An issuer-hosted 105-page annual financial
report yielded 25 selected pages, six beyond page 25. The uploaded report's BSE
annual link returned HTTP 403; that exact PDF was not rechecked. No credentialed
Gemini report was generated because no key is present in this runtime. Current
Yahoo EOD availability was outside this patch's live checks and remains uncertified.

Raw source pointers remain unchanged. A scoped source-policy version refreshes
the PULL corpus; only annual PDF-text selection gets a new extraction key.
Concall processing/model schemas and compatible request contexts retain their
cache identities. Financial-row source IDs preserve endpoint provenance through
reports and cached QA. Derived data is refreshed without deleting the database.

**Result:** multiple shared defects were found and fixed. The deterministic
financial path has substantial live verification. Credentialed Gemini research
and blocked exchange PDF access are explicitly **not certified** by this audit.

## Production-contract success and partial report — 1.5.3, 30 October 2026

The supplied `pull_contract_validation.json` records real acceptance of both
production contracts: `_MapExtraction` in about 3.32 s and `_Cases` in about
18.66 s, one attempt each, no failures and reported usage. The next supplied
`reliance_report_fixed.json` has **30/239 blocks mapped**, no HTTP 400 errors,
17 Tier-1 attempts, one quota error, zero Tier-2 calls, and no assessed promises.
The run lasted about 56.6 s. Business text is populated, but its overview is a
Jio ARPU fragment rather than an issuer-wide business description. Grounding
rejections largely concern segment scope, unproved names, and quote mismatches;
those checks must not be relaxed merely to populate the report.

The shared cooldown explains why the reasoning request was skipped after 429.
The old report discarded scope, so it cannot prove that this particular quota
was model-specific. The corrected adapter uses provider quota dimensions/IDs:
confirmed model scope isolates the affected route, while project/unknown scope
retains the conservative shared stop. Retry durations are bounded; quota errors
are never automatically retried. Controlled actual REST responses verify both
model isolation and conservative project/unknown behavior.

The release also adds literal management commentary at Tier 0. It verifies
speaker turns and exact source text, including cross-page headings, and excludes
analyst/questioner turns without relying on stale cached labels. These passages
are explicitly unscored and can enrich audit cards whose comparable structured
targets are missing. They do not supply invented dates, bounds, entity scope,
BEAT/MISSED outcomes or execution points. Existing scoring only consumes its
validated structured target/actual pairs. Missing numeric targets remain gaps.

The primary PDF used for the local check has SHA-256
`15b62bccf89efd419db6ce21d7a30d27b3caba4f3ecf9b9d3cf9da5943a34e81`,
identical to the uploaded report's Q1 FY27 source. The first 25 pages yielded
two speaker-verified commentary items without model calls or new verdicts.
This checks the literal extractor on an actual matching source, not the entire
research report. Machine-readable observations are in
`verification/reliance_followup_1_5_3.json`.

Company-wide overview selection now rejects KPI fragments and can assemble a
cited operating-activity list with an explicit missing-overview gap. Duplicate
activity names are collapsed. Annual metadata ignores irrelevant call/speaker
fields, and older calls outside the required audit window consume no mapping
attempts. Bull/bear call quotations must be management speech, not analyst premises.

Rejected cached outputs are individually invalidated and regenerated after a
stricter evidence check; valid primary/model caches remain reusable.

Validation: **353 offline tests pass**, including 18 new regressions; Ruff passes.
AST/source comparisons prove the already accepted provider-schema compiler,
mapping/case/timing model definitions and route configuration remain unchanged.
A corrected complete live RELIANCE report has not been generated in this runtime.
No API key is available here, and no scored credibility outcome is fabricated.

## RELIANCE report follow-up — 1.5.2, 30 October 2026

The supplied `reliance_report.json`, generated at 04:41 UTC, is a partial report,
not a working PULL audit. Its latest headline calculations agree with an
independent Decimal check, and all referenced source IDs resolve within its nine
sources. That checks arithmetic/linkage, not the semantic truth of every claim.
Eight primary PDFs supplied 200 extracted pages / 239 text blocks. All eight
documents have zero mapped blocks. Business snapshot and bull/bear are null,
forward guidance is empty, all four audit quarters are insufficient, and the
execution score is null. Telemetry shows 17 Tier-1 attempts and zero Tier-2 calls.

The report contains **15 HTTP 400 mapping errors**, followed by **one HTTP 429**.
The shared client's quota cooldown then prevents the bull/bear request. Four
additional annual-metadata grounding issues do not explain the mapping HTTP 400s.
Concall dates/quarters and management rosters are populated in this report.

The exact 400 response message was discarded by the old adapter, so its cause
is not proven. A leading explanation is the full map schema: nested arrays with
large `maxItems`, optional collections/fields, and local diagnostic objects can
cause a serving-grammar complexity rejection. Google explicitly documents this
failure mode: https://ai.google.dev/gemini-api/docs/generate-content/structured-output

Changes in 1.5.2:

- Project only the mapping schema to a simpler remote contract: omit local
  diagnostics, use fixed object keys and enforce array bounds locally. Exact
  local Pydantic validation, quote/speaker/date/numeric proofs, entity scope,
  units and reporting bases remain required. Other provider schemas and the
  removal of sampling overrides are preserved.
- Stop repeating a rejected HTTP 400 output contract within one research request.
  Other contracts remain callable, and a later request can try again. Skipped
  calls are not counted as API attempts. No paid fallback is introduced.
- Preserve fixed diagnostic categories (schema complexity/schema/input/auth/quota)
  from provider errors, without exposing raw messages, inputs or credentials.
- Describe failed extraction as unavailable, and unscored credibility as not
  assessed. Successful empty review still uses disclosure-absence wording with
  its limited scope. Add `mapped_pages` to distinguish successful mapping coverage
  from the legacy `pages_reviewed` PDF extraction count.
- Add `--pull-contracts` to validate the actual map/case schemas with synthetic,
  exactly checked content before a full research run. It replaces the small
  canary contract rather than adding extra successful-route calls.

Validation: **335 offline tests pass**, including 11 new regressions; Ruff passes.
Controlled actual SDK HTTP serialization checks cover the projected map schema,
unchanged generation fields and strict copying. A credentialed Google acceptance
of the corrected map contract is **still unverified here**. The new local command
in README makes that the next explicit check; offline tests do not certify the
provider's serving grammar or the resulting four-quarter financial analysis.

## Live-validation follow-up — 1.5.1, 30 October 2026

The user's second live report (`validation(1).json`) shows Tier 1 passing and
Tier 2 returning **HTTP 503**, with maximum operation latency about **80.7 s**.
This differs from the first report's approximately 45-second read timeout. The
protocol canary supplies a small copying task, not the full research corpus;
these reports do not establish a large-context explanation for the failure.

Two validator defects are fixed in 1.5.1:

- The canary bypassed engine retries. It now honors each route's retry budget,
  records every attempt in both check observations and telemetry, and performs
  bounded exponential backoff with jitter for transient 5xx/network failures.
  Tier 2 retains the requested 150-second timeout and three retries. Tier 1 and
  all engine/provider-schema files are unchanged. Quota, authentication and
  schema/grounding failures do not retry. Persistent provider errors stay failed.
- A one-bar history with no comparison/volume metrics could pass the market
  check. A pass now requires 35 completed bars and every required metric.
  Incomplete histories remain visible as unverified, and bounded source-error
  diagnostics explain missing prices or invalid history instead of hiding them.

The Yahoo adapter now uses a supported `period="3mo"` request and a new cache
namespace. Direct range probes returned 64 dated observations where explicit-date
queries were empty/truncated. However, subsequent raw responses contained a null
latest close: both TCS/RELIANCE three-month responses had only 63 non-null closes,
and five-day probes for RELIANCE/TCS/HDFCBANK/HAL also lacked the latest close.
The adapter rejects those current EOD snapshots rather than inventing a close or
silently using an older day. The source remains unverified until actual complete
current bars are supplied. No fully recovered live market snapshot is claimed
for this follow-up.

Validation: **324 offline tests pass**, including 17 added regressions; Ruff
passes across backend and tests. The actual legacy SDK's HTTP serialization was
intercepted for a 503-then-success check: two Tier-2 attempts, one Tier-1 attempt,
correct timeouts and no reintroduced sampling fields. These are controlled HTTP
responses, not a credentialed Google success. No key is available in this runtime.
The original 1.5.0 observations below and `verification/observed_results.json`
are retained as historical evidence, not a claim that later source requests pass.

## Measured evidence

| Check | Result | What this establishes |
| --- | --- | --- |
| Offline regression suite | **307 passed** | Contracts, calculations, failures, cache/telemetry behavior and 25 new audit regressions |
| Ruff across backend and tests | Passed | Static import/name/error checks |
| Live fundamentals | All **21 current portfolio/demo symbols**, plus M&M, BAJAJ-AUTO and NESTLEIND | Real HTML can be fetched and parsed; original TATAMOTORS now resolves to TMPV |
| Independent table comparison | **7,169 numeric cells on 14 live pages matched** | Raw DOM labels/cells agree with parsed period-aligned values; no synthetic HTML used for this comparison |
| Independent financial math | Checked the live snapshots using Decimal calculations | QoQ/YoY, margins, CFO/PAT, CFO/EBITDA, cumulative conversion threshold and D/E agree |
| Extra standalone-only case | **COLPAL verified live** | Empty quarterly consolidated tables with stale annuals now select a consistent current standalone snapshot, quarter ended September 2026 |
| Live EOD | RELIANCE, HDFCBANK, HAL; **35 completed sessions each** | Real closes/volumes, preceding-20-session baseline, 1D/5D changes and threshold counts agree |
| Actual SDK REST serialization | Both configured model routes checked with intercepted HTTP | Correct Developer API endpoint/model, JSON MIME and remote schema; deprecated sampling overrides omitted; no tool/paid fallback |
| Gemini response/usage handling | Real SDK messages plus controlled responses | STOP vs truncation/refusal, strict local parsing, usage including thinking, sanitized errors, quota cooldown and per-attempt telemetry |
| Credentialed Gemini calls | **Unverified here** | The Mac's GEMINI_API_KEY environment variable is not present in this runtime; model access/reasoning was not invented as a passed check |
| Live NSE JSON | **Unverified here** | The probe timed out; explicit demo fallback/cooldown worked and was not labeled live |
| Eight BSE PDF probes | **Unverified here** | TCS/HDFCBANK/HAL/NESTLEIND concall and annual-report probes returned timeouts or HTTP 403 |
| Issuer PDF metadata | Actual Reliance Q4 FY26 and Q1 FY27 covers checked | Dates, fiscal periods, management roster and timestamped speaker layout are supported; this is not a complete document/LLM audit |

First-pass five-second timeouts were retried with twelve-second limits. The
successful source results above are eventual actual successes, not assumptions
that a timeout meant a parser defect. Offline source fixtures remain explicitly
synthetic and cannot enter PULL research as primary disclosures.

Machine-readable observations are included in `verification/observed_results.json`.
Raw downloaded HTML/PDFs, databases, environment files and credentials are excluded
from the deliverable.

## Defects fixed

1. **Quarter date contamination in PUSH.** The old parser collected dates from
   an 85-character span after “quarter ended” and chose the latest one. A nearby
   approval/board/record date could become the reporting quarter. The parser now
   reads only the immediately following printed period date.
2. **Standalone fallback blocked by stale consolidated annuals.** COLPAL's live
   consolidated page retained old annual rows but no quarterly data. The old
   usability check stopped there. A missing consolidated quarterly table now
   triggers a whole-snapshot standalone check; bases are never mixed. A usable
   consolidated annual snapshot survives if the standalone check is unavailable.
3. **Wrong current fiscal calendar.** A historical majority month could override
   a recent change in year-end. The most recent complete annual period now
   establishes the current fiscal calendar; transitional/YTD periods stay distinct.
4. **Unsafe PDF substitution.** Different calls in a common listing calendar
   quarter were recorded as alternate mirrors. They remain independent candidates;
   source identity, rather than a shared month/quarter, controls deduplication.
5. **Routine-filing false positives.** PIT wording in a trading-window closure
   is not an actual insider transaction. LODR 31 shareholding-pattern reports are
   not SAST promoter pledges. Explicit material wording still takes precedence.
6. **Stale portfolio symbol.** NSE changed TATAMOTORS to TMPV effective October
   24, 2025. The default remains 16 securities using the current symbol, and old
   user inputs resolve consistently. TMCV is a separate security and is not an alias.
7. **Shared Gemini request weaknesses.** Requests now include a provider-side
   schema compiled to the requested legacy SDK's supported subset, in addition
   to the full prompt schema and strict local validation. Forced temperature and
   candidate-count fields are omitted. Both model requests were checked through
   the actual SDK's HTTP serialization path, rather than only a fake SDK client.
8. **Segment goalpost penalties.** A business-segment/unknown-scope revision can
   no longer change a company-wide management execution score.
9. **Citation laundering in factual QA.** A real citation alone cannot prove an
   unrelated paraphrase. Non-inference disclosure answers now render the exact
   validated quotations and known numeric facts. Model inferences are labeled
   `Analysis:` and retain supporting quotes/citations.
10. **Metadata numbers reused as amounts.** Dates and identifiers from a financial
    fact's JSON no longer enter the numeric evidence pool. A fiscal year cannot
    become a purported revenue amount merely because it appeared in metadata.

The earlier 1.4.1 fixes remain active: issuer cover formats, call vs filing dates,
independent extraction-item validation, timestamped/cross-page speaker evidence,
safe metadata merging, actionable bounded errors, and derived-cache invalidation.

## Module-by-module disposition

| Module | Review and verification |
| --- | --- |
| config.py | 17 categories, priorities, defaults, normalization, verified rename, Tier 0/1/2 task routing, free-only route restrictions, no key serialization |
| schemas.py | Strict periods/units/bases, numeric missing-data rules, derived arithmetic, primary citations, announcement and audit consistency; import cleanup only |
| cache.py | Content hashes, model/prompt/schema-sensitive keys, checksums, expiration, atomic writes, WAL/concurrency tests, scoped telemetry and unknown-cost handling; import cleanup only |
| market_data.py | Live source and independent EOD math checks, NSE/BSE fallback tests, missing bars, inclusive thresholds, cache rollover; formatting cleanup only |
| announcements.py | Source normalization, time windows, warmup/cooldown, 17-category rules, invalid rows, deterministic labeled seed fallback; routine false positives corrected |
| fundamentals.py | Live DOM/cell and numerical checks, banks/NBFCs, standalone selection, annual vs TTM/YTD, fiscal-calendar changes, document identity, PDF extraction boundaries and secondary-note exclusion |
| push_engine.py | Routing, real SDK request bodies, source-bound executive/results enrichment, deterministic margin fallback, windows/watched alerts, routine zero-token paths, quota and cache failures |
| pull_engine.py | Four-quarter alignment, source/speaker/unit/entity-scope checks, independent map items, Python verdicts/score, eight panels, factual QA rendering, cached retrieval, per-request telemetry |
| validate.py | New isolated live-check command; unavailable sources/credentials remain unverified; two optional uncached Gemini canaries with exact validation |

## Run the remaining credentialed checks locally

Keep your existing environment variables. From the updated project directory:

```bash
python -m pip install -e .
python -m unittest discover -s tests -q
python -m backend.validate --tickers RELIANCE TCS HDFCBANK HAL --llm --output validation.json
python -m backend.engines.pull_engine RELIANCE > reliance_report.json
```

Omit `--tickers` to check the complete current portfolio and five ad-hoc symbols.
The validation command sends one uncached protocol canary per available model
tier, with explicit route-bounded transient retries in 1.5.1; it does not generate investment claims. A route passes only after a
complete, schema-valid, exactly grounded response with reported usage. Model
access without usage is a warning. Missing key/confirmation/access is unverified,
with zero model attempts when a route is unavailable. Unverified requested checks
or failed checks produce exit code 2. The API key is never printed or written.

Canary success verifies access and the wire/response contract; inspect the full
PULL report's `document_coverage`, `issues`, score coverage and telemetry to assess
the actual research run. A genuine absence of comparable guidance is not a parser
success/failure score and must not be converted into an invented verdict.

## Remaining boundaries

- Exchange access depends on the running IP/session. This audit cannot certify
  live NSE JSON or the eight unavailable BSE PDF downloads. The prototype keeps
  visible availability gaps and explicit synthetic PUSH fallback.
- PDF/text coverage is bounded; PULL defaults to the first 25 pages and selected
  chunks. Missing evidence in reviewed excerpts is not proof of absence from all
  pages. Scanned documents need OCR, which is not implemented.
- FCF requires proved cash capex. Net investing cash flow and book asset additions
  do not replace it. Missing pledging or contingency excerpts do not establish zero.
- Operating Profit remains a labeled EBITDA proxy; bank financing profit is not
  EBITDA. Header ROCE/ROE retain their observed source meaning. Financial changes
  after demergers/restatements can require human comparability review.
- Exact quote/numeric checks constrain model output; they do not formally prove
  every analytical inference is entailed. Inferences remain labeled and reviewable.
- A free-project flag is an operator assertion, not a billing-status API check.
  No paid provider/search/model fallback was introduced. Unknown usage/cost remains
  explicitly incomplete rather than silently becoming verified zero.
- A universal 75% Tier-0 classification rate was not measured against a live NSE
  corpus. The deterministic coverage target is not presented as a benchmark result.

## Primary technical references

- [NSE symbol-change circular](https://nsearchives.nseindia.com/content/circulars/FAOP70882.pdf)
- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini structured outputs](https://ai.google.dev/gemini-api/docs/generate-content/structured-output)
- [Gemini Generate Content API](https://ai.google.dev/api/generate-content)
- [Gemini 3 migration guidance](https://ai.google.dev/gemini-api/docs/gemini-3)
