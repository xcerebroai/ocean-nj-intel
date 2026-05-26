"""Run all ENRICHMENT_SOURCE scrapers for Ocean County NJ.

Critical — failure here fails the daily refresh per §6.4 (enrichment
must reproduce in CI). Today this runs S9 (NJOGIS Ocean County parcels
+ MOD-IV).
"""

from __future__ import annotations

from scrapers.njogis_parcels_modiv import run as run_njogis


def main() -> int:
    return run_njogis(force_download=True)


if __name__ == "__main__":
    raise SystemExit(main())
