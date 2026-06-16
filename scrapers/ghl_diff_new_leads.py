#!/usr/bin/env python3
"""Ocean NJ — net-new actionable-lead diff for the direct GoHighLevel push.

CLIENT-SPECIFIC, OCEAN-ONLY. Not framework canon (no other county inherits it).

Reads the freshly built data/leads/scored_leads.json, selects ACTIONABLE leads
(S1 sheriff foreclosures + S7c HLS Brick tax-default; probate is gated OUT,
mirroring scrapers/dealmachine_enrich.py), keeps only those with at least one
phone or email, and emits the ones whose key has NEVER been pushed to GHL.

  key = <parcel_id or block-lot> | <normalized chancery docket>

The pushed-key ledger lives at data/exports/pushed_keys.json and is updated
ONLY by ghl_push.py after a 2xx from GHL — so a lead that fails to push stays
out of the ledger and is re-emitted (retried) next run.

Output: data/exports/new_leads_latest.json — a flat, GHL-ready list:
  first_name, last_name (entity name -> first_name), property_address,
  property_city, property_state, property_zip, phone_1..phone_3,
  email_1..email_2, lead_type, docket  (+ internal `key` for the ledger).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCORED = REPO / "data" / "leads" / "scored_leads.json"
EXPORT_DIR = REPO / "data" / "exports"
LEDGER = EXPORT_DIR / "pushed_keys.json"
OUT = EXPORT_DIR / "new_leads_latest.json"

# Canonical actionable source-of-record set (see dealmachine_enrich.py:59).
# Probate (surrogate_probate) is intentionally excluded.
ACTIONABLE_SOURCES = ("sheriff_foreclosure", "hls_brick_taxsale")

# distress_signal -> human label (mirrors ocean_nj_render_dashboard.py:64).
LEAD_TYPE_LABELS = {
    "foreclosure_sale_scheduled": "Sheriff Foreclosure",
    "foreclosure_notice_published": "Foreclosure Notice",
    "tax_default_brick": "Tax Default",
    "probate_filing_recent": "Probate",
}

# Tokens that mark an owner string as an ENTITY (-> whole name into first_name).
_ENTITY_RE = re.compile(
    r"\b(LLC|L\.L\.C|INC|INCORPORATED|CORP|CORPORATION|COMPANY|CO|LP|LLP|"
    r"TRUST|TRUSTEE|ESTATE|BANK|N\.A|ASSOCIATION|ASSN|FUND|PARTNERS|"
    r"HOLDINGS|PROPERTIES|REALTY|MANAGEMENT|MORTGAGE|CHURCH|CITY|TOWNSHIP|"
    r"BOROUGH|COUNTY|AUTHORITY|DEPT|DEPARTMENT)\b",
    re.IGNORECASE,
)


def is_actionable(lead: dict) -> bool:
    return any(s in ACTIONABLE_SOURCES for s in (lead.get("source_ids") or []))


def parcel_key(lead: dict) -> str:
    pid = (lead.get("parcel_id") or "").strip()
    if pid:
        return pid
    block = (lead.get("block") or "").strip()
    lot = (lead.get("lot") or "").strip()
    return f"{block}-{lot}" if (block or lot) else ""


def lead_key(lead: dict) -> str:
    docket = re.sub(r"\s+", "", (lead.get("chancery_docket") or "")).upper()
    return f"{parcel_key(lead)}|{docket}"


def normalize_phone(raw) -> str | None:
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None  # drop malformed / non-US numbers


def normalize_email(raw) -> str | None:
    e = str(raw or "").strip().lower()
    return e if "@" in e and "." in e.split("@")[-1] else None


def split_name(full: str) -> tuple[str, str]:
    """(first_name, last_name). Entities -> whole string into first_name."""
    full = re.sub(r"\s+", " ", (full or "").strip())
    if not full:
        return "", ""
    # DealMachine person names arrive "FIRST LAST"; legal owner strings can be
    # "LAST, FIRST" — handle both. Entities go whole into first_name.
    if _ENTITY_RE.search(full) or "," in full and _ENTITY_RE.search(full):
        return full.title(), ""
    if "," in full:  # "MILLER, PATRICIA" -> first=PATRICIA last=MILLER
        last, _, first = full.partition(",")
        first = first.strip().split(" ")[0]
        return first.title(), last.strip().title()
    parts = full.split(" ")
    if len(parts) == 1:
        return parts[0].title(), ""
    return parts[0].title(), " ".join(parts[1:]).title()


def pick_name(lead: dict) -> tuple[str, str]:
    dm = lead.get("dealmachine") or {}
    # 1) Prefer a DealMachine owner-contact with a clean first/last.
    contacts = dm.get("contacts") or []
    owners = [c for c in contacts if c.get("contact_type") == "owner"]
    for c in owners + contacts:
        fn = (c.get("first_name") or "").strip()
        ln = (c.get("last_name") or "").strip()
        if fn or ln:
            return fn.title(), ln.title()
    # 2) Fall back to DealMachine / lead owner strings, then defendant.
    for cand in (dm.get("owner_name"), (dm.get("owner_names") or [None])[0],
                 lead.get("owner_name"), lead.get("defendant_name")):
        if cand:
            return split_name(cand)
    return "", ""


def pick_address(lead: dict) -> tuple[str, str, str, str]:
    """(address, city, state, zip). Prefer DealMachine's normalized situs
    address — the sheriff source mis-splits two-word towns (e.g. "TOMS RIVER"
    -> address "...TOMS" / city "RIVER"); DealMachine carries it clean."""
    p = (lead.get("dealmachine") or {}).get("property") or {}
    addr = (p.get("address") or "").strip() or (lead.get("property_address") or "").strip()
    city = (p.get("city") or "").strip() or (lead.get("property_city") or "").strip()
    state = (p.get("state") or "").strip() or (lead.get("property_state") or "NJ").strip()
    zipc = (p.get("zip") or "").strip() or (lead.get("property_zip") or "").strip()
    return addr, city, state, zipc


def collect_phones(lead: dict) -> list[str]:
    dm = lead.get("dealmachine") or {}
    out: list[str] = []
    for raw in (dm.get("phones") or []):
        p = normalize_phone(raw)
        if p and p not in out:
            out.append(p)
    return out


def collect_emails(lead: dict) -> list[str]:
    dm = lead.get("dealmachine") or {}
    out: list[str] = []
    for raw in (dm.get("emails") or []):
        e = normalize_email(raw)
        if e and e not in out:
            out.append(e)
    return out


def load_ledger() -> set[str]:
    if not LEDGER.exists():
        return set()
    try:
        data = json.loads(LEDGER.read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    if isinstance(data, dict):
        return set(data.get("pushed_keys") or [])
    if isinstance(data, list):  # tolerate a bare list
        return set(data)
    return set()


def to_export(lead: dict, key: str, phones: list[str], emails: list[str]) -> dict:
    first, last = pick_name(lead)
    addr, city, state, zipc = pick_address(lead)
    phones = phones[:3]
    emails = emails[:2]
    rec = {
        "first_name": first,
        "last_name": last,
        "property_address": addr,
        "property_city": city,
        "property_state": state or "NJ",
        "property_zip": zipc,
        "phone_1": phones[0] if len(phones) > 0 else "",
        "phone_2": phones[1] if len(phones) > 1 else "",
        "phone_3": phones[2] if len(phones) > 2 else "",
        "email_1": emails[0] if len(emails) > 0 else "",
        "email_2": emails[1] if len(emails) > 1 else "",
        "lead_type": LEAD_TYPE_LABELS.get(lead.get("distress_signal"),
                                          lead.get("distress_signal") or ""),
        "docket": lead.get("chancery_docket") or "",
        "key": key,  # internal — consumed by ghl_push.py for the ledger
    }
    return rec


def main() -> int:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    blob = json.loads(SCORED.read_text())
    leads = blob.get("leads") or []
    ledger = load_ledger()

    actionable = [l for l in leads if is_actionable(l)]
    by_key: dict[str, dict] = {}
    contactful = 0
    for lead in actionable:
        phones = collect_phones(lead)
        emails = collect_emails(lead)
        if not phones and not emails:
            continue  # must have at least one phone or email
        contactful += 1
        key = lead_key(lead)
        if not key or key == "|":
            continue  # no stable key -> cannot ledger; skip
        if key in ledger:
            continue  # already pushed in a prior run
        rec = to_export(lead, key, phones, emails)
        # Dedup within this run by key; keep the record with the most contacts.
        prev = by_key.get(key)
        if prev is None or _contact_count(rec) > _contact_count(prev):
            by_key[key] = rec

    new_leads = list(by_key.values())
    OUT.write_text(json.dumps(new_leads, indent=2))

    print(f"actionable leads:            {len(actionable)}")
    print(f"  with >=1 phone or email:   {contactful}")
    print(f"  already in ledger:         {len(ledger)} keys")
    print(f"NET-NEW contactable leads:   {len(new_leads)}  -> {OUT.relative_to(REPO)}")
    return 0


def _contact_count(rec: dict) -> int:
    return sum(1 for k in ("phone_1", "phone_2", "phone_3", "email_1", "email_2") if rec.get(k))


if __name__ == "__main__":
    raise SystemExit(main())
