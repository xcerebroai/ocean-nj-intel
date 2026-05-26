"""Run all PRIMARY_EVENT_SOURCE scrapers for Ocean County NJ.

Critical sources — failure aborts the daily refresh. Today this runs S1
(sheriff foreclosures) only. S2 (Surrogate Bluestone) needs Playwright
and is wired into run_browser_sources.py instead.
"""

from __future__ import annotations

import sys

from scrapers.sheriff_foreclosure import run as run_sheriff


def main() -> int:
    rc = 0
    rc |= run_sheriff()
    rc |= __import__("scrapers", fromlist=["_blocked_sources"])._blocked_sources.main()  # type: ignore[attr-defined]
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
