"""Filing passage cache: citable passages from documents the user is allowed
to use - manually seeded filings and user-uploaded PDFs.

Terms-of-use constraint (verified 2026-11-02): NSE and BSE website terms
prohibit systematic/automated retrieval (scraping, extraction, harvesting)
without express written consent, which we do not have. So this module NEVER
fetches filing PDFs from exchange archives or any other host. Permitted
inputs only:
- the repo's manually transcribed seed filing (real_seed: TCS Q2 FY27 press
  release, downloaded once by hand, figures typed in with page refs), and
- PDFs the user uploads themselves through the app.

Downstox announcement metadata (subjects, categories, dates, PDF deep links)
remains usable elsewhere in the app: we link out to source PDFs, we do not
retrieve them. Extracted PDF text is untrusted data, never instructions.
"""
from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .extract import extract_pages

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "finn" / "filings"

# Passage selection prefers financially dense pages.
_FIN_HINTS = ("revenue", "profit", "ebitda", "margin", "crore", "\u20b9",
              "income", "expense", "quarter", "eps")

PASSAGES_PER_FILING = 6
MAX_PASSAGE_CHARS = 1600
MAX_UPLOAD_BYTES = 25_000_000


class FilingIngestError(RuntimeError):
    pass


def _chunk_pages(pages, limit: int) -> list[dict]:
    """Pick the most financially dense page chunks, capped per filing."""
    out = []
    for page_no, text in pages:
        text = re.sub(r"\s+", " ", (text or "")).strip()
        if len(text) < 80:
            continue
        score = sum(text.lower().count(h) for h in _FIN_HINTS)
        out.append({"page": page_no, "text": text[:MAX_PASSAGE_CHARS], "score": score})
    out.sort(key=lambda p: (-p["score"], p["page"]))
    picked = out[:limit]
    picked.sort(key=lambda p: p["page"])
    return [{"page": p["page"], "text": p["text"]} for p in picked]


def cache_path(symbol: str, cache_dir: Optional[Path] = None) -> Path:
    d = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    return d / f"{symbol.upper()}.json"


def load_filing_cache(symbol: str, cache_dir: Optional[Path] = None) -> Optional[dict]:
    p = cache_path(symbol, cache_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def ingest_pdf_bytes(symbol: str, data: bytes, *, title: str,
                     cache_dir: Optional[Path] = None,
                     source_note: Optional[str] = None) -> dict:
    """Ingest a PDF the user supplied (upload or manual seed file).

    Validates magic bytes and size, extracts page-aware text, merges the
    filing into the symbol's cache (replacing an earlier entry with the same
    title), and returns the cache. Never touches the network.
    """
    sym = symbol.upper()
    if not data:
        raise FilingIngestError("empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise FilingIngestError(f"file exceeds {MAX_UPLOAD_BYTES} bytes")
    if not data.startswith(b"%PDF-"):
        raise FilingIngestError("not a PDF (magic-bytes check failed)")
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "upload.pdf"
        dest.write_bytes(data)
        try:
            pages = extract_pages(dest)
        except Exception as exc:
            raise FilingIngestError(f"could not read PDF: {exc}") from exc
    passages = _chunk_pages(pages, PASSAGES_PER_FILING)
    if not passages:
        raise FilingIngestError("no extractable text (scanned or image-only PDF)")

    filing = {
        "subject": title[:200],
        "category": None,
        "exchange": None,
        "published_at": None,
        "pdf_url": None,
        "origin": "user_upload",
        "source_note": (source_note or "")[:200] or None,
        "page_count": len(pages),
        "passages": passages,
    }
    existing = load_filing_cache(sym, cache_dir) or {"filings": [], "errors": []}
    filings = [f for f in existing.get("filings", []) if f.get("subject") != filing["subject"]]
    filings.append(filing)
    out = {
        "symbol": sym,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "discovery": "user-supplied documents only (automated exchange retrieval disabled per NSE/BSE terms)",
        "degraded_sources": [],
        "category_filter": None,
        "filings": filings,
        "errors": existing.get("errors", []),
        "cache_hit": False,
    }
    p = cache_path(sym, cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out))
    tmp.replace(p)
    return out


def _seed_passages(symbol: str) -> list[dict]:
    """Passages from the repo's manually transcribed seed filing (TCS).

    real_seed documents one hand-downloaded, hand-transcribed TCS Q2 FY27
    press release with page refs. We surface its figures as an honestly
    labelled passage; the source URL is a deep link for the user, never
    fetched by us."""
    if symbol.upper() != "TCS":
        return []
    try:
        from . import real_seed
        fs = real_seed.tcs_filings()
    except Exception:
        return []
    if not fs:
        return []
    periods = ", ".join(f.period_end.isoformat() for f in fs)
    rev = " / ".join("{:,}".format(int(f.revenue_from_operations.amount)) for f in fs)
    pbt = " / ".join("{:,}".format(int(f.pbt.amount)) for f in fs)
    pat = " / ".join("{:,}".format(int(f.pat.amount)) for f in fs)
    text = (f"TCS consolidated Ind-AS figures (press release page 3), periods ended {periods}: "
            f"revenue from operations Rs {rev} crore; profit before tax Rs {pbt} crore; "
            f"profit after tax Rs {pat} crore. EBITDA is not stated in the press release. "
            f"Stated revenue growth: +{real_seed.STATED_QOQ_PCT}% quarter on quarter, +11.2% year on year. "
            "Figures manually transcribed from the filed press release.")
    return [{
        "evidence_kind": "filing_passage",
        "has_passage": True,
        "attribution": ("TCS quarterly results press release, Q2 FY27 (periods ended " + periods + "), "
                        "manually transcribed seed from the NSE-filed PDF, page 3"),
        "source_url": real_seed.SOURCE_URL,
        "text": text,
    }]


def cached_passages(symbol: str, cache_dir: Optional[Path] = None,
                    max_passages: int = 8) -> list[dict]:
    """Evidence items for risk_grounding: passages from permitted documents.

    Shape matches risk_grounding._item('filing_passage', True, ...). Combines
    the manual seed (when the symbol matches) with user-uploaded filings.
    Provenance in the attribution says exactly what each passage is. Never
    fetches anything.
    """
    items = _seed_passages(symbol)
    data = load_filing_cache(symbol, cache_dir)
    if data:
        built = (data.get("built_at") or "")[:10] or "unknown date"
        for f in data.get("filings", []):
            origin = "uploaded by the user" if f.get("origin") == "user_upload" else "manually seeded"
            note = f.get("source_note") or "source not stated"
            for ps in f.get("passages", []):
                items.append({
                    "evidence_kind": "filing_passage",
                    "has_passage": True,
                    "attribution": (f"{data['symbol']} filing \"{(f.get('subject') or '')[:80]}\" "
                                    f"({origin}, {note}, page {ps['page']}, extracted {built})"),
                    "source_url": f.get("pdf_url"),
                    "text": ps["text"],
                })
                if len(items) >= max_passages:
                    return items
    return items[:max_passages]
