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

from scrapers.sheriff_foreclosure import run as run_sheriff
from scrapers.njpa_legal_notices import run as run_njpa
from scrapers._blocked_sources import main as run_blocked_punchlist


def main() -> int:
    rc = 0
    rc |= run_sheriff()               # S1 — critical
    rc |= run_njpa()                  # S6 — supporting, never fails fatal
    rc |= run_blocked_punchlist()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
