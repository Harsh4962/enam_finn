# Offline scenario evaluations

Run from the repository root:

```sh
.venv/bin/python -m pytest evals/test_offline_scenarios.py -q
```

The suite uses `pydantic_evals.Dataset` and `Case` in code. It runs fixture and literal inputs only: no live model provider, network, or LLM judge is used. It writes `evals/results.json`, including each scenario's fixture citation, elapsed wall time, model request count, tool request count, and tool trajectory. Those counters describe this local run; token and cost fields are not available for these deterministic checks and can be added when a future live run records them.

## Scenarios and evidence

| Scenario | Fixture / input | What it proves | Limits |
| --- | --- | --- | --- |
| 24 hour digest | `tests/fixtures/downstox-ann-feed.json` | Filters to the feed's 24 hour window, repeated ingestion yields duplicates, a changed revision is a correction, and fixture newspaper notices are grouped into one routine digest row. | The store detects a changed body under an existing ID; the fixture has no explicit correction record. Routine grouping here is a small evaluation policy, not a production digest renderer. |
| Unusual activity | Literal closes and volumes through `finn.alerts.price_volume_alert` | Volume multiplier and EOD move thresholds are configurable; absent history abstains. | Synthetic series prove threshold semantics only, not a vendor's historical coverage or alert quality. |
| Results KPI | `tests/fixtures/zentec-q2fy27.pdf` | Missing metric stays `None`; Sales YoY is 98.8 while Total Revenue YoY is 81.9; the parsed table carries consolidated basis. Standalone/consolidated figures are marked incomparable. | The last mismatch assertion checks basis policy against this consolidated fixture; it does not parse a standalone ZENTEC source. |
| Guidance | `tests/fixtures/downstox-promises-TCS.json` and literal 10% boundary | Inclusive 10% lower bound, range, aspiration hedge, metric, source basis, and horizon are retained. | The double-digit boundary is a policy boundary assertion, not output from an extraction model. |
| Tool routing | `tests/fixtures/downstox-stock-TCS.json`, `tests/fixtures/downstox-holidays.json` | A deterministic `FunctionModel` receives the prompt and emits a tool call; `Hooks(before_tool_execute=...)` records selected tool and validated arguments. Snapshot routes with `TCS`; market-hours routes to status. | Deterministic-model checks only; they are not evidence of live-model routing quality. |
| Unknown ticker | Fixture directory miss for `ZZZUNKNOWN` | Snapshot tool returns the fixture lookup error, and the market-data client raises `MarketDataError`; no quote is fabricated. | Proves local missing-fixture behavior, not every upstream error mode. |
| Upstream failure coverage | `tests/fixtures/downstox-ann-TCS.json` | BSE failed/withheld status produces an explicit partial-coverage note. | The fixture models one degraded feed response. |
| Primary-source-only | Literal global-news question; available sources restricted to local exchange fixtures | A policy refusal is represented for a question unsupported by available primary sources. | This is a deterministic policy assertion, not a live agent refusal test. |

The installed versions were inspected before implementation. In this environment `pydantic-evals.Dataset.evaluate` accepts a callable task and `pydantic-ai 2.64.0` provides `FunctionModel` and `capabilities.Hooks`; the routing scenarios use those actual APIs. No production files were changed.
# Live Gemini budget guard

Live Gemini requests are gated and recorded in an atomic JSON ledger at `~/.finn_budget.json` (cap configurable with `FINN_BUDGET_USD`, default $1.00); missing usage is charged a conservative token estimate, and unreadable ledgers block calls. The guard uses paid-tier rates of $0.75/M input and $3.75/M output tokens as of 1 Nov 2026; free-tier keys may bill $0, but the guard still charges these conservative rates. Offline `TestModel` runs do not use the ledger.
