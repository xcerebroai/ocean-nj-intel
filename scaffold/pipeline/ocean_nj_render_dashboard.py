"""Ocean County, NJ — dashboard renderer (v5.5.0 §5).

Reads data/leads/scored_leads.json (produced by ocean_nj_build_leads.py)
and writes:

    dashboard/index.html
    dashboard/dashboard_data.json     (REQUIRED_DASHBOARD_FIELDS-compliant)
    dashboard/build_manifest.json

Contract compliance (validated by scaffold/pipeline/dashboard_contract.py):
- REQUIRED_DASHBOARD_FIELDS — every row carries all 10 fields.
- STANDARD_FILTERS — all 9 canonical filters wired into the UI.
- DEFAULT_FILTER_STATE — every filter starts neutral (None / "").
- BANNER_PROHIBITED_TOKENS — no PARTIAL_BUILD / SOURCE_LIMITED / etc on
  the client surface.
- §5.3 address-resolved-first sort.
- §5.6 default-hidden lead types: hidden but toggle-able.
- §5.7 banner DOM node exists only for "could not load data" errors.
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
SCORED = REPO_ROOT / "data" / "leads" / "scored_leads.json"
DASH = REPO_ROOT / "dashboard"
OUT_DATA = DASH / "dashboard_data.json"
OUT_HTML = DASH / "index.html"
OUT_MANIFEST = DASH / "build_manifest.json"


def _signal_to_distress(signal: str) -> str:
    return {
        "foreclosure_sale_scheduled": "Sheriff foreclosure sale",
        "probate_filing_recent": "Probate filing (estate-titled owner research)",
    }.get(signal, signal or "")


def _owner_type(lead: dict) -> str:
    # Daniel's-Law-redacted parcels — we know nothing about owner residency
    # tier, so "Unknown" is the honest call.
    if lead.get("owner_resolved"):
        return "Resolved"
    return "Unknown"


def _enrichment_status(lead: dict) -> str:
    if lead.get("parcel_id"):
        return "ENRICHED"
    return "UNENRICHED"


def _full_address(lead: dict) -> str:
    a = (lead.get("property_address") or "").strip()
    if not a:
        return ""
    parts = [a, lead.get("property_city") or "", lead.get("property_state") or "",
             lead.get("property_zip") or ""]
    return ", ".join(p for p in parts if p)


def _to_row(lead: dict) -> dict:
    return {
        # REQUIRED_DASHBOARD_FIELDS (10)
        "lead_id": lead["lead_id"],
        "owner_name": lead.get("owner_name") or "",
        "owner_type": _owner_type(lead),
        "signal_type": lead.get("distress_signal") or "",
        "property_full_address": _full_address(lead),
        "recorded_date": lead.get("primary_event_date") or "",
        "review_status": lead.get("lead_status") or "REVIEW_REQUIRED",
        "lead_origin_type": lead.get("lead_origin_type") or "",
        "enrichment_status": _enrichment_status(lead),
        "event_source": (lead.get("source_ids") or [""])[0],
        # OPTIONAL_DASHBOARD_FIELDS
        "address_resolved": bool((_full_address(lead) or "").strip()),
        "primary_parcel_id": lead.get("parcel_id") or "",
        "assessed_value": lead.get("net_value"),
        "last_sale_price": lead.get("last_sale_price"),
        "last_sale_date": lead.get("last_sale_date") or "",
        "year_built": lead.get("year_built"),
        "qualification_status": lead.get("qualification_status") or "",
        # Display-only extras
        "distress_label": _signal_to_distress(lead.get("distress_signal") or ""),
        "city": lead.get("property_city") or "",
        "plaintiff": lead.get("plaintiff") or "",
        "defendant": lead.get("defendant_name") or "",
        "upset_amount": lead.get("upset_amount"),
        "sale_date": lead.get("sale_date") or "",
        "adjournment_date": lead.get("adjournment_date") or "",
        "sheriff_status": lead.get("sheriff_status") or "",
        "decedent_name": lead.get("decedent_name") or "",
        "decedent_dod": lead.get("decedent_date_of_death") or "",
        "probate_case_type": lead.get("probate_case_type") or "",
        "block": lead.get("block") or "",
        "lot": lead.get("lot") or "",
        "muni": lead.get("parcel_muni") or lead.get("property_city") or "",
        "attorney_firm": lead.get("attorney_firm") or "",
        "owner_resolution_status": lead.get("owner_resolution_status") or "",
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Ocean County NJ — Distress Lead Dashboard</title>
<style>
  :root {
    --bg:#0e1116; --panel:#161b22; --text:#e6edf3; --muted:#8b949e;
    --accent:#58a6ff; --warn:#d29922; --bad:#f85149; --good:#3fb950;
    --border:#30363d;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       background:var(--bg);color:var(--text);font-size:14px;line-height:1.4}
  header{padding:18px 24px;border-bottom:1px solid var(--border);
         display:flex;align-items:baseline;gap:18px;flex-wrap:wrap}
  header h1{margin:0;font-size:20px;font-weight:600}
  header .meta{color:var(--muted);font-size:13px}
  #error-banner{display:none;background:var(--bad);color:#fff;padding:10px 24px}
  main{padding:20px 24px;max-width:1600px;margin:0 auto}
  .filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;
           padding-bottom:16px;border-bottom:1px solid var(--border);margin-bottom:18px}
  .filter-group{display:flex;flex-wrap:wrap;gap:6px;align-items:center;
                margin-right:14px}
  .filter-group .label{color:var(--muted);font-size:12px;margin-right:4px;
                       text-transform:uppercase;letter-spacing:0.05em}
  .chip{background:var(--panel);border:1px solid var(--border);color:var(--text);
        padding:5px 11px;border-radius:999px;font-size:12.5px;cursor:pointer;
        user-select:none;transition:background 0.1s}
  .chip:hover{background:#1f2630}
  .chip.active{background:var(--accent);color:#0a0d12;border-color:var(--accent);
               font-weight:600}
  input[type="search"]{background:var(--panel);border:1px solid var(--border);
                       color:var(--text);padding:6px 12px;border-radius:6px;
                       font-size:13px;min-width:260px}
  button.reset{background:transparent;border:1px solid var(--border);color:var(--muted);
               padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px}
  button.reset:hover{color:var(--text);border-color:var(--text)}
  .counts{margin-left:auto;color:var(--muted);font-size:13px}
  .counts strong{color:var(--text)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:14px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:8px;
        padding:14px 16px;display:flex;flex-direction:column;gap:6px}
  .card.review-required{border-left:3px solid var(--warn)}
  .card.approved{border-left:3px solid var(--good)}
  .card .signal{font-weight:600;color:var(--accent);font-size:13px}
  .card .signal.probate{color:#d2a8ff}
  .card .address{font-weight:600;font-size:14.5px}
  .card .empty-addr{color:var(--muted);font-style:italic}
  .card .row{display:flex;justify-content:space-between;gap:10px;font-size:12.5px;
             color:var(--muted)}
  .card .row .v{color:var(--text)}
  .card .badge{display:inline-block;padding:2px 8px;border-radius:4px;
               font-size:11px;background:#1f2630;color:var(--muted);
               border:1px solid var(--border);margin-right:4px}
  .card .badge.warn{background:#3d2f0c;color:var(--warn);border-color:#5a4810}
  .card .badge.good{background:#0f3a1d;color:var(--good);border-color:#1f5e30}
  .card .badge.bad{background:#3d1010;color:var(--bad);border-color:#5a1a1a}
  .empty-state{text-align:center;color:var(--muted);padding:60px 0;
               font-size:14px}
  footer{padding:18px 24px;color:var(--muted);font-size:12px;
         border-top:1px solid var(--border);margin-top:30px}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
</style>
</head>
<body>
<header>
  <h1>Ocean County, NJ — Distress Lead Dashboard</h1>
  <div class="meta">Last build: <span id="build-ts">—</span></div>
</header>
<div id="error-banner">Could not load lead data.</div>
<main>
  <div class="filters">
    <div class="filter-group">
      <span class="label">Distress</span>
      <span class="chip" data-filter="distress_type" data-value="foreclosure_sale_scheduled">Sheriff foreclosure</span>
      <span class="chip" data-filter="distress_type" data-value="probate_filing_recent">Probate filing</span>
    </div>
    <div class="filter-group">
      <span class="label">Owner</span>
      <span class="chip" data-filter="owner_type" data-value="Resolved">Resolved</span>
      <span class="chip" data-filter="owner_type" data-value="Unknown">Unknown</span>
    </div>
    <div class="filter-group">
      <span class="label">Recency</span>
      <span class="chip" data-filter="recency" data-value="last_30_days">Last 30 days</span>
      <span class="chip" data-filter="recency" data-value="last_90_days">Last 90 days</span>
    </div>
    <div class="filter-group">
      <span class="label">Years delinquent</span>
      <span class="chip" data-filter="years_delinquent" data-value="1">1</span>
      <span class="chip" data-filter="years_delinquent" data-value="2">2</span>
      <span class="chip" data-filter="years_delinquent" data-value="3">3</span>
      <span class="chip" data-filter="years_delinquent" data-value="4">4</span>
      <span class="chip" data-filter="years_delinquent" data-value="5+">5+</span>
    </div>
    <div class="filter-group">
      <span class="label">Owner residency</span>
      <span class="chip" data-filter="absentee_or_out_of_state" data-value="absentee">Absentee</span>
      <span class="chip" data-filter="absentee_or_out_of_state" data-value="out_of_state">Out-of-state</span>
    </div>
    <div class="filter-group">
      <span class="label">Address</span>
      <span class="chip" data-filter="address_resolved" data-value="true">Resolved</span>
      <span class="chip" data-filter="address_resolved" data-value="false">Unresolved</span>
    </div>
    <div class="filter-group">
      <span class="label">Review</span>
      <span class="chip" data-filter="review_status" data-value="APPROVED_FOR_DASHBOARD">Approved</span>
      <span class="chip" data-filter="review_status" data-value="REVIEW_REQUIRED">Needs review</span>
    </div>
    <input type="search" id="search" placeholder="Search address, defendant, decedent, docket…" />
    <button class="reset" data-filter="reset">Reset</button>
    <div class="counts"><strong id="shown">0</strong> of <span id="total">0</span> leads</div>
  </div>
  <div id="grid" class="grid"></div>
  <div id="empty" class="empty-state" style="display:none">No leads match the current filters.</div>
</main>
<footer>
  Built by the Xcerebro County Intelligence Harness (framework v5.5.0).
  Data sources: Ocean County Sheriff foreclosure listings, Ocean County
  Surrogate (Bluestone), NJ Office of GIS parcels + MOD-IV.
</footer>
<script>
const STATE = {
  filters: { distress_type:null, owner_type:null, recency:null,
             years_delinquent:null, absentee_or_out_of_state:null,
             address_resolved:null, review_status:null },
  search: "",
  rows: [],
};

async function load() {
  try {
    const res = await fetch('dashboard_data.json', {cache:'no-store'});
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();
    document.getElementById('build-ts').textContent = data.build_timestamp || '—';
    STATE.rows = (data.records || data.rows || []).slice().sort((a,b)=>{
      // §5.3: address-resolved first, then recorded_date desc
      const aR = a.address_resolved ? 1 : 0;
      const bR = b.address_resolved ? 1 : 0;
      if (aR !== bR) return bR - aR;
      return (b.recorded_date || '').localeCompare(a.recorded_date || '');
    });
    document.getElementById('total').textContent = STATE.rows.length;
    render();
  } catch (err) {
    document.getElementById('error-banner').style.display = 'block';
    console.error(err);
  }
}

function toggleChip(filter, value) {
  STATE.filters[filter] = STATE.filters[filter] === value ? null : value;
}

function matches(row) {
  const f = STATE.filters;
  if (f.distress_type && row.signal_type !== f.distress_type) return false;
  if (f.owner_type && row.owner_type !== f.owner_type) return false;
  if (f.review_status && row.review_status !== f.review_status) return false;
  if (f.address_resolved !== null) {
    if (f.address_resolved === 'true' && !row.address_resolved) return false;
    if (f.address_resolved === 'false' && row.address_resolved) return false;
  }
  if (f.recency) {
    const cutoff = new Date();
    if (f.recency === 'last_30_days') cutoff.setDate(cutoff.getDate() - 30);
    else if (f.recency === 'last_90_days') cutoff.setDate(cutoff.getDate() - 90);
    if (!row.recorded_date) return false;
    if (row.recorded_date < cutoff.toISOString().slice(0,10)) return false;
  }
  // years_delinquent + absentee_or_out_of_state: no source data today → these
  // filters match nothing, which is the honest behavior (operator unlock S8).
  if (f.years_delinquent) return false;
  if (f.absentee_or_out_of_state) return false;
  if (STATE.search) {
    const q = STATE.search.toLowerCase();
    const hay = [row.property_full_address, row.owner_name, row.defendant,
                 row.decedent_name, row.plaintiff, row.lead_id, row.muni,
                 row.block, row.lot].filter(Boolean).join(' ').toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

function fmtMoney(n) {
  if (n === null || n === undefined || n === '') return '—';
  return '$' + Number(n).toLocaleString();
}

function escape(s) {
  return (s || '').toString().replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderCard(row) {
  const isProbate = row.signal_type === 'probate_filing_recent';
  const cls = row.review_status === 'APPROVED_FOR_DASHBOARD' ? 'approved' : 'review-required';
  const addr = row.property_full_address
    ? `<div class="address">${escape(row.property_full_address)}</div>`
    : `<div class="address empty-addr">No street address (probate / unjoined)</div>`;
  const badges = [];
  if (row.review_status === 'APPROVED_FOR_DASHBOARD')
    badges.push('<span class="badge good">APPROVED</span>');
  else badges.push('<span class="badge warn">REVIEW</span>');
  if (row.enrichment_status === 'ENRICHED')
    badges.push('<span class="badge">Parcel joined</span>');
  if (row.sheriff_status === 'ADJOURNED UNTIL')
    badges.push(`<span class="badge warn">Adjourned ${escape(row.adjournment_date || '')}</span>`);
  if (row.sheriff_status === 'BANKRUPTCY')
    badges.push('<span class="badge bad">Bankruptcy</span>');
  if (row.sheriff_status === 'CANCELLATION')
    badges.push('<span class="badge bad">Cancelled</span>');
  const sig = isProbate ? 'probate' : '';
  let body;
  if (isProbate) {
    body = `
      <div class="row"><span>Decedent</span><span class="v">${escape(row.decedent_name)}</span></div>
      <div class="row"><span>Case type</span><span class="v">${escape(row.probate_case_type)}</span></div>
      <div class="row"><span>Date of death</span><span class="v">${escape(row.decedent_dod || '—')}</span></div>
      <div class="row"><span>Town</span><span class="v">${escape(row.city)}</span></div>
      <div class="row"><span>Filed</span><span class="v">${escape(row.recorded_date)}</span></div>
      <div class="row"><span>Docket</span><span class="v">${escape(row.lead_id.split(':')[1])}</span></div>`;
  } else {
    body = `
      <div class="row"><span>Defendant</span><span class="v">${escape(row.defendant)}</span></div>
      <div class="row"><span>Plaintiff</span><span class="v">${escape(row.plaintiff)}</span></div>
      <div class="row"><span>Upset amount</span><span class="v">${fmtMoney(row.upset_amount)}</span></div>
      <div class="row"><span>Sale date</span><span class="v">${escape(row.sale_date)}</span></div>
      ${row.adjournment_date ? `<div class="row"><span>Adjourned to</span><span class="v">${escape(row.adjournment_date)}</span></div>` : ''}
      <div class="row"><span>Assessed value</span><span class="v">${fmtMoney(row.assessed_value)}</span></div>
      <div class="row"><span>Year built</span><span class="v">${escape(row.year_built || '—')}</span></div>
      <div class="row"><span>Last sale</span><span class="v">${fmtMoney(row.last_sale_price)} ${row.last_sale_date ? '(' + escape(row.last_sale_date) + ')' : ''}</span></div>
      <div class="row"><span>Block / Lot</span><span class="v">${escape(row.block)} / ${escape(row.lot)}</span></div>
      <div class="row"><span>Docket</span><span class="v">${escape(row.lead_id.split(':')[1].replace('_', ' '))}</span></div>
      <div class="row"><span>Attorney</span><span class="v">${escape(row.attorney_firm)}</span></div>`;
  }
  return `<div class="card ${cls}">
    <div class="signal ${sig}">${escape(row.distress_label)}</div>
    ${addr}
    <div>${badges.join('')}</div>
    ${body}
  </div>`;
}

function render() {
  const filtered = STATE.rows.filter(matches);
  document.getElementById('shown').textContent = filtered.length;
  const grid = document.getElementById('grid');
  const empty = document.getElementById('empty');
  if (!filtered.length) { grid.innerHTML = ''; empty.style.display = 'block'; return; }
  empty.style.display = 'none';
  grid.innerHTML = filtered.map(renderCard).join('');
  // Refresh chip-active states
  document.querySelectorAll('.chip').forEach(el => {
    const f = el.dataset.filter, v = el.dataset.value;
    if (f === 'reset') return;
    el.classList.toggle('active', STATE.filters[f] === v);
  });
}

document.addEventListener('click', (e) => {
  const t = e.target.closest('.chip');
  if (!t) return;
  if (t.dataset.filter === 'reset') {
    Object.keys(STATE.filters).forEach(k => STATE.filters[k] = null);
    STATE.search = '';
    document.getElementById('search').value = '';
  } else {
    toggleChip(t.dataset.filter, t.dataset.value);
  }
  render();
});

document.getElementById('search').addEventListener('input', (e) => {
  STATE.search = e.target.value.trim();
  render();
});

load();
</script>
</body>
</html>
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--in", dest="inp", default=str(SCORED))
    args = p.parse_args()

    DASH.mkdir(parents=True, exist_ok=True)
    src = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    rows = [_to_row(l) for l in src.get("leads", [])]
    dashboard_data = {
        "build_timestamp": src["build_timestamp"],
        "framework_version": src["framework_version"],
        "row_count": len(rows),
        "records": rows,
    }
    OUT_DATA.write_text(json.dumps(dashboard_data, indent=2) + "\n",
                        encoding="utf-8")
    OUT_HTML.write_text(HTML_TEMPLATE, encoding="utf-8")
    OUT_MANIFEST.write_text(json.dumps({
        "rendered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "row_count": len(rows),
        "src": str(args.inp),
    }, indent=2) + "\n", encoding="utf-8")
    print(f"[render_dashboard] rows={len(rows)} -> {OUT_DATA} + {OUT_HTML}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
