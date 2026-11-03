# Integration notes (updated 1 Nov 2026, verified against the running system)

## Stack (verified)
- pydantic-ai 2.64.0 (no [google]/[web] extras; google support built in,
  `Agent.to_web()` provides the web UI and mounts the API at /api).
- Live model: google:gemini-3.8-flash (the google-gla: prefix is NOT accepted
  by this pydantic-ai version; guarded_model normalizes it).
- Python 3.11, fastmcp-slim[server]==4.1.4, pypdf, pytest 9.2.0.

## Entry points
- Web chat: `uvicorn` on 127.0.0.1:7932 running `build_agent().to_web(...)`
  from finn/web_agent.py (see launch command in ops notes). Public access via
  cloudflared quick tunnel (ephemeral hostname).
- Extraction agents: finn/agents.py (classifier / results / guidance),
  finn/extract.py. Offline demo: FINN_MODEL=demo (FunctionModel router) or
  TestModel for tests; FINN_FIXTURES=tests/fixtures pins data to snapshots.

## Spend control (budget guard v3)
- Every paid model construction goes through finn.budget.guarded_model():
  free models (TestModel/FunctionModel) pass through; paid models must be
  google:gemini-3.8-flash on an actual GoogleModel instance, anything else
  fails closed. FINN_FALLBACK_MODELS is refused (unpriced providers disabled).
  All Google clients are built single-attempt (HttpRetryOptions(attempts=1),
  which also disables google-genai's implicit connection retries) so one
  reservation covers every HTTP attempt.
- Reservation ledger at ~/.finn_budget.json (absolute, fcntl cross-process
  lock, fail-closed when missing/corrupt). Cap is startup-only via
  FINN_BUDGET_USD (currently 5.00 TOTAL, shared across web/extract/demo).
- Per request: reserve the DOCUMENTED per-request maximum BEFORE the call -
  full context window (1,048,576 input tokens, gemini-3.8-flash limit per
  ai.google.dev/gemini-api/docs/generate-content/whats-new-gemini-3.8) at
  input rate + 2 x 2048 output worst case. Output is provider-bounded:
  max_output_tokens=2048 is enforced and includes thought tokens
  (ai.google.dev/gemini-api/docs/thinking). Reconcile actual usage after,
  billing thoughts_tokens (reported separately from candidates output).
  Errors, disconnects and missing usage forfeit the full reservation into
  forfeited_usd (counted toward the cap, tracked separately from measured
  spend). Reservations settle exactly once.

## Data sources and honesty rules
- Market data: Downstox free public API (not an exchange feed). Freshness is
  computed from quoted_at against the server clock (freshness_label); the
  upstream stale flag is advisory only. Degraded feeds are named in answers.
- Guidance: verbatim quotes with bounds/hedges preserved; ranges stay ranges.

## Validation
- 459 tests + evals pass (pytest tests/ evals/), including budget-guard v4,
  conversational regression and S1-S8 persona scenario tests.
