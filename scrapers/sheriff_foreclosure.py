"""Ocean County, NJ — Sheriff foreclosure-sale adapter (PRIMARY_EVENT_SOURCE).

Source : Ocean County Sheriff's Office foreclosure page
Portal : https://sheriff.co.ocean.nj.us/frmForeclosures
PDF    : https://www.co.ocean.nj.us/WebContentFiles/<guid>.pdf
         (Oracle-Reports-generated REAL ESTATE LISTING for the current sale date)

Source role (v5.5.0 §0.1): PRIMARY_EVENT_SOURCE.
Lead origin types (v5.5.0 §3.8): RECORDED_EVENT (sheriff schedule entry).
§3.9 scheduled-event categories: UPCOMING_SALE (future sale_date or
adjournment_date), PAST_SALE (past), HISTORICAL_CONTEXT_ONLY (cancellations).

Access (recon-confirmed 2026-05-26)
-----------------------------------
The index page sheriff.co.ocean.nj.us/frmForeclosures is server-rendered
HTML; the "Foreclosure Listing" anchor links to the current weekly PDF
on co.ocean.nj.us/WebContentFiles/. The PDF GUID changes per refresh,
but the anchor text is stable. Stdlib + pdfplumber. No login, no
CAPTCHA, no JS.

Entry shape per PDF (6 pages ~ 50 entries on the sample pull):
    CH <docket>
    PLAINTIFF <plaintiff>     $<upset>     [<status>]      <col S>
    F<file_no> DEFENDANT <defendant>       [<adjourn_date>]
    ATTORNEY <firm> <phone>
    <attorney_case_id> <attorney_contact>
    SEQ 001 <street>  <city> NJ <zip>
    Lot: <lot> Block: <block>

Status flags observed: empty, ADJOURNED UNTIL MM/DD/YYYY, BANKRUPTCY,
CANCELLATION. "ADJOURNED UNTIL" rewrites primary_event_date forward.

Output (MASTER_PROMPT §4.32 wrapped raw-record shape)
-----------------------------------------------------
data/raw/sheriff_foreclosure.jsonl — one record per chancery docket.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import pdfplumber

SOURCE_ID = "sheriff_foreclosure"
INDEX_URL = "https://sheriff.co.ocean.nj.us/frmForeclosures"
LISTING_ANCHOR_RE = re.compile(
    r'<a[^>]+href=["\']([^"\']+\.pdf[^"\']*)["\'][^>]*>\s*Foreclosure\s+Listing\s*</a>',
    re.I,
)
DEFAULT_TIMEOUT = 60
REPO_ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ocean-nj/v5.5.0"


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


def _fetch_bytes(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_text(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    return _fetch_bytes(url, timeout).decode("utf-8", errors="ignore")


def discover_listing_pdf_url(index_html: Optional[str] = None) -> Optional[str]:
    """Return the current 'Foreclosure Listing' PDF URL, or None if absent."""
    html = index_html if index_html is not None else _fetch_text(INDEX_URL)
    m = LISTING_ANCHOR_RE.search(html)
    if not m:
        return None
    href = m.group(1).strip()
    href = re.sub(r"^http://", "https://", href)
    href = href.replace("//WebContentFiles//", "/WebContentFiles/")
    return href


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

SALE_DATE_HEADER_RE = re.compile(
    r"REAL ESTATE LISTING FOR\s+(\d{2}/\d{2}/\d{4})", re.I
)
DOCKET_LINE_RE = re.compile(r"^CH\s+(\d+)\b", re.M)
ADJOURN_DATE_RE = re.compile(r"ADJOURNED UNTIL\s*(\d{2}/\d{2}/\d{4})?", re.I)
STATUS_TOKENS = ("ADJOURNED UNTIL", "BANKRUPTCY", "CANCELLATION", "BANKRUPTCY/")
MONEY_RE = re.compile(r"\$([\d,]+\.\d{2})")
LOT_BLOCK_RE = re.compile(r"Lot:\s*(.+?)\s+Block:\s*(\S+)", re.I)
SEQ_ADDR_RE = re.compile(
    r"SEQ\s+\d+\s+(?P<addr>.+?)\s+NJ\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$"
)
PLAINTIFF_LINE_RE = re.compile(r"^PLAINTIFF\s+(?P<plaintiff>.+?)\s+\$([\d,]+\.\d{2})\s*(?P<rest>.*)$")
DEFENDANT_LINE_RE = re.compile(r"^F\s*\d+\s+DEFENDANT\s+(?P<defendant>.+?)(?:\s+(\d{2}/\d{2}/\d{4}))?$")
FILE_NO_RE = re.compile(r"^F\s*(\d+)\b")
ATTORNEY_LINE_RE = re.compile(r"^ATTORNEY\s+(?P<firm>.+?)\s+(?P<phone>[\d\-\(\) ]{7,})\s*$")


def _parse_money(s: str) -> Optional[float]:
    m = MONEY_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _norm_status(rest: str) -> tuple[Optional[str], Optional[str]]:
    rest = rest.strip()
    for tok in STATUS_TOKENS:
        if tok in rest.upper():
            adj = ADJOURN_DATE_RE.search(rest)
            adj_date = adj.group(1) if adj and adj.group(1) else None
            return tok.replace("/", "").strip(), adj_date
    return None, None


def parse_pdf(pdf_bytes: bytes) -> tuple[Optional[str], list[dict]]:
    """Extract (sale_date_mdy, entries[]) from the listing PDF."""
    entries: list[dict] = []
    sale_date: Optional[str] = None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if sale_date is None:
                m = SALE_DATE_HEADER_RE.search(text)
                if m:
                    sale_date = m.group(1)
            # Find docket positions and split into blocks
            positions = [m.start() for m in DOCKET_LINE_RE.finditer(text)]
            positions.append(len(text))
            for i in range(len(positions) - 1):
                block = text[positions[i]:positions[i + 1]]
                entry = _parse_block(block)
                if entry is not None:
                    entries.append(entry)
    return sale_date, entries


def _parse_block(block: str) -> Optional[dict]:
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("CH"):
        return None
    docket_m = DOCKET_LINE_RE.match(lines[0])
    if not docket_m:
        return None
    docket = docket_m.group(1)

    plaintiff = upset = status = adjournment = None
    file_no = defendant = None
    attorney_firm = attorney_phone = None
    attorney_case_id = attorney_contact = None
    address = city = zipc = None
    lot = block_no = None

    for ln in lines[1:]:
        pm = PLAINTIFF_LINE_RE.match(ln)
        if pm and plaintiff is None:
            plaintiff = pm.group("plaintiff").strip()
            upset = float(pm.group(2).replace(",", ""))
            status, adjournment = _norm_status(pm.group("rest"))
            continue
        dm = DEFENDANT_LINE_RE.match(ln)
        if dm and defendant is None:
            fn = FILE_NO_RE.match(ln)
            if fn:
                file_no = fn.group(1)
            defendant = dm.group("defendant").strip()
            if dm.group(2) and not adjournment:
                adjournment = dm.group(2)
            continue
        am = ATTORNEY_LINE_RE.match(ln)
        if am and attorney_firm is None:
            attorney_firm = am.group("firm").strip()
            attorney_phone = re.sub(r"\s+", "", am.group("phone"))
            continue
        sm = SEQ_ADDR_RE.match(ln)
        if sm:
            addr_blob = sm.group("addr").strip()
            zipc = sm.group("zip")
            parts = addr_blob.rsplit(" ", 1)
            if len(parts) == 2:
                address, city = parts[0].strip(), parts[1].strip()
            else:
                address = addr_blob
            continue
        lbm = LOT_BLOCK_RE.search(ln)
        if lbm and lot is None:
            lot = lbm.group(1).strip()
            block_no = lbm.group(2).strip()
            continue
        # Attorney case id / contact lives between ATTORNEY and SEQ lines.
        if (
            attorney_firm is not None
            and defendant is not None
            and attorney_case_id is None
            and not ln.startswith(("SEQ", "Lot:", "CH ", "PLAINTIFF", "F"))
        ):
            tokens = ln.split(None, 1)
            attorney_case_id = tokens[0] if tokens else None
            attorney_contact = tokens[1] if len(tokens) > 1 else None

    if not (plaintiff or defendant or address):
        return None

    return {
        "chancery_docket": f"CH {docket}",
        "file_no": file_no,
        "plaintiff": plaintiff,
        "defendant": defendant,
        "upset_amount": upset,
        "status": status,
        "adjournment_date": _iso_date(adjournment),
        "attorney_firm": attorney_firm,
        "attorney_phone": attorney_phone,
        "attorney_case_id": attorney_case_id,
        "attorney_contact": attorney_contact,
        "address": address,
        "city": city,
        "state": "NJ",
        "zip": zipc,
        "block": block_no,
        "lot": lot,
    }


def _iso_date(mdy: Optional[str]) -> Optional[str]:
    if not mdy:
        return None
    try:
        return datetime.strptime(mdy, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Wrap to canonical raw_event_record shape (§4.32)
# ---------------------------------------------------------------------------


def _wrap(entry: dict, *, source_url: str, fetched_at: str, sale_date_iso: Optional[str]) -> dict:
    docket = entry["chancery_docket"].replace(" ", "_")
    raw_record_id = f"{SOURCE_ID}:{docket}"
    primary_event_date = entry.get("adjournment_date") or sale_date_iso
    payload = dict(entry)
    payload["sale_date"] = sale_date_iso
    payload["primary_event_date"] = primary_event_date
    payload["doc_type"] = "sheriff_sale_listing"
    payload["scheduled_event_classification_hint"] = (
        "HISTORICAL_CONTEXT_ONLY" if (entry.get("status") or "").upper().startswith("CANCELLATION")
        else "UPCOMING_SALE"
    )
    return {
        "raw_record_id": raw_record_id,
        "source_id": SOURCE_ID,
        "source_url": source_url,
        "source_fetched_at": fetched_at,
        "parser_confidence": 90,
        "source_role": "PRIMARY_EVENT_SOURCE",
        "lead_origin_type": "RECORDED_EVENT",
        "raw_payload": payload,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _read_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(out_path: Optional[Path] = None, pdf_url_override: Optional[str] = None) -> int:
    out_path = out_path or REPO_ROOT / "data" / "raw" / f"{SOURCE_ID}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pdf_url = pdf_url_override or discover_listing_pdf_url()
    if not pdf_url:
        print(f"[{SOURCE_ID}] no Foreclosure Listing PDF anchor found", file=sys.stderr)
        return 2

    pdf_bytes = _fetch_bytes(pdf_url)
    sale_date_mdy, entries = parse_pdf(pdf_bytes)
    sale_date_iso = _iso_date(sale_date_mdy)
    fetched_at = _now_iso()

    new_records = [_wrap(e, source_url=pdf_url, fetched_at=fetched_at,
                         sale_date_iso=sale_date_iso) for e in entries]

    existing = _read_existing(out_path)
    by_id: dict[str, dict] = {r["raw_record_id"]: r for r in existing}
    for r in new_records:
        by_id[r["raw_record_id"]] = r

    tmp = out_path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in by_id.values():
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(out_path)

    print(
        f"[{SOURCE_ID}] sale_date={sale_date_mdy} entries_parsed={len(entries)} "
        f"records_in_file={len(by_id)} pdf={pdf_url.rsplit('/', 1)[-1]}"
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=None)
    p.add_argument("--pdf-url", default=None,
                   help="Override the discovered PDF URL (for fixture replay)")
    args = p.parse_args()
    out_path = Path(args.out) if args.out else None
    return run(out_path=out_path, pdf_url_override=args.pdf_url)


if __name__ == "__main__":
    raise SystemExit(main())
