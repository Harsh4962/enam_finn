"""One real, manually downloaded filing, transcribed by hand into typed rows.

Source: TCS Q2 FY27 press release as filed with NSE/BSE on 8 Oct 2026 (Regulation 30),
downloaded once from NSE's announcement archive:
https://nsearchives.nseindia.com/corporate/TCS_CORPCS_08102026154325_PressReleaseletter.pdf
Consolidated Ind-AS figures in Rs crore, financial summary table on page 3.
EBITDA is not stated in the press release, so it is abstained, not estimated.
The press release itself states revenue growth of +1.3% QoQ / +11.2% YoY, which the
ledger's computed growth is validated against."""
from __future__ import annotations

from datetime import date

from .models import MoneyValue, ResultsFiling

SOURCE_URL = "https://nsearchives.nseindia.com/corporate/TCS_CORPCS_08102026154325_PressReleaseletter.pdf"
STATED_QOQ_PCT = 1.3


def tcs_filings() -> list[ResultsFiling]:
    def f(end, rev, pbt, pat):
        return ResultsFiling(company="Tata Consultancy Services", period_end=end, consolidated=True, standard="Ind-AS",
                             revenue_from_operations=MoneyValue(amount=rev, page=3), ebitda=None,
                             pbt=MoneyValue(amount=pbt), pat=MoneyValue(amount=pat),
                             source_page=3)
    return [
        f(date(2025, 9, 30), 65799, 17203, 12904),
        f(date(2026, 6, 30), 72275, 18612, 13849),
        f(date(2026, 9, 30), 73188, 18638, 13884),
    ]
