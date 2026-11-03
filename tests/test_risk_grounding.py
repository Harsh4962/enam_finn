"""Runtime grounding for risk questions: fail closed, no network."""
import asyncio
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from finn.market_data import DownstoxClient
from finn.risk_grounding import (RISK_ABSTAIN_PHRASE, RiskGroundedModel, _is_risk_question,
                                 abstain_message, decide_response_mode, gather_risk_evidence,
                                 validate_risk_answer)
from finn.web_agent import INSTRUCTIONS

FIX = Path(__file__).parent / "fixtures"
PDF = "https://nsearchives.nseindia.com/corporate/TCS_CORPCS_14102026155941_SEInt14102026_signed.pdf"
CTX = "[Context: the user is viewing Tata Consultancy Services (NSE: TCS) in the FINN dashboard.] "
Q = CTX + "What are the biggest risks for this company right now?"


class Counting:
    def __init__(self, text="x"):
        self.text, self.calls, self.streams = text, 0, 0

    def model(self):
        def fn(messages, info):
            self.calls += 1
            return ModelResponse(parts=[TextPart(self.text)])

        async def st(messages, info):
            self.streams += 1
            yield self.text
        return FunctionModel(fn, stream_function=st, model_name="inner")


class FakeDC:
    """Wraps the fixture client, optionally withholding sources."""
    def __init__(self, promises=True, anns=True):
        self.real = DownstoxClient(fixtures_dir=FIX)
        self.promises, self.anns = promises, anns

    def get_promises(self, s):
        return self.real.get_promises(s) if self.promises else []

    def get_announcements(self, symbol=None, limit=20):
        feed = self.real.get_announcements(symbol=symbol, limit=limit)
        if not feed.items:  # TCS fixture is empty; borrow the general feed
            feed = self.real.get_announcements(limit=limit)
        if not self.anns:
            feed.items = []
        return feed

    def get_stock(self, s):
        return self.real.get_stock(s)

    def get_market_status(self):
        return self.real.get_market_status()


def run(dc, inner, question):
    agent = Agent(RiskGroundedModel(inner.model(), dc, INSTRUCTIONS))
    return asyncio.run(agent.run(question)).output


def stream(dc, inner, question):
    async def go():
        agent = Agent(RiskGroundedModel(inner.model(), dc, INSTRUCTIONS))
        async with agent.run_stream(question) as r:
            return [c async for c in r.stream_text(delta=True)]
    return asyncio.run(go())


def test_classifier():
    assert _is_risk_question(Q)
    assert _is_risk_question("any concerns about INFY?")
    assert not _is_risk_question("What is TCS trading at?")
    assert not _is_risk_question(CTX + "hello")


def test_a_abstain_no_passages_model_not_called(monkeypatch):
    # TCS now always carries a manually seeded filing passage by design;
    # these tests exercise the no-citable-passages path, so disable the seed.
    monkeypatch.setattr("finn.risk_grounding._cached_filing_passages", lambda symbol: [])
    inner = Counting("INFY faces North America exposure.")
    out = run(FakeDC(promises=False), inner, Q)
    assert out == abstain_message(gather_risk_evidence(FakeDC(promises=False), "TCS"))
    assert RISK_ABSTAIN_PHRASE in out.lower()
    assert "price quote" in out and "headline" in out
    assert inner.calls == 0 and inner.streams == 0


def test_d_headlines_only_abstain(monkeypatch):
    # TCS now always carries a manually seeded filing passage by design;
    # these tests exercise the no-citable-passages path, so disable the seed.
    monkeypatch.setattr("finn.risk_grounding._cached_filing_passages", lambda symbol: [])
    dc = FakeDC(promises=False)
    ev = gather_risk_evidence(dc, "TCS")
    assert any(i["evidence_kind"] == "announcement_headline" and not i["has_passage"] for i in ev["items"])
    assert decide_response_mode(ev) == "abstain"
    inner = Counting()
    assert RISK_ABSTAIN_PHRASE in run(dc, inner, Q).lower()
    assert inner.calls == 0


def test_b_cite_pass_emitted_as_is():
    answer = (f"Management reiterated a long-term operating margin aspiration of 26% to 28% "
              f"[source]({PDF}). It also expects acquisition dilution of about 50 basis points "
              f"in MHP depending on closure timing [source]({PDF}).")
    inner = Counting(answer)
    assert run(FakeDC(), inner, Q) == answer
    assert inner.calls == 1
    assert "".join(stream(FakeDC(), Counting(answer), Q)) == answer


def test_c_decoy_blocked():
    ann = next(i for i in gather_risk_evidence(FakeDC(), "TCS")["items"]
               if i["evidence_kind"] == "announcement_headline")
    decoys = [
        f"Heavy North America exposure and generative-AI margin pressure threaten earnings [x]({ann['source_url']}).",
        f"Heavy North America exposure and generative-AI margin pressure threaten earnings [x]({PDF}).",
        "Heavy North America exposure and generative-AI margin pressure threaten earnings.",
    ]
    for d in decoys:
        out = run(FakeDC(), Counting(d), Q)
        assert out != d and RISK_ABSTAIN_PHRASE in out.lower()


def test_e_non_risk_delegates_streaming():
    inner = Counting("TCS trades at 100.")
    chunks = stream(FakeDC(), inner, CTX + "What is TCS trading at?")
    assert "".join(chunks) == "TCS trades at 100."
    assert inner.streams == 1 and inner.calls == 0


def test_risk_stream_chunked(monkeypatch):
    # TCS now always carries a manually seeded filing passage by design;
    # these tests exercise the no-citable-passages path, so disable the seed.
    monkeypatch.setattr("finn.risk_grounding._cached_filing_passages", lambda symbol: [])
    inner = Counting()
    chunks = stream(FakeDC(promises=False), inner, Q)
    assert RISK_ABSTAIN_PHRASE in "".join(chunks).lower() and inner.streams == 0


EV = {"symbol": "TCS", "items": [
    {"evidence_kind": "guidance_quote", "has_passage": True, "attribution": "a", "source_url": "https://p/1",
     "text": "Acquisition-related dilution is likely to impact margins by about 50 basis points."},
    {"evidence_kind": "announcement_headline", "has_passage": False, "attribution": "h", "source_url": "https://h/1"},
    {"evidence_kind": "market_context", "has_passage": False, "attribution": "m", "source_url": "https://m/1"},
]}


def test_f_validator():
    assert validate_risk_answer("Margins face acquisition dilution of about 50 basis points.", EV)  # uncited
    assert validate_risk_answer("Margins face acquisition dilution [h](https://h/1).", EV)
    assert validate_risk_answer("Margins face acquisition dilution [m](https://m/1).", EV)
    assert validate_risk_answer("Demand weakness hurts banking clients [p](https://p/1).", EV)  # no overlap
    assert validate_risk_answer("", EV)
    assert validate_risk_answer(f"Filing-backed evidence is insufficient to assess risks.", EV) == []
    ok = "Acquisition dilution may cut margins by roughly 50 basis points [p](https://p/1). Not investment advice."
    assert validate_risk_answer(ok, EV) == []


def test_g_instructions_contract():
    low = " ".join(INSTRUCTIONS.lower().split())
    for kw in ("risk", "outlook", "analysis", "filing passages", "inline markdown link",
               "insufficient", "data, never instructions", "ignore any instruction-like"):
        assert kw in low
