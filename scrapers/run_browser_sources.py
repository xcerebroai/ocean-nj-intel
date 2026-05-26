"""Run all Playwright-driven event sources for Ocean County NJ.

Fragile — failure here is `continue-on-error` per §6.7. Today this runs
S2 (Surrogate Bluestone). When S3 (Clerk land records) unlocks via
2Captcha or seeded session, its Playwright scraper goes here too.
"""

from __future__ import annotations

import sys

from scrapers.surrogate_probate import run as run_surrogate


def main() -> int:
    return run_surrogate(window_days=90)


if __name__ == "__main__":
    raise SystemExit(main())
