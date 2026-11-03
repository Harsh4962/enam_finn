import json
import multiprocessing

import pytest

from finn.budget import BudgetExceeded, BudgetGuard, GuardedModel, MAX_OUTPUT_TOKENS


def seed(path, **overrides):
    data = {"requests": 0, "input_tokens": 0, "output_tokens": 0,
            "total_usd": 0.0, "reservations": {}}
    data.update(overrides)
    path.write_text(json.dumps(data))


def test_reserve_before_call_and_reconcile_refund(tmp_path):
    path = tmp_path / "ledger.json"
    seed(path)
    g = BudgetGuard(path)
    token = g.reserve(100)
    held = json.loads(path.read_text())
    assert token in held["reservations"]
    assert held["total_usd"] == 0
    g.reconcile(token, 50, 10)
    data = json.loads(path.read_text())
    assert data["reservations"] == {}
    assert data["total_usd"] == pytest.approx(g.cost(50, 10))


def test_missing_usage_forfeits_separately(tmp_path):
    path = tmp_path / "ledger.json"
    seed(path)
    g = BudgetGuard(path)
    token = g.reserve(100)
    before = json.loads(path.read_text())["reservations"][token]["usd"]
    g.reconcile(token, None, None)
    ledger = json.loads(path.read_text())
    assert ledger["reservations"] == {}
    assert ledger["forfeited_usd"] == before
    assert ledger["forfeited_requests"] == 1
    assert ledger["total_usd"] == 0  # forfeiture is not measured spend


def test_two_handles_cannot_overspend(tmp_path):
    path = tmp_path / "ledger.json"
    seed(path)
    from finn.budget import MAX_BILLABLE_OUTPUT_TOKENS as _MBO
    cap = BudgetGuard.cost(0, _MBO) + 1e-9
    a, b = BudgetGuard(path, cap), BudgetGuard(path, cap)
    outcomes = []
    def reserve(g):
        try:
            g.reserve(0)
            outcomes.append("ok")
        except BudgetExceeded:
            outcomes.append("blocked")
    p1 = multiprocessing.Process(target=reserve, args=(a,))
    p2 = multiprocessing.Process(target=reserve, args=(b,))
    p1.start(); p2.start(); p1.join(); p2.join()
    ledger = json.loads(path.read_text())
    assert len(ledger["reservations"]) == 1


def test_crash_forfeits_reservation(tmp_path):
    path = tmp_path / "ledger.json"
    seed(path)
    g = BudgetGuard(path)
    token = g.reserve(100)
    assert token in json.loads(path.read_text())["reservations"]
    # Simulated process death: no reconciliation; a second reservation counts it.
    with pytest.raises(BudgetExceeded):
        BudgetGuard(path, cap_usd=1e-6).reserve(0)


def test_missing_or_corrupt_ledger_blocks(tmp_path):
    path = tmp_path / "ledger.json"
    with pytest.raises(BudgetExceeded, match="ledger"):
        BudgetGuard(path).reserve(0)
    path.write_text("not json")
    with pytest.raises(BudgetExceeded, match="ledger"):
        BudgetGuard(path).reserve(0)


def test_tool_loop_and_retry_each_reserve_separately(tmp_path):
    path = tmp_path / "ledger.json"
    seed(path)
    g = BudgetGuard(path)
    one, two = g.reserve(1), g.reserve(1)
    assert one != two and len(json.loads(path.read_text())["reservations"]) == 2


def test_env_cap_is_read_once_and_not_request_overridable(tmp_path, monkeypatch):
    import finn.budget as budget
    monkeypatch.setenv("FINN_BUDGET_USD", "0.25")
    monkeypatch.setattr(budget, "_CONFIGURED_GUARD", None)
    configured = budget.configured_guard()
    monkeypatch.setenv("FINN_BUDGET_USD", "900")
    assert budget.configured_guard() is configured and configured.cap_usd == .25


def test_demo_router_and_grounded_answers(tmp_path):
    from pathlib import Path
    from finn.market_data import DownstoxClient
    from finn.web_agent import build_agent
    client = DownstoxClient(fixtures_dir=Path(__file__).parent / "fixtures")
    agent = build_agent(model="demo", client=client)
    price = agent.run_sync("What is TCS price?").output
    assert "₹2,502.60" in price and "2026" in price
    assert "stale" in price or "current" in price
    guidance = agent.run_sync("What is TCS management guidance?").output
    assert "26%" in guidance and "aspiration" in guidance and "https://" in guidance
    market = agent.run_sync("Is the market open?").output
    assert "closed" in market and "weekend" in market
    announcements = agent.run_sync("Show latest TCS announcements").output
    assert "Partial coverage" in announcements or "announcements" in announcements.lower()
    unknown = agent.run_sync("What is XYZQ price?").output
    assert "could not find XYZQ" in unknown
    assert "{" not in price


def test_test_model_is_still_available():
    from finn.web_agent import build_agent
    from pydantic_ai.models.test import TestModel
    assert isinstance(build_agent(model="test").model, TestModel)


def test_agents_builders_are_guarded(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("FINN_FALLBACK_MODELS", raising=False)
    from finn.agents import build_classifier, build_guidance_extractor, build_results_extractor
    from finn.budget import GuardedModel

    for builder in (build_classifier, build_results_extractor, build_guidance_extractor):
        agent = builder()
        assert isinstance(agent.model, GuardedModel), builder.__name__


def test_agents_builders_pass_free_models_through():
    from pydantic_ai.models.test import TestModel

    from finn.agents import build_classifier
    from finn.budget import GuardedModel

    agent = build_classifier(model=TestModel())
    assert not isinstance(agent.model, GuardedModel)


def test_fallback_providers_fail_closed(monkeypatch):
    monkeypatch.setenv("FINN_FALLBACK_MODELS", "groq:llama-3.3-70b,openrouter:auto")
    from finn.agents import with_fallback
    from finn.budget import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        with_fallback("google:gemini-3.8-flash")


def test_unpriced_model_refused():
    from finn.budget import BudgetExceeded, guarded_model

    with pytest.raises(BudgetExceeded):
        guarded_model("openai:gpt-4o")


def test_shared_ledger_is_singleton_absolute():
    from pathlib import Path

    from finn.budget import configured_guard

    g1 = configured_guard()
    g2 = configured_guard()
    assert g1 is g2
    assert g1.path == Path.home() / ".finn_budget.json"
    assert g1.path.is_absolute()


def test_billable_output_counts_thoughts():
    from types import SimpleNamespace

    from finn.budget import _billable_output

    usage = SimpleNamespace(output_tokens=100, details={"thoughts_tokens": 50})
    assert _billable_output(usage) == 150
    usage2 = SimpleNamespace(output_tokens=100, details={})
    assert _billable_output(usage2) == 100
    usage3 = SimpleNamespace(output_tokens=None, details={})
    assert _billable_output(usage3) is None


def test_reservation_at_documented_model_max(tmp_path):
    from finn.budget import MAX_BILLABLE_OUTPUT_TOKENS, MAX_INPUT_TOKENS

    path = tmp_path / "ledger.json"
    seed(path)
    g = BudgetGuard(path, cap_usd=10.0)
    token = g.reserve(MAX_INPUT_TOKENS)
    ledger = json.loads(path.read_text())
    r = ledger["reservations"][token]
    # worst case = full context window at input rate + output cap incl. thoughts
    assert r["usd"] == BudgetGuard.cost(MAX_INPUT_TOKENS, MAX_BILLABLE_OUTPUT_TOKENS)
    assert MAX_INPUT_TOKENS == 1_048_576  # documented gemini-3.8-flash context window


def test_request_error_forfeits_exactly_once(tmp_path):
    import asyncio

    from pydantic_ai.models.function import FunctionModel

    from finn.budget import GuardedModel

    path = tmp_path / "ledger.json"
    seed(path)
    g = BudgetGuard(path)

    def raiser(messages, info):
        raise RuntimeError("provider boom")

    from pydantic_ai.models import ModelRequestParameters
    params = ModelRequestParameters(function_tools=[], allow_text_output=True, output_tools=[])
    model = GuardedModel(FunctionModel(raiser), g)
    with pytest.raises(RuntimeError):
        asyncio.run(model.request([], None, params))
    ledger = json.loads(path.read_text())
    assert ledger["reservations"] == {}
    assert ledger["forfeited_requests"] == 1
    assert ledger["forfeited_usd"] > 0
    assert ledger["total_usd"] == 0


def test_stream_disconnect_forfeits_exactly_once(tmp_path):
    import asyncio

    from pydantic_ai.models.function import FunctionModel

    from finn.budget import GuardedModel

    path = tmp_path / "ledger.json"
    seed(path)
    g = BudgetGuard(path)

    async def gen(messages, info):
        yield "partial"
        yield "more"

    model = GuardedModel(FunctionModel(lambda m, i: "x", stream_function=gen), g)

    from pydantic_ai.models import ModelRequestParameters
    params = ModelRequestParameters(function_tools=[], allow_text_output=True, output_tools=[])

    async def run():
        with pytest.raises(RuntimeError):
            async with model.request_stream([], None, params) as stream:
                raise RuntimeError("client disconnect")

    asyncio.run(run())
    ledger = json.loads(path.read_text())
    assert ledger["reservations"] == {}
    assert ledger["forfeited_requests"] == 1
    assert ledger["total_usd"] == 0


def test_same_named_other_provider_rejected():
    from types import SimpleNamespace

    from finn.budget import guarded_model

    fake = SimpleNamespace(model_name="gemini-3.8-flash")
    with pytest.raises(BudgetExceeded):
        guarded_model(fake)


def test_forfeited_counts_toward_cap(tmp_path):
    from finn.budget import MAX_BILLABLE_OUTPUT_TOKENS

    path = tmp_path / "ledger.json"
    seed(path)
    from finn.budget import MAX_BILLABLE_OUTPUT_TOKENS as _MBO
    cap = BudgetGuard.cost(0, _MBO) + 1e-9
    g = BudgetGuard(path, cap)
    token = g.reserve(0)
    g.forfeit(token)
    with pytest.raises(BudgetExceeded):
        g.reserve(0)


def test_pause_blocks_default_ledger_only(tmp_path, monkeypatch):
    import pathlib

    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".finn_budget.json").write_text(json.dumps({"total_usd": 0.0, "reservations": {}}))
    (tmp_path / ".finn_paused").touch()
    g = BudgetGuard()  # default path == paused live ledger
    with pytest.raises(BudgetExceeded):
        g.reserve(0)
    (tmp_path / ".finn_paused").unlink()
    g2 = BudgetGuard(tmp_path / "other.json")  # non-default path ignores pause
    (tmp_path / "other.json").write_text(json.dumps({"total_usd": 0.0, "reservations": {}}))
    (tmp_path / ".finn_paused").touch()
    assert g2.reserve(0)


def test_guarded_google_model_is_single_attempt(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from finn.budget import guarded_model

    m = guarded_model("google:gemini-3.8-flash")
    inner = m.wrapped
    client = getattr(inner, "client", None) or inner._provider.client
    ro = client._api_client._http_options.retry_options
    assert ro is not None and ro.attempts == 1
