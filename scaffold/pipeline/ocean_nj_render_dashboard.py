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


def _resolved_owner(lead: dict) -> str:
    """The owner name to DISPLAY. Falls back to the legal party when the parcel
    owner_name is redacted (Daniel's Law): for a foreclosure the DEFENDANT is the
    owner; for a probate the DECEDENT is the estate owner. Trims legal suffixes."""
    n = (lead.get("owner_name") or "").strip()
    if n:
        return n
    cand = (lead.get("defendant_name") or lead.get("decedent_name") or "").strip()
    if not cand:
        return ""
    up = cand.upper()
    for suf in (", ET ALS", ", ET AL", ", ETC", " ET ALS", " ET AL", ", A/K/A", ", AKA"):
        i = up.find(suf)
        if i != -1:
            cand = cand[:i]
            break
    return cand.strip()


def _owner_type(lead: dict) -> str:
    """Owner-type tag from the (now DealMachine-enriched) owner name."""
    name = _resolved_owner(lead).upper().strip()
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


def _ymd(v) -> str:
    """Normalize any date to YYYY-MM-DD. DealMachine returns ISO timestamps
    (2025-09-29T00:00:00.000Z); county dates are already YYYY-MM-DD."""
    if not v:
        return ""
    return str(v)[:10]


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
        "owner_name": _resolved_owner(lead),
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
        "assessed_value": lead.get("net_value") or _dmp(lead, "total_assessed_value"),
        "last_sale_price": lead.get("last_sale_price") or _dmp(lead, "last_sale_price"),
        "last_sale_date": _ymd(lead.get("last_sale_date") or _dmp(lead, "last_sale_date")),
        "year_built": lead.get("year_built") or _dmp(lead, "year_built"),
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
        "probate_docket": lead.get("probate_docket")
            or ((lead.get("lead_id") or "").split(":")[1]
                if lead.get("distress_signal") == "probate_filing_recent" else ""),
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
        # Motivation MULTIPLIER flags (DealMachine owner attributes) — context on an
        # existing lead, never a lead origin. (source: dealmachine)
        "senior_owner_flag": bool(lead.get("senior_owner_flag")),
        "tired_landlord_flag": bool(lead.get("tired_landlord_flag")),
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
<title>Ocean County NJ — Distress Lead Intelligence</title>
<style>
  :root{
    --bg:#0b0e13; --panel:#141922; --panel2:#1b212c; --text:#e6edf3; --muted:#8b949e;
    --accent:#58a6ff; --warn:#d29922; --bad:#f85149; --good:#3fb950; --purple:#a371f7;
    --border:#262d38; --border2:#30384a;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:var(--bg);color:var(--text);font-size:13.5px;line-height:1.45}
  a{color:var(--accent);text-decoration:none}
  /* ---- header ---- */
  header{padding:18px 26px 0;border-bottom:1px solid var(--border);background:#0d1117}
  header h1{margin:0 0 4px;font-size:19px;font-weight:650;letter-spacing:.2px}
  header .summary{color:var(--muted);font-size:12.5px;margin-bottom:14px}
  header .summary b{color:var(--text)}
  /* ---- tabs ---- */
  .tabs{display:flex;gap:4px;flex-wrap:wrap;overflow-x:auto}
  .tab{padding:8px 14px;border:1px solid transparent;border-bottom:none;border-radius:8px 8px 0 0;
       background:transparent;color:var(--muted);cursor:pointer;font-size:13px;white-space:nowrap;
       display:flex;align-items:center;gap:7px}
  .tab:hover{color:var(--text);background:var(--panel)}
  .tab.active{background:var(--panel);color:var(--text);border-color:var(--border);font-weight:600}
  .tab .n{font-size:11px;color:var(--muted);background:var(--panel2);padding:1px 7px;border-radius:10px}
  .tab.active .n{color:var(--accent)}
  .tab.t-probate.active{color:var(--purple)} .tab.t-probate.active .n{color:var(--purple)}
  .tab.t-list.active{color:var(--purple)} .tab.t-list.active .n{color:var(--purple)}
  /* ---- controls ---- */
  main{padding:16px 26px 40px;max-width:1640px;margin:0 auto}
  .controls{display:flex;flex-direction:column;gap:10px;padding-bottom:14px;
            border-bottom:1px solid var(--border);margin-bottom:18px}
  .row-ctrl{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
  .lbl{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin-right:2px}
  .pill,.chip{border:1px solid var(--border2);background:var(--panel);color:var(--text);
        padding:5px 11px;border-radius:999px;font-size:12.5px;cursor:pointer;user-select:none;
        transition:.1s}
  .pill:hover,.chip:hover{background:var(--panel2)}
  .pill.active{background:#0c2f33;border-color:#1f6b73;color:#56d4dd;font-weight:600}
  .chip.active{background:var(--accent);border-color:var(--accent);color:#06121f;font-weight:600}
  input[type=search]{background:var(--panel);border:1px solid var(--border2);color:var(--text);
       padding:7px 12px;border-radius:8px;font-size:13px;min-width:240px}
  .btn{background:transparent;border:1px solid var(--border2);color:var(--muted);padding:6px 12px;
       border-radius:8px;cursor:pointer;font-size:12.5px}
  .btn:hover{color:var(--text);border-color:var(--text)}
  .btn.export{background:var(--good);border-color:var(--good);color:#06210f;font-weight:600}
  .toggle{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12px;cursor:pointer}
  .toggle input{cursor:pointer}
  .count{margin-left:auto;color:var(--muted);font-size:13px}
  .count b{color:var(--text);font-size:15px}
  /* ---- grid + cards ---- */
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:14px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:hidden;
        display:flex;flex-direction:column}
  .card.foreclosure{border-top:3px solid var(--warn)}
  .card.tax{border-top:3px solid var(--accent)}
  .card.probate{border-top:3px solid var(--purple)}
  .card.list{border-top:3px solid var(--purple);background:#15131c}
  .card-head{padding:12px 14px 10px;border-bottom:1px solid var(--border)}
  .card-head .addr{font-weight:650;font-size:14px;line-height:1.3}
  .card-head .addr.empty{color:var(--muted);font-style:italic;font-weight:500}
  .card-head .sub{color:var(--muted);font-size:11.5px;margin-top:2px}
  .hbadges{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
  .badge{font-size:10.5px;padding:2px 8px;border-radius:5px;background:var(--panel2);
         color:var(--muted);border:1px solid var(--border2);text-transform:uppercase;letter-spacing:.03em}
  .badge.type{background:#10243d;color:#79c0ff;border-color:#1f4870}
  .badge.type.tax{background:#0c2f33;color:#56d4dd;border-color:#1f6b73}
  .badge.type.list{background:#241a3d;color:#d2a8ff;border-color:#3d2f66}
  .badge.type.probate{background:#241a3d;color:#d2a8ff;border-color:#3d2f66}
  .badge.good{background:#0f3a1d;color:var(--good);border-color:#1f5e30}
  .badge.warn{background:#3d2f0c;color:var(--warn);border-color:#5a4810}
  .badge.bad{background:#3d1010;color:var(--bad);border-color:#5a1a1a}
  .badge.motiv{background:#0c2f33;color:#56d4dd;border-color:#164b52}
  .sec{padding:10px 14px;border-bottom:1px solid var(--border)}
  .sec:last-child{border-bottom:none}
  .sec-title{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);
             margin-bottom:7px;font-weight:600}
  .owner-name{font-size:14px;font-weight:600;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .owner-name.unres{color:var(--muted);font-weight:500;font-style:italic}
  .otag{font-size:9.5px;text-transform:uppercase;letter-spacing:.04em;padding:1px 6px;border-radius:4px;
        background:var(--panel2);border:1px solid var(--border2);color:var(--muted);font-style:normal}
  .contact{font-size:12.5px;color:var(--text);margin-top:5px;word-break:break-word}
  .contact .ic{color:var(--muted);margin-right:5px}
  .facts{display:grid;grid-template-columns:1fr 1fr;gap:7px 14px}
  .fact{display:flex;flex-direction:column}
  .fact .k{font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
  .fact .v{font-size:13px;color:var(--text);font-weight:500}
  .prov{padding:9px 14px;font-size:10.5px;color:var(--muted);background:var(--panel2)}
  .prov .dm{color:var(--purple)}
  .empty-state{text-align:center;color:var(--muted);padding:70px 0;font-size:14px}
  footer{padding:16px 26px;color:var(--muted);font-size:11.5px;border-top:1px solid var(--border);margin-top:30px}
  #error-banner{display:none;background:var(--bad);color:#fff;padding:10px 26px}
</style>
</head>
<body>
<header>
  <h1>Ocean County, NJ — Distress Lead Intelligence</h1>
  <div class="summary" id="summary">Loading…</div>
  <nav class="tabs" id="tabs"></nav>
</header>
<div id="error-banner">Could not load lead data.</div>
<main>
  <div class="controls">
    <div class="row-ctrl">
      <span class="lbl">Motivation</span>
      <span class="pill" data-motiv="senior_owner_flag">Senior owner</span>
      <span class="pill" data-motiv="tired_landlord_flag">Tired landlord</span>
      <span class="pill" data-motiv="absentee">Absentee</span>
      <span class="pill" data-motiv="out_of_state">Out-of-state</span>
    </div>
    <div class="row-ctrl">
      <span class="lbl">Owner</span>
      <span class="chip" data-filter="owner_type" data-value="Individual">Individual</span>
      <span class="chip" data-filter="owner_type" data-value="Entity">Entity</span>
      <span class="chip" data-filter="owner_type" data-value="Estate">Estate</span>
      <span class="chip" data-filter="owner_type" data-value="Unknown">Unknown</span>
      <span class="lbl" style="margin-left:10px">Recency</span>
      <span class="chip" data-filter="recency" data-value="last_30_days">30 days</span>
      <span class="chip" data-filter="recency" data-value="last_90_days">90 days</span>
      <span class="lbl" style="margin-left:10px">Address</span>
      <span class="chip" data-filter="address_resolved" data-value="true">Resolved</span>
      <span class="chip" data-filter="address_resolved" data-value="false">Unresolved</span>
    </div>
    <div class="row-ctrl">
      <input type="search" id="search" placeholder="Search address, owner, defendant, phone, docket…" />
      <button class="btn" id="reset">Reset</button>
      <button class="btn export" id="export-csv">⬇ Export CSV</button>
      <label class="toggle"><input type="checkbox" id="show-probate" checked />Include probate (<span id="probate-count">0</span>)</label>
      <label class="toggle"><input type="checkbox" id="show-lists" checked />Include commercial lists (<span id="list-count">0</span>)</label>
      <div class="count"><b id="shown">0</b> of <span id="total">0</span> leads</div>
    </div>
  </div>
  <div id="grid" class="grid"></div>
  <div id="empty" class="empty-state" style="display:none">No leads match the current tab + filters.</div>
</main>
<footer>
  Xcerebro County Intelligence — Ocean County, NJ. Sources: Ocean County Sheriff foreclosures,
  Ocean County Surrogate, NJ GIS parcels + MOD-IV; owner/contact skip-trace enrichment.
</footer>
<script>
const TABS = [
  {id:"all",      label:"All",               cls:""},
  {id:"sheriff",  label:"Sheriff Foreclosure", cls:""},
  {id:"tax",      label:"Tax Default",       cls:""},
  {id:"vacant",   label:"Vacant",            cls:"t-list"},
  {id:"expired",  label:"Expired Listing",   cls:"t-list"},
  {id:"hoa",      label:"HOA Lien",          cls:"t-list"},
  {id:"zombie",   label:"Zombie",            cls:"t-list"},
  {id:"probate",  label:"Probate",           cls:"t-probate"},
];

// Ocean County Surrogate (Bluestone) portal. No per-docket deep link exists
// (ASP.NET postback navigation), so the docket links to the searchable portal.
const SURROGATE_URL = "https://surrogateweb.co.ocean.nj.us/BluestoneWeb/default.aspx?FROM_MSG=99";

const STATE = {
  tab:"all",
  filters:{owner_type:null, recency:null, address_resolved:null},
  motiv:{senior_owner_flag:false, tired_landlord_flag:false, absentee:false, out_of_state:false},
  search:"",
  showProbate:true,
  showLists:true,
  rows:[],
};

function inScope(r){
  // which rows the "All" tab includes (toggles add probate/lists)
  if(r.is_probate) return STATE.showProbate;
  if(r.is_dealmachine_list) return STATE.showLists;
  return true; // source-of-record
}
function tabPred(tab){
  switch(tab){
    case "all":     return r=>inScope(r);
    case "sheriff": return r=>r.signal_type==="foreclosure_sale_scheduled";
    case "tax":     return r=>r.signal_type==="tax_default_brick";
    case "vacant":  return r=>r.is_dealmachine_list&&r.dm_list_type==="vacant";
    case "expired": return r=>r.is_dealmachine_list&&r.dm_list_type==="expired_listing";
    case "hoa":     return r=>r.is_dealmachine_list&&r.dm_list_type==="hoa_lien";
    case "zombie":  return r=>r.is_dealmachine_list&&r.dm_list_type==="zombie";
    case "probate": return r=>r.is_probate;
  }
  return ()=>true;
}

function matches(r){
  if(!tabPred(STATE.tab)(r)) return false;
  const f=STATE.filters;
  if(f.owner_type && r.owner_type!==f.owner_type) return false;
  if(f.address_resolved!==null){
    if(f.address_resolved==="true" && !r.address_resolved) return false;
    if(f.address_resolved==="false" && r.address_resolved) return false;
  }
  if(f.recency){
    const c=new Date();
    c.setDate(c.getDate() - (f.recency==="last_30_days"?30:90));
    if(!r.recorded_date) return false;
    if(r.recorded_date < c.toISOString().slice(0,10)) return false;
  }
  for(const k of ["senior_owner_flag","tired_landlord_flag","absentee","out_of_state"])
    if(STATE.motiv[k] && !r[k]) return false;
  if(STATE.search){
    const q=STATE.search.toLowerCase();
    const hay=[r.property_full_address,r.owner_name,r.defendant,r.plaintiff,r.decedent_name,
      r.lead_id,r.muni,r.block,r.lot,(r.owner_phones||[]).join(" "),(r.owner_emails||[]).join(" ")]
      .filter(Boolean).join(" ").toLowerCase();
    if(!hay.includes(q)) return false;
  }
  return true;
}

function esc(s){return (s==null?"":String(s)).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function money(v){
  if(v==null||v==="") return "";
  if(typeof v==="string"){ return v.startsWith("$")?v:("$"+v); }
  return "$"+Number(v).toLocaleString();
}
function fact(k,v){ return (v==null||v===""||v==="—")?"":`<div class="fact"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`; }
function section(title,inner){ return inner.trim()?`<div class="sec"><div class="sec-title">${title}</div>${inner}</div>`:""; }

function renderCard(r){
  let variant="", typeBadge="", typeCls="";
  if(r.is_dealmachine_list){ variant="list"; typeCls="list"; }
  else if(r.is_probate){ variant="probate"; typeCls="probate"; }
  else if(r.signal_type==="foreclosure_sale_scheduled"||r.signal_type==="foreclosure_notice_published"){ variant="foreclosure"; typeCls=""; }
  else if(r.signal_type==="tax_default_brick"){ variant="tax"; typeCls="tax"; }
  typeBadge=`<span class="badge type ${typeCls}">${esc(r.distress_label||r.lead_type||"")}</span>`;

  // header badges: type + status + sheriff status
  let hb=[typeBadge];
  if(!r.is_dealmachine_list){
    if(r.review_status==="APPROVED_FOR_DASHBOARD") hb.push('<span class="badge good">Approved</span>');
    else if(r.review_status&&r.review_status!=="DEALMACHINE_LIST") hb.push('<span class="badge warn">Review</span>');
  }
  if(r.sheriff_status==="ADJOURNED UNTIL"&&r.adjournment_date) hb.push(`<span class="badge warn">Adj ${esc(r.adjournment_date)}</span>`);
  if(r.sheriff_status==="BANKRUPTCY") hb.push('<span class="badge bad">Bankruptcy</span>');
  if(r.sheriff_status==="CANCELLATION") hb.push('<span class="badge bad">Cancelled</span>');
  if(r.out_of_state) hb.push('<span class="badge warn">Out-of-state</span>');
  else if(r.absentee) hb.push('<span class="badge">Absentee</span>');
  if(r.senior_owner_flag) hb.push('<span class="badge motiv">Senior owner</span>');
  if(r.tired_landlord_flag) hb.push('<span class="badge motiv">Tired landlord</span>');

  const addr = r.property_full_address
    ? `<div class="addr">${esc(r.property_full_address)}</div>`
    : `<div class="addr empty">${r.is_probate?"No property address (probate research)":"Address unresolved"}</div>`;

  // OWNER section (skip for probate which has decedent instead)
  let ownerSec="";
  if(!r.is_probate){
    let inner="";
    if(r.owner_name){
      inner+=`<div class="owner-name">${esc(r.owner_name)}<span class="otag">${esc(r.owner_type)}</span></div>`;
    } else {
      inner+=`<div class="owner-name unres">${r.is_dealmachine_list?"Owner — pending skip-trace":"Owner not resolved"}</div>`;
    }
    const ph=(r.owner_phones||[]).slice(0,6);
    const em=(r.owner_emails||[]).slice(0,3);
    if(ph.length) inner+=`<div class="contact"><span class="ic">📞</span>${ph.map(esc).join(" · ")}</div>`;
    if(em.length) inner+=`<div class="contact"><span class="ic">✉</span>${em.map(esc).join(" · ")}</div>`;
    ownerSec=section("Owner", inner);
  }

  // PROPERTY facts (omit empties)
  let pf="";
  pf+=fact("Est. value", money(r.dm_property_value||r.dm_estimated_value));
  pf+=fact("Assessed", money(r.assessed_value));
  if(r.property_type) pf+=fact("Type", r.property_type);
  pf+=fact("Year built", r.year_built);
  if(r.last_sale_price) pf+=fact("Last sale", money(r.last_sale_price)+(r.last_sale_date?` · ${r.last_sale_date}`:""));
  if(r.block||r.lot) pf+=fact("Block / Lot", `${r.block||"?"} / ${r.lot||"?"}`);
  const propSec = pf.trim()?`<div class="sec"><div class="sec-title">Property</div><div class="facts">${pf}</div></div>`:"";

  // FORECLOSURE section (only foreclosure types)
  let fcSec="";
  if(r.signal_type==="foreclosure_sale_scheduled"||r.signal_type==="foreclosure_notice_published"){
    let ff="";
    ff+=fact("Defendant", r.defendant);
    ff+=fact("Plaintiff", r.plaintiff);
    ff+=fact("Upset amount", money(r.upset_amount));
    ff+=fact("Sale date", r.sale_date);
    ff+=fact("Docket", (r.lead_id||"").split(":")[1]?.replace("_"," "));
    ff+=fact("Attorney", r.attorney_firm);
    if(ff.trim()) fcSec=`<div class="sec"><div class="sec-title">Foreclosure</div><div class="facts">${ff}</div></div>`;
  }
  // PROBATE section
  let pbSec="";
  if(r.is_probate){
    let pp="";
    pp+=fact("Decedent", r.decedent_name);
    pp+=fact("Case type", r.probate_case_type);
    pp+=fact("Date of death", r.decedent_dod);
    pp+=fact("Town", r.city);
    pp+=fact("Filed", r.recorded_date);
    const docket=r.probate_docket||(r.lead_id||"").split(":")[1]||"";
    if(docket){
      pp+=`<div class="fact"><span class="k">Docket</span><span class="v"><a href="${SURROGATE_URL}" target="_blank" rel="noopener">${esc(docket)} — review on Surrogate portal ↗</a></span></div>`;
    }
    if(pp.trim()) pbSec=`<div class="sec"><div class="sec-title">Probate filing</div><div class="facts">${pp}</div></div>`;
  }

  // PROVENANCE line
  let prov="";
  if(r.is_dealmachine_list) prov=`Ocean County · ${esc(r.distress_label)}`;
  else if(r.dealmachine_matched) prov=`Ocean County · owner/contacts via skip-trace${r.daniels_law_backfilled?" · owner backfilled (Daniel's Law)":""}`;
  else if(r.dm_enrichment_status==="pending_retry") prov=`Ocean County · skip-trace pending`;
  else if(r.dm_enrichment_status==="no_dm_record") prov=`Ocean County · no skip-trace match`;
  else prov=`Ocean County${r.event_source?` · ${esc(r.event_source)}`:""}`;
  const provLine=`<div class="prov">${prov}</div>`;

  return `<div class="card ${variant}">
    <div class="card-head">${addr}<div class="hbadges">${hb.join("")}</div></div>
    ${ownerSec}${propSec}${fcSec}${pbSec}${provLine}
  </div>`;
}

function renderTabs(){
  const el=document.getElementById("tabs");
  el.innerHTML = TABS.map(t=>{
    const n=STATE.rows.filter(tabPred(t.id)).length;
    return `<button class="tab ${t.cls} ${STATE.tab===t.id?"active":""}" data-tab="${t.id}">${esc(t.label)}<span class="n">${n.toLocaleString()}</span></button>`;
  }).join("");
}

function render(){
  const out=STATE.rows.filter(matches);
  document.getElementById("shown").textContent=out.length.toLocaleString();
  document.getElementById("total").textContent=STATE.rows.filter(tabPred(STATE.tab)).length.toLocaleString();
  const grid=document.getElementById("grid"), empty=document.getElementById("empty");
  if(!out.length){ grid.innerHTML=""; empty.style.display="block"; }
  else { empty.style.display="none"; grid.innerHTML=out.map(renderCard).join(""); }
  // active states
  document.querySelectorAll(".tab").forEach(b=>b.classList.toggle("active", b.dataset.tab===STATE.tab));
  document.querySelectorAll(".chip").forEach(c=>c.classList.toggle("active", STATE.filters[c.dataset.filter]===c.dataset.value));
  document.querySelectorAll(".pill").forEach(p=>p.classList.toggle("active", !!STATE.motiv[p.dataset.motiv]));
}

async function load(){
  try{
    const res=await fetch("dashboard_data.json",{cache:"no-store"});
    if(!res.ok) throw new Error(res.statusText);
    const data=await res.json();
    STATE.rows=(data.records||[]).slice().sort((a,b)=>{
      const ar=a.address_resolved?1:0, br=b.address_resolved?1:0;
      if(ar!==br) return br-ar;
      return (b.recorded_date||"").localeCompare(a.recorded_date||"");
    });
    const sor=STATE.rows.filter(r=>!r.is_probate&&!r.is_dealmachine_list).length;
    const prob=STATE.rows.filter(r=>r.is_probate).length;
    const lists=STATE.rows.filter(r=>r.is_dealmachine_list).length;
    document.getElementById("summary").innerHTML=
      `<b>${(sor+prob+lists).toLocaleString()}</b> Ocean County leads &nbsp;·&nbsp; <b>${sor.toLocaleString()}</b> foreclosure/tax · <b>${prob.toLocaleString()}</b> probate · <b>${lists.toLocaleString()}</b> commercial &nbsp;·&nbsp; built ${esc((data.build_timestamp||"").slice(0,10))}`;
    document.getElementById("probate-count").textContent=prob.toLocaleString();
    document.getElementById("list-count").textContent=lists.toLocaleString();
    renderTabs(); render();
  }catch(err){ document.getElementById("error-banner").style.display="block"; console.error(err); }
}

// ---- events ----
document.getElementById("tabs").addEventListener("click",e=>{
  const t=e.target.closest(".tab"); if(!t) return;
  STATE.tab=t.dataset.tab; render();
});
document.addEventListener("click",e=>{
  const chip=e.target.closest(".chip");
  if(chip){ const f=chip.dataset.filter,v=chip.dataset.value;
    STATE.filters[f]=STATE.filters[f]===v?null:v; render(); return; }
  const pill=e.target.closest(".pill");
  if(pill){ const k=pill.dataset.motiv; STATE.motiv[k]=!STATE.motiv[k]; render(); return; }
});
document.getElementById("reset").addEventListener("click",()=>{
  STATE.filters={owner_type:null,recency:null,address_resolved:null};
  STATE.motiv={senior_owner_flag:false,tired_landlord_flag:false,absentee:false,out_of_state:false};
  STATE.search=""; document.getElementById("search").value="";
  render();
});
document.getElementById("search").addEventListener("input",e=>{ STATE.search=e.target.value.trim(); render(); });
document.getElementById("show-probate").addEventListener("change",e=>{ STATE.showProbate=e.target.checked; renderTabs(); render(); });
document.getElementById("show-lists").addEventListener("change",e=>{ STATE.showLists=e.target.checked; renderTabs(); render(); });

// ---- CSV export (currently-filtered set; exact client column order) ----
const EXPORT_COLUMNS=["lead_type","first_name_or_entity_name","last_name","property_address",
  "property_city","property_state","property_zip","mailing_address","mailing_city","mailing_state",
  "mailing_zip","phone_1","phone_2","phone_3","phone_4","phone_5","phone_6","email"];
function csvCell(v){const s=v==null?"":String(v);return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
function exportCSV(){
  const rows=STATE.rows.filter(matches);
  const lines=[EXPORT_COLUMNS.join(",")];
  for(const r of rows){
    const ph=r.owner_phones||[];
    const e={lead_type:r.lead_type||r.distress_label||"",first_name_or_entity_name:r.export_first_name||"",
      last_name:r.export_last_name||"",property_address:r.exp_prop_address||"",property_city:r.exp_prop_city||"",
      property_state:r.exp_prop_state||"",property_zip:r.exp_prop_zip||"",mailing_address:r.exp_mail_address||"",
      mailing_city:r.exp_mail_city||"",mailing_state:r.exp_mail_state||"",mailing_zip:r.exp_mail_zip||"",
      phone_1:ph[0]||"",phone_2:ph[1]||"",phone_3:ph[2]||"",phone_4:ph[3]||"",phone_5:ph[4]||"",phone_6:ph[5]||"",
      email:(r.owner_emails||[])[0]||""};
    lines.push(EXPORT_COLUMNS.map(c=>csvCell(e[c])).join(","));
  }
  const blob=new Blob([lines.join("\r\n")],{type:"text/csv;charset=utf-8"});
  const url=URL.createObjectURL(blob),a=document.createElement("a");
  a.href=url; a.download=`ocean_nj_${STATE.tab}_${rows.length}.csv`;
  document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
}
document.getElementById("export-csv").addEventListener("click",exportCSV);

load();
</script>
</body>
</html>"""


def attach_skiptrace(leads: list[dict]) -> int:
    """Attach bulk skip-trace contacts (data/enriched/ocean_county_results.json,
    keyed by APN/address) onto every lead lacking contacts, so the dashboard
    shows phones/emails/owner. Mirrors the routing in ocean_county_bulk_enrich."""
    import sys
    results_path = REPO_ROOT / "data" / "enriched" / "ocean_county_results.json"
    if not results_path.exists():
        return 0
    try:
        results = json.loads(results_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    sys.path.insert(0, str(REPO_ROOT))
    from scrapers.dealmachine_enrich import (  # noqa: E402
        build_block, derive_apn, load_parcel_situs, situs_address,
    )
    need = [l for l in leads
            if not ((l.get("dealmachine") or {}).get("phones")
                    or (l.get("dealmachine") or {}).get("emails"))]
    situs = load_parcel_situs({l.get("parcel_id") for l in need if l.get("parcel_id")})
    attached = 0
    for l in need:
        apn = derive_apn(l.get("parcel_id"))
        key = apn or situs_address(l, situs)
        if not key:
            continue
        rec = results.get(key)
        if rec and rec.get("matched") and (rec.get("contacts") or rec.get("phones")):
            block = build_block(rec, "apn" if apn else "address", "", "")
            l["dealmachine"] = block
            if block.get("owner_name") and not l.get("owner_name"):
                l["owner_name"] = block["owner_name"]
                l["owner_name_source"] = "dealmachine"
            attached += 1
    return attached


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
    n_attached = attach_skiptrace(leads)
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
