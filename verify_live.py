"""Ocean County, NJ — §7.1 live-URL verification entry point.

Runs the STATIC half from a local dashboard build (or a live URL if
supplied). The INTERACTIVE half is BLOCKED until the operator wires the
GitHub Pages remote.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent
COUNTY_SLUG = "ocean_nj"
DASH = REPO_ROOT / "dashboard"

sys.path.insert(0, str(REPO_ROOT / "scaffold" / "ops"))

from verify_live_contract import (  # noqa: E402
    declared_interactive_check_names,
    run_static_checks,
)


def _fetch(url: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 ocean-nj-verify/v5.5.0"})
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=None,
                   help="Live base URL (omit to read local dashboard/ files)")
    p.add_argument("--out", default=None, help="JSON report output path")
    args = p.parse_args()

    if args.base_url:
        html_url = args.base_url.rstrip("/") + "/"
        data_url = args.base_url.rstrip("/") + "/dashboard_data.json"
        html = _fetch(html_url)
        data_text = _fetch(data_url)
    else:
        html = (DASH / "index.html").read_text(encoding="utf-8")
        data_text = (DASH / "dashboard_data.json").read_text(encoding="utf-8")

    result = run_static_checks(
        dashboard_html=html,
        data_artifact_text=data_text,
        current_county_slug=COUNTY_SLUG,
    )

    report = {
        "half": result.half,
        "verdict": result.verdict,
        "checks": [asdict(c) for c in result.checks],
        "interactive_checks_declared_but_blocked": list(
            declared_interactive_check_names()
        ),
        "interactive_blocked_reason": (
            "operator unlock pending: GitHub Pages remote not wired yet "
            "(Phase 4 decision). The INTERACTIVE half re-runs after the "
            "live URL exists and the daily-refresh workflow has deployed."
        ),
    }

    print(f"[verify_live] STATIC verdict: {result.verdict}")
    for c in result.checks:
        marker = {"PASS": "✓", "FAIL": "✗", "SKIPPED": "·"}.get(c.status, "?")
        print(f"  {marker} {c.name}" + (f" — {c.detail}" if c.detail else ""))

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"[verify_live] report -> {out}")

    return 0 if result.verdict == "STATIC_OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
