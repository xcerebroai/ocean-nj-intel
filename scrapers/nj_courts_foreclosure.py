"""NJ Courts Civil + Foreclosure — operator-seeded session adapter (S4).

Source       : NJ Courts Civil & Foreclosure Public Access (Pega CIVSearch)
Portal       : https://portalcivsearch-cloud.njcourts.gov/prweb/PRServletPublicAuth?AppName=CIVSearch
Source role  : PRIMARY_EVENT_SOURCE (foreclosure case docket events).
Lead origin  : RECORDED_EVENT — court filings (complaint, lis pendens,
               judgment, writ of execution). Cross-linked to S1 sheriff
               by chancery docket on the join.

Access (v5.5.0 §2.2 — operator-seeded session)
----------------------------------------------
The portal is `LOGIN_REQUIRED`. We do NOT automate the login. The
operator registers, logs in via Pega ESSO, runs one test search, then
exports the seven session cookies (per the operator handoff doc) into:

    runs/ocean_nj/.session_njcourts.json     (gitignored, see .gitignore)

This adapter reads that file, instantiates a `requests.Session` from
it, and drives the CIVSearch app's search form like a logged-in user.
Three-state outcome at every run:

    VALID            → fetched HTML carries the post-auth chrome (search
                       form + results grid).
    EXPIRED          → fetched HTML redirects back to the Pega login
                       page or shows an "Your session has expired"
                       banner.  Writes a re-seed marker to
                       `runs/ocean_nj/last_failed_refresh.json` per §6.5
                       and exits non-zero so the §6.5 last-good-
                       preservation path keeps the live board.
    UNAUTHENTICATED  → cookie jar absent OR malformed. Exits 0 with a
                       BLOCKED_SOURCE record so the build can still
                       publish from the other sources (graceful degrade).

The §3.9 scheduled-event classifier treats:
    - foreclosure_complaint / lis_pendens → RECORDED_EVENT
    - final_judgment / writ_of_execution  → POST_SALE_TITLE_EVENT
      cross-linked to S1 sheriff upcoming-sale by docket.

Output (MASTER_PROMPT §4.32 wrapped raw-record shape)
-----------------------------------------------------
data/raw/nj_courts_foreclosure.jsonl — one record per case docket.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import requests

SOURCE_ID = "nj_courts_foreclosure"
SEARCH_APP_BASE = "https://portalcivsearch-cloud.njcourts.gov"
SEARCH_APP_ENTRY = SEARCH_APP_BASE + "/prweb/PRServletPublicAuth?AppName=CIVSearch"

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_PATH = REPO_ROOT / "runs" / "ocean_nj" / ".session_njcourts.json"
LAST_FAIL_PATH = REPO_ROOT / "runs" / "ocean_nj" / "last_failed_refresh.json"
OUTPUT_PATH = REPO_ROOT / "data" / "raw" / f"{SOURCE_ID}.jsonl"

# Match the browser that seeded the cookies. If the operator captures from a
# different browser, the UA hint should be updated; Pega does not bind sessions
# to UA, but the WAF (Imperva Incapsula) may flag mismatches.
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ----- Probe markers -----------------------------------------------------
# The probe classifies the entry-URL response into one of four states.
# Each marker family is intentionally narrow so the detector tells the
# operator WHICH layer rejected them (WAF vs Pega) — those are different
# unlocks.

# (a) Imperva Incapsula WAF challenge — the "Pardon Our Interruption"
#     page. Means the .njcourts.gov Incapsula trio is stale.
IMPERVA_WAF_MARKERS = (
    re.compile(r"Pardon Our Interruption", re.I),
    re.compile(r"window\.reeseSkipExpirationCheck", re.I),
    re.compile(r"onProtectionInitialized", re.I),
)

# (b) Pega bounced us back to the ESSO login. Means Pega session expired
#     (most common failure mode after the cookies time out).
PEGA_LOGIN_MARKERS = (
    re.compile(r"PRAuth/CloudSAMLAuth", re.I),
    re.compile(r"AppName=ESSO\b", re.I),
    re.compile(r"id=\"UserIdentifier\"", re.I),
    re.compile(r"session\s+(?:has\s+)?expired", re.I),
)

# (c) Post-auth Pega CIVSearch harness markers.  Authenticated landings
#     emit Pega's per-page state hidden inputs and a search form.
PEGA_AUTH_MARKERS = (
    re.compile(r"pyActivity", re.I),                     # Pega activity bus
    re.compile(r"pzPostData|pzHarnessID|pyForUpload", re.I),
    re.compile(r"<form[^>]+method=\"post\"", re.I),
)
# Auth landing URL pattern: Pega rewrites the URL to /prweb/.../app/<APP>/...
PEGA_AUTH_URL_RE = re.compile(
    r"/prweb/PRServletPublicAuth/app/[A-Za-z]+/[^/]+/!STANDARD", re.I,
)


# ---------------------------------------------------------------------------
# Session loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SessionLoadResult:
    status: str                        # "LOADED" | "MISSING" | "MALFORMED"
    session: Optional[requests.Session]
    captured_at: Optional[str]
    user_label: Optional[str]
    cookie_count: int
    detail: Optional[str]


def load_seeded_session(path: Path = SESSION_PATH) -> SessionLoadResult:
    """Read the operator cookie jar and return a requests.Session ready
    to talk to portalcivsearch-cloud.njcourts.gov.

    The file shape (see operator handoff doc) is:

        {
          "captured_at": "<ISO-8601 UTC>",
          "session_user": "<email or label>",
          "cookies": [
            {"name": ..., "value": ..., "domain": ..., "path": ...},
            ...
          ]
        }
    """
    if not path.exists():
        return SessionLoadResult(
            status="MISSING",
            session=None,
            captured_at=None,
            user_label=None,
            cookie_count=0,
            detail=(f"cookie jar not found at {path} — the operator must "
                    "complete the registration + cookie-capture handoff "
                    "(runs/ocean_nj/recon/operator_verified_sources.yml)"),
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return SessionLoadResult(
            status="MALFORMED",
            session=None,
            captured_at=None,
            user_label=None,
            cookie_count=0,
            detail=f"cookie jar present but unparseable: {exc}",
        )

    cookies = payload.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return SessionLoadResult(
            status="MALFORMED",
            session=None,
            captured_at=payload.get("captured_at"),
            user_label=payload.get("session_user"),
            cookie_count=0,
            detail="cookie jar missing non-empty 'cookies' array",
        )

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
    })
    loaded = 0
    for c in cookies:
        try:
            sess.cookies.set(
                name=c["name"],
                value=c["value"],
                domain=c.get("domain") or "",
                path=c.get("path") or "/",
            )
            loaded += 1
        except (KeyError, TypeError):
            continue

    if loaded == 0:
        return SessionLoadResult(
            status="MALFORMED",
            session=None,
            captured_at=payload.get("captured_at"),
            user_label=payload.get("session_user"),
            cookie_count=0,
            detail="none of the supplied cookies had name+value+domain fields",
        )

    return SessionLoadResult(
        status="LOADED",
        session=sess,
        captured_at=payload.get("captured_at"),
        user_label=payload.get("session_user"),
        cookie_count=loaded,
        detail=None,
    )


# ---------------------------------------------------------------------------
# Session probe — VALID / EXPIRED / UNAUTHENTICATED
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SessionProbeResult:
    status: str            # "VALID" | "EXPIRED" | "WAF_BLOCKED" | "UNKNOWN"
    failure_layer: Optional[str]   # "imperva" | "pega" | None
    final_url: Optional[str]
    landing_excerpt: Optional[str]
    detail: Optional[str]


def probe_session(session: requests.Session) -> SessionProbeResult:
    """Hit the CIVSearch entry URL and classify the response into one of
    four states (VALID, EXPIRED, WAF_BLOCKED, UNKNOWN).

    EXPIRED and WAF_BLOCKED are both "operator must re-seed cookies"
    cases, but they identify DIFFERENT cookies as the cause:

        EXPIRED      → re-seed the Pega cookies (PD-S-SESSION-ID,
                       JSESSIONID, PEGA-SESSION-COOKIE*).
        WAF_BLOCKED  → re-seed the Imperva cookies (visid_incap_*,
                       nlbi_*, incap_ses_*).  In practice a single
                       browser-based re-seed gives you both.

    UNKNOWN is the fallback when neither marker family matches; the
    caller should treat UNKNOWN the same as EXPIRED for the §6.5
    last-good preservation path, but the diagnostic surfaces the
    response excerpt so the rule can be tightened next pass.
    """
    try:
        resp = session.get(SEARCH_APP_ENTRY, timeout=30, allow_redirects=True)
    except requests.RequestException as exc:
        return SessionProbeResult(
            status="EXPIRED",
            failure_layer=None,
            final_url=None,
            landing_excerpt=None,
            detail=f"network error on probe: {exc}",
        )

    body = resp.text or ""
    final_url = resp.url or ""
    excerpt = body[:600]

    # (a) Imperva WAF first — its challenge page hides Pega entirely.
    if any(m.search(body) for m in IMPERVA_WAF_MARKERS):
        return SessionProbeResult(
            status="WAF_BLOCKED",
            failure_layer="imperva",
            final_url=final_url,
            landing_excerpt=excerpt,
            detail=("Imperva 'Pardon Our Interruption' challenge — the "
                    "visid_incap_* / nlbi_* / incap_ses_* cookies are "
                    "stale or absent. Re-seed from a fresh browser "
                    "session."),
        )

    # (b) Pega login redirect / banner — auth-layer expiry.
    pega_login = any(m.search(body) for m in PEGA_LOGIN_MARKERS) or any(
        m.search(final_url) for m in PEGA_LOGIN_MARKERS
    )
    if pega_login:
        return SessionProbeResult(
            status="EXPIRED",
            failure_layer="pega",
            final_url=final_url,
            landing_excerpt=excerpt,
            detail=("Pega bounced to ESSO login or shows session-expired "
                    "banner — re-seed PD-S-SESSION-ID + the PEGA-SESSION-* "
                    "cookies via a fresh browser login."),
        )

    # (c) Affirmative VALID: URL rewritten into the Pega app harness AND
    #     body carries Pega's authenticated state markers.
    url_ok = bool(PEGA_AUTH_URL_RE.search(final_url))
    body_ok = sum(1 for m in PEGA_AUTH_MARKERS if m.search(body)) >= 2
    if url_ok and body_ok:
        return SessionProbeResult(
            status="VALID",
            failure_layer=None,
            final_url=final_url,
            landing_excerpt=excerpt,
            detail=None,
        )

    return SessionProbeResult(
        status="UNKNOWN",
        failure_layer=None,
        final_url=final_url,
        landing_excerpt=excerpt,
        detail=("probe did not match Imperva, Pega-login, or Pega-auth "
                "markers — treat as expired and tighten markers from "
                "the diagnostic excerpt"),
    )


# ---------------------------------------------------------------------------
# Search — county filter, foreclosure court, bounded backward window
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SearchPlan:
    county: str
    days_back: int
    filter_label: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> datetime:
    return datetime.now(timezone.utc)


def _extract_pega_form_state(html: str) -> dict[str, str]:
    """Pega harness pages emit a battery of hidden inputs (pyActivity,
    pyOrigActivity, pyForUpload, harness identifiers, etc.) that must
    be echoed on every POST. We pull them out of the form whose action
    points at the same harness.
    """
    state: dict[str, str] = {}
    for m in re.finditer(
        r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]*value="([^"]*)"',
        html, re.I,
    ):
        name, val = m.group(1), m.group(2)
        if name in state:
            continue
        state[name] = val
    return state


def _find_form_action(html: str) -> Optional[str]:
    m = re.search(r'<form[^>]+action="([^"]+)"', html, re.I)
    if m:
        return m.group(1)
    return None


def search_foreclosure_cases(
    session: requests.Session,
    *,
    county: str = "Ocean",
    days_back: int = 30,
) -> tuple[list[dict], dict[str, Any]]:
    """Drive the Pega CIVSearch form filtered to:

        Court     = Civil — Foreclosure
        County    = <county>
        Filed-from = today − days_back

    Returns (records, diagnostics). On the FIRST run after the operator
    seeds cookies, the diagnostics block captures the harness URL +
    form-state keys so the parser can be tightened to the actual layout
    we observe live. (Pega tokenizes element names per session, so we
    cannot hard-code them blind; this code does discovery.)
    """
    diagnostics: dict[str, Any] = {
        "search_plan": asdict(SearchPlan(
            county=county, days_back=days_back,
            filter_label=f"Civil—Foreclosure, {county} County, last {days_back}d",
        )),
        "started_at": _now_iso(),
    }

    entry = session.get(SEARCH_APP_ENTRY, timeout=30, allow_redirects=True)
    html = entry.text
    diagnostics["entry_url"] = entry.url
    diagnostics["entry_status"] = entry.status_code

    # Pull the harness URL the form posts back to.
    action = _find_form_action(html) or entry.url
    if action.startswith("/"):
        action = SEARCH_APP_BASE + action
    diagnostics["form_action"] = action

    state = _extract_pega_form_state(html)
    diagnostics["form_state_keys"] = sorted(state.keys())[:60]
    diagnostics["form_state_count"] = len(state)

    # ⚠️ Pega field names are session-tokenized. The first live run will
    # capture the actual field names into diagnostics; subsequent runs
    # tighten this map. For now we set the canonical Pega "submit" key
    # so the harness re-renders into the search panel.
    candidates_for_county = [k for k in state if "county" in k.lower()]
    candidates_for_court = [k for k in state if "court" in k.lower()]
    candidates_for_from_date = [
        k for k in state
        if any(s in k.lower() for s in ("fromdate", "from_date", "filedfrom"))
    ]
    diagnostics["candidate_keys"] = {
        "county": candidates_for_county,
        "court": candidates_for_court,
        "from_date": candidates_for_from_date,
    }

    # Best-effort payload. If the candidates are empty, the diagnostics
    # capture is the deliverable and the caller decides next steps.
    from_date = (_today() - timedelta(days=days_back)).strftime("%m/%d/%Y")
    payload = dict(state)
    if candidates_for_county:
        payload[candidates_for_county[0]] = county
    if candidates_for_court:
        payload[candidates_for_court[0]] = "Civil-Foreclosure"
    if candidates_for_from_date:
        payload[candidates_for_from_date[0]] = from_date
    payload.setdefault("pyActivity", "ProcessAction")

    diagnostics["payload_sample"] = {
        k: payload[k][:80] if isinstance(payload.get(k), str) else payload.get(k)
        for k in list(payload)[:30]
    }

    resp = session.post(
        action,
        data=payload,
        headers={"Referer": entry.url},
        timeout=60,
    )
    diagnostics["search_status"] = resp.status_code
    diagnostics["search_url"] = resp.url
    diagnostics["search_body_len"] = len(resp.text)

    records = _parse_results(resp.text, diagnostics=diagnostics,
                             county=county)
    diagnostics["records_parsed"] = len(records)
    return records, diagnostics


# ---------------------------------------------------------------------------
# Results parser — discovery-mode tolerant
# ---------------------------------------------------------------------------


_DOCKET_RE = re.compile(r"\bF-?(\d{4,8}-\d{2})\b")        # Pega foreclosure docket
_CASE_TYPE_RES = (
    re.compile(r"\b(Mortgage Foreclosure|Tax Foreclosure|Strict Foreclosure|"
               r"Residential Foreclosure)\b", re.I),
)
_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")


def _parse_results(html: str, *, diagnostics: dict[str, Any],
                   county: str) -> list[dict]:
    """Pull case rows out of the search-result harness.

    The Pega result grid is rendered as a table with class containing
    `pegag` or `dx-grid` or `Grid` — we walk every `<tr>` inside the
    largest table on the page and emit one record per row that contains
    a docket pattern. Subsequent live runs will narrow this to the
    exact result-table id once observed.
    """
    rows: list[dict] = []
    # Capture all <tr>...</tr> blocks (greedy across tables)
    table_matches = list(re.finditer(r"<table[^>]*>(.*?)</table>",
                                     html, re.I | re.S))
    if not table_matches:
        diagnostics["results_parse_note"] = "no <table> in response"
        return rows

    biggest = max(table_matches, key=lambda m: len(m.group(0)))
    diagnostics["chosen_table_size"] = len(biggest.group(0))
    for tr in re.finditer(r"<tr[^>]*>(.*?)</tr>", biggest.group(1),
                          re.I | re.S):
        tr_html = tr.group(1)
        text = re.sub(r"<[^>]+>", " ", tr_html)
        text = re.sub(r"\s+", " ", text).strip()
        m = _DOCKET_RE.search(text)
        if not m:
            continue
        docket = m.group(0)
        dates = _DATE_RE.findall(text)
        case_type = None
        for ct_re in _CASE_TYPE_RES:
            cm = ct_re.search(text)
            if cm:
                case_type = cm.group(1)
                break
        rows.append({
            "docket": docket,
            "case_type": case_type,
            "filed_dates_seen": dates,
            "row_text": text[:600],
            "court_county": county,
        })
    return rows


# ---------------------------------------------------------------------------
# Wrap + persist
# ---------------------------------------------------------------------------


def _wrap(row: dict, *, fetched_at: str) -> dict:
    docket = row["docket"]
    raw_record_id = f"{SOURCE_ID}:{docket}"
    primary_event_date = None
    if row.get("filed_dates_seen"):
        # Heuristic: smallest date is usually the filed date.
        try:
            sorted_iso = sorted(
                datetime.strptime(d, "%m/%d/%Y").date().isoformat()
                for d in row["filed_dates_seen"]
            )
            primary_event_date = sorted_iso[0]
        except ValueError:
            pass
    payload = {
        "chancery_docket": docket,
        "court_county": row.get("court_county"),
        "case_type": row.get("case_type") or "Foreclosure (uncategorized)",
        "filed_date": primary_event_date,
        "primary_event_date": primary_event_date,
        "doc_type": "foreclosure_complaint",
        "row_text_excerpt": row.get("row_text"),
    }
    return {
        "raw_record_id": raw_record_id,
        "source_id": SOURCE_ID,
        "source_url": SEARCH_APP_ENTRY,
        "source_fetched_at": fetched_at,
        "parser_confidence": 60,        # discovery mode; bump after live tighten
        "source_role": "PRIMARY_EVENT_SOURCE",
        "lead_origin_type": "RECORDED_EVENT",
        "raw_payload": payload,
    }


def _emit_blocked_record(out_path: Path, status: str, detail: str,
                          captured_at: Optional[str]) -> None:
    """When the session is MISSING / MALFORMED / EXPIRED, write a single
    BLOCKED_SOURCE record into the source's JSONL so downstream code can
    surface the blocker honestly per §6.7 / §5.7 (the dashboard never
    silently shows a stale board).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "raw_record_id": f"{SOURCE_ID}:_BLOCKED_",
        "source_id": SOURCE_ID,
        "source_url": SEARCH_APP_ENTRY,
        "source_fetched_at": _now_iso(),
        "parser_confidence": 0,
        "source_role": "BLOCKED_SOURCE",
        "lead_origin_type": None,
        "raw_payload": {
            "status": status,
            "detail": detail,
            "operator_session_captured_at": captured_at,
            "operator_unlock": (
                "re-run the registration + cookie-capture handoff and "
                "rewrite runs/ocean_nj/.session_njcourts.json"
            ),
        },
    }
    with out_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _write_reseed_marker(reason: str, captured_at: Optional[str],
                         probe: Optional[SessionProbeResult]) -> None:
    """§6.5 last-good preservation hook. The daily refresh workflow reads
    this on `steps.gate.outcome != 'success'` to decide preserve-vs-publish.
    """
    LAST_FAIL_PATH.parent.mkdir(parents=True, exist_ok=True)
    layer = probe.failure_layer if probe else None
    operator_action = {
        "imperva": ("Imperva WAF rejected the request. Re-seed by opening "
                    "https://www.njcourts.gov in a fresh browser tab to "
                    "regenerate the visid_incap_* / nlbi_* / incap_ses_* "
                    "cookies, then capture all seven cookies again."),
        "pega":    ("Pega ESSO session expired. Log in again at "
                    "https://portal-cloud.njcourts.gov/prweb/PRAuth/CloudSAMLAuth "
                    "?AppName=ESSO and recapture all seven cookies."),
    }.get(layer or "", (
        "Re-seed runs/ocean_nj/.session_njcourts.json per "
        "runs/ocean_nj/recon/operator_verified_sources.yml."))
    payload = {
        "source_id": SOURCE_ID,
        "failure_kind": reason,
        "failure_layer": layer,
        "failed_at": _now_iso(),
        "operator_session_captured_at": captured_at,
        "needs_operator_action": operator_action,
        "probe_final_url": probe.final_url if probe else None,
        "probe_excerpt": probe.landing_excerpt if probe else None,
    }
    LAST_FAIL_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run(
    *,
    out_path: Path = OUTPUT_PATH,
    session_path: Path = SESSION_PATH,
    days_back: int = 30,
    diagnostics_path: Optional[Path] = None,
    probe_only: bool = False,
) -> int:
    """Return-code contract:

        0  — VALID session, records (or zero-but-real) written.
        0  — MISSING / MALFORMED session, BLOCKED record written
             (graceful degrade; build still publishes from other sources).
        2  — EXPIRED session, last-good marker written, no data written
             (§6.5 last-good preservation runs).
        3  — Unhandled internal error.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    load = load_seeded_session(session_path)
    print(f"[{SOURCE_ID}] session load: status={load.status} "
          f"cookies={load.cookie_count} user={load.user_label}")

    if load.status in ("MISSING", "MALFORMED"):
        _emit_blocked_record(out_path, load.status, load.detail or "",
                             load.captured_at)
        print(f"[{SOURCE_ID}] BLOCKED ({load.status}): {load.detail}",
              file=sys.stderr)
        return 0

    assert load.session is not None
    probe = probe_session(load.session)
    print(f"[{SOURCE_ID}] session probe: {probe.status} "
          f"layer={probe.failure_layer} ({probe.final_url})")

    if probe.status in ("EXPIRED", "WAF_BLOCKED", "UNKNOWN"):
        _emit_blocked_record(out_path, probe.status,
                             probe.detail or f"probe verdict {probe.status}",
                             load.captured_at)
        _write_reseed_marker(
            f"session_{probe.status.lower()}",
            load.captured_at, probe,
        )
        print(f"[{SOURCE_ID}] {probe.status} — last-good preservation should "
              "run (see runs/ocean_nj/last_failed_refresh.json)",
              file=sys.stderr)
        return 2

    if probe_only:
        print(f"[{SOURCE_ID}] probe-only: session is VALID; exiting before "
              "search.")
        return 0

    rows, diagnostics = search_foreclosure_cases(
        load.session, county="Ocean", days_back=days_back,
    )
    if diagnostics_path:
        diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
        diagnostics_path.write_text(
            json.dumps(diagnostics, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    fetched_at = _now_iso()
    wrapped = [_wrap(r, fetched_at=fetched_at) for r in rows]
    with out_path.open("w", encoding="utf-8") as fh:
        for r in wrapped:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[{SOURCE_ID}] window_days={days_back} records_written={len(wrapped)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=str(OUTPUT_PATH))
    p.add_argument("--session", default=str(SESSION_PATH),
                   help="Operator cookie jar path")
    p.add_argument("--days-back", type=int, default=30)
    p.add_argument("--diagnostics-out", default=None,
                   help="Where to dump first-run discovery diagnostics")
    p.add_argument("--probe-only", action="store_true",
                   help="Stop after the VALID/EXPIRED/UNAUTHENTICATED probe")
    args = p.parse_args()
    try:
        return run(
            out_path=Path(args.out),
            session_path=Path(args.session),
            days_back=args.days_back,
            diagnostics_path=Path(args.diagnostics_out) if args.diagnostics_out else None,
            probe_only=args.probe_only,
        )
    except Exception as exc:
        print(f"[{SOURCE_ID}] internal error: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
