"""Persona scenario evals S1-S8 (offline, fixture-backed demo router)."""
from finn.market_data import DownstoxClient
from finn.web_agent import build_agent

FIXTURES = "tests/fixtures"


def _agent():
    return build_agent(model="demo", client=DownstoxClient(fixtures_dir=FIXTURES))


def test_s1_greeting_plain_no_error_no_suggestions():
    out = _agent().run_sync("hi").output
    assert "ask me" in out.lower()
    assert "identify" not in out and "error" not in out.lower()


def test_s2_ambiguous_entity_resolves_and_answers():
    out = _agent().run_sync("how did TCS do").output
    assert "2,502.60" in out  # resolves TCS and answers from loaded data


def test_s3_missing_data_scoped_abstention_no_invention():
    out = _agent().run_sync("What is ZENTEC management guidance?").output
    assert "could not find ZENTEC" in out
    assert "%" not in out.split("could not find")[0]  # no invented figures


def test_s4_freshness_label_present_on_price():
    out = _agent().run_sync("What is TCS trading at?").output
    assert "quoted" in out
    assert any(k in out for k in ("live", "recent", "stale", "clock-skew", "EOD", "old"))


def test_s5_guidance_preserves_bounds_and_hedges():
    out = _agent().run_sync("What is TCS management guidance?").output
    assert "26%" in out and "aspiration" in out  # hedge wording preserved


def test_s6_price_answer_carries_asof_timestamp():
    out = _agent().run_sync("TCS price please").output
    assert "2026-10-30T09:58:16" in out  # quoted_at as-of


def test_s7_followup_resolves_context():
    a = _agent()
    first = a.run_sync("What is TCS trading at?")
    out = a.run_sync("and its guidance?", message_history=first.all_messages()).output
    assert "26%" in out


def test_s8_offtopic_friendly_steer_no_error():
    out = _agent().run_sync("what's the weather").output
    assert "identify" not in out.lower()
    assert "could not find" in out or "finance" in out.lower() or "market" in out.lower()
