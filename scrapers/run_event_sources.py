"""Run all PRIMARY_EVENT_SOURCE scrapers for Ocean County NJ.

Critical sources — failure of a TRULY critical one aborts the daily
refresh. Sources today:

  * S1 sheriff_foreclosure    — stdlib + pdfplumber, always runs
  * S4 nj_courts_foreclosure  — operator-seeded session per §2.2;
                                returns 0 if cookies missing
                                (graceful degrade), returns 2 if
                                cookies expired (§6.5 last-good path)
  * _blocked_sources         — emits the BLOCKED_SOURCE punch-list
"""

from __future__ import annotations

from scrapers.sheriff_foreclosure import run as run_sheriff
from scrapers.nj_courts_foreclosure import run as run_courts
from scrapers._blocked_sources import main as run_blocked_punchlist


def main() -> int:
    """Returns 0 on success of the *critical* sources. S4's session-expiry
    failures (rc=2) are surfaced via the marker file + the BLOCKED record
    in data/raw/nj_courts_foreclosure.jsonl, NOT by failing this step —
    the rest of the build still produces a publishable board from S1 +
    S2 + S9. The §6.5 last-good preservation path runs only if the §6.4
    publish gate downstream rejects the overall result.
    """
    rc = 0
    rc |= run_sheriff()                 # S1 — critical
    rc |= run_blocked_punchlist()       # punch-list emit, never fails

    courts_rc = run_courts()
    if courts_rc == 2:
        # Session needs re-seed; marker already written. Don't fail this
        # step — the operator sees the warning in runs/ocean_nj/last_failed_refresh.json
        # and in the dashboard manifest.
        print("[run_event_sources] S4 nj_courts_foreclosure needs re-seed "
              "(see runs/ocean_nj/last_failed_refresh.json) — continuing.")
    elif courts_rc != 0:
        rc |= courts_rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
