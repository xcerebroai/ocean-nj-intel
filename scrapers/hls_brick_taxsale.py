"""HLS Systems — Brick Township tax-sale list (S7c).

Source       : Brick Township, Ocean County, NJ — tax collector
Portal       : https://apps.hlssystems.com/Brick/TaxsaleList
Backend API  : https://apps.hlssystems.com/Brick/TaxsaleList/GetOpenTaxsaleItems
Source role  : PRIMARY_DEFAULT_SOURCE (v5.5.0 §0.1) — the municipal
               tax-sale record-of-truth for parcels with delinquent
               taxes / municipal liens.

Daniel's Law applicability
--------------------------
HLS Brick exposes the OwnerName field directly in the API response
(verified 2026-05-26: rows carry real owner names like "SHAFER, ANGELA R
& ALLIE R").  The Daniel's-Law redaction that blocks NJOGIS and the
Ocean County Tax Board does NOT apply at the HLS-managed muni layer.
For Brick parcels we therefore have OWNER as well as delinquency.

§3.3 five-criteria qualification
--------------------------------
Per v5.5.0 §3.3, each row maps to a tax-default qualification:

  (a) Tax-default record exists                   — TaxAmount + MUAAmount
  (b) Delinquency amount is non-trivial           — TotalDue threshold
  (c) Lien holder identified (sold/struck-off)    — InvestorName populated
  (d) Property is identifiable                    — BlockLot + PropertyLocation
  (e) Owner is identifiable                       — OwnerName populated

Rows that satisfy a-e are TAX_DEFAULT QUALIFIED.  Rows missing (c) (no
investor — these are still on the open advertisement list but not yet
sold) are TAX_DEFAULT_OPEN_NOTICE; rows missing (b) (sub-$100 total
due) are tax_default_low_priority per §3.3 / §5.6.

Output (MASTER_PROMPT §4.32)
----------------------------
data/raw/hls_brick_taxsale.jsonl — one record per AccountNumber.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

SOURCE_ID = "hls_brick_taxsale"
PORTAL_URL = "https://apps.hlssystems.com/Brick/TaxsaleList"
API_URL = "https://apps.hlssystems.com/Brick/TaxsaleList/GetOpenTaxsaleItems"
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "raw" / f"{SOURCE_ID}.jsonl"

# §3.3 qualification thresholds.  Adjustable per operator policy.
DEFAULT_LOW_PRIORITY_THRESHOLD_USD = 100.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_taxsale_items(timeout: int = 30) -> dict:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 "
                       "ocean-nj/v5.5.0"),
    })
    s.get(PORTAL_URL, timeout=timeout)   # seed AWS ALB + ASP.NET cookies
    r = s.get(API_URL, headers={"Accept": "application/json", "Referer": PORTAL_URL},
              timeout=timeout)
    r.raise_for_status()
    return r.json()


def _classify(item: dict, *, low_priority_threshold: float) -> tuple[str, str, list[str]]:
    """Return (qualification_status, lead_type, evidence_list) per §3.3."""
    total_due = float(item.get("TotalDue") or 0)
    owner = (item.get("OwnerName") or "").strip()
    block_lot = (item.get("BlockLot") or "").strip()
    prop_loc = (item.get("PropertyLocation") or "").strip()
    investor = (item.get("InvestorName") or "").strip()

    evidence: list[str] = []
    if total_due > 0:
        evidence.append("a_tax_default_record_exists")
    if total_due >= low_priority_threshold:
        evidence.append("b_delinquency_amount_nontrivial")
    if investor:
        evidence.append("c_lien_holder_identified")
    if block_lot and prop_loc:
        evidence.append("d_property_identifiable")
    if owner:
        evidence.append("e_owner_identifiable")

    qualified = (len(evidence) >= 4 and
                 "a_tax_default_record_exists" in evidence and
                 "d_property_identifiable" in evidence)
    if not qualified:
        return "NOT_QUALIFIED", "review_required", evidence

    if investor:
        # Sold to a lien holder — this is the canonical "tax_certificate" /
        # "tax_sale" outcome.
        return "QUALIFIED", "tax_certificate", evidence
    if total_due < low_priority_threshold:
        return "QUALIFIED", "tax_default_low_priority", evidence
    return "QUALIFIED", "tax_default", evidence


def _wrap(item: dict, *, fetched_at: str, public_notice: Optional[str],
          sale_date_iso: Optional[str], low_priority_threshold: float) -> dict:
    acct = item.get("AccountNumber")
    if acct is None:
        return None  # type: ignore[return-value]
    raw_record_id = f"{SOURCE_ID}:{acct}"
    qual, lead_type, evidence = _classify(item, low_priority_threshold=low_priority_threshold)
    # Parse block/lot into pieces — Brick uses "BLOCK LOT QUAL" with spaces.
    parts = (item.get("BlockLot") or "").strip().split()
    block, lot, qual_code = "", "", ""
    if len(parts) >= 1: block = parts[0]
    if len(parts) >= 2: lot = parts[1]
    if len(parts) >= 3: qual_code = " ".join(parts[2:])

    payload = {
        "account_number": acct,
        "block": block,
        "lot": lot,
        "qual_code": qual_code,
        "block_lot_raw": item.get("BlockLot"),
        "owner_name": (item.get("OwnerName") or "").strip(),
        "property_location": (item.get("PropertyLocation") or "").strip(),
        "tax_amount": item.get("TaxAmount"),
        "mua_amount": item.get("MUAAmount"),
        "cost_amount": item.get("CostAmount"),
        "total_due": item.get("TotalDue"),
        "investor_name": (item.get("InvestorName") or "").strip() or None,
        "muni_name": "Brick Township",
        "muni_county": "Ocean",
        "muni_state": "NJ",
        "sale_date": sale_date_iso,
        "public_notice_excerpt": (public_notice or "")[:500],
        "primary_event_date": sale_date_iso,
        "doc_type": ("tax_certificate" if lead_type == "tax_certificate"
                     else "tax_default"),
        "qualification_status": qual,
        "qualification_evidence": evidence,
        "lead_type": lead_type,
    }
    return {
        "raw_record_id": raw_record_id,
        "source_id": SOURCE_ID,
        "source_url": PORTAL_URL,
        "source_fetched_at": fetched_at,
        "parser_confidence": 95,
        "source_role": "PRIMARY_DEFAULT_SOURCE",
        "lead_origin_type": "TAX_DEFAULT",
        "raw_payload": payload,
    }


_SALE_DATE_RE = re.compile(
    r"on the (\d{1,2})(?:st|nd|rd|th)?\s+day of\s+"
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)[,]?\s+(\d{4})",
    re.I,
)
_MONTH = {m.lower(): i for i, m in enumerate(
    ["", "January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}


def _parse_sale_date(public_notice: Optional[str]) -> Optional[str]:
    if not public_notice:
        return None
    m = _SALE_DATE_RE.search(public_notice)
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), _MONTH[m.group(2).lower()],
                        int(m.group(1))).date().isoformat()
    except (KeyError, ValueError):
        return None


def run(out_path: Path = OUTPUT_PATH,
        low_priority_threshold: float = DEFAULT_LOW_PRIORITY_THRESHOLD_USD) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fetched_at = _now_iso()
    try:
        data = fetch_taxsale_items()
    except Exception as exc:
        print(f"[{SOURCE_ID}] fetch failure: {exc}", file=sys.stderr)
        return 0  # PRIMARY_DEFAULT for one muni — non-fatal

    vm = data.get("taxsaleListVM", {})
    items = vm.get("TaxsaleLists", []) or []
    public_notice = vm.get("PublicNotice")
    sale_date_iso = _parse_sale_date(public_notice)
    grand_total = vm.get("GrandTotal")

    wrapped = []
    for item in items:
        w = _wrap(item, fetched_at=fetched_at, public_notice=public_notice,
                  sale_date_iso=sale_date_iso,
                  low_priority_threshold=low_priority_threshold)
        if w is not None:
            wrapped.append(w)

    with out_path.open("w", encoding="utf-8") as fh:
        for w in wrapped:
            fh.write(json.dumps(w, ensure_ascii=False) + "\n")

    qual_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for w in wrapped:
        qual_counts[w["raw_payload"]["qualification_status"]] = \
            qual_counts.get(w["raw_payload"]["qualification_status"], 0) + 1
        type_counts[w["raw_payload"]["lead_type"]] = \
            type_counts.get(w["raw_payload"]["lead_type"], 0) + 1
    print(f"[{SOURCE_ID}] records_written={len(wrapped)} "
          f"sale_date={sale_date_iso} grand_total=${grand_total:,.2f} "
          f"qualification={qual_counts} types={type_counts}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=str(OUTPUT_PATH))
    p.add_argument("--low-priority-threshold", type=float,
                   default=DEFAULT_LOW_PRIORITY_THRESHOLD_USD)
    args = p.parse_args()
    return run(out_path=Path(args.out),
               low_priority_threshold=args.low_priority_threshold)


if __name__ == "__main__":
    raise SystemExit(main())
