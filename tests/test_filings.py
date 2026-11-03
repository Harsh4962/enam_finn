"""Tests for finn.filings: passage chunking, permitted-source ingestion
(user uploads, manual seed), cache IO, and evidence shape. No network, no
model calls. The module must never fetch: exchange-archive retrieval is
disabled per NSE/BSE website terms."""
import json

import pytest

from finn import filings
from finn.filings import FilingIngestError


def _make_pdf_bytes(text="Revenue for the quarter rose 12%. Profit after tax 500 crore. EBITDA margin expanded. "):
    import fitz
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((72, 72), text * 10)
    data = doc.tobytes()
    doc.close()
    return data


def test_chunk_pages_prefers_financial_pages_and_caps():
    pages = [(1, "Contents page " * 20), (2, "revenue profit ebitda margin crore quarter eps income expense " * 20),
             (3, "auditor signature " * 20)]
    picked = filings._chunk_pages(pages, 1)
    assert len(picked) == 1
    assert picked[0]["page"] == 2


def test_ingest_pdf_bytes_roundtrip(tmp_path):
    out = filings.ingest_pdf_bytes("TEST", _make_pdf_bytes(), title="Q2 results", cache_dir=tmp_path,
                                   source_note="downloaded by the user from the company site")
    assert out["symbol"] == "TEST" and len(out["filings"]) == 1
    f = out["filings"][0]
    assert f["origin"] == "user_upload" and f["passages"] and f["page_count"] == 1
    assert "automated exchange retrieval disabled" in out["discovery"]
    items = filings.cached_passages("TEST", cache_dir=tmp_path)
    assert items and items[0]["evidence_kind"] == "filing_passage"
    assert "uploaded by the user" in items[0]["attribution"]


def test_ingest_merges_and_replaces_same_title(tmp_path):
    filings.ingest_pdf_bytes("TEST", _make_pdf_bytes(), title="Q2 results", cache_dir=tmp_path)
    filings.ingest_pdf_bytes("TEST", _make_pdf_bytes("Different text about revenue and profit. "), title="Q3 results", cache_dir=tmp_path)
    out = filings.ingest_pdf_bytes("TEST", _make_pdf_bytes(), title="Q2 results", cache_dir=tmp_path)
    assert len(out["filings"]) == 2  # Q2 replaced in place, Q3 kept


def test_ingest_rejects_non_pdf_and_empty(tmp_path):
    with pytest.raises(FilingIngestError):
        filings.ingest_pdf_bytes("TEST", b"not a pdf at all", title="x", cache_dir=tmp_path)
    with pytest.raises(FilingIngestError):
        filings.ingest_pdf_bytes("TEST", b"", title="x", cache_dir=tmp_path)


def test_cached_passages_empty_without_cache(tmp_path):
    assert filings.cached_passages("NOPE", cache_dir=tmp_path) == []


def test_tcs_seed_passages_present_and_labelled():
    items = filings.cached_passages("TCS", cache_dir=None)
    seeds = [i for i in items if "manually transcribed seed" in i["attribution"]]
    assert len(seeds) == 1
    s = seeds[0]
    assert s["source_url"].startswith("https://nsearchives.nseindia.com/")
    assert "73,188" in s["text"] and "EBITDA is not stated" in s["text"]
    assert s["evidence_kind"] == "filing_passage" and s["has_passage"] is True


def test_seed_not_contributed_for_other_symbols(tmp_path):
    items = filings.cached_passages("INFY", cache_dir=tmp_path)
    assert items == []
