"""Run all PRIMARY_EVENT_SOURCE scrapers for Ocean County NJ.

Sources today:

  * S1 sheriff_foreclosure  — stdlib + pdfplumber, always runs
  * S6 njpa_legal_notices   — stdlib + ASP.NET ViewState, always runs
  * _blocked_sources        — emits the BLOCKED_SOURCE punch-list

S4 nj_courts_foreclosure was REJECTED in this pass — recon pulled the
authenticated CIVSearch app and confirmed zero records returned for the
Ocean / Foreclosure filter even with a valid operator session. The
sheriff feed (S1) carries the actual foreclosure-case docket numbers
that would have come from S4. See runs/ocean_nj/recon/recon_summary.md
§S4-REJECTION-NOTE.
"""

from __future__ import annotations

import sys

from scrapers.sheriff_foreclosure import run as run_sheriff
from scrapers.njpa_legal_notices import run as run_njpa
from scrapers.civilview_sheriff_sales import run as run_civilview
from scrapers.hls_brick_taxsale import run as run_hls_brick
from scrapers._blocked_sources import main as run_blocked_punchlist


def _safe(name, fn, critical):
    """Run one source; contain any exception so a single flaky/timing-out source
    cannot crash the whole refresh (v5.5.0 resilience). Returns (ok, is_critical)."""
    try:
        rc = fn()
        ok = (rc == 0)
        if not ok:
            print(f"::warning title=source {name} returned rc={rc}::continuing", file=sys.stderr)
        return ok, critical
    except Exception as exc:  # noqa: BLE001 — contain ALL source failures
        print(f"::warning title=source {name} raised::{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return False, critical


def main() -> int:
    # Each source is isolated: one timing out/raising does NOT kill the rest.
    # CRITICAL = source-of-record feeds; the publish gate (§6.4) is the quality
    # backstop, and §6.5 preserves last-good if the result isn't publishable.
    results = [
        _safe("S1 sheriff_foreclosure", run_sheriff, critical=True),
        _safe("S1' civilview", run_civilview, critical=False),   # dormant today
        _safe("S6 njpa_legal_notices", run_njpa, critical=False),
        _safe("S7c hls_brick_taxsale", run_hls_brick, critical=True),
        _safe("blocked-source punchlist", run_blocked_punchlist, critical=False),
    ]
    crit_ok = [ok for ok, crit in results if crit]
    # Proceed (rc 0) as long as AT LEAST ONE critical source-of-record produced
    # data; only hard-fail if every critical feed failed (nothing to build from).
    if not any(crit_ok):
        print("::error::all critical event sources failed — nothing to build", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
