"""Punch-list of BLOCKED_SOURCEs for Ocean County NJ build (v5.5.0 §2.4).

Each entry below corresponds to a BLOCKED_SOURCE in
runs/ocean_nj/recon/source_of_record_matrix.json with an explicit
operator unlock path. Running this script emits a JSONL blocker
report so the daily refresh and dashboard surfaces are honest about
which event streams are missing today.

The framework canon (`MASTER_PROMPT.md §4.21` / §4.26) requires that
BLOCKED_SOURCEs do NOT silently emit zero rows — they must declare
themselves blocked so a refresh-time gate can choose between failing
loudly or preserving last-good.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BLOCKER_REPORT = REPO_ROOT / "data" / "raw" / "_blocked_sources.jsonl"


BLOCKED = [
    {
        "source_id": "clerk_land_records",
        "name": "Ocean County Clerk Land Records",
        "url": "https://sng.co.ocean.nj.us/publicsearch/",
        "intended_role": "PRIMARY_EVENT_SOURCE",
        "failure_classification": "CAPTCHA_REQUIRED",
        "blocker_detail": ("Angular SPA + reCAPTCHA on every query; no anonymous "
                           "machine path."),
        "operator_unlock": [
            "Playwright + 2Captcha/anticaptcha (captcha_solver_allowed: true)",
            "Operator-seeded session cookie (operator_seeded_session_allowed: true)",
            "Recon alternate portal: oceancountyclerk.com/frmSearch",
        ],
        "intended_lead_origins": ["RECORDED_EVENT", "POST_SALE_TITLE_EVENT",
                                  "OWNER_STATUS"],
    },
    {
        "source_id": "nj_courts_civil",
        "name": "NJ Courts Civil + Foreclosure Public Access",
        "url": ("https://www.njcourts.gov/public/find-a-case/"
                "civil-and-foreclosure-public-access"),
        "intended_role": "PRIMARY_EVENT_SOURCE",
        "failure_classification": "LOGIN_REQUIRED",
        "blocker_detail": ("Registration required for all searches; no "
                           "anonymous public tier."),
        "operator_unlock": [
            "Operator registers public NJ Courts account; supplies session "
            "cookie via operator_verified_sources.yml.nj_courts_civil.session_cookie"
        ],
        "intended_lead_origins": ["RECORDED_EVENT"],
    },
    {
        "source_id": "nj_dca_noi",
        "name": "NJ DCA Notice of Intention to Foreclose Database",
        "url": "https://www.nj.gov/dca/foreclosure.html",
        "intended_role": "PRIMARY_EVENT_SOURCE",
        "failure_classification": "LOGIN_REQUIRED",
        "blocker_detail": ("Read-only access restricted to Counties, "
                           "Municipalities, State Agencies."),
        "operator_unlock": [
            "County- or municipality-level data-sharing arrangement",
        ],
        "intended_lead_origins": ["RECORDED_EVENT"],
    },
    {
        "source_id": "muni_tax_sales",
        "name": "Per-Municipality Tax-Lien Sales (33 sub-feeds)",
        "url": "https://<municipality>.newjerseytaxsale.com",
        "intended_role": "PRIMARY_DEFAULT_SOURCE",
        "failure_classification": "WAF_BLOCKED",
        "blocker_detail": ("Direct fetch returns HTTP 403 (WAF); per-muni "
                           "vendor enumeration also incomplete (33 munis "
                           "split across newjerseytaxsale.com, RealAuction, "
                           "MicroBilt, and in-person)."),
        "operator_unlock": [
            "Playwright + stealth (stealth_browser_allowed: true)",
            "Per-municipality vendor recon pass; populate "
            "operator_verified_sources.yml.muni_tax_sales.per_muni_overrides",
        ],
        "intended_lead_origins": ["TAX_DEFAULT"],
    },
    {
        "source_id": "njpa_public_notices",
        "name": "NJPA Public Notices Aggregator",
        "url": "https://www.njpublicnotices.com/Search.aspx",
        "intended_role": "SUPPORTING_EVENT_SOURCE",
        "failure_classification": "DEFERRED_TO_NEXT_RELEASE",
        "blocker_detail": ("Reachable but classified P1 — cannot be a sole "
                           "distress feed per v5.5.0 P-tier rules. Wired "
                           "into the blocker report only so the catalog "
                           "stays honest; will be built when an unblocked "
                           "primary source needs cross-confirmation."),
        "operator_unlock": [
            "Operator authorization to build the supporting adapter (low "
            "priority — depends on the §6.4 gate verdict)",
        ],
        "intended_lead_origins": [],
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(BLOCKER_REPORT))
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with out_path.open("w", encoding="utf-8") as fh:
        for src in BLOCKED:
            rec = dict(src)
            rec["source_role"] = "BLOCKED_SOURCE"
            rec["reported_at"] = now
            rec["v5_5_0_recon_anchor"] = (
                f"runs/ocean_nj/recon/source_of_record_matrix.json#sources"
            )
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"[_blocked_sources] {len(BLOCKED)} BLOCKED_SOURCEs reported -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
