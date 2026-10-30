# FINN foundation and data plumbing

Runnable Steps 1 through 5 for the ENAM Holdings FINN challenge. Python 3.11+
and Pydantic v2; Step 2 adds yfinance, pandas and requests. SQLite, threading and
the test runner use the standard library.
Imports do not read credentials, open a database, or make network/model calls.

## Run

From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

On Windows, activate with `.venv\Scripts\activate` instead. The conditional
`tzdata` dependency supplies the IANA market timezone there.

Validated with Python 3.12, Pydantic 2.14.1, SQLite 3.53.1, yfinance 1.7.0 and
requests 2.34.2. Step 3 also uses BeautifulSoup 4.15.0, curl_cffi 0.17.0 and
PyMuPDF 1.26.6. Step 4 uses DDGS 9.16.1. All 380 offline tests pass. Fixtures
are synthetic; they are not investment research. An earlier live Yahoo check
returned 35 completed RELIANCE.NS sessions; the later response had a missing
latest close, so current EOD availability is not certified. The live NSE check timed out, and the real
transport-failure path correctly served explicitly labeled demo filings.

| File | Responsibility |
| --- | --- |
| `backend/config.py` | Categories, portfolios, alert defaults, explicit task routing, model lifecycle/pricing, settings |
| `backend/schemas.py` | Strict PUSH/PULL contracts, citations, data gaps, deterministic derived figures, telemetry models |
| `backend/cache.py` | SQLite payload cache, expiry, validation, atomic writes and persistent cost/latency ledger |
| `backend/data/market_data.py` | 35 completed Yahoo sessions, .NS/.BO selection, 1D/5D changes, volume baseline, alerts and six-hour caching |
| `backend/data/announcements.py` | Cookie-warmed NSE session, source normalization, all 17 regex categories, outages and labeled demo fallback |
| `backend/data/seed_announcements.json` | 33 synthetic filings across all 16 portfolio symbols, with results tables and a CFO transition |
| `backend/data/fundamentals.py` | Dynamic Screener financials, deterministic accounting ratios/flags, primary-document discovery and bounded PDF extraction |
| `backend/engines/push_engine.py` | Windowed digest, watched-category/EOD alerts, cited executive bio pills, results first-cut cards and per-request telemetry |
| `backend/engines/pull_engine.py` | Primary-only research, date-aligned four-quarter execution audit, deterministic score, eight report menus and cached-source QA |
| `backend/validate.py` | Live source/model checks, explicit per-attempt model retries, exact canary validation and incomplete-history detection |

## Reviewed backend proposals in 1.5.4

The proposed fixes were checked against the current implementation, intercepted
real SDK requests, synthetic failure cases and live primary sources. See
`PROPOSED_FIX_REVIEW.md` for every adopted/rejected change and
`verification/proposed_fixes_1_5_4.json` for the evidence and limits.

- Live Google requests are paced per model, with 4.1-second request-start
  spacing by default. Configure `router.gemini_min_request_interval_seconds`
  through `FINN_CONFIG_JSON` for your active AI Studio limits. This local pacer
  does not enforce token/day quotas or coordinate separate processes. The
  existing model/project quota distinction and stop on 429 remain intact.
- Annual PDFs retain <=25 selected pages, including covers and diverse financial
  sections, instead of only the first 25. A bounded local scan examines at most
  1,200 pages; unselected text is never sent to a model. `selected_pages` exposes
  physical page indices. Concall extraction still uses its first bounded pages.
- Public Screener cash/investing schedules supplement cash balances and signed
  fixed-asset purchases, with separate URL/hash source records. Eligible cash
  capex drives deterministic FCF; conflicting disclosures leave FCF unavailable.
  Bank balances, net investing flows and book additions are not substituted.
- Quote matching handles PDF punctuation/soft hyphens within the exact declared
  block. Numeric prose may round to its stated one/two decimal places; invented
  zeroes, whole-number approximations and sign changes stay rejected. Common
  abbreviations no longer look like additional sentences.
- Filtered map results retain/cache accepted siblings and explicit rejection
  counts, including all-rejected batches. Those outcomes never establish
  disclosure absence, execution verdicts or credibility points. Out-of-window
  historical deltas no longer appear as missing issuer disclosures.

Both route configurations and the already accepted provider schemas are
unchanged. There was no observed `MAX_TOKENS` failure to justify the proposed
blanket Tier-1 budget/retry increase. Mixed-document reporting basis never
defaults to the financial table's basis; an explicit same-block statement
heading can establish basis for that specific excerpt.

Validation: 380 offline tests and Ruff; live cash/FCF paths for RELIANCE, TCS and
HAL, all with registered source citations and zero LLM calls. The uploaded
report's BSE annual link returned 403 here. Annual selection was instead checked
on an accessible issuer-hosted 105-page financial report, with six retained
financial pages beyond page 25; it is a different PDF. A complete credentialed
Gemini research run remains unverified in this runtime.

Update the existing installation while retaining `finn_cache.db`, then run:

```bash
python -m pip install -e .
python -m unittest discover -s tests -q
python -m backend.engines.pull_engine RELIANCE \
  --max-tier1-calls 12 --max-tier2-calls 8 > reliance_report_v154.json
```

Corpus/annual-selection policies refresh derived data using retained raw
HTML/PDF caches; no database deletion is required. Valid concall extraction and
compatible model-cache entries remain reusable. A null execution score is still
correct where disclosed promises are not comparable to subsequent results.

## Quota isolation and primary commentary in 1.5.3

The supplied production-contract probes both passed: Tier 1 accepted the actual
map schema and Tier 2 accepted the case schema. The subsequent report mapped
30 of 239 blocks, with no HTTP 400 errors. It still had no scored promises and
zero Tier-2 calls after a Tier-1 HTTP 429. That report did not retain quota scope,
so it cannot establish whether that particular quota was model- or project-wide.

The adapter now reads structured quota violations and retry durations. A
confirmed model-specific quota cools down only that model, preserving the other
eligible free model. Project-wide or unspecified quotas still block both.
Quota errors are not retried, no key/model/billing fallback is introduced, and
safe diagnostics explain the active cooldown. The accepted provider schema,
mapping/case/timing models, route defaults and sampling fields are unchanged.

The report now includes `management_commentary`, with up to two literal,
speaker-verified prospective passages per call at zero model cost. When no
comparable structured promise exists, an audit card can show these in
`qualitative_commentary` with an explicit **unscored** label. This does not create
a quarterly target, a group/segment comparison, a BEAT/MISSED verdict or a
credibility score. Analyst headings such as `Questioner (Name, Firm)` clear
management speaker context; stale cached speaker labels are never trusted.
Bull/bear call evidence must also come from identified management.

A product KPI fragment such as ARPU cannot become a company-wide business
overview. If only segment descriptions are proved, the snapshot displays a cited
activity list and a missing-overview gap. Duplicate activity labels are collapsed.
Annual-report metadata ignores irrelevant call-date/management fields, and calls
older than the required four-quarter source window are not sent for model mapping.

All 353 offline tests and Ruff pass, including 18 new regressions. Actual SDK REST
error handling was intercepted to verify that model-scoped quota details survive
and the other model remains callable. A local issuer-PDF check, with the same hash
as the uploaded report, extracted two unscored passages from the first 25 pages
with zero LLM calls. Machine-readable evidence is in
`verification/reliance_followup_1_5_3.json`. No corrected complete live report is
claimed from this runtime; the user's key is not available here.

Apply updates in the existing project folder so `finn_cache.db` and successful
primary/model caches are retained. Outputs rejected by current evidence checks
are individually invalidated and regenerated; valid outputs remain reusable.
The already-passed contract probes do not
need to be repeated just to install this patch. A bounded research run is:

```bash
python -m pip install -e .
python -m backend.engines.pull_engine RELIANCE \
  --max-tier1-calls 12 --max-tier2-calls 8 > reliance_report_v153.json
```

Twelve Tier-1 attempts cap new extraction work and quota pressure; they do not
guarantee the project's available quota. Eight Tier-2 attempts allow the two
reasoning tasks to use the configured three retries when needed, without forcing
extra calls on success. A score can correctly remain null when the primary
disclosures do not establish comparable targets. Literal commentary is never a
substitute for those targets.

## PULL request-contract recovery in 1.5.2

The supplied RELIANCE report has populated financial tables and dated primary
PDFs, but **zero mapped blocks**. Its 15 mapping HTTP 400 errors followed by one
HTTP 429 are extraction failures, not proof that management disclosed no guidance.
The original provider message was discarded, so the precise 400 cause cannot be
proved from that report. Schema complexity is a leading explanation: Google's
structured-output documentation warns that nested bounded arrays and optional
properties can exceed the serving grammar's complexity limit.

The remote mapping schema now omits local diagnostics, requires fixed object
keys and leaves array limits to strict local Pydantic/item validation. Other
output schemas and sampling fields are unchanged. Local quote, speaker, date,
number, entity-scope and reporting-basis checks remain active. A rejected HTTP
400 contract is blocked for the rest of the request, preventing identical
failures across every document while keeping other output contracts available.
Safe fixed provider-error categories are retained without storing provider text.

Failed/unperformed extraction is now labeled unavailable or not assessed, rather
than reported as disclosure absence. Document coverage adds `mapped_pages`;
the legacy `pages_reviewed` field counts extracted PDF pages, not successful
LLM review. Successful empty extraction remains distinct from extraction failure.

The new `--pull-contracts` option replaces the tiny protocol schema with the
actual `_MapExtraction` and `_Cases` contracts, using clearly synthetic exact-copy
tasks that never enter company research. Run this before another full report:

```bash
python -m pip install -e .
python -m backend.validate --tickers RELIANCE --llm --pull-contracts \
  --skip-market --skip-announcements --skip-documents \
  --output pull_contract_validation.json
```

Only after both production-contract probes pass, run:

```bash
python -m backend.engines.pull_engine RELIANCE > reliance_report_fixed.json
```

Do not clear the database or add `--refresh` merely to install this patch. The
PULL prompt/cache namespace changed; successful primary-document caches remain
reusable. All 335 offline tests and Ruff pass, including 11 new regressions and
actual SDK HTTP serialization checks. The corrected production contract has not
been accepted by a credentialed Google request in this runtime: the user's key
remains on their computer. Passing the new probe establishes request compatibility,
not the quality/completeness of every investment conclusion.

## Validation recovery in 1.5.1

The supplied live report changed from a 45-second Tier-2 read timeout to HTTP
503 after about 80.7 seconds. That is a provider availability failure, not
evidence of a schema or financial-context parsing error. The validator used to
call the transport once, bypassing the engine's retry loop. It now honors the
route's retry budget and records each actual attempt separately. Tier 2 keeps
`timeout_seconds=150.0` and `max_retries=3`, allowing at most four attempts with
bounded exponential backoff and jitter. Tier 1 remains at 45 seconds and zero
retries. Quota/auth/client errors and invalid model output are not retried;
SDK retries and paid fallbacks remain disabled. Provider-side JSON schema and
sampling fields are unchanged.

The same report incorrectly marked one-bar market histories as passed. A market
check now requires 35 completed bars and all four EOD comparison/volume metrics;
short or unusable history is explicitly `unverified`, with available observations.
Yahoo fetching now requests its supported three-month range instead of the
explicit-date query that returned empty/truncated history in these probes. A
new cache namespace prevents reuse of old incomplete snapshots without deleting
the application database. Latest-close validation still applies: subsequent live
probes returned 64 dated observations but a null latest close for TCS/RELIANCE;
separate five-day queries also had a null latest close for all four checked NSE
symbols. Those responses cannot establish a valid current EOD snapshot. Quotes,
previous-day closes and zeroes are not substituted for the missing close.

All 324 offline tests and Ruff pass, including 17 new regressions. An intercepted
real SDK HTTP request verifies 503-then-success recovery, separate attempt counts,
the 150-second timeout and unchanged generation fields. No credentialed Tier-2
success is claimed from this runtime. To isolate model access while avoiding PDF,
market and announcement downloads, keep your current environment exports and run:

```bash
python -m backend.validate --tickers RELIANCE --llm \
  --skip-market --skip-announcements --skip-documents \
  --output validation_llm.json
```

This still checks the ticker's Screener tables. A persistent 503 remains a failed
model check after the configured retries; it is not disguised as a successful
investment report.

## Full backend audit in 1.5.0

The complete findings, measured live coverage and remaining unverified areas are
in [AUDIT.md](AUDIT.md). This update changes shared configuration, announcement
classification, source selection and both intelligence engines; install the
complete project rather than replacing only the PULL file.

```bash
python -m pip install -e .
python -m unittest discover -s tests -q
python -m backend.validate --tickers RELIANCE TCS HDFCBANK HAL --llm --output validation.json
```

Without `--tickers`, validation checks the complete portfolio and five ad-hoc
symbols. It uses an isolated in-memory cache and does not delete/change the
application cache. Each available Gemini tier receives a clearly labeled,
uncached protocol canary with route-bounded transient retries only when `--llm`, the existing key and the free-project
assertion are present. The checks validate actual model access, complete JSON,
exact quoting, copied dates/numbers and reported token usage. They do not claim
to certify an entire generated investment report. Missing credentials/model
access remains `unverified`; it is never reported as a passed API check. Missing
usage remains a warning and incomplete cost telemetry. Source/LLM failures give
exit code 2. `--skip-documents`, `--skip-market` and `--skip-announcements` can
isolate a failing source. PDF checks review the first three pages for availability;
the PULL research page/chunk limits remain separately disclosed.

The default portfolio still has 16 securities. The obsolete `TATAMOTORS` input
resolves to **TMPV**, following NSE's October 24, 2025 symbol change. TMCV is a
separate security and is never merged into this alias. The source circular is
recorded in `config.py`; old input configurations are canonicalized consistently
across market data, announcements, fundamentals and cache keys. Existing old-symbol
scrape keys are not silently reused as current data.

The audit fixed the standalone fallback when consolidated quarterly tables are
empty but stale annual rows remain (verified live with COLPAL), current fiscal
calendar selection after a year-end change, incorrect quarter-vs-board dates,
routine PIT trading-window notices and LODR shareholding reports misclassified
as material events, false same-quarter PDF mirrors, and segment-vs-company
revision scoring. Factual copilot prose now renders exact supported disclosures
and deterministic figures; model inferences are explicitly labeled. Financial
amount validation no longer borrows numeric tokens from metadata dates/IDs.

The 1.5.0 audit passed 307 offline tests. Its live coverage included all 21 current portfolio/demo
symbols plus three arbitrary symbols for financial tables, independent checking
of 7,169 numeric cells on 14 actual pages, three 35-session Yahoo snapshots,
and a live standalone-fallback check on COLPAL. Both actual SDK REST request
bodies were tested with HTTP interception. Credentialed Gemini calls could not
be run in this build runtime because the user's local key is not available here.
NSE live access and eight BSE PDF probes were unavailable in this runtime; those
are recorded as unverified, with explicit fallback/gap behavior. See the audit
for the distinction between source availability, deterministic checks, and LLM
reasoning quality.

## Step 5: PULL primary-source research

```bash
python -m backend.engines.pull_engine TCS
python -m backend.engines.pull_engine TCS --ask 'Do profits turn into cash?'
python -m backend.engines.pull_engine TCS --no-llm
```

Keep the existing Gemini free-project setup below. Step 5 adds no dependency,
paid API, search or embedding service. Both the report and copilot use the
existing `finn_cache.db`. `--ask` reads a cached corpus only; generate a report
first. `--refresh` on a report refreshes sources/model outputs; on a question it
only regenerates that answer from cached sources. There is no synthetic PULL
fallback.

```python
from backend.cache import SQLiteCache
from backend.engines.pull_engine import PullEngine

with SQLiteCache("finn_cache.db") as cache, PullEngine(cache) as engine:
    report = engine.generate_report("TCS")
    for quarter in report.narrative_vs_numbers:
        print(quarter.quarter_label, quarter.management_promise)
        print(quarter.actual_delivered, quarter.verdict, quarter.auditor_commentary)
    print(report.management_execution_score)
    answer = engine.ask_primary_filings("TCS", "What is disclosed about working capital?")
    print(answer.answer, answer.citations, answer.telemetry)
```

One-shot `generate_pull_report(ticker, ...)` and
`ask_primary_filings(ticker, question, ...)` functions are also exported.
The engine's `PullResearchReport` and `NarrativeVsNumbersQuarter` extend the
Step-1 models: existing provenance, missing-data and time-alignment validators
remain active. Use the engine's enhanced report class as the API response model
if you want its extra panels in JSON. The report exposes all eight menu keys:
`business_snapshot`, `financial_snapshot`, `trajectory`, `balance_sheet`,
`cash_quality`, `narrative_vs_numbers`, `forward_guidance`, `bull_vs_bear`.
It retains the original combined trajectory/balance-sheet and bull/bear fields
for compatibility. Missing modules carry the exact phrase
`Not disclosed in primary filings` and explicit coverage/data gaps.

Source discipline:

- Only Screener's standardized financial tables and the issuer/exchange PDFs
  discovered by `fundamentals.py` enter the corpus. No news, broker research,
  biographies, outside model memory or web-search context is supplied.
- Screener embedded/AI concall summaries remain secondary and are excluded.
  Issuer-prepared notes can be used when supplied as a primary concall PDF;
  an AI summary cannot masquerade as a management quotation.
- Each text claim has a source tag, with `citation_catalog` resolving tags to
  source URLs, physical PDF pages and block IDs. Copilot citations include the
  exact supporting quote. Numeric facts retain their table sections, periods,
  units, basis and calculation descriptions. Results tags backed by Screener
  explicitly identify that origin; they do not imply a downloaded results PDF.
- The first 25 PDF pages are the default limit. Up to six 6,000-character blocks
  per document are selected by disclosed guidance/business/risk keywords and
  mapped in batches of three. Page/chunk truncation and unmapped coverage are
  visible. An absent extracted passage does not establish absence from every
  unread page of a full filing. Missing/scanned/mislabeled documents stay gaps.

The four audit slots are the latest four delivered quarterly periods in the
financial table, oldest first. The preceding call provides each slot's promise;
the newest call often provides the next, still-forward target. Consequently four
complete delivered quarters can require **five calls**. The bundled fundamentals
adapter adds a backward-compatible `document_limit` parameter (default still
four); PULL requests an expanded catalog and reviews up to six call candidates
plus two annual reports. When copied alone onto an older adapter, the engine
still runs, flags its catalog limit, and leaves any missing first promise empty.

Call dates and reported quarters require matching primary text; website listing
months never become invented call dates. FY labels use the observed annual
fiscal year-end, or explicit date labels if no fiscal calendar can be established.
PDF source publication fields record snapshot observation when the actual issuer
release date is unknown; verified call dates are kept separately. A current scrape
cannot be used as an earlier point-in-time research snapshot.

Tier 1 maps exact quotes, management/speaker evidence, explicit target periods,
units, bounds, issuer-versus-segment scope and reporting basis. Numbers are extracted as displayed tokens and
normalized in Python. Analyst questions, retrospective results commentary and
unproven fiscal periods are rejected. Qualitative statements remain available
without invented numerical thresholds. Annual guidance remains annual and is
never judged against one quarter. Constant-currency/organic guidance cannot be
scored against reported INR growth; standalone and consolidated data cannot mix.
Screener Operating Profit stays a labeled proxy and does not automatically
establish the issuer's EBITDA definition for promise scoring.

Tier 0 compares exact metrics/periods and computes growth, margins, delivery and
execution points. Tier 2 reduces those comparisons into one analytical line per
assessable quarter; it cannot override the arithmetic. A goalpost flag needs an
explicit later adverse revision/deferral naming the same original metric,
basis and quarter, with both original/revision citations. New/upward guidance
alone is not penalized. Cash-cycle days have the correct lower-is-better
orientation; capex above its promised band is not automatically a beat.

Execution scoring is explicit product policy: BEAT and DELIVERED = 100, MISSED
= 0, verified adverse GOALPOST_SHIFTED = 25. Promises are averaged within a
quarter, then assessed quarters receive equal weight. Missing/uncomparable
promises are excluded. At least two assessed quarters are required for a numeric
score; otherwise it is null with a reason. Assessed-quarter coverage is separate
from PDF/page/chunk coverage. This is a score of reviewed execution evidence,
not an assertion about personal honesty. With no assessable evidence, `verdict`
is null and the existing audit status/gaps explain why; the engine does not
fabricate one of the four scored labels to fill the UI.

The financial panels remain deterministic. Three/five-year CAGR uses exact
full-year endpoints (four/six observations), not TTM/interim data or an average
of annual growth rates. Balance-sheet debt/equity includes reserves. Working
Capital Days and cash conversion cycle remain separate. Missing pledging does
not mean zero pledging; absent contingency excerpts are not a clean bill of
health. CFO/PAT and CFO/EBITDA keep matched annual periods. FCF requires separately
proved cash capex; net investing cash flow and book asset additions are not
substituted. Header ROCE/ROE are labeled observed ratios, with the underlying
calculation period left unasserted. Amounts preserve source numeric precision;
display growth/margins use two decimals with full computed values retained.

Bull and bear each have three distinct, cited primary-evidence arguments when
supported. The module stays unavailable if that evidence cannot supply three
points each way; there is no generic padding. Its arguments are labeled inference.

The copilot performs deterministic lexical retrieval over cached source blocks
and numeric facts, then uses Tier 2 for the answer. No cross-ticker context or
network source refresh occurs during a question. Unsupported answers use the
required missing-disclosure phrase and a status explaining whether evidence,
cached data or model access was missing. With no model access, financial-only
questions can return cited Python facts; why/causation questions cannot receive
invented explanations. Cached model answers consume zero new tokens.

All source/LLM cache lookups and measured operations use a request ID. The report
returns Tier 0/1/2 telemetry plus wall latency and issue codes. The default budget
is 24 Tier-1 calls and two Tier-2 calls per request, with the existing free-only
provider restrictions and quota cooldown. A conservative character bound limits
reasoning context; it is not claimed as an exact tokenizer count. Calls are
individually metered; validated outputs are cached by content, model, schema and
prompt. Deterministic and cached operation continue when free quotas are unavailable.

The original 56 PULL-specific tests, supplemented by the full-backend audit tests, cover the full four-quarter BEAT/DELIVERED/MISSED/shift
pipeline and score, eight panels, exact period/currency alignment, source/speaker
checks, cash-capex/FCF separation, CAGR, resolvable citation tags, legacy adapter
compatibility, cached QA, failure isolation and zero-new-call repeats. Full
regression: 380 tests. Credentialed live model calls were not exercised in this runtime.

### PULL extraction fix in 1.4.1

The CLI returns the complete report JSON, including the citation catalog. A report
can have working financial tables and still have unavailable narrative modules;
check `document_coverage`, `issues`, `management_execution_score` and `telemetry`.
To keep the report available for inspection:

```bash
python -m backend.engines.pull_engine RELIANCE > reliance_report.json
```

The original parser missed issuer covers with fiscal-year ranges, adjacent call
titles/dates, comma-separated executive roles, or timestamped speaker headings.
The 1.4.1 parser supports these layouts deterministically. A filing-letter date
and a reported-quarter date cannot become an invented call date. Consolidated
and standalone discussion in one PDF leaves document-wide basis unknown; a
segment/subsidiary promise cannot be scored against group results. An identified
speaker can continue across a page break only through a cited original heading;
an intervening analyst or another speaker resets that attribution.

Each map item is now parsed and validated independently. A malformed guidance
item or unsupported cash-capex number does not discard valid sibling guidance or
business quotes. Only accepted items enter the research facts; rejection details
stay visible and survive cached partial successes. An entirely failed batch is
not cached as a successful empty extraction. Model-supplied diagnostics are
ignored. Error details show bounded field paths/types or the exact local
validation rule, without echoing raw model payloads or credentials. Compact map
instructions limit extraction volume to fit the existing Tier-1 output budget.

Verified deterministic timing survives null/partially invalid LLM metadata.
Derived PULL corpus/prompt versions were advanced, so the original failed
metadata is not reused. No database deletion, key replacement or API setting
change is required. Cached financial HTML/PDF payloads remain reusable. Corpora
built with model access disabled do not block later metadata enrichment.

The fix was checked against Reliance's actual 30-page Q4 FY26 and 32-page Q1 FY27
primary PDFs: the cover parser identified the reported quarter, actual call date
and all nine named management participants with zero model calls. This verifies
metadata extraction, not a complete credentialed Gemini research run. Sixteen
new regression tests additionally cover partial map validation/cache behavior,
field-specific diagnostics, mixed reporting basis, segment isolation, filing-date
separation and timestamped/cross-page speaker attribution. Explicit comparable
quarterly guidance can still legitimately be absent; the engine does not invent
an execution score to fill that gap.

## Step 4: PUSH intelligence

```bash
python -m backend.engines.push_engine --demo --no-market
python -m backend.engines.push_engine RELIANCE TCS --price-threshold-pct 3 --volume-multiplier 2
python -m backend.engines.push_engine INFY --no-llm --no-web-search
```

The active routes are **Gemini 3.5 Flash-Lite (Tier 1)** and **Gemini 3.8
Flash (Tier 2)**. These are the current free-tier replacements selected for this
project: Gemini 1.5 Flash/Pro shut down on September 29, 2025. Retired 1.5 IDs
are rejected rather than silently rerouted. PUSH remains Tier 1 only; PULL uses
the configured Tier-2 route.

The requested `google-generativeai` package is retained and tested at 0.8.6.
Google marks it unmaintained and recommends `google-genai` for new projects;
this revision deliberately preserves the requested legacy-package compatibility.

For zero API charges, use an eligible Google AI Studio project with **Cloud
Billing disabled**. The code cannot determine project billing status from an
API key. After verifying that project in AI Studio/Cloud Billing, set:

```bash
export GEMINI_API_KEY
export FINN_GEMINI_FREE_TIER_CONFIRMED=1
```

Set `GEMINI_API_KEY` securely in your shell/environment before running those
commands. The second variable is your assertion that the project is unbilled;
it does not disable billing, check the account, or make a paid key free. Leave
it unset for cached/local-only operation. A key alone does not enable requests.
If your project has no free-tier eligibility, calls remain unavailable; the app
never enables billing or switches to a paid provider.

Install the complete package with `pip install -e .`. If applying just the two
updated modules to Step 4, also install:

```bash
python -m pip install 'google-generativeai>=0.8.6,<0.9' 'google-api-core>=2.24,<2.34'
```

The API Core upper bound keeps this legacy SDK's protobuf dependency compatible.
The Gemini adapter uses the package's public protos and bundled Google
GenerativeService REST client. Each instance has its own API-key credentials
and a fixed Developer API endpoint; it does not use global `genai.configure()`,
ADC, Vertex, model tools, hosted context caching, or automatic paid fallback.
The adapter sends JSON MIME mode and a provider-side response schema compiled to
the legacy SDK's supported subset. The full JSON schema also stays in the prompt;
strict local Pydantic/source-quote checks remain mandatory. The current Gemini
routes omit forced temperature and candidate-count fields. Only successful
validated outputs enter SQLite. The database schema is unchanged; parser and
prompt versions invalidate affected derived entries after this audit.

```python
from backend.cache import SQLiteCache
from backend.engines.push_engine import PushEngine

with SQLiteCache("finn_cache.db") as cache, PushEngine(cache) as engine:
    feed = engine.get_push_feed(
        ["RELIANCE", "INFY", "TCS", "M&M"],
        window_hours=24,
        price_threshold_pct=3.0,
        volume_multiplier=2.0,
        watched_categories=["results", "management_change", "M&A", "pledging"],
    )
    for card in feed.priority_digest:
        announcement = card.announcement
        print(card.priority_rank, card.ticker, card.takeaway)
        print(announcement.results_headline_table)
        print(announcement.management_bio_enrichment)
    print(feed.routine_drawer.count, feed.telemetry)
```

`get_push_feed(...)` is also available as a one-shot convenience function.
Reuse an engine for polling so the NSE cooldown and HTTP sessions persist.
Symbols are dynamic, with no portfolio allowlist. An empty watch list disables
category alerts; routine categories cannot be watched as material events.
Explicit watches can include dividend/call notices as well as the default ten
material categories. All 17 category priorities remain the deterministic policy
in `config.py`; the model cannot invent a priority. `priority_rank` is the
stable ordinal after sorting by weight, publication time, ticker and filing ID.

The typed `PushFeed` returns `triggered_alerts`, `priority_digest`,
`routine_drawer`, `telemetry`, source modes, issues, market-session dates and a
review queue. Each digest card includes the validated `CorporateAnnouncement`,
per-person `executive_transitions`, results alignment, enrichment status and
data gaps. The announcement holds the existing `ResultsHeadlineTable` and
`ManagementBioEnrichment` contracts. Its summary is the cited takeaway.

Confirmed `unclassified`, `postal_ballot` and `agm_egm` compliance filings take
the Tier-0 drawer path with **zero LLM tokens, PDF downloads or web searches**.
An unrecognized/ambiguous subject is not proof of compliance: it stays in
`review_queue`, also at zero token cost. Conflicting *material* classifications
can use Tier 1; unresolved candidates remain visible and cannot trigger category
alerts. Routine investor-meeting/call notices remain low-ranked deterministic
digest entries without an LLM takeaway.

Executive extraction uses the actual exchange metadata and, when present,
bounded attachment text. Every name/role/action and effective date needs a
matching source quote; dates remain separate for multiple people. The legacy
enrichment's common `effective_on` is populated only if all established dates
agree. A name is never inferred from the web.

The helper searches an exact name plus dynamically resolved company name.
It uses **public DDGS only**. Tavily/Serper keys are ignored, including keys
already present in the environment; paid search cannot be called. No key is
required for DDGS. Only a matching name and
issuer context on LinkedIn `/in/` profiles, exchange archives or explicitly
supplied issuer domains can provide background evidence. For example, pass
`company_domains={"TCS": ("tcs.com",)}` to the engine to include that issuer's
site. News/broker sites are excluded. Search snippets stay labeled as snippets;
no full-profile retrieval, sign-in or private data access is attempted. Missing
profiles fall back to the filing's biography, not invented employment history.

Bio synthesis produces 2–3 sentences combining cited career/expertise facts
with a separately requested investor implication. Every career quote must name
the same executive. The resulting evidence text is marked as inference because
it includes that implication. Without career evidence, the person/role/date
remains available with an explicit missing-background reason. No generic
two-sentence biography is fabricated merely to fill the card.

Results cards match the explicit quarter in the filing to Screener's exact
quarter. Historical announcements do not inherit the newest quarter, and a new
quarter missing from Screener stays unavailable. A standalone-only filing
cannot borrow consolidated figures. If no quarter is established, the latest
table is labeled **context only**. Scheduled board meetings/future-quarter
notices cannot become released-results alerts; annual-only results are not
converted to a quarterly table. Retrieval snapshots can contain restatements;
these are not a point-in-time historical database.

Revenue, EBITDA/Operating Profit proxy, PBT and PAT, growth rates and margins
stay deterministic. Operating leverage here means the change in EBITDA margin
(QoQ when available, otherwise YoY), without asserting an undisclosed cause.
The one-line LLM result must agree with that direction, name the comparison,
keep the proxy label and leave numerical claims to the table. If the LLM is
unavailable, Python emits the margin observation. Banks' financing profit is
not renamed EBITDA.

Other material cards receive at most 22 whitespace-separated words, a verified
supporting quote and a cited investor implication. Unsupported numerical claims,
overlong/promotional text, malformed JSON, refusals or incomplete responses
produce explicit gaps and a verbatim source excerpt. Quote matching and identity
checks reduce errors; cited LLM paraphrases still require evaluation against the
underlying filing. No Tier-2 route is callable by this engine.

The digest window is an inclusive snapshot fixed at request start. Market
alerts use completed EOD sessions whose 15:30 IST close falls inside that
window. Thresholds are inclusive (`abs(change) >= X`, `volume_multiple >= K`).
Old weekend bars do not become new breaches. Alert IDs are stable across polls;
the eventual notification service should persist delivery/acknowledgement by
ID. `--include-demo-alerts` explicitly opts into synthetic category alerts;
otherwise synthetic fallback is visible in the digest and cannot issue live
alerts. `--demo --no-market` is a fully offline, zero-token demonstration.

The engine shares `finn_cache.db` with existing adapters. Content, prompt,
output schema, model/provider and generation parameters determine LLM cache
keys. Successful, validated model outputs are cached for the configured LLM
TTL; invalid outputs are not cached. Profiles cache for seven days, negative
searches for 15 minutes, and raw/extracted attachment PDFs for 30 days. Results
metadata that already states the quarter avoids an unnecessary PDF download.
Other attachments use up to 20 text-layer pages, 20 MiB, bounded redirects,
timeouts and public HTTPS checks. HTML challenges/encrypted/scanned files fall
back explicitly; no OCR or secondary summaries masquerade as a primary filing.

Every feed gets a unique request ID. A scoped cache facade tags existing data
clients' cache lookups/local operations along with each actual Tier-1 attempt,
so concurrent engines sharing SQLite do not contaminate the feed's totals.
Tier-0 calls count measured local operations, not the number of filings. Tokens
come from provider usage, including its cached-input subset; missing usage is
unpriced, not silently guessed. Successful confirmed free-tier calls estimate
$0 while still reporting tokens. The legacy SDK lacks a separate thinking-token
field, so with model tools disabled the adapter computes output tokens as
`total_token_count - prompt_token_count`; visible output is not added twice.
Missing/inconsistent usage stays unavailable. Tier-1 default model retries are
zero; Tier 2 allows three retries with a 150-second per-attempt timeout.
Explicit transient 5xx retries are individually counted and remain free-only.
A 429 quota error is never retried or upgraded. Structured model-specific quota
violations cool down that model; project/unknown quotas cool down the client.
The duration follows a bounded provider retry delay, or five minutes when absent,
while the feed keeps source cards. The per-feed `max_llm_calls` budget
defaults to 64. SDK retries are disabled. Local cached outputs spend zero new
tokens. DDGS uses no paid API and reports zero paid-search attempts. The sum of operation latencies can exceed wall latency because
operations can be nested. `wall_latency_ms` is the user-visible elapsed time.

All I/O clients and the Tier-1 transport can be injected for tests; factories
receive the request-scoped cache. One engine serializes its shared HTTP sessions;
separate engines may share one local database. Imports perform no I/O. Missing
provider keys yield deterministic cards and gaps rather than breaking the feed.
The 58 PUSH tests and 29 Gemini/free-policy tests cover extraction, profile identity/citations,
failure isolation, exact windows/thresholds, demo separation, budget/retry
accounting, results alignment and zero-call cache repeats. The Gemini transport was
verified with actual legacy SDK request/response messages and fixture service
responses; no credentialed live model call was made.
The installed DDGS helper was also exercised against its live public backend;
it timed out in this environment and correctly returned an empty, labeled
outcome with zero LLM calls. Public search availability is not assumed.

References:

- [Gemini model lifecycle](https://ai.google.dev/gemini-api/docs/deprecations)
- [Gemini 1.5 shutdown](https://ai.google.dev/gemini-api/docs/changelog)
- [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini billing](https://ai.google.dev/gemini-api/docs/billing)
- [Gemini SDK support](https://ai.google.dev/gemini-api/docs/libraries)
- [DDGS maintainer documentation](https://github.com/deedy5/ddgs)

## Step 3: fundamentals and primary transcripts

```bash
python -m backend.data.fundamentals TCS
python -m backend.data.fundamentals HAL --transcripts
python -m backend.data.fundamentals 'M&M' --strict-ebitda
```

The first command fetches only HTML, tables and document links for a fast
response. `--transcripts` downloads the four selected transcript candidates and
extracts up to 25 pages each. `--refresh` bypasses retrieval/extraction caches.
`--max-pages` can reduce the limit but cannot exceed 25. If copying only the new
module onto Step 2, install its dependencies:

```bash
python -m pip install 'beautifulsoup4>=4.13,<5' 'curl_cffi>=0.15,<1' 'pymupdf>=1.26.6,<2'
```

No Step 1/2 schema or cache migration is required for Step 3. All new contracts
and logic are in `backend/data/fundamentals.py`.

```python
from backend.cache import SQLiteCache
from backend.data.fundamentals import FundamentalsClient

with SQLiteCache("finn_cache.db") as cache:
    with FundamentalsClient(cache) as client:
        company = client.fetch("HAL", include_transcripts=True)
        print(company.basis, company.latest_quarter_headlines)
        print(company.three_year_cash_conversion)
        for flag in company.flags:
            print(flag.code, flag.value, flag.explanation)
        for transcript in company.transcripts:
            print(transcript.status, transcript.reported_quarter_end)
            for chunk in transcript.chunks:
                print(chunk.section, chunk.page_start, chunk.page_end, chunk.chunk_id)
```

There is no ticker allowlist. The client first requests
`/company/SYMBOL/consolidated/`, then the standalone page on a 404/410 or empty
core financial tables. It switches the entire reporting basis, avoiding
standalone quarters combined with consolidated annual figures. A 403/rate-limit
or other transport failure is explicit, not a reason to fabricate fundamentals
or repeatedly probe another company URL. Redirects, when present, are followed
with source/URL checks. The NSE identity is checked when disclosed on the page.

HTML is fetched with a browser-compatible `curl_cffi` session because ordinary
Requests timed out in this environment while the browser-compatible transport
succeeded. Sessions are injectable for tests. Network calls use timeouts, byte
limits and bounded redirects; no login, CAPTCHA solving or authentication is
attempted.

The parser targets `#quarters`, `#profit-loss`, `#balance-sheet`, `#cash-flow`,
`#ratios`, `#shareholding` and `#top-ratios`. It uses actual date headers and row
labels, preserves raw cell text and missing values, normalizes monetary amounts
to INR crore, and retains percent/INR/share-count/day units separately. The
quarter limit defaults to 12 and accepts 8 through 12. Both quarterly and annual
shareholding tables are retained when present. Promoter pledging is only
reported if a matching row exists; no row never means zero pledge.

Important accounting contracts:

- `latest_quarter_headlines` is the existing typed `ResultsHeadlineTable`, with
  Python QoQ/YoY calculations, EBITDA margin and PAT margin. The prior quarter
  and year-ago quarter are looked up by exact dates, not by a positional shift.
  Missing quarters and loss/zero bases remain explicit gaps. Half-year data
  cannot become quarterly results, and future columns cannot become actuals.
- Screener's `Operating Profit` can supply an **explicitly labeled EBITDA
  proxy**. `ebitda_basis` and the underlying fact's calculation describe that
  choice. `--strict-ebitda` disables the proxy; a financing-profit row, such as
  a bank's, is never renamed EBITDA. Verify the primary filing's definition
  before comparing EBITDA guidance. PBT is derived only when all four required
  components exist, with the formula and source rows recorded.
- `cash_conversion` matches full-year CFO with annual PAT and EBITDA on exactly
  the same reporting period and basis. TTM and interim cash flows are excluded.
  A full-year column after a half-year column remains a full year. Fiscal-year
  changes are aligned to the actual annual P&L periods; transitional 9/15-month
  periods are not promoted to annual observations.
- The three-year red flag uses **sum(CFO) / sum(PAT)** for three consecutive
  matched full years. It does not average annual ratios. Exactly 0.70 does not
  trigger; below 0.70 does. Missing data or a nonpositive cumulative PAT leaves
  an unavailable ratio with a reason, rather than a healthy-looking zero.
- CFO/EBITDA is calculated from the displayed cash-from-operations row. It is
  not copied from Screener's before-tax CFO/EBITDA widget, whose basis differs.
- D/E is Borrowings / (Equity Capital + Reserves), not debt divided by paid-up
  capital alone. Nonpositive equity gives no misleading leverage ratio.
- Cash conversion cycle is debtor days + inventory days - payable days when
  all three exist. Reported CCC remains separate when components are absent.
  The **Working Capital Days** trend compares the exact prior financial
  year-end; an increase strictly above 15 days triggers the latest-period flag.
  Working Capital Days and cash conversion cycle are not interchanged.

`CompanyFundamentals` exposes tables, header ratios, source provenance, headline
figures, histories, flags, document links and explicit data gaps. Calculations
use Decimal arithmetic over the displayed source values and return finite JSON
numbers. They do not recover precision the source did not publish. The source
snapshot date is an observation date, not an inferred issuer filing date.
Screener's prose, machine-generated pros/cons, news and broker commentary are
not parsed into the financial result.

`#documents` discovery handles annual-report links, transcript/PPT anchors,
duplicate exchange mirrors, and notes/AI-summary buttons with observed
`data-url` attributes. Annual report year slots are deduplicated. Transcripts
prioritize distinct listing calendar quarters, then fill spare slots with
independent extra candidates. Sharing a listing quarter does not establish that
two PDFs are mirrors. Only observed document identity can establish duplication.
A listing month is a sorting hint, not proof of the call date/reporting quarter.

The PDF extractor uses actual text where an explicit `quarter ended ...` date
is available and returns `reported_quarter_end` plus its evidence. If that date
disagrees with the listing period, `listing_period_mismatch=True` and a partial
status prevent silent reassignment to the requested quarter. If no explicit
date is found, the period remains unknown for the later extraction pipeline to
resolve. A listing alone does not establish that an event was a quarterly
earnings call.

Embedded Screener notes are retained with `notes_are_primary=False`. Optional
`client.fetch_concall_notes(document)` can fetch a public notes URL that was
actually observed; login walls are unavailable results. These notes are
secondary and never appended to primary PDF text or transcript chunks.

PDF retrieval follows only observed redirects/iframe/embed links, including
exchange PDF-viewer pages, with public HTTPS/source-host checks and a 20 MiB
limit. The raw PDF is cached, then PyMuPDF retains at most 25 pages: the first
pages for concalls, or covers plus selected financial sections for annuals.
Pages retain their 1-based numbers. Overlapping chunks default to 6,000
characters with 300-character overlap and stable IDs. Section heuristics split
front matter, management opening, analyst Q&A and closing remarks; the result
explicitly labels boundary detection as heuristic. A scanned/empty PDF reports
`needs_ocr`; a failed download is an error object, not invented transcript text.
`truncated`, `total_pages`, SHA-256 and per-page data make limits visible.

HTML/request pointers are cached for six hours. PDF bytes and extracted text
are cached for 30 days. Extraction keys include content, engine/version,
document identity/listing month, page limit and chunk settings. Cache hits avoid
downloads and re-extraction. One failing transcript does not discard the rest
of the company's fundamentals. Step 3 makes zero LLM calls.

Live verification succeeded for TCS, HDFC Bank and the arbitrary ticker
TATAELXSI. A primary company-hosted TCS transcript was downloaded and 25 of its
29 pages became 12 chunks. Its explicit June 2025 quarter-end conflicted with
Screener's July 2026 listing; the module flagged the mismatch. The latest TCS
exchange-hosted endpoint timed out here and remained a per-document error.
Standalone fallback, accounting transitions, missing cells, exact thresholds,
PDF page limits, viewer pages and cache reuse are covered by offline tests.

Reference sources checked during implementation:

- [Screener financial definitions](https://support.screener.in/article/11-reconciling-numbers)
- [Screener notes feature](https://www.screener.in/docs/changelog/Concall-Summaries/)
- [PyMuPDF text extraction](https://pymupdf.readthedocs.io/en/latest/recipes-text.html)

## Run the two adapters

```bash
python -m backend.data.market_data RELIANCE
python -m backend.data.market_data HAL --price-threshold 4 --volume-threshold 2.5
python -m backend.data.announcements RELIANCE TCS
python -m backend.data.announcements --demo
```

`--demo` performs no NSE HTTP requests and covers the full 16-stock portfolio by
default. It still uses the real schemas, classifier, financial calculations and
SQLite telemetry. Both commands accept `--refresh` to bypass cached retrieval.
Announcements also accepts `--no-seed-fallback` to make outages explicit errors,
or `--window-hours 48` to choose a wider filing window.

Programmatic use:

```python
from backend.cache import SQLiteCache
from backend.data.market_data import MarketDataClient, evaluate_eod_triggers
from backend.data.announcements import NSEAnnouncementsClient

with SQLiteCache("finn_cache.db") as cache:
    with MarketDataClient(cache) as market:
        metrics = market.fetch("RELIANCE")
        alerts = evaluate_eod_triggers(metrics, 5.0, 2.0)
    with NSEAnnouncementsClient(cache) as filings:
        batch = filings.fetch("RELIANCE")
        cards = [item for item in batch.announcements if not item.collapse_in_digest]
        drawer = [item for item in batch.announcements if item.collapse_in_digest]
        print(batch.source_mode, batch.fallback_reason)
```

For an entirely offline filing example, call `filings.fetch("RELIANCE", seed_only=True)`.
Reuse one `NSEAnnouncementsClient` for a portfolio with `fetch_portfolio()`; it
reuses cookies, spaces requests and pauses further live calls after a block or
transport failure. It respects HTTP Retry-After without repeatedly retrying a
blocked endpoint. TLS verification remains enabled.

## EOD data contract

`fetch_market_data(ticker)` and `MarketDataClient.fetch(ticker)` return the
existing `EODMarketMetric`. `fetch_snapshot()` additionally exposes the retained
35 daily bars and short-history notes. The fields map as follows:

| Requested metric | Model field |
| --- | --- |
| Latest close, INR | `close_inr` |
| Previous close, INR | `previous_close_inr` |
| 1D change, percent | `price_change_pct` |
| 5 trading-session change, percent | `price_change_5d_pct` |
| Latest volume | `volume` |
| Preceding 20-session average volume | `average_volume_20d` |
| Volume spike ratio | `volume_spike_ratio` (also `volume_multiple`) |

Yahoo is asked for a 100-calendar-day buffer at daily frequency, then the latest
35 actual completed sessions are retained. The 5D comparison uses the close
five observations back (six closing prices). The volume average excludes the
latest day and requires all preceding 20 volumes. Missing/invalid historical
values remain missing; they are not filled or removed to shorten the baseline.
Newly listed stocks can return fewer than 35 bars with explicit gaps. Missing
latest price/volume makes that source unusable rather than silently promoting
an older row to the latest session.

The default EOD availability time is conservatively 16:15 Asia/Kolkata, after
the ordinary market close and closing/trade-modification window. Before that,
today's candle is excluded. `eod_ready_time` is configurable for special
sessions. No weekday-only holiday calendar is invented: observed Yahoo sessions
determine the returned dates. Request cache keys roll at the availability
boundary so a six-hour intraday cache cannot hide the new EOD session.

The adapter tries `SYMBOL.NS`, then the same base symbol with `.BO` if NSE data
is missing or invalid. `provider_symbol` and `market_exchange` expose the actual
source; volumes from the two exchanges are never blended. Some BSE Yahoo base
symbols differ from NSE symbols; unsupported mappings fail explicitly rather
than guessing a different issuer.

`auto_adjust=False` and `repair=False` preserve Yahoo Close and optional action
columns without extra repair downloads. The model labels this as raw provider
data, not a FINN-guaranteed corporate-action adjustment. A reported split in the
20-session window adds an explicit comparability note. Validate such dates
against primary exchange data when expanding beyond the prototype.

## Corporate filing contract and fallback

`fetch_announcements()` / `NSEAnnouncementsClient.fetch()` return an
`AnnouncementBatch` with a live/demo/unavailable source mode, cache flag, original
fetch timestamp, selected window, invalid-row count and typed announcements.
Live raw JSON and request pointers are cached for **five minutes** so PUSH
filings refresh more often than EOD prices. A successful empty response stays
live and empty. Demo output is never written under a live-data request key.

NSE's raw `desc` is treated as the subject and `attchmntText` as the description.
Publication timestamps are parsed as IST unless the source provides an explicit
offset. Malformed/future timestamps are rejected, duplicate exchange IDs are
merged, and cards are sorted by priority then publication time. Invalid rows
are counted; an entirely invalid response causes a visible source failure.

At this step, `filing` cites the actual NSE announcement metadata and
`attachment_url` holds the exchange's document link. The attachment PDF is not
claimed to have been parsed or hashed. Live results/bios are not invented from
the synthetic examples. Only seed entries contain prebuilt demo headline tables
and demo executive enrichment; live PDF extraction is the next stage.

Regex rules prioritize explicit event language, protect personal insider
purchases from M&A classification, distinguish earnings-call transcripts from
new results, and keep material content out of routine drawers. Unmatched or
conflicting input sets `requires_llm_classification=True` and remains visible
for Tier 1; there is no hidden model call in Step 2.

The seed generator is deterministic for an accounting anchor and can be run as:

```bash
python -m backend.data.announcements --write-seed backend/data/seed_announcements.json
```

The packaged file covers all 17 categories. It has 16 results filings, plus a
CFO appointment, subsidiary acquisition, factory expansion, promoter pledge,
funding, regulatory, insider, earnings-call, rating, dividend, bonus, order-win
and routine notices. Every item has `[DEMO]` in its title, `is_demo=True` and a
synthetic publisher. Demo document/attachment URLs are null. Publication times
are rebased to the demo clock; the explicitly printed accounting periods remain
those of the fixture. These figures and names are invented, not statements
about the real issuers. Demo evidence is rejected by `PullResearchReport`.
If the packaged JSON is missing, the loader uses the same built-in generator
in memory, including in a read-only installation. Corrupt custom seeds raise a
typed error; an unsupported demo ticker yields an unavailable empty batch.

Step 2 requires the included schema update (version 1.1.0): 5D baselines,
exchange identity, raw descriptions, attachment links, pending classification,
explicit demo provenance, and **inclusive** trigger validation. Do not copy
only the two adapters onto the old schema. `config.py` carries the schema
version bump; `cache.py` needs no changes.

Source API documentation checked during implementation:

- [yfinance PriceHistory](https://ranaroussi.github.io/yfinance/reference/yfinance.price_history.html)
- [Requests sessions](https://requests.readthedocs.io/en/latest/user/advanced/)
- [NSE market timings](https://www.nseindia.com/static/market-data/market-timings)
- [NSE corporate filings](https://www.nseindia.com/companies-listing/corporate-filings-announcements)

## Product policy

The 16 portfolio symbols and five ad-hoc demo symbols match the requested set.
They are display defaults, never a research allowlist. Boundary normalization
accepts `M&M`, `BAJAJ-AUTO`, numeric-leading symbols and Yahoo's `.NS` suffix.
The scraper must resolve symbol existence/renames; this module does not assume
that every syntactically valid symbol currently trades.

Default digest window: 24 hours. Default alerts: absolute EOD price change
at least 5%, or volume at least 2x the average of
the **preceding 20 trading sessions**, excluding the current session. Missing
history or a zero-volume baseline produces no fabricated multiple. The metrics
model records the adjustment basis; ingestion must handle split/bonus effects
consistently across both price and volume. These are EOD alerts, not an intraday
feed. User overrides are validated with `AlertThresholds`.

Category priorities are product defaults, not inferred financial materiality:

| Canonical category | Priority, 5 highest | Default drawer eligibility |
| --- | ---: | --- |
| `results` | 5 | No |
| `M&A` | 5 | No |
| `fund_raise` | 4 | No |
| `management_change` | 5 | No |
| `litigation` | 5 | No |
| `insider_trading` | 3 | No |
| `earnings_call` | 2 | No |
| `investor_meetings` | 2 | No |
| `credit_rating` | 4 | No |
| `dividend` | 2 | No |
| `bonus_split` | 2 | No |
| `capex` | 4 | No |
| `press_release` | 3 | No |
| `postal_ballot` | 1 | Yes |
| `agm_egm` | 1 | Yes |
| `pledging` | 4 | No |
| `unclassified` | 1 | Yes |

A drawer-eligible item collapses only after classification explicitly sets
`routine_confirmed=True`. An unresolved `unclassified` filing stays visible.
A cited `priority_override` can surface material content inside an otherwise
routine category, or demote a results-publication advertisement. Material
announcement routing should apply the investor's selected categories and
minimum priority before constructing `TriggeredAlert`.

## Model routing

| Tier | Tasks | Default |
| --- | --- | --- |
| 0 | Financial/market arithmetic, rule classification, report assembly | No provider |
| 1 | Ambiguous classification, management bio, guidance and primary-fact extraction | `gemini-3.5-flash-lite` |
| 2 | Four-quarter audit, bull/bear case, primary-source Q&A | `gemini-3.8-flash` |

`router.route(task)` returns a `ModelRoute` or `None` for Tier 0. Unknown tasks
fail; there is no generic chatbot route, automatic escalation or implicit
cross-provider fallback. The future adapter must enforce the configured input,
output, timeout and retry limits. Disable hidden SDK retries and track each
actual attempt separately. Chunk size defaults to 1,500 tokens with 150 overlap.
Measure routine-rule coverage on collected filings; 75% is a target, not an
assumed classification accuracy or measured saving.

Active routing is free-only Gemini. `LLMRouterConfig` rejects paid providers,
standard billing routes and models outside the verified free-model allowlist.
Historical OpenAI/other pricing records remain solely for existing ledger/cost
calculations; they are not callable through the router or PUSH engine. Model
and project eligibility must stay consistent with the selected free tier.
The requested retired Gemini 1.5 identifiers are explicitly rejected. Dates and
pricing references were checked on October 29, 2026.

Use `Settings.from_env()` or cached `get_settings()`. `FINN_CONFIG_JSON` accepts
the full nested settings object, with defaults for omitted fields. For example:

```bash
export FINN_CACHE_PATH='./data/finn_cache.db'
export FINN_CONFIG_JSON='{"thresholds":{"price_move_pct":4.0,"volume_multiplier":2.5},"router":{"tier2":{"provider":"google","model":"gemini-3.8-flash","billing_tier":"free","max_output_tokens":5000}}}'
```

The active API secret is `GEMINI_API_KEY`; OpenAI/Anthropic keys are ignored.
Configuration contains environment-variable names and the project confirmation
flag, never secret values. `FINN_GEMINI_FREE_TIER_CONFIRMED=1` enables requests
only after you have checked that Cloud Billing is disabled. Remove old paid
router overrides from `FINN_CONFIG_JSON`; they now fail validation.
Unsupported/custom model prices stay unknown unless `pricing_override` supplies
explicit USD-per-million text token rates and a dated source.

## Financial and evidence contracts

Use `model_validate_json()` for incoming JSON, including LLM output. Python-mode
construction deliberately requires proper enum/date/datetime instances and
tuples. Extra fields, numeric strings, booleans as numbers, non-finite floats
and naive datetimes are rejected. Avoid `model_construct` and unvalidated
`model_copy(update=...)` at application boundaries.

- Every available `NumericFact` has citations, period, unit and consolidated or
  standalone basis. Missing numbers use `value=None` plus `missing_reason`.
  Real zero remains zero. INR headlines are normalized to crore.
- `FinancialSnapshot` computes EBITDA and PAT margins. `ResultsHeadlineTable`
  computes four Revenue/EBITDA/PBT/PAT rows and their QoQ/YoY percentages from
  comparable quarters. Zero/negative growth bases return an explained missing
  percentage. Show the underlying signed amounts for loss-to-profit changes.
- `CashFlowPeriod` computes CFO/PAT and CFO minus positive capex. Zero/negative
  PAT produces no cash-conversion ratio. Cash-flow history cannot mix quarterly
  PAT with annual CFO, reporting bases or frequencies. Indian cash-flow data
  may only be half-yearly/annual: preserve its actual reporting period.
- `WorkingCapitalPeriod` computes inventory days + receivable days - payable
  days. `TrajectoryAndBalanceSheet` computes changes in cycle days in
  chronological order; positive means the cycle lengthened.
- Decimal arithmetic from source-normalized numeric strings avoids binary
  rounding creating a false threshold crossing. Public numeric outputs are
  finite floats for JSON/UI use; retain raw source bytes for original precision.
- EBITDA and industrial working-capital metrics may be inapplicable for banks
  and NBFCs. Use missing reasons. Do not relabel Screener's operating profit as
  EBITDA without verifying definitions against the filing.

`SourceDocument.content_hash` hashes the stored source representation: original
document bytes for document ingestion, canonical Yahoo bars for EOD, and a
canonical NSE metadata row for announcements. The full NSE response is also
stored as raw bytes. Attachment bytes are not substituted with metadata hashes.
`Citation` needs a page, section or stable chunk identifier. Root models resolve
all citation IDs. PULL accepts company/exchange annual reports, transcripts,
results, presentations and filings; explicitly labeled Screener financials
can support numeric facts. Narrative claims and guidance cannot cite Screener
as a substitute for primary documents. Professional profiles are allowed for
PUSH bio enrichment and excluded from PULL. Bull/bear arguments and assessed
promise-delivery judgments are explicitly labeled inferences.

These validators enforce the evidence contract, not the truth of fetched
content. Ingestion must verify issuer/origin, compare extracted quotes verbatim
with source chunks and retain the source bytes. A URL and an LLM-supplied
`kind="annual_report"` alone do not establish primary-source authenticity.

## Four-quarter audit

Exactly four consecutive quarterly slots, oldest first, are required. Choose
the latest four reported quarters at ingestion; dates and gaps remain visible.
Unavailable quarters stay present with `partial` or `insufficient_data` and an
explanation. Missing report modules likewise require named gaps.

Each `PromiseDelivery` links a management quote to its target period and an
actual on the same metric, unit and reporting basis. Numeric range verdicts are
checked deterministically. Annual guidance cannot be scored against a single
quarter; use `not_assessable` with a reason. Forward guidance must predate the
target period's end. Same-quarter results commentary cannot become a prior
promise. Store the original call date in `SourceDocument.event_on` when the
transcript is published later; the quote's issue date must match that call.

The audit generally needs the preceding call as well as the calls/results for
its four target quarters, and sometimes older guidance. Four transcripts alone
do not guarantee four evaluable promise-delivery comparisons. Qualitative
guidance has no artificial numeric range or unit.

## Cache and telemetry

`SQLiteCache` / `FinnCache` uses `(ticker, data_type, content_hash)` as the primary
key. `CacheKey.for_content` hashes deterministic, type-framed JSON or bytes;
the key's hash is a request/content fingerprint and can differ from a raw
source-document SHA-256. `for_llm` additionally includes task, provider, model,
generation settings, prompt version, schema version and the actual output
schema. Pass the complete rendered request as `inputs`, including messages,
question, source hashes/chunk IDs and every generation option. Changing prompts,
sources or schema then invalidates the cached response.

Raw scrape content is only known after fetching. To avoid repeat downloads,
the scraper should cache a URL/parameters/parser-version request fingerprint
pointing to the latest raw-content key, with a short TTL; use HTTP conditional
requests where supported. A raw content hash alone cannot establish freshness
or discover the newest filing. Both request pointers and content payloads can
use the supplied `get`/`put` API.

Default TTLs are six hours for scrapes and 30 days for LLM outputs. Explicit
`ttl_seconds=None` retains immutable content indefinitely. Cache lookup results
distinguish a real cached JSON `null` from a miss. Checksums, JSON parsing and
optional model revalidation protect reads; invalid/expired entries are evicted
and recorded as misses. Database failures propagate. No pickle/deserialization
of executable objects is used.

This executable example uses synthetic arithmetic only:

```python
from backend.cache import CacheKey, SQLiteCache
from backend.config import Settings

settings = Settings.from_env()
inputs = {"revenue_inr_crore": 1000.0, "ebitda_inr_crore": 180.0}
key = CacheKey.for_content("RELIANCE", "financial_metrics", inputs,
                          context={"calculator_version": "1"})

with SQLiteCache(settings=settings.cache) as cache:
    cached = cache.get(key, request_id="example-1")
    if cached.hit:
        result = cached.value
    else:
        with cache.track("financial_metrics", ticker="RELIANCE", tier=0,
                         request_id="example-1"):
            result = {"ebitda_margin_pct": inputs["ebitda_inr_crore"] / inputs["revenue_inr_crore"] * 100}
        cache.put(key, result)
    print(result)
    print(cache.telemetry(request_id="example-1").model_dump_json(indent=2))
```

For provider work, call `track(..., tier=1 or 2, route=router.route(task))` only
after a miss. Report normalized usage with `usage.set_tokens(...)`: total input
**includes** cached provider input; output includes billed reasoning/thinking
tokens exactly once. The ledger discounts provider-cached input once. Local
cache hits consume no tokens and increment no provider-call counter. Unreported
usage or unknown prices increment `unpriced_llm_calls`; they are never silently
free. Pricing estimates cover text tokens, excluding tools, hosted-cache storage,
non-text inputs and infrastructure charges.

Retries are separate events. Event IDs make ledger replay idempotent. `put`
can commit a validated output and a successful `TelemetryEvent` in one
transaction; choose that pattern or `track`, avoiding duplicate events for one
attempt. Failed calls should still record their usage, without caching errors.
Only error class names are stored, not prompts or exception messages. Costs
are persisted as integer nano-USD; telemetry aggregates cache counts, tier
calls, failures, tokens, known costs, unpriced calls and operation latency.

WAL, a busy timeout and short transactions support concurrent threads and local
processes. A manager serializes its connection and should be instantiated after
forking. Async servers should call these synchronous methods in a worker
thread. Keep the database on a persistent local volume, not a network-mounted
SQLite file. Multi-worker deduplication of expensive in-flight LLM requests
belongs in the orchestration layer; concurrent upserts alone do not provide
exactly-once inference. `purge_expired`, scoped `invalidate`, and
`prune_telemetry` provide explicit maintenance.

The next implementation step is the provider adapters and primary-source
research/audit orchestration, followed by the mobile workflow. All three data
pillars and the regex classifier run without LLM credentials.
