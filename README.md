# FINN

An investment-research assistant for listed Indian companies, built for the
ENAM AI Engineer Build Challenge.

FINN answers market questions like a calm research partner: it answers first,
cites a source next to every claim, labels how fresh the data is, and says
plainly when it does not have the evidence. It never invents numbers - every
market fact comes through a typed tool, and deterministic code (never the
model) does the arithmetic and comparisons.

## What it does

- **Research chat** - a Pydantic AI agent with typed market-data tools, served
  through the official Pydantic AI web UI. Quotes, announcements, guidance,
  market status and filing-backed answers, each with inline source links.
- **Company dashboard** - a mobile-first web UI: search a company, load its
  snapshot, sections and signals, and upload a results PDF to ground
  risk/outlook answers in that filing.
- **Guidance and announcements** - verbatim management guidance with bounds
  and hedges preserved, NSE/BSE announcement ingestion with duplicate and
  revision handling, and 24-hour digests.
- **Threshold alerts** - user-defined volume/price-move rules; the alert
  abstains when there is no history to compare against.
- **MCP server** - the same typed market-data tools exposed over stdio for
  other agents.

## Repository layout

| Path | Responsibility |
| --- | --- |
| `finn/web_agent.py` | Research agent and web app: `build_agent()`, `build_web_app()` |
| `finn/ui_api.py` + `finn/static/` | Dashboard API (Starlette) and the static UI |
| `finn/mcp_server.py` | Market-data MCP server (stdio) |
| `finn/market_data.py` | Typed Downstox client: snapshots, announcements, guidance, market status |
| `finn/extract.py`, `finn/filings.py`, `finn/fetch_filing.py` | Results-PDF extraction, filing passage cache, allowlisted PDF fetch |
| `finn/ledger.py` | Deterministic quarter-over-quarter comparison (no model calls) |
| `finn/risk_grounding.py` | Fail-closed grounding for risk/outlook questions |
| `finn/store.py`, `finn/live_feed.py`, `finn/alerts.py` | Announcement store with dedupe/revisions, live feed, threshold alerts |
| `finn/budget.py` | Fail-closed spend guard for paid model calls (see below) |
| `backend/` | Earlier foundation: strict schemas, SQLite cache/telemetry, pull/push engines |
| `evals/` | Offline pydantic-evals scenario suite and its README |
| `tests/` | Unit and integration tests plus recorded fixtures |
| `docs/FOUNDATION.md` | Original step-by-step foundation document |
| `AUDIT.md`, `PROPOSED_FIX_REVIEW.md`, `verification/` | Audit trail of the foundation work |

## Setup

Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

The web app, evals and MCP server additionally use packages not in the core
dependency set:

```bash
python -m pip install uvicorn starlette httpx pydantic-evals "fastmcp-slim[server]"
```

## Run

Offline demo (deterministic model, recorded fixtures, no API key):

```bash
FINN_MODEL=demo FINN_FIXTURES=tests/fixtures python -m finn.web_agent
```

Then open http://127.0.0.1:7932 - the dashboard is at `/`, the research chat
at `/chat`. Local development only: the chat endpoint is unauthenticated by
design, so bind localhost.

Live mode needs a Google API key:

```bash
export GOOGLE_API_KEY=...          # Gemini key
export FINN_MODEL=google:gemini-3.8-flash
export FINN_BUDGET_USD=5.00        # total spend cap, see below
python -m finn.web_agent
```

Other entry points:

```bash
python -m finn.mcp_server     # market-data MCP server over stdio
python -m finn.demo           # print one seeded day as a digest
python -m finn.pull tcs.html  # static research page from typed data
python -m finn.web out.html   # static digest page
```

## Data sources and honesty rules

- Market data comes from the **Downstox free public API**, not an official
  exchange feed. Answers say so, state the computed freshness of each quote,
  and name degraded feeds instead of guessing. Absence of an announcement in
  a degraded feed is never treated as proof that no filing exists.
- Guidance is quoted verbatim with its target bounds and source; a range
  stays a range and a hedge stays a hedge.
- Risk, outlook and analysis questions are answered **only** from retrieved
  filing passages, each claim carrying an inline link. When filing-backed
  evidence is insufficient, the agent says so instead of narrating from
  general knowledge.
- Filing PDFs are fetched only from allowlisted NSE/BSE archive hosts, or
  uploaded by the user. Exchange websites are not scraped.
- Demo fixtures under `finn/seed.py` are synthetic and labeled as such.

## Spend control

Every paid model call goes through `finn.budget`: a process-safe reservation
ledger at `~/.finn_budget.json` that reserves the documented per-request
maximum before the call, reconciles actual usage after, and fails closed if
the ledger is missing or corrupt. `FINN_BUDGET_USD` sets the total cap
(default $1.00) shared across web, extraction and demo entry points. Offline
`TestModel`/`demo` runs never touch the ledger.

## Tests

```bash
python -m pytest tests/ evals/ -q
```

507 tests and 187 subtests pass, fully offline: no live model provider,
network or LLM judge. The eval suite (`evals/`) records per-scenario
evidence, latency and tool trajectories in `evals/results.json`; see
`evals/README.md` for what each scenario proves and its limits.

## Limitations

- Free third-party data: quotes are not tick-level, and BSE coverage can be
  degraded; freshness and coverage are labeled, not hidden.
- The offline evals prove deterministic behavior against recorded fixtures;
  they are not evidence of live-model routing quality.
- The chat endpoint has no authentication - run it locally or behind your
  own proxy.
- Nothing here is investment advice.
# finn_enam
