"""NJPA — NJ Public Notices aggregator, Ocean County sheriff-sale notices (S6).

Source       : New Jersey Press Association — public notices portal
Portal       : https://www.njpublicnotices.com/Search.aspx
Source role  : SUPPORTING_EVENT_SOURCE (v5.5.0 §0.1 / §1.5).

In NJ, sheriff foreclosure-sale notices are statutorily published in a
newspaper of general circulation 4 weeks before the sale.  NJPA
aggregates those publications.  For Ocean County the records add:

  - sale dates that are NOT yet in the current sheriff PDF (the sheriff
    publishes only the next sale Tuesday's PDF; NJPA shows publication
    notices 14-28 days ahead of sale).
  - the statutory publication date itself (proves notice was served).

§1.5 classification: NJPA is NOT a marketplace re-lister — it
re-publishes statutory notices submitted by newspapers + courts.  It
is the OFFICIAL_PUBLICATION_REGISTRY for sheriff-sale notices in NJ.
The §1.5 OFFICIAL-VENUE TEST does not apply here (NJPA doesn't conduct
sales; it publishes the legally-required notices); the analogue is
"OFFICIAL_PUBLICATION_REGISTRY" — admitted as SUPPORTING_EVENT_SOURCE,
cannot be a sole distress feed per v5.5.0 P-tier rules.

Access (recon-confirmed 2026-05-26)
-----------------------------------
ASP.NET WebForms + AjaxControlToolkit UpdatePanels.  Direct POSTs to
Search.aspx do not propagate the county filter through to the result
set; the canonical client-side flow is:

    1. GET Search.aspx
    2. JS-click checkbox `lstCounty$14` (Ocean) — fires __doPostBack
    3. fill txtSearch = 'sheriff sale'
    4. click btnGo
    5. parse the rendered table rows

Confirmed live: Ocean+keyword 'sheriff sale' returns sheriff-sale
notices with chancery dockets in the body text and the publication
date in the row header.

Output (MASTER_PROMPT §4.32 wrapped raw-record shape)
-----------------------------------------------------
data/raw/njpa_legal_notices.jsonl — one record per (docket, sale_date).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

SOURCE_ID = "njpa_legal_notices"
PORTAL_URL = "https://www.njpublicnotices.com/Search.aspx"
OCEAN_COUNTY_CHECKBOX_ID = "ctl00_ContentPlaceHolder1_as1_lstCounty_14"
SEARCH_BOX_ID = "ctl00_ContentPlaceHolder1_as1_txtSearch"
GO_BUTTON_NAME = "ctl00$ContentPlaceHolder1$as1$btnGo"

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "raw" / f"{SOURCE_ID}.jsonl"

OCEAN_TOKENS = (
    "ocean county", "toms river", "brick township", "lakewood", "jackson township",
    "manchester township", "manchester twp", "barnegat", "stafford", "lacey",
    "tuckerton", "lavallette", "seaside heights", "seaside park", "point pleasant",
    "ship bottom", "beach haven", "berkeley township", "berkeley twp",
    "harvey cedars", "barnegat light", "long beach", "mantoloking", "bay head",
    "south toms river", "ocean gate", "pine beach", "beachwood", "island heights",
    "lakehurst", "plumsted", "little egg harbor", "eagleswood", "surf city",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Row-text parsing
# ---------------------------------------------------------------------------


# A notice row text looks like:
#   "<publication>, <city>\nSunday, May 21, 2026\n\nOCEAN COUNTY SHERIFF'S
#    SALE By virtue of ... Docket No. F01368625 ... TUESDAY the 9TH DAY OF
#    JUNE, A.D. 2026 ... <property address> ..."
#
# We extract these fields without committing to one fixed layout — the source
# of truth is the row TEXT (which is also what an operator sees on the page).
_DOCKET_RE = re.compile(
    r"Docket\s*No\.?\s*([CF]-?\d{4,8}(?:-\d{2})?|[CF]\d{6,9})",
    re.I,
)
_PUB_DATE_HEADER_RE = re.compile(
    r"\b(?:Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday),\s+"
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{1,2}),\s+(\d{4})",
    re.I,
)
_SALE_DATE_PHRASE_RE = re.compile(
    r"(?:on\s+)?(?:Tuesday|Monday|Wednesday|Thursday|Friday)\s+"
    r"the\s+(\d{1,2})(?:ST|ND|RD|TH)?\s+DAY\s+OF\s+"
    r"(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
    r"SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER),?\s*A\.?D\.?\s*(\d{4})",
    re.I,
)
_PUB_HEADER_LINE_RE = re.compile(
    r"^([^,]+?),\s*([A-Za-z .]+)\s*(?:Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)",
    re.I,
)


_MONTH = {m.lower(): i for i, m in enumerate(
    ["", "January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"]
)}


def _iso_from_phrase(month: str, day: str, year: str) -> Optional[str]:
    mi = _MONTH.get(month.lower())
    if not mi:
        return None
    try:
        return datetime(int(year), mi, int(day)).date().isoformat()
    except (ValueError, TypeError):
        return None


def parse_row_text(text: str) -> Optional[dict]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None

    docket = None
    m = _DOCKET_RE.search(text)
    if m:
        docket = m.group(1).upper().replace("-", "")

    pub_iso = None
    m = _PUB_DATE_HEADER_RE.search(text)
    if m:
        pub_iso = _iso_from_phrase(m.group(1), m.group(2), m.group(3))

    sale_iso = None
    m = _SALE_DATE_PHRASE_RE.search(text)
    if m:
        sale_iso = _iso_from_phrase(m.group(2), m.group(1), m.group(3))

    publication_source = None
    m = _PUB_HEADER_LINE_RE.search(text)
    if m:
        publication_source = (m.group(1) + ", " + m.group(2)).strip()

    return {
        "publication_source": publication_source,
        "publication_date": pub_iso,
        "sale_date": sale_iso,
        "chancery_docket": docket,
        "notice_type": "sheriff_sale_notice",
        "notice_text_excerpt": text[:600],
    }


def _is_ocean(text: str) -> bool:
    low = text.lower()
    return any(tok in low for tok in OCEAN_TOKENS)


# ---------------------------------------------------------------------------
# Playwright driver
# ---------------------------------------------------------------------------


def fetch_ocean_sheriff_notices(window_days: int = 30,
                                 headless: bool = True,
                                 timeout_ms: int = 45_000) -> list[dict]:
    """Drive the NJPA Search UI: Ocean + 'sheriff sale' + Go.  Returns
    the parsed notice rows (dedup'd by docket+sale_date)."""
    from playwright.sync_api import sync_playwright

    rows: list[dict] = []
    seen_signatures: set[tuple] = set()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(
            viewport={"width": 1500, "height": 1300},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 "
                        "ocean-nj/v5.5.0"),
        )
        page = ctx.new_page()
        page.goto(PORTAL_URL, timeout=timeout_ms, wait_until="networkidle")
        page.wait_for_timeout(600)
        # 1. Tick Ocean checkbox via JS (the input is invisible behind the
        #    custom-styled list; the onclick handler fires __doPostBack).
        page.evaluate(
            f"() => document.getElementById('{OCEAN_COUNTY_CHECKBOX_ID}').click()"
        )
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        page.wait_for_timeout(1_000)
        # 2. Search keyword + Go.
        page.fill(f"#{SEARCH_BOX_ID}", "sheriff sale")
        page.locator(f'input[name="{GO_BUTTON_NAME}"]').click(force=True)
        try:
            page.wait_for_load_state("networkidle", timeout=25_000)
        except Exception:
            pass
        page.wait_for_timeout(1_500)

        # 3. Scrape all visible rows.  Iterate pages if pagination present.
        for page_idx in range(10):  # cap at 10 pages of results
            page_rows = page.evaluate("""() => {
                const out = [];
                const seen = new Set();
                document.querySelectorAll('table tr').forEach(tr => {
                    const t = (tr.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (t.length > 60 && /sheriff|foreclos|notice/i.test(t)
                        && !seen.has(t.slice(0, 100))) {
                        seen.add(t.slice(0, 100));
                        out.push(t);
                    }
                });
                return out;
            }""")
            for text in page_rows:
                if not _is_ocean(text):
                    continue
                parsed = parse_row_text(text)
                if parsed is None:
                    continue
                sig = (parsed.get("chancery_docket"),
                       parsed.get("sale_date"),
                       parsed.get("publication_date"))
                if sig in seen_signatures:
                    continue
                seen_signatures.add(sig)
                rows.append(parsed)

            # Look for a "Next" pager link.  NJPA pagination is
            # __doPostBack-driven; try a generic next-page button by text.
            nxt = page.locator("a:has-text('Next'), a[id*='Next']").first
            if nxt.count() == 0:
                break
            try:
                nxt.click(force=True, timeout=5_000)
                page.wait_for_load_state("networkidle", timeout=15_000)
                page.wait_for_timeout(1_000)
            except Exception:
                break

        browser.close()
    # Window-bound (forward sale dates within window_days; or publication
    # within window_days BACKWARD for §6.6 SUPPORTING).
    today = datetime.now(timezone.utc).date()
    backward = today - timedelta(days=window_days)
    forward = today + timedelta(days=90)
    filtered: list[dict] = []
    for r in rows:
        keep = False
        pd = r.get("publication_date")
        sd = r.get("sale_date")
        if pd:
            try:
                pdt = datetime.strptime(pd, "%Y-%m-%d").date()
                if backward <= pdt <= forward:
                    keep = True
            except ValueError:
                pass
        if sd:
            try:
                sdt = datetime.strptime(sd, "%Y-%m-%d").date()
                if today - timedelta(days=14) <= sdt <= forward:
                    keep = True
            except ValueError:
                pass
        if pd is None and sd is None:
            keep = True   # no dates parsed — keep, downstream re-classifies
        if keep:
            filtered.append(r)
    return filtered


# ---------------------------------------------------------------------------
# Wrap + persist
# ---------------------------------------------------------------------------


def _stable_notice_id(r: dict) -> str:
    """Compose a stable id from docket+sale_date or fall back to a hash
    of the excerpt."""
    if r.get("chancery_docket") and r.get("sale_date"):
        return f"{r['chancery_docket']}_{r['sale_date']}"
    if r.get("chancery_docket"):
        return r["chancery_docket"]
    h = hashlib.sha256(
        (r.get("notice_text_excerpt") or "").encode("utf-8")
    ).hexdigest()[:16]
    return f"NOID_{h}"


def _wrap(rec: dict, *, fetched_at: str) -> dict:
    nid = _stable_notice_id(rec)
    payload = dict(rec)
    payload["notice_id"] = nid
    payload["primary_event_date"] = rec.get("sale_date") or rec.get("publication_date")
    payload["doc_type"] = "sheriff_sale_notice"
    return {
        "raw_record_id": f"{SOURCE_ID}:{nid}",
        "source_id": SOURCE_ID,
        "source_url": PORTAL_URL,
        "source_fetched_at": fetched_at,
        "parser_confidence": 80,
        "source_role": "SUPPORTING_EVENT_SOURCE",
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


def run(out_path: Optional[Path] = None, window_days: int = 30,
        headless: bool = True) -> int:
    out_path = out_path or OUTPUT_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fetched_at = _now_iso()
    try:
        rows = fetch_ocean_sheriff_notices(window_days=window_days, headless=headless)
    except Exception as exc:
        print(f"[{SOURCE_ID}] scrape failure: {exc}", file=sys.stderr)
        return 0   # SUPPORTING source — non-fatal

    wrapped = [_wrap(r, fetched_at=fetched_at) for r in rows]
    existing = _read_existing(out_path)
    by_id = {r["raw_record_id"]: r for r in existing}
    for r in wrapped:
        by_id[r["raw_record_id"]] = r

    tmp = out_path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in by_id.values():
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(out_path)
    print(f"[{SOURCE_ID}] window_days={window_days} rows_pulled={len(rows)} "
          f"records_in_file={len(by_id)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=None)
    p.add_argument("--window-days", type=int, default=30)
    p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    args = p.parse_args()
    return run(out_path=Path(args.out) if args.out else None,
               window_days=args.window_days, headless=args.headless)


if __name__ == "__main__":
    raise SystemExit(main())
