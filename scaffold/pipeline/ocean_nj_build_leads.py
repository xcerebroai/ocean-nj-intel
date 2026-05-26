"""Ocean County, NJ — build leads from raw sources + enrichment.

This is the county-side build_leads driver that the framework's staged
pipeline calls. For v5.5.0 it implements:

- §3.7 enrichment join (sheriff/foreclosure address → MOD-IV parcel).
- §3.5 owner-status pathway (surrogate probate → estate_titled_owner
  candidates) — but with the §4 PARTIAL-BUILD caveat: under Daniel's
  Law the parcel index has no owner_name, so estate-titled leads are
  emitted as REVIEW_REQUIRED rather than STACKED_LEAD until the S8
  owner-probe (or S3 land-records unlock) is satisfied.
- §3.9 scheduled-event classification for the sheriff feed
  (UPCOMING_SALE vs PAST_SALE).
- §4.17 evidence-first row contract — every lead carries its
  source_ids + evidence_ids.

Output
------
data/leads/scored_leads.json — the §5 dashboard input.
data/leads/build_manifest.json — counts + verification metadata.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "data"

SHERIFF_RAW = DATA / "raw" / "sheriff_foreclosure.jsonl"
SURROGATE_RAW = DATA / "raw" / "surrogate_probate.jsonl"
NJPA_RAW = DATA / "raw" / "njpa_legal_notices.jsonl"
HLS_BRICK_RAW = DATA / "raw" / "hls_brick_taxsale.jsonl"
CIVILVIEW_RAW = DATA / "raw" / "civilview_sheriff_sales.jsonl"
PARCEL_IDX = DATA / "enriched" / "parcel_index.jsonl"
TAX_BOARD_DETAIL = DATA / "enriched" / "ocean_tax_board_detail.jsonl"
BLOCKED = DATA / "raw" / "_blocked_sources.jsonl"

OUT_LEADS = DATA / "leads" / "scored_leads.json"
OUT_MANIFEST = DATA / "leads" / "build_manifest.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _iter_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    for line in path.open("r", encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


# ---------------------------------------------------------------------------
# Parcel index — keyed by (muni_token, block, lot)
# ---------------------------------------------------------------------------


_MUNI_ALIASES = {
    # Sheriff PDF city tokens → MOD-IV MUN_NAME tokens
    "MANCHESTER": "MANCHESTER TWP",
    "BARNEGAT": "BARNEGAT TWP",
    "BRICK": "BRICK TWP",
    "TOMS RIVER": "TOMS RIVER TWP",
    "BAYVILLE": "BERKELEY TWP",         # Bayville is the main hamlet of Berkeley Twp
    "JACKSON": "JACKSON TWP",
    "LAKEWOOD": "LAKEWOOD TWP",
    "LITTLE EGG HARBOR": "LITTLE EGG HARBOR TWP",
    "LONG BEACH": "LONG BEACH TWP",
    "OCEAN TOWNSHIP": "OCEAN TWP",
    "PLUMSTED": "PLUMSTED TWP",
    "STAFFORD": "STAFFORD TWP",
    "BERKELEY": "BERKELEY TWP",
    "LACEY": "LACEY TWP",
    "EAGLESWOOD": "EAGLESWOOD TWP",
    "FORKED RIVER": "LACEY TWP",        # Forked River is in Lacey Twp
    "MANAHAWKIN": "STAFFORD TWP",
    "BEACH HAVEN WEST": "STAFFORD TWP",
    "WHITING": "MANCHESTER TWP",
    "WARETOWN": "OCEAN TWP",
    "TUCKERTON": "TUCKERTON BORO",
    "SEASIDE HEIGHTS": "SEASIDE HEIGHTS",
    "SEASIDE PARK": "SEASIDE PARK",
    "POINT PLEASANT": "POINT PLEASANT BORO",
    "POINT PLEASANT BEACH": "POINT PLEASANT BEACH",
    "PINE BEACH": "PINE BEACH BORO",
    "OCEAN GATE": "OCEAN GATE BORO",
    "MANTOLOKING": "MANTOLOKING BORO",
    "LAVALLETTE": "LAVALLETTE",
    "LAKEHURST": "LAKEHURST BORO",
    "ISLAND HEIGHTS": "ISLAND HEIGHTS",
    "HARVEY CEDARS": "HARVEY CEDARS BORO",
    "BEACHWOOD": "BEACHWOOD BORO",
    "BAY HEAD": "BAY HEAD BORO",
    "BARNEGAT LIGHT": "BARNEGAT LIGHT",
    "BEACH HAVEN": "BEACH HAVEN BORO",
    "SHIP BOTTOM": "SHIP BOTTOM BORO",
    "SURF CITY": "SURF CITY BORO",
    "SOUTH TOMS RIVER": "SOUTH TOMS RIVER",
}


def _norm_muni(city: str) -> Optional[str]:
    """Map a sheriff-PDF city token to a MOD-IV MUN_NAME."""
    if not city:
        return None
    key = city.strip().upper()
    if key in _MUNI_ALIASES:
        return _MUNI_ALIASES[key]
    # Try with "TWP" appended
    if (key + " TWP") in {v.upper() for v in _MUNI_ALIASES.values()}:
        return key + " TWP"
    return key


def _norm_block_lot(b: str) -> str:
    return (b or "").strip().lstrip("0")


def _load_parcel_index() -> dict[tuple, dict]:
    """Return {(muni_norm, block_norm, lot_norm): parcel_dict}."""
    idx: dict[tuple, dict] = {}
    for rec in _iter_jsonl(PARCEL_IDX):
        p = rec["raw_payload"]
        key = (
            (p.get("muni") or "").strip().upper(),
            _norm_block_lot(p.get("block") or ""),
            _norm_block_lot(p.get("lot") or ""),
        )
        # Prefer first-seen; collisions are rare for (muni, block, lot)
        idx.setdefault(key, p)
    return idx


# ---------------------------------------------------------------------------
# Lead building
# ---------------------------------------------------------------------------


def _scheduled_event_class(payload: dict) -> str:
    """v5.5.0 §3.9 lite — classify sheriff entries.

    UPCOMING_SALE if primary_event_date is today/future; PAST_SALE if past;
    HISTORICAL_CONTEXT_ONLY for cancellations.
    """
    status = (payload.get("status") or "").upper()
    if "CANCELLATION" in status:
        return "HISTORICAL_CONTEXT_ONLY"
    pe = payload.get("primary_event_date")
    if not pe:
        return "UPCOMING_SALE"
    try:
        pe_d = datetime.strptime(pe, "%Y-%m-%d").date()
    except ValueError:
        return "UPCOMING_SALE"
    return "UPCOMING_SALE" if pe_d >= _today() else "PAST_SALE"


def build_sheriff_leads(parcel_idx: dict[tuple, dict]) -> list[dict]:
    leads: list[dict] = []
    join_hits = 0
    join_misses = 0
    for rec in _iter_jsonl(SHERIFF_RAW):
        p = rec["raw_payload"]
        muni_norm = _norm_muni(p.get("city") or "")
        block_norm = _norm_block_lot(p.get("block") or "")
        # Sheriff lots can be "21, 22-25" — try the first numeric token
        first_lot = (p.get("lot") or "").split(",")[0].split("-")[0].strip()
        lot_norm = _norm_block_lot(first_lot)
        parcel = parcel_idx.get((muni_norm or "", block_norm, lot_norm))
        cls = _scheduled_event_class(p)

        if parcel is None:
            # Try lookup without the muni alias guess: scan all munis
            parcel = next(
                (v for k, v in parcel_idx.items()
                 if k[1] == block_norm and k[2] == lot_norm
                 and ((p.get("city") or "").upper() in k[0]
                      or k[0].split()[0] == (p.get("city") or "").upper().split()[0])),
                None,
            )

        if parcel is not None:
            join_hits += 1
        else:
            join_misses += 1

        lead = {
            "lead_id": f"sheriff_foreclosure:{p['chancery_docket'].replace(' ', '_')}",
            "lead_origin_type": "RECORDED_EVENT",
            "primary_event_date": p.get("primary_event_date"),
            "scheduled_event_category": cls,
            "qualification_status": (
                "REVIEW_REQUIRED" if p.get("status") in {"BANKRUPTCY", "ADJOURNED UNTIL"}
                else "QUALIFIED"
            ),
            "source_ids": ["sheriff_foreclosure"] + (
                ["njogis_parcels_modiv"] if parcel else []),
            "evidence_ids": [rec["raw_record_id"]] + (
                [f"njogis_parcels_modiv:{parcel['parcel_id']}"] if parcel else []),
            "lead_status": (
                "APPROVED_FOR_DASHBOARD"
                if cls == "UPCOMING_SALE" and p.get("status") not in {"BANKRUPTCY", "CANCELLATION"}
                else "REVIEW_REQUIRED"
            ),
            "doc_type": "sheriff_sale_listing",
            "distress_signal": "foreclosure_sale_scheduled",
            # ── Event fields (sheriff) ──
            "chancery_docket": p.get("chancery_docket"),
            "plaintiff": p.get("plaintiff"),
            "defendant_name": p.get("defendant"),
            "upset_amount": p.get("upset_amount"),
            "sale_date": p.get("sale_date"),
            "adjournment_date": p.get("adjournment_date"),
            "sheriff_status": p.get("status"),
            "attorney_firm": p.get("attorney_firm"),
            "attorney_phone": p.get("attorney_phone"),
            # ── Property fields (sheriff + parcel) ──
            "property_address": p.get("address"),
            "property_city": p.get("city"),
            "property_state": "NJ",
            "property_zip": p.get("zip"),
            "block": p.get("block"),
            "lot": p.get("lot"),
            # ── Owner fields ──
            "owner_name": None,                         # Daniel's-Law-redacted; see resolution_status
            "owner_resolved": False,
            "owner_resolution_status": (
                "DANIELS_LAW_REDACTED"
                if parcel is not None
                else "PARCEL_NOT_JOINED"
            ),
            # ── Parcel enrichment ──
            "parcel_id": parcel.get("parcel_id") if parcel else None,
            "parcel_muni": parcel.get("muni") if parcel else None,
            "parcel_address": parcel.get("prop_loc") if parcel else None,
            "land_value": parcel.get("land_val") if parcel else None,
            "improvement_value": parcel.get("imprvt_val") if parcel else None,
            "net_value": parcel.get("net_value") if parcel else None,
            "year_built": parcel.get("yr_constr") if parcel else None,
            "acreage": parcel.get("calc_acre") if parcel else None,
            "dwelling_units": parcel.get("dwell") if parcel else None,
            "last_sale_price": parcel.get("sale_price") if parcel else None,
            "last_sale_date": parcel.get("deed_date") if parcel else None,
            "prop_class": parcel.get("prop_class") if parcel else None,
        }
        leads.append(lead)
    return leads, join_hits, join_misses


def build_surrogate_leads() -> list[dict]:
    """Surrogate probate filings → OWNER_STATUS leads, REVIEW_REQUIRED.

    No address per decedent in the source. §3.5 owner_status_classifier
    cannot run without (decedent_name → parcel owner_name) join, which
    Daniel's Law blocks. Emit as research targets with the
    `enrichment_join_unavailable` flag so the §6.4 publish gate accepts
    them.
    """
    leads: list[dict] = []
    for rec in _iter_jsonl(SURROGATE_RAW):
        p = rec["raw_payload"]
        filed = p.get("filed_date")
        # Skip implausible future-dated filings (data-entry errors in source).
        try:
            if filed and datetime.strptime(filed, "%Y-%m-%d").date() > _today():
                continue
        except ValueError:
            pass
        leads.append({
            "lead_id": f"surrogate_probate:{p['docket']}",
            "lead_origin_type": "RECORDED_EVENT",
            "additional_lead_origin_types": ["OWNER_STATUS"],
            "primary_event_date": filed,
            "qualification_status": "REVIEW_REQUIRED",
            "source_ids": ["surrogate_probate"],
            "evidence_ids": [rec["raw_record_id"]],
            "lead_status": "REVIEW_REQUIRED",
            "doc_type": "probate_filing",
            "distress_signal": "probate_filing_recent",
            # ── Event fields ──
            "probate_docket": p.get("docket"),
            "probate_case_type": p.get("case_type"),
            "filed_date": filed,
            # ── Decedent ──
            "decedent_name": p.get("decedent_name"),
            "decedent_date_of_birth": p.get("date_of_birth"),
            "decedent_date_of_death": p.get("date_of_death"),
            # ── Property ──
            "property_city": p.get("town"),
            "property_state": "NJ",
            # ── Owner (Daniel's Law redacted from S9; surrogate has no parcel pointer) ──
            "owner_name": None,
            "owner_resolved": False,
            "owner_resolution_status": "DANIELS_LAW_REDACTED_AND_NO_PROBATE_ADDRESS",
            "enrichment_join_unavailable": True,
        })
    return leads


def build_njpa_leads(parcel_idx: dict[tuple, dict]) -> list[dict]:
    """S6 — NJPA legal-notice records.  Each notice carries (publication
    date, notice text, county filter). Foreclosure-notice rows give us
    the statutory-publication signal that confirms (and pre-dates by
    ~10–14 days) the sheriff-PDF schedule entry for the same docket.

    Cross-link to S1 by chancery_docket when present in the notice text.
    """
    leads: list[dict] = []
    for rec in _iter_jsonl(NJPA_RAW):
        p = rec["raw_payload"]
        docket = p.get("chancery_docket")
        leads.append({
            "lead_id": f"njpa_legal_notices:{p.get('notice_id') or p.get('chancery_docket') or p.get('publication_date')}",
            "lead_origin_type": "RECORDED_EVENT",
            "primary_event_date": p.get("sale_date") or p.get("publication_date"),
            "qualification_status": "REVIEW_REQUIRED",
            "source_ids": ["njpa_legal_notices"],
            "evidence_ids": [rec["raw_record_id"]],
            "lead_status": "REVIEW_REQUIRED",
            "doc_type": p.get("doc_type") or "sheriff_sale_notice",
            "distress_signal": "foreclosure_notice_published",
            "chancery_docket": docket,
            "publication_date": p.get("publication_date"),
            "sale_date": p.get("sale_date"),
            "notice_type": p.get("notice_type"),
            "publication_source": p.get("publication_source"),
            "property_address": p.get("property_address"),
            "property_city": p.get("property_city"),
            "property_state": "NJ",
            "owner_name": None,
            "owner_resolved": False,
            "owner_resolution_status": "NOTICE_TEXT_ONLY",
            "enrichment_join_unavailable": True,
        })
    return leads


def build_hls_brick_leads(parcel_idx: dict[tuple, dict],
                          tax_board_idx: dict[tuple, dict]) -> tuple[list[dict], dict]:
    """S7c — Brick Township tax-default leads via HLS Systems.

    Each row is already §3.3-qualified by the adapter. Apply the §6.4
    actionable contract: QUALIFIED rows with `tax_certificate` or
    `tax_default` lead types are APPROVED_FOR_DASHBOARD; everything
    else is REVIEW_REQUIRED.
    """
    leads: list[dict] = []
    qual_counts: dict = {}
    type_counts: dict = {}
    parcel_hits = 0
    for rec in _iter_jsonl(HLS_BRICK_RAW):
        p = rec["raw_payload"]
        qual_counts[p["qualification_status"]] = qual_counts.get(p["qualification_status"], 0) + 1
        type_counts[p["lead_type"]] = type_counts.get(p["lead_type"], 0) + 1
        block_norm = _norm_block_lot(p.get("block") or "")
        lot_norm = _norm_block_lot(p.get("lot") or "")
        parcel = parcel_idx.get(("BRICK TWP", block_norm, lot_norm))
        if parcel:
            parcel_hits += 1
        approved = (p["qualification_status"] == "QUALIFIED" and
                    p["lead_type"] in ("tax_certificate", "tax_default"))
        leads.append({
            "lead_id": f"hls_brick_taxsale:{p['account_number']}",
            "lead_origin_type": "TAX_DEFAULT",
            "primary_event_date": p.get("sale_date"),
            "qualification_status": p["qualification_status"],
            "qualification_evidence": p.get("qualification_evidence", []),
            "source_ids": ["hls_brick_taxsale"] + (
                ["njogis_parcels_modiv"] if parcel else []),
            "evidence_ids": [rec["raw_record_id"]] + (
                [f"njogis_parcels_modiv:{parcel['parcel_id']}"] if parcel else []),
            "lead_status": ("APPROVED_FOR_DASHBOARD" if approved
                             else "REVIEW_REQUIRED"),
            "doc_type": p.get("doc_type"),
            "distress_signal": "tax_default_brick",
            "muni_name": "Brick Township",
            "account_number": p["account_number"],
            "block": p.get("block"),
            "lot": p.get("lot"),
            "qual_code": p.get("qual_code"),
            "property_address": p.get("property_location"),
            "property_city": "BRICK",
            "property_state": "NJ",
            "owner_name": p.get("owner_name"),
            "owner_resolved": bool(p.get("owner_name")),
            "owner_resolution_status": "HLS_BRICK_EXPOSED",
            "tax_amount": p.get("tax_amount"),
            "mua_amount": p.get("mua_amount"),
            "cost_amount": p.get("cost_amount"),
            "total_due": p.get("total_due"),
            "investor_name": p.get("investor_name"),
            "sale_date": p.get("sale_date"),
            "lead_type": p.get("lead_type"),
            # parcel enrichment
            "parcel_id": parcel.get("parcel_id") if parcel else None,
            "net_value": parcel.get("net_value") if parcel else None,
            "year_built": parcel.get("yr_constr") if parcel else None,
            "last_sale_price": parcel.get("sale_price") if parcel else None,
        })
    status = {
        "leads_emitted": len(leads),
        "qualification_counts": qual_counts,
        "lead_type_counts": type_counts,
        "njogis_parcel_join_hits": parcel_hits,
    }
    return leads, status


def build_civilview_status() -> dict:
    """Surface CivilView's dormancy in the manifest without emitting leads."""
    out = {"source_id": "civilview_sheriff_sales", "active_leads": 0,
           "dormant": False, "dormancy_detail": None}
    for rec in _iter_jsonl(CIVILVIEW_RAW):
        if rec.get("source_role") == "DORMANT_PRIMARY_EVENT_SOURCE":
            out["dormant"] = True
            out["dormancy_detail"] = rec["raw_payload"].get("detail")
        else:
            out["active_leads"] += 1
    return out


def build_tax_board_detail_index() -> dict[tuple, dict]:
    """Index Tax Board detail records by (district_code, block, lot) so the
    §3.7 enrichment can supplement NJOGIS with mailing address +
    absentee-owner signal."""
    idx: dict[tuple, dict] = {}
    for rec in _iter_jsonl(TAX_BOARD_DETAIL):
        p = rec["raw_payload"]
        key = (
            (p.get("district_code") or "").strip(),
            _norm_block_lot(p.get("block") or ""),
            _norm_block_lot(p.get("lot") or ""),
        )
        idx.setdefault(key, p)
    return idx


def build_blocked_punchlist() -> list[dict]:
    """Surface the BLOCKED_SOURCEs in the build manifest."""
    return [b for b in _iter_jsonl(BLOCKED)]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-leads", default=str(OUT_LEADS))
    p.add_argument("--out-manifest", default=str(OUT_MANIFEST))
    args = p.parse_args()

    out_leads = Path(args.out_leads)
    out_manifest = Path(args.out_manifest)
    out_leads.parent.mkdir(parents=True, exist_ok=True)

    print("[build_leads] loading parcel index…")
    parcel_idx = _load_parcel_index()
    print(f"[build_leads] parcels indexed: {len(parcel_idx)}")

    print("[build_leads] building sheriff leads…")
    sheriff_leads, join_hits, join_misses = build_sheriff_leads(parcel_idx)
    print(f"[build_leads] sheriff leads: {len(sheriff_leads)} "
          f"(parcel join hits {join_hits} / misses {join_misses})")

    print("[build_leads] building surrogate leads…")
    surrogate_leads = build_surrogate_leads()
    print(f"[build_leads] surrogate leads: {len(surrogate_leads)}")

    print("[build_leads] building NJPA legal-notice leads…")
    njpa_leads = build_njpa_leads(parcel_idx)
    print(f"[build_leads] NJPA leads: {len(njpa_leads)}")

    print("[build_leads] building HLS Brick tax-default leads…")
    tax_board_idx = build_tax_board_detail_index()
    hls_leads, hls_status = build_hls_brick_leads(parcel_idx, tax_board_idx)
    print(f"[build_leads] HLS Brick leads: {len(hls_leads)} "
          f"qual={hls_status['qualification_counts']} "
          f"njogis_joins={hls_status['njogis_parcel_join_hits']}")

    civilview_status = build_civilview_status()
    print(f"[build_leads] CivilView: dormant={civilview_status['dormant']} "
          f"active_leads={civilview_status['active_leads']}")

    blocked = build_blocked_punchlist()
    print(f"[build_leads] blocked sources: {len(blocked)}")

    all_leads = sheriff_leads + surrogate_leads + njpa_leads + hls_leads
    payload = {
        "county_slug": "ocean_nj",
        "county_name": "Ocean County",
        "state_name": "New Jersey",
        "framework_version": "v5.5.0",
        "build_timestamp": _now_iso(),
        "build_mode": "PARTIAL_BUILD",
        "total_leads": len(all_leads),
        "lead_counts_by_signal": {
            "foreclosure_sale_scheduled": len(sheriff_leads),
            "foreclosure_notice_published": len(njpa_leads),
            "probate_filing_recent": len(surrogate_leads),
            "tax_default_brick": len(hls_leads),
        },
        "hls_brick_status": hls_status,
        "civilview_status": civilview_status,
        "tax_board_detail_indexed": len(tax_board_idx),
        "lead_counts_by_status": {
            "APPROVED_FOR_DASHBOARD": sum(
                1 for l in all_leads if l["lead_status"] == "APPROVED_FOR_DASHBOARD"),
            "REVIEW_REQUIRED": sum(
                1 for l in all_leads if l["lead_status"] == "REVIEW_REQUIRED"),
        },
        "parcel_join": {
            "hits": join_hits,
            "misses": join_misses,
            "resolved_owner_fraction": 0.0,
            "enrichment_join_unavailable": True,
            "enrichment_join_unavailable_reason": (
                "Daniel's Law (P.L. 2020, c. 125) redacts owner_name from "
                "NJOGIS Ocean County parcels/MOD-IV. Surrogate probate "
                "records carry decedent name + town but no street address. "
                "Owner-resolution path requires either S8 Tax Board owner "
                "probe OR S3 Clerk land-records unlock."
            ),
        },
        "blocked_sources": blocked,
        "leads": all_leads,
    }

    tmp = out_leads.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(out_leads)

    # Manifest separately (just counts; no PII)
    manifest = {k: v for k, v in payload.items() if k != "leads"}
    out_manifest.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"[build_leads] wrote {out_leads} (total leads {len(all_leads)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
