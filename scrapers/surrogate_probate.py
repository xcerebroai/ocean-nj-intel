"""Ocean County, NJ — Surrogate / Bluestone probate-records adapter.

Source : Ocean County Surrogate's Office (Bluestone Public Search)
Portal : https://surrogateweb.co.ocean.nj.us/BluestoneWeb/default.aspx?FROM_MSG=99
Role   : PRIMARY_EVENT_SOURCE (probate filings = RECORDED_EVENT) AND
         PRIMARY_OWNER_STATUS_SOURCE (decedent → §3.5 estate-titled owner).

Access (recon-confirmed 2026-05-26)
-----------------------------------
The portal's "Export All" link downloads the full Ocean County
Surrogate index as `OceanIndex.csv` (~17 MB, ~247 K rows back to 2018).
The CSV does not respect the date-range search; it is the WHOLE index.
The window filter is applied here, after download. A non-empty primary
criterion (e.g. last_name='SMITH') is required to enable the Export All
button; the criterion does not affect the exported rows. No login, no
CAPTCHA. Playwright drives the click sequence because the underlying
ASP.NET WebForms callbacks need a real browser context to satisfy
DevExpress validators.

CSV schema (verified live):
    Docket, Name, File_Date, Case_Type, DOB, DOD, Town

Lead origins:
- RECORDED_EVENT — probate filing on `File_Date`.
- OWNER_STATUS — decedent name + DOD feed §3.5 owner_status_classifier
  after the parcel join to determine `estate_titled_owner` vs
  `life_estate` vs `not_estate`.

Output (MASTER_PROMPT §4.32)
----------------------------
data/raw/surrogate_probate.jsonl — one record per docket.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

SOURCE_ID = "surrogate_probate"
URL = "https://surrogateweb.co.ocean.nj.us/BluestoneWeb/default.aspx?FROM_MSG=99"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_date(mdy: Optional[str]) -> Optional[str]:
    if not mdy or not mdy.strip():
        return None
    for fmt in ("%m/%d/%Y", "%-m/%-d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(mdy.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    # Fallback: try non-zero-padded month/day
    try:
        parts = mdy.strip().split("/")
        if len(parts) == 3:
            m, d, y = parts
            return datetime(int(y), int(m), int(d)).date().isoformat()
    except (ValueError, TypeError):
        pass
    return None


def fetch_index_csv(headless: bool = True, criterion: str = "SMITH",
                    timeout_ms: int = 90_000) -> bytes:
    """Drive Bluestone's UI to produce the OceanIndex.csv download."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0 ocean-nj/v5.5.0"),
            viewport={"width": 1600, "height": 1100},
            accept_downloads=True,
        )
        page = ctx.new_page()
        page.goto(URL, timeout=timeout_ms, wait_until="networkidle")
        last_name = page.locator(
            "#ContentPlaceHolder1_ASPxSplitterDefaultMain_ASPxTextBox_search_entry_I"
        )
        last_name.click()
        last_name.type(criterion, delay=15)
        last_name.press("Tab")
        page.locator(
            "#ContentPlaceHolder1_ASPxSplitterDefaultMain_ASPxButton_search"
        ).click()
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        page.wait_for_selector(
            "#ContentPlaceHolder1_ASPxGridView_search_DXMainTable",
            timeout=timeout_ms,
        )
        with page.expect_download(timeout=timeout_ms) as dl_info:
            page.locator("#ASPxSplitterMaster_ASPxMenu1_DXI5_T").click()
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            page.locator("#ASPxButton_export").click()
        download = dl_info.value
        tmp = Path("/tmp") / f"ocean_index_{int(time.time())}.csv"
        download.save_as(str(tmp))
        data = tmp.read_bytes()
        tmp.unlink(missing_ok=True)
        browser.close()
        return data


def iter_records(csv_bytes: bytes, *, since_iso: Optional[str]) -> Iterator[dict]:
    text = csv_bytes.decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))
    cutoff = since_iso
    for row in reader:
        filed_iso = _iso_date(row.get("File_Date"))
        if cutoff:
            if not filed_iso:
                continue
            if filed_iso < cutoff:
                continue
        yield {
            "docket": (row.get("Docket") or "").strip(),
            "decedent_name": (row.get("Name") or "").strip(),
            "filed_date": filed_iso,
            "case_type": (row.get("Case_Type") or "").strip(),
            "date_of_birth": _iso_date(row.get("DOB")),
            "date_of_death": _iso_date(row.get("DOD")),
            "town": (row.get("Town") or "").strip(),
        }


def _wrap(rec: dict, *, fetched_at: str) -> dict:
    docket = rec.get("docket") or ""
    if not docket:
        return None  # type: ignore[return-value]
    raw_record_id = f"{SOURCE_ID}:{docket}"
    payload = dict(rec)
    payload["doc_type"] = "probate_filing"
    payload["primary_event_date"] = rec.get("filed_date")
    return {
        "raw_record_id": raw_record_id,
        "source_id": SOURCE_ID,
        "source_url": URL,
        "source_fetched_at": fetched_at,
        "parser_confidence": 92,
        "source_role": "PRIMARY_EVENT_SOURCE",
        "additional_source_roles": ["PRIMARY_OWNER_STATUS_SOURCE"],
        "lead_origin_type": "RECORDED_EVENT",
        "raw_payload": payload,
    }


def _read_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.open("r", encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def run(out_path: Optional[Path] = None, window_days: int = 90,
        headless: bool = True) -> int:
    out_path = out_path or REPO_ROOT / "data" / "raw" / f"{SOURCE_ID}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fetched_at = _now_iso()
    since_iso = (datetime.now(timezone.utc).date()
                 - timedelta(days=window_days)).isoformat()

    try:
        csv_bytes = fetch_index_csv(headless=headless)
    except Exception as exc:
        print(f"[{SOURCE_ID}] BLOCKED — fetch failure: {exc}", file=sys.stderr)
        return 3

    wrapped = []
    for rec in iter_records(csv_bytes, since_iso=since_iso):
        w = _wrap(rec, fetched_at=fetched_at)
        if w is not None:
            wrapped.append(w)

    existing = _read_existing(out_path)
    by_id = {r["raw_record_id"]: r for r in existing}
    for r in wrapped:
        by_id[r["raw_record_id"]] = r

    tmp = out_path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in by_id.values():
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(out_path)
    print(f"[{SOURCE_ID}] window_days={window_days} new_in_window={len(wrapped)} "
          f"records_in_file={len(by_id)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=None)
    p.add_argument("--window-days", type=int, default=90)
    p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    args = p.parse_args()
    return run(out_path=Path(args.out) if args.out else None,
               window_days=args.window_days, headless=args.headless)


if __name__ == "__main__":
    raise SystemExit(main())
