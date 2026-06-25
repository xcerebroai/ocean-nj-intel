#!/usr/bin/env python3
"""Ocean County — ONE-TIME bulk DealMachine enrichment of ALL dashboard leads.

CLIENT-SPECIFIC, OCEAN-ONLY. Not framework canon. Separate from the daily
actionable enrichment (scrapers/dealmachine_enrich.py) — this enriches EVERY
dashboard lead (source leads in scored_leads.json + the DealMachine-list leads
in dealmachine_listpull.json) that still lacks contacts, so the whole county can
be pushed to GHL.

Reuses the proven DealMachine primitives (APN/address routing, adaptive batching
with circuit breaker, resumable checkpoint) from dealmachine_enrich. Routes each
lead to APN (3-part parcels) or situs address (everything with a ZIP); the rest
are unroutable and reported.

Output: data/exports/ocean_county_all_leads.json — flat GHL-ready records for
EVERY lead that ends up with >=1 phone or email (already-enriched + newly
fetched). Resumable: re-running skips DealMachine keys already in the checkpoint.

MUST run in CI (needs DEALMACHINE_API_KEY / `dm login`). --dry-run plans only.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scrapers.dealmachine_enrich import (  # noqa: E402  (path set above)
    APIDownError, CONTACT_AUDIENCE, _dm_enrich, api_healthy, build_block,
    derive_apn, dm_usage, enrich_addresses, enrich_apns, ensure_cli,
    load_parcel_situs, situs_address,
)
from scrapers.ghl_diff_new_leads import (  # noqa: E402
    collect_emails, collect_phones, pick_address, pick_name,
)

SCORED = REPO / "data" / "leads" / "scored_leads.json"
LISTPULL = REPO / "data" / "enriched" / "dealmachine_listpull.json"
RESULTS = REPO / "data" / "enriched" / "ocean_county_results.json"  # resumable checkpoint
OUT = REPO / "data" / "exports" / "ocean_county_all_leads.json"

# distress_signal / list lead_type -> human label (mirrors the dashboard).
LABELS = {
    "foreclosure_sale_scheduled": "Sheriff Foreclosure",
    "foreclosure_notice_published": "Foreclosure Notice",
    "tax_default_brick": "Tax Default",
    "probate_filing_recent": "Probate",
    "expired_listing": "Expired Listing",
    "vacant": "Vacant Home",
    "hoa_lien": "HOA Lien",
    "zombie": "Zombie Property",
}


def lead_type_label(lead: dict) -> str:
    key = lead.get("lead_type") or lead.get("distress_signal")
    return LABELS.get(key, key or "")


def has_contacts(lead: dict) -> bool:
    dm = lead.get("dealmachine") or {}
    return bool(dm.get("phones") or dm.get("emails"))


def load_all_leads() -> list[dict]:
    """Every dashboard lead: source leads + DealMachine-list leads, de-duped by id."""
    by_id: dict[str, dict] = {}
    for l in json.loads(SCORED.read_text()).get("leads", []):
        by_id[l["lead_id"]] = l
    for l in json.loads(LISTPULL.read_text()).get("leads", []):
        by_id.setdefault(l["lead_id"], l)  # source lead wins on collision
    return list(by_id.values())


def main() -> int:
    ap = argparse.ArgumentParser(description="Bulk DealMachine enrichment of ALL Ocean dashboard leads")
    ap.add_argument("--limit", type=int, default=0, help="Cap leads to enrich (validation)")
    ap.add_argument("--dry-run", action="store_true", help="Plan only; no API calls")
    ap.add_argument("--probe", action="store_true",
                    help="One lightweight test enrich call; report status and exit "
                         "(0=API healthy/2xx, 3=down/504/timeout). No other work.")
    args = ap.parse_args()

    if args.probe:
        ensure_cli()
        cr = dm_usage().get("credits", {})
        print("credits:", json.dumps(cr.get("breakdown", cr)))
        lp = json.loads(LISTPULL.read_text()).get("leads", [])
        first = lp[0]
        addr = ((first.get("dealmachine") or {}).get("property") or {}).get("full_address") \
            or f"{first.get('property_address')}, {first.get('property_city')}, NJ {first.get('property_zip')}"
        print(f"PROBE: single address skip-trace enrich -> {addr}")
        try:
            resp = _dm_enrich("address", {"data": [{"full_address": addr}],
                                          "include_contacts": True,
                                          "contact_audience": CONTACT_AUDIENCE})
            rec = (resp.get("data") or [{}])[0]
            print(f"PROBE RESULT: HTTP 2xx OK | matched={rec.get('matched')} | "
                  f"contacts={len(rec.get('contacts') or [])}")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"PROBE RESULT: FAILED (API down/degraded) -> {str(exc)[:220]}")
            return 3

    OUT.parent.mkdir(parents=True, exist_ok=True)
    leads = load_all_leads()
    by_id = {l["lead_id"]: l for l in leads}
    already = [l for l in leads if has_contacts(l)]
    need = [l for l in leads if not has_contacts(l)]
    if args.limit:
        need = need[: args.limit]
    print(f"total dashboard leads:   {len(leads)}")
    print(f"  already contactable:   {len(already)}")
    print(f"  to enrich this run:    {len(need)}{f' (capped at {args.limit})' if args.limit else ''}")

    # Route each lead: 3-part parcel -> APN; else situs address (needs a ZIP).
    situs = load_parcel_situs({l.get("parcel_id") for l in need if l.get("parcel_id")})
    apn_of: dict[str, str] = {}
    addr_of: dict[str, str] = {}
    unroutable = 0
    for l in need:
        apn = derive_apn(l.get("parcel_id"))
        if apn:
            apn_of[l["lead_id"]] = apn
            continue
        a = situs_address(l, situs)
        if a:
            addr_of[l["lead_id"]] = a
        else:
            unroutable += 1
    uniq_apns = sorted(set(apn_of.values()))
    uniq_addrs = sorted(set(addr_of.values()))
    print(f"  routing: APN={len(apn_of)} ({len(uniq_apns)} uniq) | "
          f"address={len(addr_of)} ({len(uniq_addrs)} uniq) | unroutable={unroutable}")

    if args.dry_run:
        print("DRY RUN — no API calls, no enrichment.")
    else:
        ensure_cli()
        print("credits before:", json.dumps(dm_usage().get("credits", {}).get("breakdown", {})))
        results = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}

        def save():
            RESULTS.write_text(json.dumps(results))

        # Gate on the ADDRESS endpoint, NOT the stock api_healthy() APN probe:
        # the APN endpoint is the degraded one, so an APN-based health check fails
        # and skips enrichment entirely (incl. the address-routed list leads).
        def address_healthy() -> bool:
            try:
                r = _dm_enrich("address", {"data": [{"full_address": addr_probe}],
                                           "include_contacts": True,
                                           "contact_audience": CONTACT_AUDIENCE})
                return bool(r.get("data"))
            except Exception:  # noqa: BLE001
                return False

        addr_probe = uniq_addrs[0] if uniq_addrs else \
            "827 N GREEN ST, LITTLE EGG HARBOR TWP, NJ 08087"
        if not address_healthy():
            print("::warning:: DealMachine ADDRESS endpoint health probe FAILED — applying "
                  "checkpoint only (resumable; re-run when healthy).", file=sys.stderr)
        else:
            # ADDRESS phase FIRST: the list leads (vacant/expired/HOA/zombie) — the
            # bulk target — route by address, and the address endpoint is the one
            # the health probe confirms healthy. The /enrichment/apn endpoint has
            # been intermittently 504-ing; isolate each phase so an APN-endpoint
            # outage can't trip the breaker and abort the address phase before it
            # even starts (which is exactly what kept happening).
            try:
                if uniq_addrs:
                    enrich_addresses(uniq_addrs, results, save)
            except APIDownError as exc:
                print(f"::warning:: address phase aborted (resumable): {exc}", file=sys.stderr)
            try:
                if uniq_apns:
                    enrich_apns(uniq_apns, results, save)
            except APIDownError as exc:
                print(f"::warning:: APN phase aborted (resumable): {exc}", file=sys.stderr)
        save()

        when = datetime.now(timezone.utc).isoformat()
        applied = 0
        for lid, apn in apn_of.items():
            rec = results.get(apn)
            if rec is not None:
                by_id[lid]["dealmachine"] = build_block(rec, "apn", when, "")
                applied += 1
        for lid, addr in addr_of.items():
            rec = results.get(addr)
            if rec is not None:
                by_id[lid]["dealmachine"] = build_block(rec, "address", when, "")
                applied += 1
        print(f"  enrichment applied to {applied} leads "
              f"({sum(1 for l in need if has_contacts(l))} now contactable)")
        print("credits after: ", json.dumps(dm_usage().get("credits", {}).get("breakdown", {})))

    # Build the flat GHL export over EVERY contactable lead (old + new).
    export = []
    for l in leads:
        if not has_contacts(l):
            continue
        phones = collect_phones(l)
        emails = collect_emails(l)
        if not phones and not emails:
            continue
        first, last = pick_name(l)
        addr, city, state, zipc = pick_address(l)
        export.append({
            "first_name": first, "last_name": last,
            "property_address": addr, "property_city": city,
            "property_state": state or "NJ", "property_zip": zipc,
            "phone_1": phones[0] if phones else "",
            "phone_2": phones[1] if len(phones) > 1 else "",
            "phone_3": phones[2] if len(phones) > 2 else "",
            "email_1": emails[0] if emails else "",
            "email_2": emails[1] if len(emails) > 1 else "",
            "lead_type": lead_type_label(l),
            "key": l["lead_id"],   # stable unique key for the push ledger
        })
    OUT.write_text(json.dumps(export, indent=2))
    print(f"EXPORT: {len(export)} contactable leads -> {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
