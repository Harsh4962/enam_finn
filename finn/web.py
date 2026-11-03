"""Render the replay digest as a mobile-first HTML page: python -m finn.web out.html"""
from __future__ import annotations

import html
import sys

from .ledger import growth_pct, promised_vs_delivered
from .models import Metric
from .real_seed import SOURCE_URL as TCS_SOURCE
from .seed import DEMO_LABEL, classify_fixture, demo_announcements, demo_filings, demo_guidance
from .store import AnnouncementStore

MATERIAL = {"results", "M&A", "fund_raise", "management_change", "litigation", "credit_rating", "pledging"}

CSS = """
:root{color-scheme:light}*{box-sizing:border-box;margin:0}
body{font:16px/1.5 -apple-system,system-ui,sans-serif;background:#f6f4f0;color:#1c1a17;padding:16px;max-width:430px;margin:0 auto}
.banner{background:#fde8e8;color:#8f1d1d;border-radius:10px;padding:10px 14px;font-size:13px;font-weight:600;margin-bottom:16px}
h1{font-family:Georgia,serif;font-size:26px;font-weight:600;margin:4px 0 2px}
.sub{color:#6b655c;font-size:13px;margin-bottom:18px}
h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#6b655c;margin:22px 0 8px}
.card{background:#fff;border:1px solid #e7e2d9;border-radius:12px;padding:12px 14px;margin-bottom:8px}
.card .cat{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;background:#eef3ee;color:#2f5d3a;border-radius:6px;padding:2px 8px;margin-bottom:6px}
.card.correction{border-color:#d9a13b;background:#fdf8ee}
.card.correction .cat{background:#f6e8cf;color:#8a6116}
.card .co{font-weight:600}
.card .txt{font-size:14px;color:#3a362f;margin-top:2px}
.card .meta{font-size:12px;color:#8a6116;margin-top:6px}
.collapsed{font-size:13px;color:#6b655c;padding:4px 2px}
.ledger .row{display:flex;justify-content:space-between;align-items:baseline;padding:10px 2px;border-bottom:1px solid #eee8dd}
.ledger .row:last-child{border-bottom:0}
.pill{font-size:11px;font-weight:700;border-radius:20px;padding:3px 10px;white-space:nowrap}
.delivered{background:#e2f0e4;color:#2f5d3a}.missed{background:#fbe3e3;color:#8f1d1d}
.qualitative,.no_stated_guidance,.not_comparable{background:#eee9e0;color:#6b655c}
.num{font-size:12px;color:#6b655c}
.quote{font-size:13px;color:#5c564c;font-style:italic;margin:6px 0 12px}
.growth{font-family:Georgia,serif;font-size:34px;margin:6px 0}
"""

STATUS_LABEL = {"delivered": "Delivered", "missed": "Missed", "qualitative": "Qualitative",
                "no_stated_guidance": "No guidance found", "not_comparable": "Not comparable"}


METRIC_LABEL = {
    Metric.REVENUE_FROM_OPERATIONS: "Revenue from operations",
    Metric.TOTAL_REVENUE: "Total revenue (incl. other income)",
    Metric.EBITDA: "EBITDA", Metric.PBT: "PBT", Metric.PAT: "PAT",
}
GROUNDING_LABEL = {"verified": "page verified", "page_mismatch": "page mismatch",
                   "label_unconfirmed": "figure on page, metric label not confirmed",
                   "unverified": "unverified"}


def render_filing_rows(filing, grounding: dict | None = None) -> str:
    """One labeled row per stated figure; revenue definitions are never merged."""
    rows = []
    for m in Metric:
        v = filing.metric_value(m)
        if v is None:
            continue
        bits = [f"p.{v.page}" if v.page is not None else "page not established"]
        if grounding and m.value in grounding:
            bits.append(GROUNDING_LABEL.get(grounding[m.value], "unverified"))
        rows.append(f'<div class="row"><div><b>{METRIC_LABEL[m]}</b>'
                    f'<div class="num">{" · ".join(bits)}</div></div>'
                    f'<div>Rs {v.in_crore():,.2f} cr</div></div>')
    return "".join(rows)


def render_ledger_rows(entries) -> tuple[str, list[str]]:
    """Ledger rows plus verbatim quotes. A grouped no-guidance entry (one per
    company) renders as a single card row, not one row per metric."""
    rows, quotes = [], []
    for e in entries:
        label = STATUS_LABEL[e["status"]]
        if e["metric"] == "all":
            rows.append(f'<div class="row"><div><b>All metrics</b><div class="num">{html.escape(e["note"])}</div></div>'
                        f'<span class="pill {e["status"]}">{label}</span></div>')
            continue
        name = METRIC_LABEL[Metric(e["metric"])] if e["metric"] in Metric._value2member_map_ else e["metric"].capitalize()
        num = f'<div class="num">target {e.get("target_display", str(e["target_pct"]) + "%")} · actual {e["actual_pct"]}%</div>' if "actual_pct" in e else ""
        if e["status"] in ("no_stated_guidance", "not_comparable") and e.get("note"):
            num += f'<div class="num">{html.escape(e["note"])}</div>'
        rows.append(f'<div class="row"><div><b>{html.escape(name)}</b>{num}</div><span class="pill {e["status"]}">{label}</span></div>')
        if e.get("quote"):
            quotes.append(f'<div class="quote">"{html.escape(e["quote"])}" - concall p.{e.get("page","?")}</div>')
    return "".join(rows), quotes


def build() -> str:
    store = AnnouncementStore()
    corrections, cards, collapsed = [], [], 0
    for ann in demo_announcements():
        outcome = store.ingest(ann)
        if outcome.kind == "duplicate":
            continue
        ann.category = classify_fixture(ann.headline)
        if outcome.kind == "correction":
            if outcome.supersedes:
                cards = [c for c in cards if f"data-id=\"{outcome.supersedes}\"" not in c]
            meta = f'<div class="meta">Correction{(" - supersedes " + outcome.supersedes) if outcome.supersedes else ""}; updated filing replaces the earlier push.</div>'
            corrections.append(f'<div class="card correction" data-id="{ann.announcement_id}"><span class="cat">{ann.category} · correction</span>'
                         f'<div class="co">{html.escape(ann.company)}</div><div class="txt">{html.escape(ann.headline)}</div>{meta}</div>')
        elif ann.category in MATERIAL:
            cards.append(f'<div class="card" data-id="{ann.announcement_id}"><span class="cat">{ann.category}</span>'
                         f'<div class="co">{html.escape(ann.company)}</div><div class="txt">{html.escape(ann.headline)}</div></div>')
        else:
            collapsed += 1

    filings = demo_filings()
    entries = promised_vs_delivered("Demotech Industries", demo_guidance(), filings)
    qoq = growth_pct(filings[-1], filings[-2], Metric.REVENUE_FROM_OPERATIONS)
    rows, quotes = render_ledger_rows(entries)
    filing_rows = render_filing_rows(filings[-1])

    tcs_card = (f'<div class="card"><span class="cat">results · real filing</span>'
                f'<div class="co">Tata Consultancy Services</div>'
                f'<div class="txt">Q2 FY27 results press release (filed 8 Oct 2026). Revenue from operations Rs 73,188 cr, PAT Rs 13,884 cr; EBITDA not stated in the release.</div>'
                f'<div class="meta" style="color:#2f5d3a">Seeded by hand from the NSE announcement archive: <a href="{TCS_SOURCE}">source PDF</a></div></div>')
    controls = ('<h2>Alert thresholds</h2><div class="card">'
      '<label class="num">Price move +/-<input id="pt" type="number" value="5" style="width:48px">% &nbsp; Volume x<input id="vm" type="number" value="2" step="0.5" style="width:44px"></label>'
      '<div class="num" id="alertOut" style="margin-top:8px"></div></div>'
      '<script>const closes=[100,101,99,102,107.5],vols=[10,11,9,10,31];'
      'function check(){var pt=parseFloat(document.getElementById("pt").value),vm=parseFloat(document.getElementById("vm").value),'
      'move=(closes[4]-closes[3])/closes[3]*100,avg=(vols[0]+vols[1]+vols[2]+vols[3])/4,'
      'out="No alert at current thresholds (demo series).";'
      'if(Math.abs(move)>=pt){out="PRICE ALERT: "+move.toFixed(1)+"% move crosses +/-"+pt+"% (demo series)."}'
      'else if(vols[4]>=vm*avg){out="VOLUME ALERT: "+(vols[4]/avg).toFixed(1)+"x average crosses "+vm+"x (demo series)."}'
      'document.getElementById("alertOut").textContent=out}'
      'document.getElementById("pt").oninput=check;document.getElementById("vm").oninput=check;check();</script>')
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>FINN</title><style>{CSS}</style></head><body>
<div class="banner">FICTIONAL DEMO DATA (Demotech, Meridian Demo Foods are invented) plus one real manually-seeded filing (TCS); not live exchange alerts</div>
<h1>Today</h1><div class="sub">Digest · last 24 hours · 3 companies</div>
<h2>Priority</h2>{tcs_card + ''.join(corrections + cards)}<div class="collapsed">{collapsed} routine item(s) collapsed</div>
{controls}
<h2>Demotech Industries (FICTIONAL demo) · revenue from operations QoQ</h2><div class="growth">{qoq:+.1f}%</div>
<h2>Demotech Industries (FICTIONAL demo) · latest results</h2><div class="card ledger">{filing_rows}</div>
<h2>Promised vs delivered · Demotech Industries (FICTIONAL demo)</h2><div class="card ledger">{rows}</div>
{''.join(quotes)}
</body></html>"""


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/downloads/finn-digest.html"
    open(out, "w").write(build())
    print(out)
