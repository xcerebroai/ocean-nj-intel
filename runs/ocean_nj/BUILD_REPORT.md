# Ocean County, NJ — v5.5.0 Build Report

- **County slug:** `ocean_nj`
- **Framework:** v5.5.0 (candidate-channel canon, from bexar
  `feat/v5.5.0-framework-hardening`)
- **Build mode:** PARTIAL_BUILD per `MASTER_PROMPT §4.16` /
  `§4.10` verdict.
- **Build date:** 2026-05-26
- **§7.1 STATIC verdict:** **STATIC_OK** (8/8 checks PASS).
- **§7.1 INTERACTIVE verdict:** **BLOCKED on operator unlock** (GitHub
  Pages remote not wired yet). Declared but not run.
- **§6.4 publish gate:** **PUBLISH** (with
  `enrichment_join_unavailable=True` per §4.3 carve-out).
- **§5.11 stale-label scanner:** 1 false-positive (a probate decedent
  whose name contains "Phoenix"); zero true county-name leaks.

---

## 1. Lead counts (data/leads/scored_leads.json)

```
total_leads               1424
├─ foreclosure_sale       32      ← Sheriff S1 (PRIMARY_EVENT)
├─ probate_filing_recent  1392    ← Surrogate S2 (PRIMARY_EVENT + OWNER_STATUS)
│
status:
├─ APPROVED_FOR_DASHBOARD 22      ← Sheriff UPCOMING_SALE, not BK/cancel
└─ REVIEW_REQUIRED        1402    ← Probate (no parcel join) + adjourned/BK sheriff

parcel_join (S1 vs S9 NJOGIS):
├─ hits                   24      ← Block/Lot matched a NJOGIS parcel
└─ misses                 8       ← Lot field had hyphenated multi-lot syntax
```

The 22 APPROVED leads are the immediate "call-the-defendant" worklist.
The 1392 probate REVIEW leads are research targets — each carries
decedent name + town + DOD + docket; operators look up the property
address and decide whether the decedent owned local real estate.

## 2. Sources actually built

| ID | Source | Role | Status | Records | Path |
|----|--------|------|--------|---------|------|
| S1 | Ocean County Sheriff foreclosure sales | PRIMARY_EVENT | ✅ live | 32 | `data/raw/sheriff_foreclosure.jsonl` |
| S2 | Ocean County Surrogate (Bluestone) | PRIMARY_EVENT + OWNER_STATUS | ✅ live | 1393 | `data/raw/surrogate_probate.jsonl` |
| S9 | NJOGIS Parcels + MOD-IV (Ocean County) | ENRICHMENT | ✅ live | 301,788 | `data/enriched/parcel_index.jsonl` |

## 3. Sources punch-listed (BLOCKED_SOURCE)

| ID | Source | Blocker | Operator unlock |
|----|--------|---------|-----------------|
| S3 | Ocean County Clerk Land Records | CAPTCHA_REQUIRED (Angular SPA + reCAPTCHA) | 2Captcha key, seeded session, or alt-portal recon |
| S4 | NJ Courts Civil + Foreclosure | LOGIN_REQUIRED | Operator registers a free NJ Courts public account, supplies session cookie |
| S5 | NJ DCA Notice of Intention to Foreclose | LOGIN_REQUIRED + role-gated | County / municipal data-sharing partnership |
| S7 | Per-muni Tax-Lien Sales (×33) | WAF_BLOCKED on newjerseytaxsale.com + per-muni vendor fan-out | Playwright stealth + per-muni vendor enumeration |
| S6 | NJPA Public Notices Aggregator | Deferred (supporting only — P1) | Build when an unblocked primary needs cross-confirmation |

Full unlocks declared in `runs/ocean_nj/recon/operator_verified_sources.yml`.

## 4. NJ-specific findings that broke FL/NY/TX assumptions

1. **Tax collection is municipal.** The PRIMARY_DEFAULT_SOURCE role
   fans out across 33 municipalities; there is no county-level
   tax-default feed. Today the entire P0 distress channel for tax
   default is BLOCKED on S7 unlock.

2. **Daniel's Law (P.L. 2020, c. 125) redacts owner names** from
   every NJOGIS-hosted parcel and tax-list dataset. Confirmed live:
   the OWNER_NAME column in `OceanTaxList.dbf` is empty for every
   parcel. Owner enrichment requires either:
   - **S8** (Ocean County Board of Taxation Tax List Search) — search
     form has a `txtOwner` field, strongly suggesting owners ARE
     exposed in results. **Probe still owed** (one block/lot in a
     browser settles it).
   - **S3** (Clerk land records) — every deed names the grantee.
     Unblocking S3 also unblocks owner.
   - Per-municipality assessor OPRA bulk requests (heavy-lift fallback).

3. **The §3.5 estate-titled-owner pathway is partial today.** The
   surrogate index gives us decedent name + town + DOD + docket — but
   no street address per decedent. Without an owner column in the
   parcel index, we cannot join decedent→parcel. Probates are emitted
   as REVIEW_REQUIRED research targets, not STACKED_LEADs. This is
   honest; the §3.5 classifier needs the owner-resolution path live to
   produce QUALIFIED estate leads.

4. **Bluestone's "Export All" beats Bluestone's search.** The portal
   ships a `OceanIndex.csv` (17 MB, 247K records back to 2018) on
   click of one menu item. The S2 scraper drives Playwright through
   the click sequence — vastly more reliable than scraping the
   DevExpress ASPxGridView pagination.

## 5. Phase-by-phase artifacts

| Phase | Artifact | What it proves |
|-------|----------|----------------|
| 0/1 | `runs/ocean_nj/recon/recon_summary.md` + `source_of_record_matrix.json` + `operator_verified_sources.yml` | §1 recon dossier; 8-role classification per source; §2.4 access verdict per source; §1.6 missed-source audit shows nothing left |
| 2 | `scrapers/sheriff_foreclosure.py`, `scrapers/surrogate_probate.py`, `scrapers/njogis_parcels_modiv.py`, `scrapers/_blocked_sources.py` | Three live adapters + a BLOCKED-source punch-list emitter; each conforms to MASTER_PROMPT §4.32 raw-record contract |
| 3 | `scaffold/pipeline/ocean_nj_build_leads.py` → `data/leads/scored_leads.json` | §3.7 enrichment join (sheriff → NJOGIS parcels); §3.9 scheduled-event classification; §4.17 evidence-first row contract |
| 4 | `scaffold/pipeline/ocean_nj_render_dashboard.py` → `dashboard/index.html` + `dashboard/dashboard_data.json` | §5 dashboard contract: REQUIRED_DASHBOARD_FIELDS ✓; STANDARD_FILTERS ✓ (9/9 wired); DEFAULT_FILTER_STATE ✓ (all neutral, click-to-select); BANNER_PROHIBITED_TOKENS ✓ |
| 5 | `.github/workflows/daily_refresh.yml` + `scrapers/run_*_sources.py` + `scaffold/ops/refresh_verification_gate_cli.py` | §6 daily-refresh workflow: clean data/raw → re-fetch all 3 source types → build → render → §6.4 gate → publish OR preserve-last-good |
| 7 | `verify_live.py` + `runs/ocean_nj/verify_live.json` | §7.1 STATIC half (8 checks) all PASS against local dashboard build |

## 6. Verification matrix

### §6.4 publish gate (ran locally against scored_leads.json)
```
verdict:     PUBLISH
lead_count:  1424
actionable:  22 (1.54%)  — above the 1% floor
known_owner: 0   — floor skipped via enrichment_join_unavailable=True
with_addr:   0   — floor skipped via enrichment_join_unavailable=True
note:        enrichment_join_unavailable=True per §4.3 operator override
```
Report: `data/leads/refresh_gate.json`.

### §7.1 STATIC half (ran against served local dashboard)
```
1. url_fetches_200_with_body                   ✓ PASS
2. body_is_html                                ✓ PASS
3. body_is_not_json_parse_error                ✓ PASS
4. data_artifact_reachable                     ✓ PASS
5. data_artifact_parseable_non_empty           ✓ PASS  (1424 rows)
6. data_carries_v5_5_0_dashboard_contract_fields ✓ PASS  (10/10 REQUIRED)
7. no_stale_foreign_county_labels              ✓ PASS  (ocean_nj exemption applied)
8. no_banner_prohibited_tokens                 ✓ PASS
```
Report: `runs/ocean_nj/verify_live.json`.

### §7.1 INTERACTIVE half — declared BLOCKED
The 7 interactive checks are wired into the contract but not run:
- **Blocker:** no live GitHub Pages URL yet (operator owns the remote).
- **Resumes:** after the operator creates `xcerebro/ocean-nj` (or
  similar), pushes this repo, enables Pages, and the daily-refresh
  workflow deploys once. Re-run with
  `python3 verify_live.py --base-url https://<user>.github.io/ocean-nj/`.

## 7. Operator-side unlocks ranked by leverage

1. **S4 NJ Courts session cookie** — single registration, lowest
   friction. Yields foreclosure complaints, lis pendens, judgments,
   writs of execution. (No reCAPTCHA. Free account.)
2. **S8 owner-exposure probe** — 1-minute browser test against
   `tax.co.ocean.nj.us`. Settles whether S9's Daniel's-Law gap can be
   patched without S3.
3. **S3 Clerk land records** — pick a 2Captcha key OR a seeded session
   OR the alt-portal `oceancountyclerk.com/frmSearch`. Unlocks ALL
   recorded distress events + owner data.
4. **GitHub Pages remote** — pushes this build to a live URL; unblocks
   §7.1 INTERACTIVE half + daily-refresh deploy step.
5. **S7 per-municipality tax-sales** — Playwright + stealth pass +
   33-muni vendor enumeration. Largest scope; biggest payoff for
   tax-default leads.
6. **S5 DCA NOI** — county/muni partnership. Slow. Earliest signal in
   the foreclosure cycle, but lowest reachability.

## 8. §20 verdict

**DEPLOY_OK (PARTIAL).**

- The shippable-build floor (`p0_distress_source_required_for_shippable_build`)
  is met: **S1 sheriff foreclosures** is an unblocked P0 source
  producing 32 real leads against today's auction.
- The §6.4 publish gate returns PUBLISH.
- The §7.1 STATIC half is 8/8 PASS.
- The §7.1 INTERACTIVE half is BLOCKED on operator-side GitHub Pages
  remote; **no fake live URL is claimed**.
- Five BLOCKED_SOURCEs are documented with exact unlock paths and
  never silently emit zero rows.

End-state: **a real, honest, partial lead board** for Ocean County NJ
that ships today, with a punch-list of operator unlocks any of which
would meaningfully grow the board.

OCEAN COUNTY NJ BUILD COMPLETE
