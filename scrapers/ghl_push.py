#!/usr/bin/env python3
"""Ocean NJ — direct GoHighLevel contact upsert for net-new actionable leads.

CLIENT-SPECIFIC, OCEAN-ONLY. Direct API, no middleware.

Reads data/exports/new_leads_latest.json (produced by ghl_diff_new_leads.py)
and upserts each lead as a GHL contact:

  POST https://services.leadconnectorhq.com/contacts/upsert
  Authorization: Bearer $GHL_TOKEN      (read from env / GH Actions secret)
  Version: 2021-07-28
  Content-Type: application/json

Body: locationId from $GHL_LOCATION_ID, mapped name/address/phone/email,
tags ["ocean-new-lead"]. phone_1 = primary phone; phone_2/phone_3 + docket are
carried in customFields. Upsert (NOT create) — re-running never duplicates.

Resilience: per-lead try/except — one failed lead never kills the run. Every
attempt is recorded to data/exports/push_log_latest.json.

Ledger: ONLY after a 2xx does the lead's key get added to
data/exports/pushed_keys.json. Failed pushes stay OUT and retry next run.

SAFETY: dry-run by DEFAULT. The live POST only fires when GHL_PUSH_LIVE=1.
In dry-run nothing hits the network and the ledger is NOT written; the first 3
mapped payloads are printed with the token redacted for mapping verification.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXPORT_DIR = REPO / "data" / "exports"
NEW_LEADS = EXPORT_DIR / "new_leads_latest.json"
LEDGER = EXPORT_DIR / "pushed_keys.json"
PUSH_LOG = EXPORT_DIR / "push_log_latest.json"

GHL_URL = "https://services.leadconnectorhq.com/contacts/upsert"
GHL_VERSION = "2021-07-28"
TAGS = ["ocean-new-lead"]


def build_payload(lead: dict, location_id: str) -> dict:
    """Map a flat export record -> GHL contacts/upsert body."""
    phones = [lead.get(k) for k in ("phone_1", "phone_2", "phone_3") if lead.get(k)]
    emails = [lead.get(k) for k in ("email_1", "email_2") if lead.get(k)]

    # phone_2/phone_3 + docket ride along in customFields (key-based). The
    # client's GHL must have matching custom fields, else GHL ignores unknown
    # keys — lossless primary phone/email always land on the contact proper.
    custom = []
    if len(phones) > 1:
        custom.append({"key": "phone_2", "field_value": phones[1]})
    if len(phones) > 2:
        custom.append({"key": "phone_3", "field_value": phones[2]})
    if lead.get("docket"):
        custom.append({"key": "docket", "field_value": lead["docket"]})
    if lead.get("lead_type"):
        custom.append({"key": "lead_type", "field_value": lead["lead_type"]})

    body: dict = {
        "locationId": location_id,
        "firstName": lead.get("first_name") or "",
        "lastName": lead.get("last_name") or "",
        "address1": lead.get("property_address") or "",
        "city": lead.get("property_city") or "",
        "state": lead.get("property_state") or "NJ",
        "postalCode": lead.get("property_zip") or "",
        "tags": TAGS,
        "source": "ocean-nj-daily-refresh",
    }
    if phones:
        body["phone"] = phones[0]
    if emails:
        body["email"] = emails[0]
    if custom:
        body["customFields"] = custom
    return body


def redact_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token[:4]}…REDACTED" if token else "Bearer ***REDACTED***",
        "Version": GHL_VERSION,
        "Content-Type": "application/json",
    }


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
        print(f"::warning title=GHL push skipped::{NEW_LEADS.name} not found — run ghl_diff_new_leads.py first")
        return 0
    leads = json.loads(NEW_LEADS.read_text())
    print(f"net-new leads to push: {len(leads)} | mode: {'LIVE' if live else 'DRY-RUN'}")

    if not leads:
        PUSH_LOG.write_text(json.dumps({"mode": "live" if live else "dry-run",
                                        "pushed": 0, "results": []}, indent=2))
        print("nothing to push.")
        return 0

    # ---- DRY RUN: print first 3 payloads (token redacted), no network ----
    if not live:
        print("\n=== DRY RUN — first 3 mapped payloads (exactly as they would POST) ===")
        print(f"POST {GHL_URL}")
        print(f"Headers: {json.dumps(redact_headers(token))}")
        for i, lead in enumerate(leads[:3], 1):
            print(f"\n--- payload {i} (key={lead.get('key')}) ---")
            print(json.dumps(build_payload(lead, location_id or "<GHL_LOCATION_ID>"), indent=2))
        print(f"\n(dry-run: ledger NOT updated; {len(leads)} leads staged. "
              f"Set GHL_PUSH_LIVE=1 to push live.)")
        PUSH_LOG.write_text(json.dumps({"mode": "dry-run", "staged": len(leads)}, indent=2))
        return 0

    # ---- LIVE: upsert each lead, ledger only on 2xx ----
    if not token or not location_id:
        print("::warning title=GHL push skipped::GHL_TOKEN / GHL_LOCATION_ID not set — leads staged, not pushed")
        return 0

    import time
    import urllib.error
    import urllib.request

    # Pace upserts so a large first batch (empty ledger -> all leads at once)
    # doesn't burst GHL's rate limit and get throttled/dropped. ~1s/contact.
    delay = float(os.environ.get("GHL_PUSH_DELAY_SEC", "1.0"))

    ledger = load_ledger()
    pushed = set(ledger["pushed_keys"])
    headers = {"Authorization": f"Bearer {token}", "Version": GHL_VERSION,
               "Content-Type": "application/json"}
    results = []
    ok = 0
    for i, lead in enumerate(leads):
        if i:  # pace between pushes; no delay before the first
            time.sleep(delay)
        key = lead.get("key")
        payload = build_payload(lead, location_id)
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
                pushed.add(key)  # ledger ONLY on success
        except urllib.error.HTTPError as e:
            results.append({"key": key, "status": e.code,
                            "error": e.read().decode("utf-8", "replace")[:300]})
            print(f"::warning title=GHL push failed (lead)::{key} -> HTTP {e.code}")
        except Exception as e:  # noqa: BLE001 — one lead must not kill the run
            results.append({"key": key, "status": None, "error": str(e)[:300]})
            print(f"::warning title=GHL push errored (lead)::{key} -> {e}")

    PUSH_LOG.write_text(json.dumps(
        {"mode": "live", "attempted": len(leads), "pushed": ok, "results": results}, indent=2))

    # Persist ledger (additive) only if at least one push succeeded.
    if ok:
        ledger["pushed_keys"] = sorted(pushed)
        LEDGER.write_text(json.dumps(ledger, indent=2))
    print(f"GHL push complete: {ok}/{len(leads)} upserted; ledger now {len(pushed)} keys.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
