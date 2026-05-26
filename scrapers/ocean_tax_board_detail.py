"""Ocean County Tax Board — parcel detail enrichment (S8 detail).

Source       : tax.co.ocean.nj.us (Ocean County Board of Taxation)
Portal       : https://tax.co.ocean.nj.us/frmTaxBoardTaxListSearch
Detail       : https://tax.co.ocean.nj.us/frmTaxBoardTaxListDetail
Source role  : ENRICHMENT_SOURCE.

Daniel's Law applicability
--------------------------
The page redacts OWNER NAME (commented banner: "Ownership names will no
longer be available online due to pending Daniel's Law redactions").
But it still exposes the OWNER'S MAILING ADDRESS (which can differ from
the property location — that's the absentee/out-of-state signal), deed
reference (book/page), deed date, sale price + history, assessment
breakdown, year built, and property characteristics.

For Ocean County, this closes the §3.7 enrichment join on every field
except owner name — and the mailing-address-vs-property-address split
is a load-bearing signal for the §5.4 'absentee_or_out_of_state'
dashboard filter.

Workflow
--------
For each input parcel (block, lot, qualifier, district_code), drive
the search form, click through to the detail page, and parse the
label-value text block into canonical enrichment fields.

Output (MASTER_PROMPT §4.32)
----------------------------
data/enriched/ocean_tax_board_detail.jsonl — one record per parcel.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Iterable, Optional

import requests

SOURCE_ID = "ocean_tax_board_detail"
PORTAL_SEARCH = "https://tax.co.ocean.nj.us/frmTaxBoardTaxListSearch"
PORTAL_DETAIL = "https://tax.co.ocean.nj.us/frmTaxBoardTaxListDetail"
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "enriched" / "ocean_tax_board_detail.jsonl"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 ocean-nj/v5.5.0")


# District-code map — derived from the live search page dropdown.
DISTRICT_CODES = {
    "BARNEGAT": "1", "BARNEGAT LIGHT": "2", "BAY HEAD": "3",
    "BEACH HAVEN": "4", "BEACHWOOD": "5", "BERKELEY": "6",
    "BRICK": "7", "TOMS RIVER": "8", "EAGLESWOOD": "9",
    "HARVEY CEDARS": "10", "ISLAND HEIGHTS": "11", "JACKSON": "12",
    "LACEY": "13", "LAKEHURST": "14", "LAKEWOOD": "15",
    "LAVALLETTE": "16", "LITTLE EGG HARBOR": "17", "LONG BEACH": "18",
    "MANCHESTER": "19", "MANTOLOKING": "20",
    "OCEAN": "21", "OCEAN GATE": "22", "PINE BEACH": "23",
    "PLUMSTED": "24", "POINT PLEASANT": "25", "POINT PLEASANT BEACH": "26",
    "SEASIDE HEIGHTS": "27", "SEASIDE PARK": "28", "SHIP BOTTOM": "29",
    "SOUTH TOMS RIVER": "30", "STAFFORD": "31", "SURF CITY": "32",
    "TUCKERTON": "33",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_muni(s: str) -> Optional[str]:
    if not s:
        return None
    key = s.strip().upper().replace(" TWP", "").replace(" TOWNSHIP", "")\
                          .replace(" BOROUGH", "").replace(" BORO", "")\
                          .replace(" CITY", "")
    return DISTRICT_CODES.get(key)


# ---------------------------------------------------------------------------
# Session + search → detail
# ---------------------------------------------------------------------------


def open_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
    })
    s.get(PORTAL_SEARCH, timeout=30)
    return s


def _grab_hidden(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(
        r'<input[^>]+name="(__\w+|ctl00\$[^"]+)"[^>]*value="([^"]*)"', html
    ):
        out[m.group(1)] = unescape(m.group(2))
    return out


def fetch_detail(session: requests.Session, *, district_code: str,
                 block: str, lot: str, qualifier: str = "") -> Optional[str]:
    """Drive the search → detail path via two ASP.NET WebForms postbacks.

    Returns the detail-page HTML, or None on failure.
    """
    r = session.get(PORTAL_SEARCH, timeout=30)
    inputs = _grab_hidden(r.text)
    payload = dict(inputs)
    payload["__EVENTTARGET"] = ""
    payload["__EVENTARGUMENT"] = ""
    payload["ctl00$MainContent$cmbDistrict"] = district_code
    payload["ctl00$MainContent$txtBlock"] = block
    payload["ctl00$MainContent$txtLot"] = lot
    payload["ctl00$MainContent$txtQual"] = qualifier or ""
    payload["ctl00$MainContent$txtOwner"] = ""
    payload["ctl00$MainContent$txtStreet"] = ""
    payload["ctl00$MainContent$cmbPropClass"] = " "
    payload["ctl00$MainContent$btnSearch"] = "Search"
    r2 = session.post(PORTAL_SEARCH, data=payload,
                      headers={"Referer": PORTAL_SEARCH}, timeout=45)
    # Result table emits an anchor whose target is the detail page.
    # The simpler path: just fetch the detail page on the same session.
    r3 = session.get(PORTAL_DETAIL, timeout=30,
                     headers={"Referer": PORTAL_SEARCH})
    if r3.status_code != 200 or "Property Details" not in r3.text \
       and "Block" not in r3.text:
        return None
    return r3.text


# ---------------------------------------------------------------------------
# Parse detail HTML — label/value harvest
# ---------------------------------------------------------------------------


def _flat_text(html: str) -> str:
    s = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


_LABEL_RE = lambda label: re.compile(
    rf"{re.escape(label)}\s*:?\s*([^A-Z][^|]*?)(?=\s+[A-Z][a-z]+\s*:|\s+Block|\s+Lot|\s+Mailing|\s+Owner|\s+Sale|\s+Year|\s+Land|\s+Improvement|\s+Net|\s+Class|\s+Property|\s+Assessment|\s+Map|\s+Zone|\s+Exemption|\s+Statue|\s+Facility|$)",
    re.I,
)


def _parse_detail(html: str) -> dict:
    text = _flat_text(html)
    fields: dict[str, object] = {}

    def grab(label: str) -> Optional[str]:
        m = re.search(rf"{re.escape(label)}\s*:?\s*([^|]+?)(?=\s+(?:Block|Lot|Qual|Mailing|City/State|Location|Prop class|Land val|Bldg desc|Improvement val|Land desc|Exemption|Addtl lots|Zone|Map|Year blt|Net value|Book/page|Last yr taxes|Sale price|Prev block|Prev lot|Spcl tax codes|Prev qual|Exmt Prop Code|Init/Fur|Statue|Facility|Assessment|Property Details|Type/use|Story hgt|Design|Roof|Ext Finish|Foundation|Basement|Heating|Heat system|Electric|A/C|Plumbing|Fireplace|SFLA|Attic|Unf area|# bed|# bath|Attchd|Detchd|Sale History|Year|Deed date|Prop loc|Grantee|Grantor|Sales price|Rec date|Mnchstr)\s*:|$)",
                      text, re.I)
        return m.group(1).strip() if m else None

    fields["municipality"] = grab("Municipality")
    fields["deed_date"] = grab("Deed date")
    fields["block_lot"] = grab("Block / Lot") or grab("Block/Lot")
    fields["qualifier"] = grab("Qual")
    fields["mailing_address"] = grab("Mailing address")
    fields["mailing_city_state"] = grab("City/State")
    fields["property_location"] = grab("Location") or grab("Prop loc")
    fields["prop_class"] = grab("Prop class")
    fields["land_value"] = grab("Land val")
    fields["bldg_description"] = grab("Bldg desc")
    fields["improvement_value"] = grab("Improvement val")
    fields["land_description"] = grab("Land desc")
    fields["zone"] = grab("Zone")
    fields["year_built"] = grab("Year blt")
    fields["net_value"] = grab("Net value")
    fields["deed_book_page"] = grab("Book/page")
    fields["last_yr_taxes"] = grab("Last yr taxes")
    fields["last_sale_price"] = grab("Sale price")
    fields["type_use"] = grab("Type/use")
    fields["bedrooms"] = grab("# bedrooms")
    fields["bathrooms"] = grab("# bathrooms")

    # Mailing-vs-property absentee signal.
    ml = (fields.get("mailing_address") or "").upper().strip()
    pl = (fields.get("property_location") or "").upper().strip()
    fields["absentee_owner"] = bool(ml and pl and ml != pl)
    # Out-of-state if mailing city/state doesn't end in 'NJ'
    cs = (fields.get("mailing_city_state") or "").upper().strip()
    fields["out_of_state_owner"] = bool(cs and " NJ " not in f" {cs} " and not cs.endswith(" NJ"))

    return fields


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def enrich_parcels(parcels: Iterable[dict], *, sleep_between: float = 0.25,
                   limit: Optional[int] = None) -> list[dict]:
    """Each input parcel is a dict with keys: muni, block, lot, qualifier (optional)."""
    session = open_session()
    out: list[dict] = []
    count = 0
    fetched_at = _now_iso()
    for p in parcels:
        if limit and count >= limit:
            break
        muni = p.get("muni") or p.get("city")
        district = _norm_muni(muni or "")
        block = (p.get("block") or "").strip()
        lot = (p.get("lot") or "").strip()
        qual = (p.get("qual") or p.get("qualifier") or "").strip()
        if not (district and block and lot):
            continue
        try:
            html = fetch_detail(session, district_code=district,
                                block=block, lot=lot, qualifier=qual)
        except Exception:
            html = None
        if not html:
            continue
        fields = _parse_detail(html)
        # Skip empty parses
        if not fields.get("mailing_address") and not fields.get("property_location"):
            continue
        record_id = f"{SOURCE_ID}:{district}_{block}_{lot}_{qual or 'n'}"
        wrapped = {
            "raw_record_id": record_id,
            "source_id": SOURCE_ID,
            "source_url": PORTAL_DETAIL,
            "source_fetched_at": fetched_at,
            "parser_confidence": 78,
            "source_role": "ENRICHMENT_SOURCE",
            "raw_payload": {
                "muni": muni,
                "district_code": district,
                "block": block,
                "lot": lot,
                "qualifier": qual,
                **fields,
                "owner_name_redacted_under_daniels_law": True,
            },
        }
        out.append(wrapped)
        count += 1
        if sleep_between:
            time.sleep(sleep_between)
    return out


def run(out_path: Path = OUTPUT_PATH, limit: Optional[int] = None,
        input_parcels_jsonl: Optional[Path] = None) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    parcels: list[dict] = []
    if input_parcels_jsonl and input_parcels_jsonl.exists():
        for line in input_parcels_jsonl.open("r", encoding="utf-8"):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = rec.get("raw_payload") or rec
            parcels.append(p)
    else:
        # Default — enrich the sheriff foreclosure leads (smallest, highest value)
        sheriff_path = REPO_ROOT / "data" / "raw" / "sheriff_foreclosure.jsonl"
        for line in sheriff_path.open("r", encoding="utf-8"):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = rec["raw_payload"]
            first_lot = (p.get("lot") or "").split(",")[0].split("-")[0].strip()
            parcels.append({
                "muni": p.get("city"),
                "block": p.get("block"),
                "lot": first_lot,
                "qualifier": "",
            })

    enriched = enrich_parcels(parcels, limit=limit)

    with out_path.open("w", encoding="utf-8") as fh:
        for r in enriched:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    absentee = sum(1 for r in enriched if r["raw_payload"].get("absentee_owner"))
    oos = sum(1 for r in enriched if r["raw_payload"].get("out_of_state_owner"))
    print(f"[{SOURCE_ID}] parcels_enriched={len(enriched)} "
          f"absentee_owners={absentee} out_of_state={oos}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=str(OUTPUT_PATH))
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--input-parcels", default=None,
                   help="JSONL of parcels to enrich (default: sheriff_foreclosure.jsonl)")
    args = p.parse_args()
    return run(out_path=Path(args.out),
               limit=args.limit,
               input_parcels_jsonl=Path(args.input_parcels) if args.input_parcels else None)


if __name__ == "__main__":
    raise SystemExit(main())
