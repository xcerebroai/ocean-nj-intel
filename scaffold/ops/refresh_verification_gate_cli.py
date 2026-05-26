"""CLI wrapper for the §6.4 refresh publish gate.

Reads data/leads/scored_leads.json, runs verify_refresh_publishable,
exits 0 on PUBLISH, exits 2 on DO_NOT_PUBLISH (so CI's
`continue-on-error: true` + `steps.gate.outcome` boolean gating works).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from refresh_verification_gate import verify_refresh_publishable


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scored-leads", required=True,
                   help="Path to scored_leads.json (object with 'leads' key, or array)")
    p.add_argument("--report-out", required=True,
                   help="Where to write the gate verdict JSON")
    p.add_argument("--min-lead-count", type=int, default=1)
    p.add_argument("--resolved-owner-floor", type=float, default=0.05)
    p.add_argument("--resolved-address-floor", type=float, default=0.50)
    p.add_argument("--actionable-floor", type=float, default=0.01)
    p.add_argument("--enrichment-join-unavailable", action="store_true",
                   help="Set when county has a §3.7 enrichment outage")
    args = p.parse_args()

    payload = json.loads(Path(args.scored_leads).read_text(encoding="utf-8"))
    leads = payload.get("leads", payload) if isinstance(payload, dict) else payload

    result = verify_refresh_publishable(
        leads,
        min_lead_count=args.min_lead_count,
        min_resolved_owner_fraction=args.resolved_owner_floor,
        min_resolved_address_fraction=args.resolved_address_floor,
        min_actionable_fraction=args.actionable_floor,
        enrichment_join_unavailable=args.enrichment_join_unavailable,
    )

    report = {
        "verdict": result.verdict,
        "reason": result.reason,
        "counts": result.counts,
        "fractions": result.fractions,
        "notes": list(result.notes),
        "args": {
            "min_lead_count": args.min_lead_count,
            "resolved_owner_floor": args.resolved_owner_floor,
            "resolved_address_floor": args.resolved_address_floor,
            "actionable_floor": args.actionable_floor,
            "enrichment_join_unavailable": args.enrichment_join_unavailable,
        },
    }
    out = Path(args.report_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"[refresh_gate] verdict={result.verdict} "
          f"counts={result.counts} fractions={result.fractions}")
    if result.reason:
        print(f"[refresh_gate] reason: {result.reason}")
    return 0 if result.verdict == "PUBLISH" else 2


if __name__ == "__main__":
    raise SystemExit(main())
