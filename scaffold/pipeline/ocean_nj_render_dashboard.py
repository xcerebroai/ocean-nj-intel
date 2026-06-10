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
        "foreclosure_notice_published": "Foreclosure notice (NJPA publication)",
        "tax_default_brick": "Tax default — Brick Twp (HLS)",
    }.get(signal, signal or "")


_ENTITY_TOKENS = (" LLC", " L.L.C", " INC", " CORP", " CO.", " COMPANY", " LP",
                  " LLP", " TRUST", " BANK", " ASSOC", " PARTNERS", " HOLDINGS",
                  " PROPERTIES", " REALTY", " FUND", " GROUP", " ENTERPRISES")


_LIST_TYPE_LABELS = {
    "vacant": "Vacant Home", "hoa_lien": "HOA Lien", "zombie": "Zombie Property",
    "expired_listing": "Expired Listing", "senior_owner": "Senior Owner",
    "tired_landlord": "Tired Landlord",
}


def _lead_type(lead: dict) -> str:
    # DealMachine commercial-list leads carry lead_type directly.
    if lead.get("is_dealmachine_list") and lead.get("lead_type"):
        return _LIST_TYPE_LABELS.get(lead["lead_type"], lead["lead_type"])
    return {
        "foreclosure_sale_scheduled": "Sheriff Foreclosure",
        "foreclosure_notice_published": "Foreclosure Notice",
        "tax_default_brick": "Tax Default",
        "probate_filing_recent": "Probate",
    }.get(lead.get("distress_signal") or "", lead.get("distress_signal") or "")


def _owner_first_last(lead: dict, owner_type: str) -> tuple[str, str]:
    """(first_name_or_entity_name, last_name). Entities/estates -> full name in
    first column, last blank. Individuals -> split. Prefer the structured
    DealMachine contact (clean first/last); else parse owner_name; else defendant."""
    # DealMachine owner contact has structured first/last.
    for c in ((lead.get("dealmachine") or {}).get("contacts") or []):
        if c.get("contact_type") == "owner" and (c.get("first_name") or c.get("last_name")):
            if owner_type in ("Entity", "Estate"):
                return (c.get("full_name") or "").strip(), ""
            return (c.get("first_name") or "").strip(), (c.get("last_name") or "").strip()
    name = (lead.get("owner_name") or lead.get("defendant_name") or "").strip()
    if not name:
        return "", ""
    if owner_type in ("Entity", "Estate"):
        return name, ""
    # "LAST, FIRST ..." (county/defendant format)
    if "," in name:
        last, first = name.split(",", 1)
        return first.strip(), last.strip()
    # "FIRST [MIDDLE] LAST"
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def _owner_type(lead: dict) -> str:
    """Owner-type tag from the (now DealMachine-enriched) owner name."""
    name = (lead.get("owner_name") or "").upper().strip()
    if not name:
        return "Unknown"
    if "ESTATE" in name or name.endswith(" EST"):
        return "Estate"
    if any(tok in f" {name} " for tok in _ENTITY_TOKENS):
        return "Entity"
    return "Individual"


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
        "distress_label": (_LIST_TYPE_LABELS.get(lead.get("lead_type"), lead.get("lead_type"))
                           if lead.get("is_dealmachine_list")
                           else _signal_to_distress(lead.get("distress_signal") or "")),
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
        # DealMachine commercial enrichment (COMMERCIAL_ENRICHMENT_SOURCE, Ocean-scoped)
        "owner_name_source": lead.get("owner_name_source")
            or ("county" if lead.get("owner_name") else ""),
        "daniels_law_backfilled": bool(lead.get("daniels_law_backfilled")),
        # enriched | no_dm_record (DM has no record) | pending_retry (API flapped) | ""
        "dm_enrichment_status": lead.get("dm_enrichment_status") or "",
        "dealmachine_matched": bool((lead.get("dealmachine") or {}).get("matched")),
        "owner_phones": (lead.get("dealmachine") or {}).get("phones") or [],
        "owner_emails": (lead.get("dealmachine") or {}).get("emails") or [],
        "dm_estimated_value": ((lead.get("dealmachine") or {}).get("property") or {}).get("estimated_value"),
        # Owner residency (absentee / out-of-state filters)
        "owner_occupied": lead.get("owner_occupied"),
        "owner_mailing_state": lead.get("owner_mailing_state") or "",
        "absentee": bool(lead.get("absentee")),
        "out_of_state": bool(lead.get("out_of_state")),
        # Probate research targets are hidden by default behind a toggle
        "is_probate": lead.get("distress_signal") == "probate_filing_recent",
        # DealMachine commercial list-pull leads (Ocean-only, client-specific) —
        # kept structurally distinct from county source-of-record distress leads.
        "is_dealmachine_list": bool(lead.get("is_dealmachine_list")),
        "dm_list_type": lead.get("lead_type") or "" if lead.get("is_dealmachine_list") else "",
        "dm_property_value": _dmp(lead, "estimated_value"),
        "property_type": _dmp(lead, "property_type") or "",
        # ── CSV export fields (exact client column spec) ──
        "lead_type": _lead_type(lead),
        "export_first_name": _export_name(lead)[0],
        "export_last_name": _export_name(lead)[1],
        # property address components (prefer DealMachine-normalized, else county)
        "exp_prop_address": _dmp(lead, "address") or (lead.get("property_address") or ""),
        "exp_prop_city": _dmp(lead, "city") or (lead.get("property_city") or ""),
        "exp_prop_state": _dmp(lead, "state") or (lead.get("property_state") or ""),
        "exp_prop_zip": _dmp(lead, "zip") or (lead.get("property_zip") or ""),
        # owner mailing address (county MOD-IV)
        "exp_mail_address": lead.get("owner_mailing_address") or "",
        "exp_mail_city": lead.get("owner_mailing_city") or "",
        "exp_mail_state": lead.get("owner_mailing_state") or "",
        "exp_mail_zip": lead.get("owner_mailing_zip") or "",
    }


def _dmp(lead: dict, key: str):
    return ((lead.get("dealmachine") or {}).get("property") or {}).get(key)


def _export_name(lead: dict) -> tuple[str, str]:
    return _owner_first_last(lead, _owner_type(lead))


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
  button.export{background:var(--good);border:1px solid var(--good);color:#06210f;
                padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px;
                font-weight:600}
  button.export:hover{filter:brightness(1.1)}
  .probate-toggle{display:flex;align-items:center;gap:6px;color:var(--muted);
                  font-size:12.5px;cursor:pointer;margin-left:6px}
  .probate-toggle input{cursor:pointer}
  .counts{margin-left:auto;color:var(--muted);font-size:13px}
  .counts strong{color:var(--text)}
  .card .owner{font-size:13.5px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .card .owner .name{font-weight:600;color:var(--text)}
  .card .owner .otag{font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;
                     padding:1px 6px;border-radius:4px;background:#1f2630;
                     border:1px solid var(--border);color:var(--muted)}
  .card .contacts{font-size:12px;color:var(--muted);display:flex;flex-direction:column;gap:2px}
  .card .contacts .c{color:var(--text)}
  .card .src{font-size:10.5px;color:var(--muted)}
  .card .src .dm{color:#d2a8ff}
  .card .recency{font-size:11.5px;color:var(--muted)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:14px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:8px;
        padding:14px 16px;display:flex;flex-direction:column;gap:6px}
  .card.review-required{border-left:3px solid var(--warn)}
  .card.approved{border-left:3px solid var(--good)}
  /* DealMachine commercial-list leads — visually distinct from source-of-record */
  .card.dm-list{border-left:3px solid #8957e5;background:#17141f}
  .card .signal.dmlist{color:#d2a8ff}
  .card .badge.dmlist{background:#241a3d;color:#d2a8ff;border-color:#3d2f66}
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
      <span class="chip" data-filter="distress_type" data-value="foreclosure_notice_published">Foreclosure notice</span>
      <span class="chip" data-filter="distress_type" data-value="tax_default_brick">Tax default (Brick)</span>
      <span class="chip" data-filter="distress_type" data-value="probate_filing_recent">Probate filing</span>
    </div>
    <div class="filter-group">
      <span class="label">Owner type</span>
      <span class="chip" data-filter="owner_type" data-value="Individual">Individual</span>
      <span class="chip" data-filter="owner_type" data-value="Entity">Entity (LLC)</span>
      <span class="chip" data-filter="owner_type" data-value="Estate">Estate</span>
      <span class="chip" data-filter="owner_type" data-value="Unknown">Unknown</span>
    </div>
    <div class="filter-group">
      <span class="label">Recency</span>
      <span class="chip" data-filter="recency" data-value="last_30_days">Last 30 days</span>
      <span class="chip" data-filter="recency" data-value="last_90_days">Last 90 days</span>
    </div>
    <div class="filter-group">
      <span class="label">Owner residency</span>
      <span class="chip" data-filter="residency" data-value="absentee">Absentee</span>
      <span class="chip" data-filter="residency" data-value="out_of_state">Out-of-state</span>
    </div>
    <div class="filter-group">
      <span class="label">Address</span>
      <span class="chip" data-filter="address_resolved" data-value="true">Resolved</span>
      <span class="chip" data-filter="address_resolved" data-value="false">Unresolved</span>
    </div>
    <div class="filter-group" id="list-type-group" style="display:none">
      <span class="label">DM list type</span>
      <span class="chip" data-filter="dm_list_type" data-value="vacant">Vacant</span>
      <span class="chip" data-filter="dm_list_type" data-value="hoa_lien">HOA lien</span>
      <span class="chip" data-filter="dm_list_type" data-value="zombie">Zombie</span>
      <span class="chip" data-filter="dm_list_type" data-value="expired_listing">Expired listing</span>
    </div>
    <input type="search" id="search" placeholder="Search address, owner, defendant, decedent, docket…" />
    <button class="reset" data-filter="reset">Reset</button>
    <button class="export" id="export-csv">⬇ Export CSV</button>
    <label class="probate-toggle"><input type="checkbox" id="show-probate" />
      Show probate research targets (<span id="probate-count">0</span>)</label>
    <label class="probate-toggle"><input type="checkbox" id="show-lists" />
      Show DealMachine lists (<span id="list-count">0</span>)</label>
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
             residency:null, address_resolved:null, dm_list_type:null },
  search: "",
  showProbate: false,   // probate research targets hidden by default (§5)
  showLists: false,     // DealMachine commercial lists hidden by default (distinct)
  rows: [],
};

async function load() {
  try {
    const res = await fetch('dashboard_data.json', {cache:'no-store'});
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();
    document.getElementById('build-ts').textContent = data.build_timestamp || '—';
    STATE.rows = (data.records || data.rows || []).slice().sort((a,b)=>{
      // §5.3: address-resolved first, then recorded_date desc (neutral sort)
      const aR = a.address_resolved ? 1 : 0;
      const bR = b.address_resolved ? 1 : 0;
      if (aR !== bR) return bR - aR;
      return (b.recorded_date || '').localeCompare(a.recorded_date || '');
    });
    const probateN = STATE.rows.filter(r => r.is_probate).length;
    document.getElementById('probate-count').textContent = probateN.toLocaleString();
    const listN = STATE.rows.filter(r => r.is_dealmachine_list).length;
    document.getElementById('list-count').textContent = listN.toLocaleString();
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
  // Probate research targets hidden unless toggled (§5); DealMachine commercial
  // lists hidden unless toggled (kept distinct from source-of-record).
  if (row.is_probate && !STATE.showProbate) return false;
  if (row.is_dealmachine_list && !STATE.showLists) return false;
  if (f.dm_list_type && row.dm_list_type !== f.dm_list_type) return false;
  if (f.distress_type && row.signal_type !== f.distress_type) return false;
  if (f.owner_type && row.owner_type !== f.owner_type) return false;
  if (f.address_resolved !== null) {
    if (f.address_resolved === 'true' && !row.address_resolved) return false;
    if (f.address_resolved === 'false' && row.address_resolved) return false;
  }
  if (f.residency) {
    if (f.residency === 'absentee' && !row.absentee) return false;
    if (f.residency === 'out_of_state' && !row.out_of_state) return false;
  }
  if (f.recency) {
    const cutoff = new Date();
    if (f.recency === 'last_30_days') cutoff.setDate(cutoff.getDate() - 30);
    else if (f.recency === 'last_90_days') cutoff.setDate(cutoff.getDate() - 90);
    if (!row.recorded_date) return false;
    if (row.recorded_date < cutoff.toISOString().slice(0,10)) return false;
  }
  if (STATE.search) {
    const q = STATE.search.toLowerCase();
    const hay = [row.property_full_address, row.owner_name, row.defendant,
                 row.decedent_name, row.plaintiff, row.lead_id, row.muni,
                 row.block, row.lot, (row.owner_phones||[]).join(' '),
                 (row.owner_emails||[]).join(' ')].filter(Boolean).join(' ').toLowerCase();
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
  const isList = row.is_dealmachine_list;
  const cls = isList ? 'dm-list'
    : (row.review_status === 'APPROVED_FOR_DASHBOARD' ? 'approved' : 'review-required');
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
  if (row.out_of_state) badges.push('<span class="badge warn">Out-of-state owner</span>');
  else if (row.absentee) badges.push('<span class="badge">Absentee owner</span>');
  if (isList) badges.unshift('<span class="badge dmlist">DealMachine list</span>');
  const sig = isList ? 'dmlist' : (isProbate ? 'probate' : '');

  // Owner line (now DealMachine-enriched) + owner-type tag.
  const ownerName = row.owner_name
    ? `<span class="name">${escape(row.owner_name)}</span>`
    : `<span class="name" style="color:var(--muted);font-style:italic">Owner not resolved</span>`;
  const ownerBlock = isProbate ? '' : `<div class="owner">${ownerName}
    <span class="otag">${escape(row.owner_type)}</span></div>`;

  // DealMachine contacts + provenance stamp (only on enriched leads).
  let contactsBlock = '';
  if (isList) {
    contactsBlock = '<div class="src">source: <span class="dm">dealmachine</span> (commercial list — contacts gated; enrich to pull owner phones/emails)</div>';
  } else if (row.dealmachine_matched && ((row.owner_phones||[]).length || (row.owner_emails||[]).length)) {
    const ph = (row.owner_phones||[]).slice(0,3).map(p =>
      `<span class="c">${escape(p)}</span>`).join(' · ');
    const em = (row.owner_emails||[]).slice(0,2).map(e =>
      `<span class="c">${escape(e)}</span>`).join(' · ');
    contactsBlock = `<div class="contacts">
      ${ph ? `<div>📞 ${ph}</div>` : ''}
      ${em ? `<div>✉ ${em}</div>` : ''}
    </div><div class="src">source: <span class="dm">dealmachine</span>${
      row.daniels_law_backfilled ? ' · owner backfilled (Daniel\'s Law)' : ''}</div>`;
  } else if (row.dealmachine_matched) {
    // Property matched but DealMachine returned no owner contact (skip-trace miss).
    contactsBlock = '<div class="src">DealMachine: property matched — no owner contact on file</div>';
  } else if (row.dm_enrichment_status === 'pending_retry') {
    contactsBlock = '<div class="src">DealMachine: pending retry (API unavailable)</div>';
  } else if (row.dm_enrichment_status === 'no_dm_record') {
    contactsBlock = '<div class="src">DealMachine: no record</div>';
  }
  const recency = row.recorded_date
    ? `<div class="recency">Recorded ${escape(row.recorded_date)}</div>` : '';

  let body;
  if (isList) {
    body = `
      <div class="row"><span>List type</span><span class="v">${escape(row.distress_label)}</span></div>
      <div class="row"><span>Est. value</span><span class="v">${escape(row.dm_property_value || '—')}</span></div>
      <div class="row"><span>Property type</span><span class="v">${escape(row.property_type || '—')}</span></div>
      <div class="row"><span>Year built</span><span class="v">${escape(row.year_built || '—')}</span></div>
      <div class="row"><span>Town</span><span class="v">${escape(row.city)}</span></div>
      <div class="row"><span>DM property</span><span class="v">${escape(row.lead_id.split(':')[2] || '')}</span></div>`;
  } else if (isProbate) {
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
    ${ownerBlock}
    <div>${badges.join('')}</div>
    ${contactsBlock}
    ${body}
    ${recency}
  </div>`;
}

function render() {
  const filtered = STATE.rows.filter(matches);
  // Total reflects the currently-visible universe: source-of-record by default;
  // probate and DealMachine lists are each excluded unless their toggle is on.
  const universe = STATE.rows.filter(r =>
    (STATE.showProbate || !r.is_probate) && (STATE.showLists || !r.is_dealmachine_list)).length;
  document.getElementById('shown').textContent = filtered.length;
  document.getElementById('total').textContent = universe;
  document.getElementById('list-type-group').style.display = STATE.showLists ? '' : 'none';
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
  const t = e.target.closest('.chip, .reset');
  if (!t) return;
  if (t.dataset.filter === 'reset') {
    Object.keys(STATE.filters).forEach(k => STATE.filters[k] = null);
    STATE.search = '';
    STATE.showProbate = false;
    STATE.showLists = false;
    document.getElementById('search').value = '';
    document.getElementById('show-probate').checked = false;
    document.getElementById('show-lists').checked = false;
  } else {
    toggleChip(t.dataset.filter, t.dataset.value);
  }
  render();
});

document.getElementById('search').addEventListener('input', (e) => {
  STATE.search = e.target.value.trim();
  render();
});

document.getElementById('show-probate').addEventListener('change', (e) => {
  STATE.showProbate = e.target.checked;
  render();
});

document.getElementById('show-lists').addEventListener('change', (e) => {
  STATE.showLists = e.target.checked;
  render();
});

// ── CSV export — exact client column order; exports the CURRENTLY-FILTERED set
// (respects active filters AND the probate toggle). ──
const EXPORT_COLUMNS = [
  "lead_type", "first_name_or_entity_name", "last_name",
  "property_address", "property_city", "property_state", "property_zip",
  "mailing_address", "mailing_city", "mailing_state", "mailing_zip",
  "phone_1", "phone_2", "phone_3", "phone_4", "phone_5", "phone_6", "email",
];

function csvCell(v) {
  const s = (v === null || v === undefined) ? "" : String(v);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function rowToExport(r) {
  const ph = r.owner_phones || [];
  return {
    lead_type: r.lead_type || "",
    first_name_or_entity_name: r.export_first_name || "",
    last_name: r.export_last_name || "",
    property_address: r.exp_prop_address || "",
    property_city: r.exp_prop_city || "",
    property_state: r.exp_prop_state || "",
    property_zip: r.exp_prop_zip || "",
    mailing_address: r.exp_mail_address || "",
    mailing_city: r.exp_mail_city || "",
    mailing_state: r.exp_mail_state || "",
    mailing_zip: r.exp_mail_zip || "",
    phone_1: ph[0] || "", phone_2: ph[1] || "", phone_3: ph[2] || "",
    phone_4: ph[3] || "", phone_5: ph[4] || "", phone_6: ph[5] || "",
    email: (r.owner_emails || [])[0] || "",
  };
}

function exportCSV() {
  const rows = STATE.rows.filter(matches);
  const lines = [EXPORT_COLUMNS.join(",")];
  for (const r of rows) {
    const e = rowToExport(r);
    lines.push(EXPORT_COLUMNS.map(c => csvCell(e[c])).join(","));
  }
  const blob = new Blob([lines.join("\r\n")], {type: "text/csv;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const stamp = new Date().toISOString().slice(0,10);
  a.href = url;
  a.download = `ocean_nj_leads_${stamp}_${rows.length}.csv`;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

document.getElementById('export-csv').addEventListener('click', exportCSV);

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
    leads = list(src.get("leads", []))
    # Merge Ocean-only DealMachine commercial list-pull leads (client-specific),
    # kept in a separate file so county build_leads never wipes them.
    listpull = REPO_ROOT / "data" / "enriched" / "dealmachine_listpull.json"
    n_list = 0
    if listpull.exists():
        lp = json.loads(listpull.read_text(encoding="utf-8"))
        leads += lp.get("leads", [])
        n_list = len(lp.get("leads", []))
    rows = [_to_row(l) for l in leads]
    dashboard_data = {
        "build_timestamp": src["build_timestamp"],
        "framework_version": src["framework_version"],
        "row_count": len(rows),
        "dealmachine_list_count": n_list,
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
