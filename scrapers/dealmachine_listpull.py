"""Ocean County, NJ — DealMachine commercial LIST-PULL (client-specific override).

⚠ CLIENT-SPECIFIC, OCEAN-ONLY. NOT framework canon. County #5 and the framework
do NOT inherit this. See runs/ocean_nj/operator_notes.md.

Pulls additional COMMERCIAL lead lists from DealMachine's property-search API
(`dm properties search`) scoped to Ocean County, NJ (loc_county_34029). These are
aggregator lists (vacant, HOA-lien, zombie, expired-listing, …) — NOT county
source-of-record distress. They are tagged source="dealmachine",
lead_origin_type="COMMERCIAL_LIST", is_dealmachine_list=True so the dashboard keeps
them visually/structurally distinct from the sheriff / Brick source-of-record leads.

COST: `properties search` charges 1 property credit per returned property. Contacts
(people credits) are GATED — we pull property data only here; contact enrichment is
a separate, explicit step. `properties count` is free and used for estimates.

Lead types confirmed available via `dm filters --source-type properties` (2026-06):
  expired_listing  is_mls_listing_expired   (~1,681)
  hoa_lien         has_hoa_lien             (~1,097)
  vacant           is_vacant_home           (~1,173)
  zombie           is_zombie_property       (~8)
  senior_owner     has_senior_owners        (~105,662)  ← GATED: too large/broad
  tired_landlord   has_tired_landlords      (~24,730)   ← GATED: too large/broad

Output: data/enriched/dealmachine_listpull.json (list of lead records). The dashboard
renderer merges this file in at render time, so county build_leads never wipes it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "enriched" / "dealmachine_listpull.json"
OCEAN_COUNTY = {"type": "county", "code": "34029"}   # Ocean County, NJ
PER_PAGE = 100
RATE_SLEEP = 0.4

# Default pull set: the affordable, targeted lists. Senior/tired are intentionally
# NOT here (too large/broad); pull them only with explicit --include and a cap.
DEFAULT_TYPES = {
    "expired_listing": "is_mls_listing_expired",
    "hoa_lien": "has_hoa_lien",
    "vacant": "is_vacant_home",
    "zombie": "is_zombie_property",
}
GATED_TYPES = {
    "senior_owner": "has_senior_owners",
    "tired_landlord": "has_tired_landlords",
}


def _dm(args: list[str]) -> dict:
    import os
    env = {**os.environ, "DM_QUIET": "1"}
    res = subprocess.run(["dm", *args, "--quiet"], capture_output=True, text=True,
                        timeout=180, env=env)
    raw = (res.stdout or "") + "\n" + (res.stderr or "")
    i = raw.find("{")
    if i < 0:
        raise RuntimeError(f"dm {' '.join(args[:3])} -> {raw.strip()[:200]}")
    obj, _ = json.JSONDecoder().raw_decode(raw[i:])   # parse only the first JSON object
    return obj


def count(filter_id: str) -> dict:
    body = {"locations": [OCEAN_COUNTY], "filters": [{"filter_id": filter_id, "value": True}]}
    d = _dm(["properties", "count", "--body", json.dumps(body), "--json"])
    return {"properties": d.get("total_properties"), "people": d.get("total_people")}


# Fields requested per row. `properties search` returns a default identity set
# PLUS whatever is named here. We request the property facts the dashboard shows
# AND the motivation flags (the latter returnable ONLY via search + fields). Riding
# the pull we already do, so this costs nothing extra on deduplicated rows.
MOTIVATION_FIELDS = [
    "estimated_value", "year_built", "last_sale_price", "last_sale_date",
    "living_area_sqft", "property_type",
    "has_senior_owners", "has_tired_landlords",
]


def _yn(v) -> bool:
    return str(v).strip().lower() == "yes"


def search_page(filter_id: str, page: int) -> dict:
    body = {"locations": [OCEAN_COUNTY],
            "filters": [{"filter_id": filter_id, "value": True}],
            "page": page, "per_page": PER_PAGE,
            "fields": MOTIVATION_FIELDS}
    return _dm(["properties", "search", "--body", json.dumps(body), "--json"])


PROP_FIELDS = ("dm_property_id", "full_address", "address", "unit", "city", "state",
               "zip", "latitude", "longitude", "estimated_value", "property_type",
               "living_area_sqft", "year_built", "last_sale_price", "last_sale_date", "apn")


def to_lead(row: dict, lead_type: str, when: str) -> dict:
    pid = row.get("dm_property_id")
    prop = {k: row.get(k) for k in PROP_FIELDS}
    return {
        "lead_id": f"dealmachine_list:{lead_type}:{pid}",
        "lead_origin_type": "COMMERCIAL_LIST",
        "source_ids": ["dealmachine"],
        "is_dealmachine_list": True,
        "lead_type": lead_type,
        # Motivation MULTIPLIER flags (owner attributes, not lead origins).
        "senior_owner_flag": _yn(row.get("has_senior_owners")),
        "tired_landlord_flag": _yn(row.get("has_tired_landlords")),
        "distress_signal": None,
        "primary_event_date": None,
        "lead_status": "DEALMACHINE_LIST",
        "qualification_status": "COMMERCIAL_ENRICHMENT_SOURCE",
        # property location
        "property_address": row.get("address"),
        "property_city": row.get("city"),
        "property_state": row.get("state"),
        "property_zip": row.get("zip"),
        "parcel_id": None,
        # owner unresolved until contact enrichment is explicitly authorized (gated)
        "owner_name": None,
        "owner_resolved": False,
        "dm_enrichment_status": "list_property_only",
        "dealmachine": {
            "source": "dealmachine", "scope": "COMMERCIAL_LIST", "matched": True,
            "enrichment_status": "list_property_only", "lead_type": lead_type,
            "enriched_at": when, "property": prop, "contacts": [],
            "phones": [], "emails": [],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="DealMachine commercial list-pull for Ocean NJ")
    ap.add_argument("--types", nargs="*", default=list(DEFAULT_TYPES),
                    help="lead types to pull (default: the 4 affordable lists)")
    ap.add_argument("--include-gated", nargs="*", default=[],
                    help="explicitly pull a large gated type (senior_owner/tired_landlord)")
    ap.add_argument("--cap", type=int, default=0, help="max properties per type (0 = all)")
    ap.add_argument("--count-only", action="store_true", help="free count estimate, no pull")
    args = ap.parse_args()

    catalog = {**DEFAULT_TYPES, **GATED_TYPES}
    wanted = list(args.types) + list(args.include_gated)

    # cost estimate (free)
    print("=== Ocean County DealMachine list-pull — estimate (free count) ===")
    est = {}
    for lt in wanted:
        fid = catalog.get(lt)
        if not fid:
            print(f"  {lt}: UNKNOWN lead type"); continue
        c = count(fid); est[lt] = c
        cap = args.cap or c["properties"]
        print(f"  {lt:16s} {fid:24s} props={c['properties']:>7} -> pull {min(cap, c['properties'] or 0):>6} "
              f"(~{min(cap, c['properties'] or 0)} property credits)")
        time.sleep(RATE_SLEEP)
    total = sum(min(args.cap or (est[lt]['properties'] or 0), est[lt]['properties'] or 0) for lt in est)
    print(f"  ESTIMATED TOTAL: ~{total} property credits (contacts gated)")
    if args.count_only:
        return 0

    when = datetime.now(timezone.utc).isoformat()
    leads = []
    used = {}
    for lt in wanted:
        fid = catalog.get(lt)
        if not fid:
            continue
        target = est[lt]["properties"] or 0
        if args.cap:
            target = min(target, args.cap)
        got = 0; page = 1
        while got < target:
            resp = search_page(fid, page)
            rows = resp.get("data") or []
            if not rows:
                break
            for row in rows:
                if got >= target:
                    break
                leads.append(to_lead(row, lt, when))
                got += 1
            if not resp.get("pagination", {}).get("has_next_page"):
                break
            page += 1
            time.sleep(RATE_SLEEP)
        used[lt] = got
        print(f"  pulled {lt}: {got} properties")

    payload = {"pulled_at": when, "county": "ocean_nj", "scope": "COMMERCIAL_LIST",
               "source": "dealmachine", "counts": used, "estimate": est, "leads": leads}
    OUT.write_text(json.dumps(payload, indent=1))
    print(f"\nwrote {len(leads)} list-pull leads -> {OUT}")
    print("counts:", json.dumps(used))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
