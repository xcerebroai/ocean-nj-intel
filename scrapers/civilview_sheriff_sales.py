"""CivilView — sheriff foreclosure-sale official platform (S1-NJ).

Source       : CivilView salesweb (operated by ACS/Vital Integrated)
Portal       : https://salesweb.civilview.com/Sales/SalesSearch?countyId=85
Source role  : PRIMARY_EVENT_SOURCE per v5.5.0 §1.5 — the page H1
               itself declares "Ocean County, NJ - Foreclosure Sales
               Listing" alongside the sheriff-of-record's branding.
               salesweb.civilview.com is added to the §1.5
               KNOWN_OFFICIAL_VENUE_PLATFORMS registry; classifier
               verdict OFFICIAL_VENUE_PRIMARY with county evidence.

Operational caveat (recon-confirmed 2026-05-26)
-----------------------------------------------
The Ocean County (countyId=85) feed on CivilView is currently DORMANT:
- Page header literally says "last updated: 2/27/2026 11:26:00 AM".
- Both Open-sales and Sold/Cancelled searches return "No search
  results found. Please modify your search criteria and try searching
  again."
- The GetSearchCriteria AJAX returns empty municipality/date dropdowns.

So this adapter ships but yields zero Ocean rows TODAY.  When Ocean
resumes posting to CivilView (or when the harness is reused for
another NJ county whose CivilView feed is live), the adapter promotes
itself to the active foreclosure source and the sheriff PDF (S1)
demotes to SUPPORTING / cross-confirm.

Access pattern
--------------
CivilView's SalesSearch is an ASP.NET MVC form that POSTs to itself.
The criteria dropdowns are populated via the JSON endpoint
/Sales/GetSearchCriteria?IsOpen=<bool>.  Server-side result rendering
emits a table with one row per sale carrying a /Sales/SaleDetails
anchor.  Stdlib + requests is sufficient.

Output (MASTER_PROMPT §4.32)
----------------------------
data/raw/civilview_sheriff_sales.jsonl — one record per sheriff number.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import requests

SOURCE_ID = "civilview_sheriff_sales"
COUNTY_ID = 85   # Ocean County NJ — set by operator recon
BASE = "https://salesweb.civilview.com"
SEARCH_URL = f"{BASE}/Sales/SalesSearch?countyId={COUNTY_ID}"
CRITERIA_URL = f"{BASE}/Sales/GetSearchCriteria"

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "raw" / f"{SOURCE_ID}.jsonl"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 ocean-nj/v5.5.0")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_from_mdy(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Session + criteria
# ---------------------------------------------------------------------------


def open_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "text/html,*/*"})
    # Seed cookies (AWSALB / ASP.NET_SessionId) by hitting the landing page.
    s.get(SEARCH_URL, timeout=30)
    return s


def fetch_criteria(session: requests.Session, is_open: bool) -> dict:
    """Returns {'cities': [...], 'dates': [...], 'months': [...],
                'sales_types': [...], 'default_date': ''}."""
    r = session.get(
        CRITERIA_URL,
        params={"IsOpen": "true" if is_open else "false"},
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json",
                 "Referer": SEARCH_URL},
        timeout=30,
    )
    try:
        arr = r.json()
    except ValueError:
        return {}
    return {
        "cities": [c for c in (arr[0] if len(arr) > 0 else []) if c],
        "dates":  [d for d in (arr[1] if len(arr) > 1 else []) if d],
        "months": [m for m in (arr[2] if len(arr) > 2 else [])
                   if isinstance(m, dict) and m.get("MonthNumber")],
        "sales_types": list(arr[3] if len(arr) > 3 else []),
        "default_date": arr[4] if len(arr) > 4 else "",
    }


# ---------------------------------------------------------------------------
# Search + parse
# ---------------------------------------------------------------------------


_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_SALE_DETAILS_RE = re.compile(
    r'href="(/Sales/SaleDetails\?[^"]+)"', re.I,
)


def search_open_sales(session: requests.Session, *, is_open: bool = True,
                      property_status_date: str = "",
                      sales_type: str = "Real Estate") -> tuple[list[dict], int]:
    """POST the search form. Returns (rows, raw_html_len)."""
    form = {
        "IsOpen": "true" if is_open else "false",
        "SheriffNumber": "",
        "PropertyStatusDate": property_status_date,
        "MonthNumber": "0",
        "PlaintiffTitle": "",
        "DefendantTitle": "",
        "Address": "",
        "SalesType": sales_type,
    }
    r = session.post(SEARCH_URL, data=form, timeout=60,
                     headers={"Referer": SEARCH_URL})
    html = r.text
    rows = _parse_result_table(html)
    return rows, len(html)


def _parse_result_table(html: str) -> list[dict]:
    """Extract one record per SaleDetails-anchored row."""
    rows: list[dict] = []
    # Pre-scan: only rows that include a SaleDetails anchor are data rows.
    for trm in _ROW_RE.finditer(html):
        tr = trm.group(1)
        det = _SALE_DETAILS_RE.search(tr)
        if not det:
            continue
        details_href = det.group(1)
        # Pull the sale_id query param if present
        sale_id_m = re.search(r"PropertyId=(\d+)", details_href) or \
                    re.search(r"id=(\d+)", details_href, re.I)
        sale_id = sale_id_m.group(1) if sale_id_m else details_href
        cells = [_TAG_RE.sub("", c) for c in _TD_RE.findall(tr)]
        cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
        rows.append({
            "details_href": details_href,
            "sale_id": sale_id,
            "cells": cells,
        })
    return rows


def fetch_detail(session: requests.Session, details_href: str) -> dict:
    """Pull a single sale's detail page and extract canonical fields."""
    url = BASE + details_href if details_href.startswith("/") else details_href
    r = session.get(url, timeout=30, headers={"Referer": SEARCH_URL})
    html = r.text
    text = re.sub(r"\s+", " ", _TAG_RE.sub(" ", html))
    out: dict = {"detail_url": url}
    for label, key in (
        ("Sheriff #", "sheriff_number"),
        ("Court Case #", "court_case_number"),
        ("Sales Date", "sale_date_text"),
        ("Plaintiff", "plaintiff"),
        ("Defendant", "defendant"),
        ("Address", "property_address"),
        ("Approx. Upset", "upset_amount_text"),
        ("Status", "status"),
        ("Attorney", "attorney"),
    ):
        m = re.search(rf"{re.escape(label)}\s*:?\s*([^:]+?)(?=\s*[A-Z][A-Za-z]+\s*:|$)",
                      text, re.I)
        if m:
            out[key] = m.group(1).strip()[:240]
    out["sale_date_iso"] = _iso_from_mdy(out.get("sale_date_text"))
    if "upset_amount_text" in out:
        am = re.search(r"\$?([\d,]+(?:\.\d{2})?)", out["upset_amount_text"])
        if am:
            try:
                out["upset_amount"] = float(am.group(1).replace(",", ""))
            except ValueError:
                pass
    return out


def _wrap(detail: dict, row_meta: dict, *, fetched_at: str) -> dict:
    sale_id = row_meta.get("sale_id") or detail.get("sheriff_number") or ""
    raw_record_id = f"{SOURCE_ID}:{sale_id}"
    payload = dict(detail)
    payload["sale_id"] = sale_id
    payload["sheriff_number"] = detail.get("sheriff_number")
    payload["primary_event_date"] = detail.get("sale_date_iso")
    payload["doc_type"] = "sheriff_sale_listing"
    payload["scheduled_event_classification_hint"] = _classify(detail)
    return {
        "raw_record_id": raw_record_id,
        "source_id": SOURCE_ID,
        "source_url": payload["detail_url"],
        "source_fetched_at": fetched_at,
        "parser_confidence": 88,
        "source_role": "PRIMARY_EVENT_SOURCE",
        "lead_origin_type": "RECORDED_EVENT",
        "raw_payload": payload,
    }


def _classify(detail: dict) -> str:
    status = (detail.get("status") or "").upper()
    if "CANCEL" in status or "BANKRUPT" in status:
        return "HISTORICAL_CONTEXT_ONLY"
    sd = detail.get("sale_date_iso")
    if not sd:
        return "UPCOMING_SALE"
    try:
        sd_d = datetime.strptime(sd, "%Y-%m-%d").date()
    except ValueError:
        return "UPCOMING_SALE"
    today = datetime.now(timezone.utc).date()
    return "UPCOMING_SALE" if sd_d >= today else "PAST_SALE"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run(out_path: Path = OUTPUT_PATH, forward_window_days: int = 90,
        max_details: int = 250) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fetched_at = _now_iso()
    session = open_session()

    # Probe activity per recon contract. If the feed is dormant, write a
    # status record (not BLOCKED — the platform is live, the COUNTY feed is
    # empty) and exit 0.
    crit_open = fetch_criteria(session, is_open=True)
    crit_closed = fetch_criteria(session, is_open=False)
    feed_active = bool(crit_open.get("dates")) or bool(crit_closed.get("dates"))
    feed_dormant_marker = {
        "source_id": SOURCE_ID,
        "feed_active": feed_active,
        "open_dates_available": crit_open.get("dates", []),
        "closed_dates_available": crit_closed.get("dates", []),
        "open_cities_available": crit_open.get("cities", []),
        "closed_cities_available": crit_closed.get("cities", []),
        "county_id": COUNTY_ID,
        "probe_at": fetched_at,
    }

    if not feed_active:
        # Emit a single status record so downstream code can surface the
        # dormancy honestly without faking data.
        with out_path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "raw_record_id": f"{SOURCE_ID}:_DORMANT_",
                "source_id": SOURCE_ID,
                "source_url": SEARCH_URL,
                "source_fetched_at": fetched_at,
                "parser_confidence": 0,
                "source_role": "DORMANT_PRIMARY_EVENT_SOURCE",
                "lead_origin_type": None,
                "raw_payload": {
                    "status": "DORMANT",
                    "detail": ("CivilView reports 'No search results found' for "
                               "Open and Sold/Cancelled queries on countyId=85; "
                               "criteria dropdowns return empty. Falling back to "
                               "S1 sheriff_foreclosure PDF for the live "
                               "foreclosure feed."),
                    "venue_classifier_verdict": "OFFICIAL_VENUE_PRIMARY",
                    "venue_classifier_evidence": (
                        "Page H1 verbatim: 'Ocean County, NJ - Foreclosure "
                        "Sales Listing'."
                    ),
                    "feed_probe": feed_dormant_marker,
                },
            }, ensure_ascii=False) + "\n")
        print(f"[{SOURCE_ID}] DORMANT — no Open or Sold sales returned; "
              f"see {out_path} for the status record.")
        return 0

    # Active path: enumerate open + closed sale dates, fetch each result
    # page, follow details links.
    rows_all: list[dict] = []
    for is_open, dates in ((True, crit_open["dates"]), (False, crit_closed["dates"])):
        for d in dates:
            rows, _ = search_open_sales(session, is_open=is_open,
                                        property_status_date=d)
            rows_all.extend(rows)
    # Dedupe by sale_id
    seen: set = set()
    unique_rows = []
    for r in rows_all:
        if r["sale_id"] in seen:
            continue
        seen.add(r["sale_id"])
        unique_rows.append(r)
    if len(unique_rows) > max_details:
        unique_rows = unique_rows[:max_details]

    details = []
    for r in unique_rows:
        try:
            d = fetch_detail(session, r["details_href"])
        except Exception:
            continue
        details.append(_wrap(d, r, fetched_at=fetched_at))

    with out_path.open("w", encoding="utf-8") as fh:
        for rec in details:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[{SOURCE_ID}] feed_active=True records_written={len(details)} "
          f"forward_window_days={forward_window_days}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=str(OUTPUT_PATH))
    p.add_argument("--max-details", type=int, default=250)
    args = p.parse_args()
    return run(out_path=Path(args.out), max_details=args.max_details)


if __name__ == "__main__":
    raise SystemExit(main())
