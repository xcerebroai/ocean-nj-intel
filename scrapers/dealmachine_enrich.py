"""Ocean County, NJ — DealMachine commercial enrichment pass.

COMMERCIAL_ENRICHMENT_SOURCE (Ocean-scoped, NOT framework canon). Attaches
owner / contact / property intelligence from DealMachine onto ACTIONABLE
source-of-record leads (S1 sheriff foreclosures + S7c HLS Brick tax-default).
Probate research targets are gated OUT by default (--include-probate).

Auth: the official `dm` CLI (@dealmachine/cli), authenticated against
https://api.v2.dealmachine.com/v1. Key lives in ~/.dealmachine/config.json
(written by `dm login`, outside the repo) and/or env DEALMACHINE_API_KEY /
runs/ocean_nj/.dealmachine_key. We never read/store the email/password.

Cost model (confirmed live): 1 property credit per matched property + 1 people
credit per returned owner contact. Unmatched = free. In-billing-cycle duplicates
= free (DealMachine dedupes). We therefore only enrich NEW/changed actionable
leads each run (content-hash cache), and dedupe identical APNs/addresses per run.

Match strategy (per §-prior decision):
  • APN-preferred for parcel-joined leads with a 3-part block/lot parcel_id
    (429 of them). APN is exact and avoids situs-ZIP ambiguity. Format derived
    from MOD-IV parcel_id, verified live (e.g. 1507_44.01_16 -> 07-00044-01-00016,
    1508_392_15.11 -> 08-00392-0000-00015-11). location={"type":"state","code":"NJ"}.
  • Address fallback for condos (4-part qual parcels), no-parcel leads, and any
    APN miss — using the situs address (sheriff property fields, or MOD-IV
    prop_loc + postal city + ZIP). Address enrichment requires a ZIP.

Provenance (§3.8): every DealMachine-derived field is written under the lead's
`dealmachine` block stamped source="dealmachine". County-sourced fields are NOT
mutated and keep their county tag. Daniel's-Law-redacted owner_name is filled
from DealMachine and explicitly labeled (owner_name_source="dealmachine",
daniels_law_backfilled=true), preserving the prior status.

Fail-loud: if the CLI is missing or not authenticated, this step raises. The
daily-refresh workflow runs it continue-on-error so the rest can still publish
(a loud ::warning), per operator instruction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
LEADS_PATH = DATA / "leads" / "scored_leads.json"
PARCEL_IDX = DATA / "enriched" / "parcel_index.jsonl"
CACHE_PATH = DATA / "enriched" / "dealmachine_cache.json"
RESULTS_PATH = DATA / "enriched" / "dealmachine_results.json"  # resumable raw-result checkpoint
PROVENANCE_PATH = DATA / "enriched" / "dealmachine_enrichment.jsonl"

ACTIONABLE_SOURCES = ("sheriff_foreclosure", "hls_brick_taxsale")
PROBATE_SOURCE = "surrogate_probate"
BATCH_SIZE = 6                   # small batches are most reliable on this API
                                 # (>~16 reliably 504s; singles always succeed)
MIN_BATCH = 1                    # split floor for oversize (504) batches
RATE_LIMIT_SLEEP = 0.4           # seconds between calls (10 req/s cap; we go slow)
CIRCUIT_BREAK = 6                # consecutive outage errors -> abort pass (API down)
CONTACT_AUDIENCE = "owners"      # owners only — not family/renters/residents
SOURCE_TAG = "dealmachine"
SCOPE_TAG = "COMMERCIAL_ENRICHMENT_SOURCE"

DANIELS_LAW_STATUSES = {
    "DANIELS_LAW_REDACTED",
    "DANIELS_LAW_REDACTED_AND_NO_PROBATE_ADDRESS",
}


# ─────────────────────────────────────────────────────────── APN derivation ──
def derive_apn(parcel_id: str | None) -> str | None:
    """MOD-IV parcel_id -> DealMachine NJ APN, or None if not safely derivable.

    Only 3-part parcel_ids (cd_block_lot) are derived. 4-part (condo/qual)
    parcels return None -> address fallback, because the qualifier encoding is
    unverified and units would collide.

    Verified live:
      1507_44.01_16  -> 07-00044-01-00016
      1519_1.348_21  -> 19-00001-348-00021
      1508_392_15.11 -> 08-00392-0000-00015-11
      (06 / integer block 349 lot 7 -> 06-00349-0000-00007)
    """
    if not parcel_id:
        return None
    parts = parcel_id.split("_")
    if len(parts) != 3:                 # 4-part = condo/qual -> address fallback
        return None
    cd, block, lot = parts
    if len(cd) < 2:
        return None
    district = cd[-2:]

    def seg(value: str, pad_frac_when_whole: bool) -> str | None:
        # block: integer -> "{whole:05d}-0000"; fractional -> "{whole:05d}-{frac}"
        # lot:   integer -> "{whole:05d}";       fractional -> "{whole:05d}-{frac}"
        if "." in value:
            whole, frac = value.split(".", 1)
        else:
            whole, frac = value, ("0000" if pad_frac_when_whole else None)
        try:
            whole_i = int(whole)
        except ValueError:
            return None
        if frac is None:
            return f"{whole_i:05d}"
        return f"{whole_i:05d}-{frac}"

    block_seg = seg(block, pad_frac_when_whole=True)
    lot_seg = seg(lot, pad_frac_when_whole=False)
    if block_seg is None or lot_seg is None:
        return None
    return f"{district}-{block_seg}-{lot_seg}"


# ──────────────────────────────────────────────────────── parcel situs index ──
def load_parcel_situs(parcel_ids: set[str]) -> dict[str, dict]:
    """Stream the MOD-IV index once; return {parcel_id: {prop_loc, city, zip5}}.

    prop_loc is the SITUS street; city_state/zip5 are the OWNER MAILING address
    (== situs only when owner-occupied). Used only for address fallback.
    """
    out: dict[str, dict] = {}
    if not PARCEL_IDX.exists():
        return out
    with PARCEL_IDX.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                payload = json.loads(line).get("raw_payload", {})
            except json.JSONDecodeError:
                continue
            pid = payload.get("parcel_id")
            if pid in parcel_ids:
                cs = (payload.get("city_state") or "").strip()
                state = cs.split()[-1] if cs else None   # "RICHMOND TX" -> "TX"
                city = cs.replace(" NJ", "").strip()
                out[pid] = {
                    "prop_loc": payload.get("prop_loc"),
                    "city": city,
                    "zip5": payload.get("zip5"),
                    "muni": payload.get("muni"),
                    "owner_mailing_state": state,   # county MOD-IV owner mailing state
                }
                if len(out) == len(parcel_ids):
                    break
    return out


def stamp_residency(actionable: list[dict]) -> None:
    """Stamp owner-residency signals onto each actionable lead, for the
    absentee / out-of-state dashboard filters:
      • owner_occupied      — from DealMachine (source: dealmachine)
      • owner_mailing_state — from county MOD-IV mailing address (source: county)
      • absentee            — owner does not occupy (DM owner_occupied == False)
      • out_of_state        — owner mails outside NJ
    """
    situs = load_parcel_situs({l["parcel_id"] for l in actionable if l.get("parcel_id")})
    for l in actionable:
        occ = ((l.get("dealmachine") or {}).get("property") or {}).get("owner_occupied")
        l["owner_occupied"] = occ
        ms = (situs.get(l.get("parcel_id") or "") or {}).get("owner_mailing_state")
        l["owner_mailing_state"] = ms
        l["absentee"] = (occ is False)
        l["out_of_state"] = bool(ms and ms != "NJ")


def situs_address(lead: dict, situs: dict[str, dict]) -> str | None:
    """Build a situs 'street, city, NJ zip' string, or None if no usable ZIP."""
    # Sheriff leads carry a real situs ZIP.
    if lead.get("property_zip") and lead.get("property_address"):
        city = lead.get("property_city") or ""
        return f"{lead['property_address']}, {city}, NJ {lead['property_zip']}".strip()
    # Parcel-only (condo) fallback: prop_loc + mailing city/zip (best effort).
    pj = situs.get(lead.get("parcel_id") or "")
    if pj and pj.get("prop_loc") and pj.get("zip5"):
        return f"{pj['prop_loc']}, {pj.get('city') or ''}, NJ {pj['zip5']}".strip()
    return None


# ───────────────────────────────────────────────────────────── dm CLI calls ──
def ensure_cli() -> None:
    if shutil.which("dm") is None:
        raise RuntimeError(
            "dm CLI not found. Install with `npm install -g @dealmachine/cli` "
            "and authenticate with `dm login --key $DEALMACHINE_API_KEY`."
        )
    # Check LOCAL auth state (whoami reads ~/.dealmachine/config.json — no API call),
    # so a DealMachine outage is NOT misread as "not authenticated". If the env key
    # is present we trust it even if whoami is unavailable.
    if os.environ.get("DEALMACHINE_API_KEY"):
        return
    cfg = Path.home() / ".dealmachine" / "config.json"
    if cfg.exists():
        try:
            if json.loads(cfg.read_text()).get("apiKey"):
                return
        except (json.JSONDecodeError, OSError):
            pass
    res = subprocess.run(["dm", "whoami", "--quiet"],
                        capture_output=True, text=True, timeout=30)
    out = (res.stdout or "") + (res.stderr or "")
    if "Org ID" not in out and "Organization" not in out:
        raise RuntimeError(
            "dm CLI is not authenticated. Run `dm login --key $DEALMACHINE_API_KEY`.\n"
            + out[:300]
        )


def dm_usage() -> dict:
    res = subprocess.run(["dm", "usage", "--json", "--quiet"],
                        capture_output=True, text=True, timeout=60)
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return {}


# Size-related gateway errors -> SPLIT the batch and retry smaller.
_SPLIT_ERRORS = ("504", "502", "413", "payload too large")
# Upstream-outage errors (DealMachine DB pool) -> do NOT split or hammer; leave
# the chunk for the next run. Splitting/retrying only amplifies an outage.
_OUTAGE_ERRORS = ("prisma", "Timed out fetching", "timed out fetching",
                  "ECONNRESET", "ECONNREFUSED", "socket hang up", "EAI_AGAIN",
                  "503", "ETIMEDOUT", "fetch failed", "ConnectTimeout",
                  "uncaughtException", "UND_ERR")


class APIDownError(RuntimeError):
    """Raised when the DealMachine API is down (circuit breaker tripped)."""


def _dm_enrich(kind: str, body: dict) -> dict:
    """kind in {'apn','address'}. Returns parsed JSON or raises."""
    res = subprocess.run(
        ["dm", "enrich", kind, "--body", json.dumps(body), "--json", "--quiet"],
        capture_output=True, text=True, timeout=120,
    )
    if res.returncode != 0:
        raise RuntimeError(f"dm enrich {kind} failed: {res.stderr.strip() or res.stdout.strip()}")
    return json.loads(res.stdout)


def api_healthy(probe_apn: str = "08-00392-0000-00015-11") -> bool:
    """One known-good APN -> True if it matches. Used to gate a pass."""
    try:
        resp = _dm_enrich("apn", {"data": [{"apn": probe_apn}],
                                  "location": {"type": "state", "code": "NJ"}})
        return bool(resp.get("data") and resp["data"][0].get("matched"))
    except Exception:
        return False


def _enrich_adaptive(kind: str, items: list[str], field: str, extra: dict,
                     results: dict, save) -> int:
    """Batch items through `dm enrich <kind>`, splitting any chunk that hits a
    transient (504 / DB-pool timeout) error down to MIN_BATCH.

    RESUMABLE: successful records (matched OR clean no_match) are written into
    `results` keyed by input value and CHECKPOINTED via save() after each batch,
    so a kill/crash never loses progress and a re-run skips already-fetched keys.
    Transient failures are NOT persisted -> retried on the next run. Returns the
    count of items that still failed (server-degraded) this run.
    """
    todo = [x for x in items if x not in results]            # resume: skip done
    failed = 0
    consec_outage = [0]                                       # circuit breaker

    def run_chunk(chunk: list[str]) -> None:
        nonlocal failed
        body = {"data": [{field: x} for x in chunk], **extra}
        try:
            resp = _dm_enrich(kind, body)
            for rec in resp.get("data", []):
                key = (rec.get("input") or {}).get(field)
                if key is not None:
                    results[key] = rec                       # persist success
            consec_outage[0] = 0
            time.sleep(RATE_LIMIT_SLEEP)
            return
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            msg = str(exc)
            if any(t in msg for t in _SPLIT_ERRORS) and len(chunk) > MIN_BATCH:
                mid = len(chunk) // 2                         # batch too big -> split
                run_chunk(chunk[:mid])
                run_chunk(chunk[mid:])
                return
            # outage (DB pool) or unsplittable failure: leave for the next run.
            failed += len(chunk)
            if any(t in msg for t in _OUTAGE_ERRORS):
                consec_outage[0] += 1
                if consec_outage[0] >= CIRCUIT_BREAK:         # API is down — stop
                    raise APIDownError(
                        f"{consec_outage[0]} consecutive outage errors — DealMachine "
                        f"API appears down. Aborting pass (resumable; re-run later).")
            print(f"  [warn] {kind} chunk failed (size {len(chunk)}): {msg[:90]}",
                  file=sys.stderr)
            return

    for i in range(0, len(todo), BATCH_SIZE):
        run_chunk(todo[i:i + BATCH_SIZE])
        save()                                                # checkpoint
    return failed


def enrich_apns(apns: list[str], results: dict, save) -> int:
    return _enrich_adaptive(
        "apn", apns, "apn",
        {"location": {"type": "state", "code": "NJ"},
         "include_contacts": True, "contact_audience": CONTACT_AUDIENCE},
        results, save,
    )


def enrich_addresses(addrs: list[str], results: dict, save) -> int:
    return _enrich_adaptive(
        "address", addrs, "full_address",
        {"include_contacts": True, "contact_audience": CONTACT_AUDIENCE},
        results, save,
    )


# ───────────────────────────────────────────────────── record -> lead block ──
PROPERTY_FIELDS = (
    "dm_property_id", "full_address", "address", "city", "state", "zip",
    "latitude", "longitude", "estimated_value", "estimated_equity_amount",
    "estimated_equity_percentage", "year_built", "living_area_sqft",
    "lot_size_sqft", "num_bedrooms", "num_bathrooms", "last_sale_date",
    "last_sale_amount", "total_assessed_value", "annual_property_tax_amount",
    "num_mortgages", "total_original_loan_amount", "total_estimated_loan_balance",
    "owner_occupied", "apn",
)


def build_block(rec: dict, matched_by: str, when: str, cycle: str) -> dict:
    """Translate a DealMachine record into the lead `dealmachine` block."""
    if not rec.get("matched"):
        # A clean response from DealMachine that found no record. This is a
        # GENUINE_NO_MATCH (DM simply doesn't carry this property) — NOT a failure,
        # and must NOT be retried on daily refresh.
        return {
            "source": SOURCE_TAG, "scope": SCOPE_TAG, "matched": False,
            "enrichment_status": "no_dm_record",
            "matched_by": matched_by, "enriched_at": when, "billing_cycle": cycle,
            "match_failure": rec.get("match_failure"),
        }
    contacts = rec.get("contacts") or []
    phones, emails, owner_names = [], [], []
    norm_contacts = []
    for c in contacts:
        cn_phones = [
            {"number": p.get("number"), "type": p.get("type"),
             "do_not_call": p.get("do_not_call")}
            for p in (c.get("phones") or [])
        ]
        cn_emails = [e.get("address") for e in (c.get("emails") or []) if e.get("address")]
        phones.extend(p["number"] for p in cn_phones if p.get("number"))
        emails.extend(cn_emails)
        if c.get("contact_type") == "owner" and c.get("full_name"):
            owner_names.append(c["full_name"])
        norm_contacts.append({
            "dm_person_id": c.get("dm_person_id"),
            "full_name": c.get("full_name"),
            "first_name": c.get("first_name"),
            "last_name": c.get("last_name"),
            "contact_type": c.get("contact_type"),
            "is_resident": c.get("is_resident"),
            "phones": cn_phones,
            "emails": cn_emails,
        })
    # de-dup while preserving order
    phones = list(dict.fromkeys(phones))
    emails = list(dict.fromkeys(emails))

    block = {
        "source": SOURCE_TAG, "scope": SCOPE_TAG, "matched": True,
        "enrichment_status": "enriched",
        "matched_by": matched_by, "enriched_at": when, "billing_cycle": cycle,
        "owner_name": owner_names[0] if owner_names else None,
        "owner_names": owner_names,
        "phones": phones,
        "emails": emails,
        "contacts": norm_contacts,
        # owner mailing address is not separately returned by this API; situs
        # address (== mailing when owner_occupied) recorded under property.
        "mailing_address": None,
        "property": {k: rec.get(k) for k in PROPERTY_FIELDS},
        "mortgages": rec.get("mortgages") or [],
        "credits": {"properties": 1, "people": len(contacts)},
    }
    if rec.get("owner_occupied") and rec.get("full_address"):
        block["mailing_address"] = rec.get("full_address")
    return block


def lead_hash(lead: dict) -> str:
    key = json.dumps({
        "parcel_id": lead.get("parcel_id"),
        "property_address": lead.get("property_address"),
        "property_zip": lead.get("property_zip"),
        "distress_signal": lead.get("distress_signal"),
        "source_ids": sorted(lead.get("source_ids") or []),
    }, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def mark_pending(lead: dict) -> None:
    """Lead's key never got a clean DealMachine response (API flapping). Mark it
    PENDING_RETRY so the daily refresh retries ONLY these (small-batch, when healthy).
    No block is cached, so the incremental gate keeps it in the retry set."""
    lead["dm_enrichment_status"] = "pending_retry"
    srcs = lead.setdefault("enrichment_sources", [])
    if SOURCE_TAG not in srcs:
        srcs.append(SOURCE_TAG)


def apply_to_lead(lead: dict, block: dict) -> dict:
    """Attach block; fill Daniel's-Law owner_name (labeled). Returns stats dict."""
    lead["dealmachine"] = block
    lead["dm_enrichment_status"] = block.get("enrichment_status") or (
        "enriched" if block.get("matched") else "no_dm_record")
    srcs = lead.setdefault("enrichment_sources", [])
    if SOURCE_TAG not in srcs:
        srcs.append(SOURCE_TAG)
    stats = {"daniels_filled": False}
    if not block.get("matched"):
        return stats
    dm_owner = block.get("owner_name")
    status = lead.get("owner_resolution_status")
    if dm_owner and (not lead.get("owner_resolved")) and status in DANIELS_LAW_STATUSES:
        lead["owner_name"] = dm_owner
        lead["owner_name_source"] = SOURCE_TAG
        lead["owner_resolved"] = True
        lead["owner_resolution_status_prior"] = status
        lead["owner_resolution_status"] = "DEALMACHINE_RESOLVED"
        lead["daniels_law_backfilled"] = True
        stats["daniels_filled"] = True
    elif dm_owner and not lead.get("owner_name"):
        lead["owner_name"] = dm_owner
        lead["owner_name_source"] = SOURCE_TAG
    return stats


# ───────────────────────────────────────────────────────────────────── main ──
def main() -> int:
    ap = argparse.ArgumentParser(description="DealMachine enrichment for Ocean NJ actionable leads")
    ap.add_argument("--include-probate", action="store_true",
                    help="ALSO enrich the 1,392 probate research targets (gated; spends credits)")
    ap.add_argument("--force", action="store_true",
                    help="Re-enrich even cached/unchanged leads (in-cycle dupes are free)")
    ap.add_argument("--limit", type=int, default=0, help="Cap number of leads (debug)")
    ap.add_argument("--dry-run", action="store_true", help="Plan only; no API calls, no writes")
    ap.add_argument("--apply-only", action="store_true",
                    help="Skip all fetching; just apply the existing results checkpoint to leads")
    args = ap.parse_args()

    leads_doc = json.loads(LEADS_PATH.read_text())
    leads = leads_doc["leads"]

    def is_actionable(l: dict) -> bool:
        return any(s in ACTIONABLE_SOURCES for s in (l.get("source_ids") or []))

    targets = [l for l in leads if is_actionable(l)]
    if args.include_probate:
        targets += [l for l in leads if PROBATE_SOURCE in (l.get("source_ids") or [])]
    if args.limit:
        targets = targets[:args.limit]

    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
    # Incremental: only NEW/changed leads hit the API. UNCHANGED leads that have
    # a cached block are RE-ATTACHED from cache (no credits) — build_leads
    # regenerates scored_leads.json each run, so we must re-stamp every actionable
    # lead every run, but only PAY for new/changed ones.
    todo, reattach = [], []
    for l in targets:
        h = lead_hash(l)
        prev = cache.get(l["lead_id"])
        if args.force or not prev or prev.get("hash") != h or not prev.get("block"):
            todo.append(l)
        else:
            reattach.append(l)

    print(f"actionable targets: {len(targets)} | new/changed to enrich (API): {len(todo)} "
          f"| re-attach from cache (free): {len(reattach)} | include_probate={args.include_probate}")

    # route each todo lead to APN or address
    situs = load_parcel_situs({l["parcel_id"] for l in todo if l.get("parcel_id")})
    apn_of: dict[str, str] = {}
    addr_of: dict[str, str] = {}
    unroutable = []
    for l in todo:
        apn = derive_apn(l.get("parcel_id"))
        if apn:
            apn_of[l["lead_id"]] = apn
            continue
        addr = situs_address(l, situs)
        if addr:
            addr_of[l["lead_id"]] = addr
        else:
            unroutable.append(l["lead_id"])

    uniq_apns = sorted(set(apn_of.values()))
    uniq_addrs = sorted(set(addr_of.values()))
    print(f"  routing: APN={len(apn_of)} leads ({len(uniq_apns)} unique) | "
          f"address={len(addr_of)} leads ({len(uniq_addrs)} unique) | unroutable={len(unroutable)}")

    if args.dry_run:
        print("DRY RUN — no API calls.")
        return 0

    ensure_cli()
    usage_before = dm_usage()
    print("credits before:", json.dumps(usage_before.get("credits", {}).get("breakdown", usage_before.get("credits", {}))))

    # Resumable raw-result checkpoint (survives kills + DealMachine API outages).
    results = json.loads(RESULTS_PATH.read_text()) if RESULTS_PATH.exists() else {}

    def save_results():
        RESULTS_PATH.write_text(json.dumps(results))

    # FETCH phase — health-gated. Skip hitting a down API, but ALWAYS fall through
    # to the APPLY phase so whatever is already checkpointed gets attached.
    aborted = False
    apn_failed = addr_failed = 0
    if args.apply_only:
        print("APPLY-ONLY: skipping fetch; attaching existing checkpoint.")
    elif not api_healthy():
        api_down = True
        print("::warning:: DealMachine API health probe FAILED (server-side outage / "
              "DB-pool timeouts). Skipping fetch — applying any checkpointed results; "
              "resumable, re-run when healthy.", file=sys.stderr)
    else:
        api_down = False
        try:
            apn_failed = enrich_apns(uniq_apns, results, save_results) if uniq_apns else 0
            addr_failed = enrich_addresses(uniq_addrs, results, save_results) if uniq_addrs else 0
        except APIDownError as exc:
            aborted = True
            print(f"::warning:: {exc}", file=sys.stderr)
    save_results()
    if aborted:
        print("  [warn] pass aborted by circuit breaker — partial results checkpointed; "
              "re-run when the API recovers.", file=sys.stderr)
    elif apn_failed or addr_failed:
        print(f"  [warn] still-failing (DealMachine API degraded): APN={apn_failed} "
              f"address={addr_failed} — re-run to retry (errors not cached)", file=sys.stderr)

    apn_results = {a: results[a] for a in uniq_apns if a in results}
    addr_results = {a: results[a] for a in uniq_addrs if a in results}

    when = datetime.now(timezone.utc).isoformat()
    cycle = (usage_before.get("billing_cycle") or {}).get("start", "")
    by_id = {l["lead_id"]: l for l in leads}

    stats = {"matched": 0, "unmatched": 0, "owner_recovered": 0, "daniels_filled": 0,
             "contacts": 0, "phones": 0, "emails": 0, "reattached": 0, "pending_retry": 0}
    prov_lines = []
    daniels_munis = {}

    def tally(lead: dict, block: dict, daniels_filled: bool):
        if block.get("matched"):
            stats["matched"] += 1
            if block.get("owner_name"):
                stats["owner_recovered"] += 1
            stats["contacts"] += len(block.get("contacts") or [])
            stats["phones"] += len(block.get("phones") or [])
            stats["emails"] += len(block.get("emails") or [])
        else:
            stats["unmatched"] += 1
        if daniels_filled:
            stats["daniels_filled"] += 1
            muni = lead.get("parcel_muni") or lead.get("property_city") or "?"
            daniels_munis[muni] = daniels_munis.get(muni, 0) + 1

    def record(lead_id: str, rec: dict, matched_by: str):
        lead = by_id[lead_id]
        block = build_block(rec, matched_by, when, cycle)
        s = apply_to_lead(lead, block)
        tally(lead, block, s["daniels_filled"])
        # Cache matched + clean no_match (final); do NOT cache api_error stubs so
        # they are retried on the next run instead of being treated as done.
        is_api_error = (not block.get("matched")
                        and (block.get("match_failure") or {}).get("code") == "api_error")
        if not is_api_error:
            cache[lead_id] = {"hash": lead_hash(lead), "enriched_at": when, "block": block}
        prov_lines.append(json.dumps({"lead_id": lead_id, "matched_by": matched_by,
                                      "dealmachine": block}))

    pending = 0
    for lid, apn in apn_of.items():
        rec = apn_results.get(apn)
        if rec is not None:
            record(lid, rec, "apn")
        else:                                    # no clean response -> PENDING_RETRY
            mark_pending(by_id[lid]); pending += 1
    for lid, addr in addr_of.items():
        rec = addr_results.get(addr)
        if rec is not None:
            record(lid, rec, "address")
        else:
            mark_pending(by_id[lid]); pending += 1
    stats["pending_retry"] = pending

    # Re-attach cached blocks to unchanged leads (no API spend). Keeps DM
    # enrichment intact after build_leads regenerates scored_leads.json.
    for lead in reattach:
        block = cache[lead["lead_id"]]["block"]
        s = apply_to_lead(lead, block)
        tally(lead, block, s["daniels_filled"])
        stats["reattached"] += 1

    # Owner-residency signals for absentee / out-of-state dashboard filters.
    stamp_residency([l for l in leads if any(s in ACTIONABLE_SOURCES
                                             for s in (l.get("source_ids") or []))])

    usage_after = dm_usage()

    # persist
    leads_doc["dealmachine_enrichment"] = {
        "enriched_at": when, "scope": SCOPE_TAG, "source": SOURCE_TAG,
        "actionable_targets": len(targets),
        "api_calls_this_run": len(todo), "reattached_from_cache": stats["reattached"],
        "enriched": stats["matched"],
        "genuine_no_match": stats["unmatched"],     # DM has no record — do NOT retry
        "pending_retry": stats["pending_retry"],    # API flapped — retry these only
        "owner_names_recovered": stats["owner_recovered"],
        "daniels_law_backfilled": stats["daniels_filled"],
        "contacts_attached": stats["contacts"],
        "phones_attached": stats["phones"], "emails_attached": stats["emails"],
        "credits_before": usage_before.get("credits", {}),
        "credits_after": usage_after.get("credits", {}),
    }
    LEADS_PATH.write_text(json.dumps(leads_doc, indent=1))
    CACHE_PATH.write_text(json.dumps(cache, indent=1))
    # Comprehensive per-field provenance ledger: one line per actionable lead that
    # carries a DealMachine block (matched or no_dm_record), regardless of whether it
    # was fetched this run or re-attached from cache. Overwrite (idempotent).
    with PROVENANCE_PATH.open("w") as fh:
        for lead in targets:
            blk = lead.get("dealmachine")
            if blk:
                fh.write(json.dumps({"lead_id": lead["lead_id"],
                                     "matched_by": blk.get("matched_by"),
                                     "dealmachine": blk}) + "\n")

    used_before = usage_before.get("credits", {}).get("used")
    used_after = usage_after.get("credits", {}).get("used")
    print(json.dumps({
        "enriched": stats["matched"],
        "genuine_no_match": stats["unmatched"],
        "pending_retry": stats["pending_retry"],
        "reattached_from_cache": stats["reattached"],
        "owner_names_recovered": stats["owner_recovered"],
        "daniels_law_backfilled": stats["daniels_filled"],
        "daniels_munis": daniels_munis,
        "contacts": stats["contacts"], "phones": stats["phones"], "emails": stats["emails"],
        "credits_used_before": used_before, "credits_used_after": used_after,
        "credits_consumed": (used_after - used_before) if (used_before is not None and used_after is not None) else None,
        "remaining_unresolved_keys": (len(uniq_apns) + len(uniq_addrs)) - len(results),
    }, indent=1))
    # exit code: 0 = all unique keys resolved; 4 = some still failing (re-run)
    unresolved = (len(uniq_apns) + len(uniq_addrs)) - len(results)
    return 4 if (aborted or apn_failed or addr_failed or unresolved > 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
