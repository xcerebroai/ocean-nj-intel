#!/usr/bin/env python3
"""Ocean County — ONE-TIME bulk GHL upsert of ALL contactable dashboard leads.

CLIENT-SPECIFIC, OCEAN-ONLY. Reads data/exports/ocean_county_all_leads.json
(from ocean_county_bulk_enrich.py) and upserts each contact to GHL tagged
**"Ocean County"** — deliberately NOT "ocean-new-lead", so this segmentation
push does NOT trip the client's live SMS workflow (which triggers on
ocean-new-lead). Separate ledger from the daily push.

Mirrors scrapers/ghl_push.py (same endpoint/headers/WAF user-agent, per-lead
continue-on-error, ~1s pacing, ledger-only-on-2xx). Dry-run by DEFAULT; live
only when GHL_PUSH_LIVE=1. GHL_PUSH_LIMIT caps the batch (validation).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scrapers.ghl_push import (  # noqa: E402  reuse endpoint + payload mapping
    GHL_URL, GHL_VERSION, USER_AGENT, build_payload, redact_headers,
)

EXPORT_DIR = REPO / "data" / "exports"
NEW_LEADS = EXPORT_DIR / "ocean_county_all_leads.json"
LEDGER = EXPORT_DIR / "ocean_county_pushed_keys.json"
PUSH_LOG = EXPORT_DIR / "ocean_county_push_log.json"

TAG = "Ocean County"   # segmentation tag — NOT the SMS-triggering ocean-new-lead


def payload_for(lead: dict, location_id: str) -> dict:
    body = build_payload(lead, location_id)
    body["tags"] = [TAG]                       # override: Ocean County only
    body["source"] = "ocean-county-bulk"
    return body


def load_ledger() -> dict:
    if not LEDGER.exists():
        return {"pushed_keys": []}
    try:
        data = json.loads(LEDGER.read_text())
    except (json.JSONDecodeError, OSError):
        return {"pushed_keys": []}
    if isinstance(data, list):
        return {"pushed_keys": data}
    data.setdefault("pushed_keys", [])
    return data


def main() -> int:
    live = os.environ.get("GHL_PUSH_LIVE") == "1"
    token = os.environ.get("GHL_TOKEN", "")
    location_id = os.environ.get("GHL_LOCATION_ID", "")

    if not NEW_LEADS.exists():
        print(f"::warning title=Bulk push skipped::{NEW_LEADS.name} not found — run ocean_county_bulk_enrich.py first")
        return 0
    leads = json.loads(NEW_LEADS.read_text())

    # Skip anything already pushed under THIS ledger (idempotent re-runs).
    ledger = load_ledger()
    pushed = set(ledger["pushed_keys"])
    leads = [l for l in leads if l.get("key") not in pushed]

    limit = os.environ.get("GHL_PUSH_LIMIT")
    if limit:
        leads = leads[: int(limit)]
        print(f"GHL_PUSH_LIMIT={limit} — capped to first {len(leads)} lead(s)")
    print(f"bulk leads to push (tag '{TAG}'): {len(leads)} | "
          f"already ledgered: {len(pushed)} | mode: {'LIVE' if live else 'DRY-RUN'}")

    if not leads:
        PUSH_LOG.write_text(json.dumps({"mode": "live" if live else "dry-run", "pushed": 0}, indent=2))
        print("nothing to push.")
        return 0

    # ---- DRY RUN: first 3 payloads (token redacted), no network ----
    if not live:
        print(f"\n=== DRY RUN — first 3 payloads (tag '{TAG}', token redacted) ===")
        print(f"POST {GHL_URL}\nHeaders: {json.dumps(redact_headers(token))}")
        for i, lead in enumerate(leads[:3], 1):
            print(f"\n--- payload {i} (key={lead.get('key')}) ---")
            print(json.dumps(payload_for(lead, location_id or "<GHL_LOCATION_ID>"), indent=2))
        print(f"\n(dry-run: ledger NOT updated; {len(leads)} staged. Set GHL_PUSH_LIVE=1 to push live.)")
        PUSH_LOG.write_text(json.dumps({"mode": "dry-run", "staged": len(leads)}, indent=2))
        return 0

    # ---- LIVE: upsert each, ledger only on 2xx ----
    if not token or not location_id:
        print("::warning title=Bulk push skipped::GHL_TOKEN / GHL_LOCATION_ID not set — staged, not pushed")
        return 0

    import time
    import urllib.error
    import urllib.request

    delay = float(os.environ.get("GHL_PUSH_DELAY_SEC", "1.0"))
    headers = {"Authorization": f"Bearer {token}", "Version": GHL_VERSION,
               "Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": USER_AGENT}
    results = []
    ok = 0
    for i, lead in enumerate(leads):
        if i:
            time.sleep(delay)
        key = lead.get("key")
        payload = payload_for(lead, location_id)
        try:
            req = urllib.request.Request(
                GHL_URL, data=json.dumps(payload).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.getcode()
                rbody = resp.read().decode("utf-8", "replace")
            contact_id = ""
            try:
                contact_id = (json.loads(rbody).get("contact") or {}).get("id", "")
            except json.JSONDecodeError:
                pass
            results.append({"key": key, "status": status, "contact_id": contact_id})
            if 200 <= status < 300:
                ok += 1
                pushed.add(key)
        except urllib.error.HTTPError as e:
            results.append({"key": key, "status": e.code,
                            "error": e.read().decode("utf-8", "replace")[:300]})
            print(f"::warning title=Bulk push failed (lead)::{key} -> HTTP {e.code}")
        except Exception as e:  # noqa: BLE001 — one lead must not kill the run
            results.append({"key": key, "status": None, "error": str(e)[:300]})
            print(f"::warning title=Bulk push errored (lead)::{key} -> {e}")
        if i and i % 100 == 0:
            print(f"  ... {i}/{len(leads)} processed ({ok} ok)")

    PUSH_LOG.write_text(json.dumps(
        {"mode": "live", "tag": TAG, "attempted": len(leads), "pushed": ok, "results": results}, indent=2))
    if ok:
        ledger["pushed_keys"] = sorted(pushed)
        LEDGER.write_text(json.dumps(ledger, indent=2))
    print(f"Bulk push complete: {ok}/{len(leads)} upserted (tag '{TAG}'); ledger now {len(pushed)} keys.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
