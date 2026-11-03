"""Conversational regression tests for the demo router."""
from finn.market_data import DownstoxClient
from finn.web_agent import build_agent

FIXTURES = "tests/fixtures"


def _agent():
    return build_agent(model="demo", client=DownstoxClient(fixtures_dir=FIXTURES))


def test_greeting_hi_no_ticker_error():
    out = _agent().run_sync("hi").output
    assert "identify" not in out and "unsupported" not in out
    assert "ask me" in out.lower()


def test_thanks_smalltalk():
    out = _agent().run_sync("thanks").output
    assert "identify" not in out.lower()
    assert "anytime" in out.lower()


def test_followup_resolves_ticker_from_context():
    a = _agent()
    first = a.run_sync("What is TCS trading at?")
    out = a.run_sync("and its guidance?", message_history=first.all_messages()).output
    assert "26%" in out


def test_unknown_ticker_honest_not_outage():
    out = _agent().run_sync("What is XYZQ price?").output
    assert "could not find XYZQ" in out
    assert "unreachable" not in out


def test_finance_without_ticker_asks_which():
    out = _agent().run_sync("what is the stock price?").output
    assert "which company" in out.lower()


def test_market_open_question():
    out = _agent().run_sync("Is the market open?").output
    assert "closed" in out and "weekend" in out
