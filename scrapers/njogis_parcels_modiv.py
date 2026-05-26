"""NJOGIS Parcels + MOD-IV (Ocean County) — ENRICHMENT_SOURCE.

Source : NJ Office of Information Technology — Office of GIS (NJOGIS)
Portal : https://njogis-newjersey.opendata.arcgis.com/datasets/
         parcels-and-mod-iv-of-ocean-county-nj-shp-download
Direct : https://geoapps.nj.gov/njgin/parcel/parcels_shp_dbf_Ocean.zip

Source role (v5.5.0 §0.1): ENRICHMENT_SOURCE.
Lead origin types: none (enrichment alone cannot originate a lead —
MASTER_PROMPT §4.33 / HARD RULE 13.4.1).

DANIEL'S LAW: Owner names are REDACTED from this dataset
(P.L. 2020, c. 125). The OWNER_NAME column ships empty. Owner
enrichment must come from a different source (S8 Ocean County Tax
Board, if owner exposure confirmed in pilot, or per-municipality
assessor records via OPRA). Confirmed live: scrape of two consecutive
parcels (Manchester Twp Block 38.28 Lots 492.01/492.02) shows
`OWNER_NAME: ''`.

Output (MASTER_PROMPT §4.32)
----------------------------
data/enriched/parcel_index.jsonl — one record per parcel.

Schema follows the MOD-IV column names (lowercased, normalized):
    parcel_id          (GIS_PIN)
    cd_code            (county+muni code 4 digits)
    block, lot, qual
    prop_class
    county, muni
    prop_loc           (property location address)
    owner_name         (always '' under Daniel's Law)
    st_address         (property street)
    city_state, zip5
    land_val, imprvt_val, net_value (int)
    last_yr_tx         (float)
    bldg_desc, land_desc
    calc_acre, yr_constr
    deed_book, deed_page, deed_date_yymmdd
    sale_price, dwell
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import requests
import dbfread

SOURCE_ID = "njogis_parcels_modiv"
ZIP_URL = "https://geoapps.nj.gov/njgin/parcel/parcels_shp_dbf_Ocean.zip"
DBF_NAME = "OceanTaxList.dbf"
REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = Path("/tmp/ocean_parcels.zip")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 "
                   "ocean-nj/v5.5.0"),
    "Accept": "application/zip,*/*",
    "Referer": "https://njogis-newjersey.opendata.arcgis.com/",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _maybe_int(x) -> Optional[int]:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _maybe_float(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _iso_yymmdd(s: Optional[str]) -> Optional[str]:
    if not s or len(s) != 6 or not s.isdigit():
        return None
    yy = int(s[:2])
    year = 2000 + yy if yy < 70 else 1900 + yy
    try:
        return datetime(year, int(s[2:4]), int(s[4:6])).date().isoformat()
    except ValueError:
        return None


def download(force: bool = False) -> Path:
    if CACHE_PATH.exists() and not force and CACHE_PATH.stat().st_size > 10_000_000:
        return CACHE_PATH
    r = requests.get(ZIP_URL, headers=HEADERS, stream=True, timeout=300)
    r.raise_for_status()
    tmp = CACHE_PATH.with_suffix(".zip.tmp")
    with tmp.open("wb") as fh:
        for chunk in r.iter_content(1 << 16):
            fh.write(chunk)
    tmp.replace(CACHE_PATH)
    return CACHE_PATH


def iter_modiv(zip_path: Path) -> Iterator[dict]:
    with zipfile.ZipFile(zip_path) as z:
        with z.open(DBF_NAME) as zf:
            dbf_bytes = zf.read()
    # dbfread requires a real path
    tmp = Path("/tmp/_ocean_taxlist.dbf")
    tmp.write_bytes(dbf_bytes)
    tbl = dbfread.DBF(str(tmp), load=False, encoding="latin-1")
    for rec in tbl:
        yield rec


def _wrap(rec: dict) -> Optional[dict]:
    gis_pin = (rec.get("GIS_PIN") or "").strip()
    if not gis_pin:
        return None
    payload = {
        "parcel_id": gis_pin,
        "cd_code": (rec.get("CD_CODE") or "").strip(),
        "block": (rec.get("BLOCK") or "").strip(),
        "lot": (rec.get("LOT") or "").strip(),
        "qual": (rec.get("QUALIFIER") or "").strip(),
        "prop_class": (rec.get("PROP_CLASS") or "").strip(),
        "county": (rec.get("COUNTY") or "").strip(),
        "muni": (rec.get("MUN_NAME") or "").strip(),
        "prop_loc": (rec.get("PROP_LOC") or "").strip(),
        "owner_name": (rec.get("OWNER_NAME") or "").strip(),
        "st_address": (rec.get("ST_ADDRESS") or "").strip(),
        "city_state": (rec.get("CITY_STATE") or "").strip(),
        "zip5": (rec.get("ZIP5") or "").strip(),
        "land_val": _maybe_int(rec.get("LAND_VAL")),
        "imprvt_val": _maybe_int(rec.get("IMPRVT_VAL")),
        "net_value": _maybe_int(rec.get("NET_VALUE")),
        "last_yr_tx": _maybe_float(rec.get("LAST_YR_TX")),
        "bldg_desc": (rec.get("BLDG_DESC") or "").strip(),
        "land_desc": (rec.get("LAND_DESC") or "").strip(),
        "calc_acre": _maybe_float(rec.get("CALC_ACRE")),
        "yr_constr": _maybe_int(rec.get("YR_CONSTR")),
        "deed_book": (rec.get("DEED_BOOK") or "").strip(),
        "deed_page": (rec.get("DEED_PAGE") or "").strip(),
        "deed_date": _iso_yymmdd(rec.get("DEED_DATE")),
        "sale_price": _maybe_int(rec.get("SALE_PRICE")),
        "dwell": _maybe_int(rec.get("DWELL")),
        "owner_redacted_under_daniels_law": True,
    }
    return {
        "raw_record_id": f"{SOURCE_ID}:{gis_pin}",
        "source_id": SOURCE_ID,
        "source_url": ZIP_URL,
        "source_fetched_at": _now_iso(),
        "parser_confidence": 95,
        "source_role": "ENRICHMENT_SOURCE",
        "raw_payload": payload,
    }


def run(out_path: Optional[Path] = None, force_download: bool = False,
        limit: Optional[int] = None) -> int:
    out_path = out_path or REPO_ROOT / "data" / "enriched" / "parcel_index.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    zip_path = download(force=force_download)
    count = 0
    tmp_out = out_path.with_suffix(".jsonl.tmp")
    with tmp_out.open("w", encoding="utf-8") as fh:
        for rec in iter_modiv(zip_path):
            wrapped = _wrap(rec)
            if wrapped is None:
                continue
            fh.write(json.dumps(wrapped, ensure_ascii=False) + "\n")
            count += 1
            if limit and count >= limit:
                break
    tmp_out.replace(out_path)
    print(f"[{SOURCE_ID}] parcels_written={count} (Daniel's Law: owners redacted)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=None)
    p.add_argument("--force-download", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()
    return run(out_path=Path(args.out) if args.out else None,
               force_download=args.force_download, limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
