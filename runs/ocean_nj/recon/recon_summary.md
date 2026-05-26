# Ocean County, New Jersey — Phase 0 Recon Summary

- **County:** Ocean County, New Jersey
- **Slug:** `ocean_nj`
- **Framework:** v5.5.0 (candidate)
- **Recon date:** 2026-05-26
- **State pattern:** First NJ county in the harness — no FL/NY/TX assumptions carried over.
- **Verdict:** **PROCEED_WITH_PARTIAL_BUILD** (per `MASTER_PROMPT.md §4.10`).
  - **Two unblocked PRIMARY_EVENT_SOURCEs** ship a real lead board on day one
    (Ocean County Sheriff foreclosure sales, Ocean County Surrogate / Bluestone).
  - **One unblocked PRIMARY_OWNER_STATUS_SOURCE** (Bluestone duplicates).
  - **Five BLOCKED_SOURCEs** with declared operator unlocks (land records,
    NJ Courts civil/foreclosure, NJ DCA NOI, per-municipality tax-sale
    portals, owner-bearing parcel data).
  - **One ENRICHMENT_SOURCE** without owner names (NJOGIS / Daniel's Law) —
    so enrichment supplies geometry + address + block/lot, **not owner**.
    The §6.4 publish gate must therefore allow `enrichment_join_unavailable`
    for owner_resolved_fraction on day one, or owner-status enrichment must
    be sourced from the (BLOCKED) Ocean County Tax Board owner column.

The verdict is **not** PROCEED_FULL because four of the eight source classes the
operator named — tax-lien default, land records, NJ Courts foreclosure cases,
and DCA NOI — are gated behind unlocks the operator must perform. A
shippable-but-honest build can land before those unlock; the punch-list at
the bottom records each one with the exact next step.

---

## 1. NJ-specific structural notes (these break FL/NY/TX assumptions)

1. **Tax collection is MUNICIPAL, not county.** Ocean County has 33
   municipalities (14 townships + 19 boroughs). Each runs its own tax
   collector, its own annual tax sale, and its own delinquency list. There
   is no single "county tax collector" feed. The `PRIMARY_DEFAULT_SOURCE`
   role therefore fans out across ≤33 sub-feeds, not one.

2. **Daniel's Law (P.L. 2020, c. 125) redacts owner names** from every
   parcel and tax-list dataset hosted by NJ Office of GIS (NJOGIS). The
   statewide MOD-IV download is therefore an ENRICHMENT_SOURCE for parcel
   geometry, address, block/lot, and assessment value — **but not for
   owner names**. Owner names must come from a non-NJOGIS authority
   (Ocean County Board of Taxation if its public search exposes them,
   or the per-municipality assessor offices).

3. **Two distinct foreclosure systems run in parallel.** Sheriff conducts
   the auction. NJ Superior Court Chancery Division manages the case
   docket. A complete foreclosure-event picture requires both — sheriff
   PDFs cover upcoming sales; NJ Courts covers filings, lis pendens,
   judgments, and case status changes. They are not interchangeable.

4. **Ocean County is one of only two NJ counties with full Surrogate
   online public access** (per Surrogate's office own statement). The
   `Bluestone Public Search` portal exposes wills, executor / administrator
   appointments, dockets, and decedent records — i.e., §3.5 estate-titled
   owner origination is unusually well-served here.

5. **Public-notice publication mandate changed in 2025.** Under recent
   NJ law, public entities must publish legal notices on their own
   municipal websites starting March 1, 2026; the NJPA aggregator
   (njpublicnotices.com) remains operational but its coverage may
   thin over the build's lifetime. Treat it as a SUPPORTING_EVENT_SOURCE
   only.

---

## 2. Complete source catalog (v5.5.0 8-role classification)

Source roles (from `scaffold/pipeline/contracts/records.py SOURCE_ROLES`):
`PRIMARY_EVENT_SOURCE`, `PRIMARY_DEFAULT_SOURCE`, `PRIMARY_OWNER_STATUS_SOURCE`,
`SUPPORTING_EVENT_SOURCE`, `ENRICHMENT_SOURCE`, `REFERENCE_SOURCE`,
`BLOCKED_SOURCE`, `REJECTED_SOURCE`.

### 2.1 PRIMARY_EVENT_SOURCE candidates (lead-originating, recorded/scheduled)

#### S1 — Ocean County Sheriff Foreclosure Sales — **UNBLOCKED** ✅
- **URL:** https://sheriff.co.ocean.nj.us/frmForeclosures
- **Data:** Weekly sale schedule (Tuesdays @ 2pm); per-sale list of properties
  posted as a PDF under `co.ocean.nj.us/WebContentFiles/<guid>.pdf`. The PDF
  is Oracle-Reports-generated (text-extractable, 6 pages × ~50 properties).
  Fields: docket #, address, plaintiff, defendant, upset amount.
- **Access §2.4:** **stdlib HTTP + pdfplumber** (no JS, no CAPTCHA, no login).
- **§3.9 classification:** `UPCOMING_SALE` for future sale dates, transitions
  to `PAST_SALE` after sale date; sheriff's deed recording transitions to
  `POST_SALE_TITLE_EVENT` (cross-link to clerk land records once unblocked).
- **§4.5 doc types:** `notice_of_sale`, `sheriff_sale_listing`.
- **Refresh cadence:** Weekly schedule; recommended pull every Mon/Wed/Fri
  to catch list updates and cancellations.

#### S2 — Ocean County Surrogate (Bluestone Public Search) — **UNBLOCKED** ✅
- **URL:** https://surrogateweb.co.ocean.nj.us/BluestoneWeb/default.aspx?FROM_MSG=99
- **Data:** Probate dockets, executor/administrator appointments, wills,
  decedent records. Search fields: last name, first name, docket #, case
  type, birth date, death date, town. Results carry name, case description,
  town, DOB, DOD, docket, filed date, and image access link.
- **Access §2.4:** **stdlib HTTP + ASP.NET ViewState handling** (or
  Playwright for simplicity). DevExpress controls, server-rendered. No
  login. No CAPTCHA. No disclaimer click-through on the main search.
- **§3.5 / §3.9 classification:** **dual role** —
  - `PRIMARY_EVENT_SOURCE`: probate filing = `RECORDED_EVENT` (lead origin).
  - `PRIMARY_OWNER_STATUS_SOURCE`: decedent-with-real-estate ⇒
    `estate_titled_owner` per §3.5 owner_status_classifier (after parcel
    join). The §3.5 `LIFE ESTATE` split applies.
- **§4.5 doc types:** `probate_filing`, `executor_appointment`,
  `administrator_appointment`, `will_record`.
- **Refresh cadence:** Daily. Probate filings drive the highest-value
  estate-origination leads.

### 2.2 PRIMARY_EVENT_SOURCE candidates (BLOCKED — operator unlock required)

#### S3 — Ocean County Clerk Land Records (deeds / mortgages / lis pendens / liens) — **BLOCKED_SOURCE** ⛔
- **URL:** https://sng.co.ocean.nj.us/publicsearch/
- **Failure classification:** `CAPTCHA_REQUIRED` + JS-heavy (Angular SPA).
- **Blocker:** "Please verify that you are not a robot" CAPTCHA on every
  query. Search results are rendered client-side via Angular templating.
  No vendor branding (LandLink / Cott / Tyler not detected) — appears to
  be a custom or generic gov portal.
- **Operator unlock options (pick one):**
  1. **Playwright + 2Captcha/anticaptcha** for reCAPTCHA solving
     (`captcha_solver_allowed: true` per `FRAMEWORK_VERSION.json`).
  2. **Operator-seeded session**: operator runs one manual search with
     a logged-in account (if available); scraper reuses the cookie jar.
  3. **Alternate portal**: `oceancountyclerk.com/frmSearch` — may be
     a simpler interface backing the same records; requires recon.
  4. **OPRA bulk request** for a date-bounded daily extract of new
     recordings (heavy operator overhead; daily-refresh-incompatible).
- **§4.5 doc types when unblocked:** `deed`, `mortgage`, `lis_pendens`,
  `notice_of_default`, `assignment_of_mortgage`, `release_of_mortgage`,
  `tax_sale_certificate`, `sheriff_deed`, `executor_deed`, `quitclaim_deed`,
  `mechanics_lien`.
- **Lead-origin channels when unblocked:** `RECORDED_EVENT` (mortgages,
  liens, lis pendens), `POST_SALE_TITLE_EVENT` (sheriff deeds), `OWNER_STATUS`
  (executor / life-estate deeds).
- **Until unblock:** Sheriff PDFs cover the `UPCOMING_SALE` half of the
  foreclosure cycle but not the `RECORDED_EVENT` half (lis pendens /
  mortgage defaults).

#### S4 — NJ Courts Civil + Foreclosure Public Access — **BLOCKED_SOURCE** ⛔
- **URL:** https://www.njcourts.gov/public/find-a-case/civil-and-foreclosure-public-access
- **Failure classification:** `LOGIN_REQUIRED`.
- **Blocker:** "First-time users must register with the New Jersey Courts."
  No anonymous public tier. Registration is free but requires an account
  identity (email + verified profile).
- **Operator unlock:** Register a public account at NJ Courts; capture
  the session cookie / OAuth token; supply to scraper via
  `operator_verified_sources.yml.session_cookie`
  (`operator_credentialed_login_allowed: true` per FRAMEWORK_VERSION).
- **§4.5 doc types when unblocked:** `foreclosure_complaint`,
  `final_judgment`, `writ_of_execution`, `lis_pendens` (court-side),
  `motion_to_dismiss`, `default_judgment`.
- **Lead-origin channels when unblocked:** `RECORDED_EVENT` (filings),
  cross-linked to sheriff `UPCOMING_SALE` via docket number.

#### S5 — NJ DCA Notice of Intention to Foreclose (NOI) Database — **BLOCKED_SOURCE** ⛔
- **URL:** https://www.nj.gov/dca/foreclosure.html
- **Failure classification:** `LOGIN_REQUIRED` + access restricted by role.
- **Blocker:** Read-only access is granted only to Counties, Municipalities,
  and State Agencies. The general public — and therefore Xcerebro — has
  no programmatic access path.
- **Operator unlock:** Engagement with the Ocean County Department of
  Planning (county-level credential) OR an individual Ocean County
  municipality (municipal-level credential) under a data-sharing
  arrangement. This is the **highest-friction unlock** in the catalog;
  do not block ship on it.
- **Lead-origin when unblocked:** `RECORDED_EVENT` — earliest signal in
  the foreclosure cycle (NOI is the 30-day pre-complaint notice).
- **Until unblock:** NOI signal is unobserved; foreclosure leads originate
  from filing (S4) and sale (S1) downstream.

### 2.3 SUPPORTING_EVENT_SOURCE

#### S6 — NJPA Public Notices Aggregator — **UNBLOCKED** ✅ (supporting only)
- **URL:** https://www.njpublicnotices.com/Search.aspx
- **Data:** County-filterable public notice search (Ocean is a filter
  option). Notice types include "Foreclosure", "Tax Sale", "Sheriff Sales".
  12-month rolling archive; older notices in a separate archive search.
- **Access §2.4:** **stdlib HTTP + ASP.NET ViewState handling**. No login
  visible for basic search. No CAPTCHA observed.
- **Role:** `SUPPORTING_EVENT_SOURCE` — cross-confirms sheriff and tax-sale
  events with the official-publication record. **Cannot be a sole distress
  feed** per the v5.5.0 P-tier definitions; supplements S1 (sheriff) and
  the eventual S7 (tax sales).
- **Caveat:** Under recent NJ law, public entities must publish notices
  on their own municipal websites starting 2026-03-01; aggregator
  coverage may thin over time.

### 2.4 PRIMARY_DEFAULT_SOURCE candidates (§3.3 tax-default gate) — mostly BLOCKED

#### S7 — Per-municipality online tax-lien sales (newjerseytaxsale.com et al.) — **BLOCKED_SOURCE** ⛔ (33 sub-feeds)
- **URL pattern:** `https://<municipality>.newjerseytaxsale.com` (confirmed
  subdomain pattern: e.g., `oceangate.newjerseytaxsale.com`,
  `wall.newjerseytaxsale.com`, `salem.newjerseytaxsale.com`).
- **Failure classification:** `WAF_BLOCKED` — direct WebFetch returned
  HTTP 403 (anti-bot WAF likely; `ocean Gate` subdomain hit denied).
- **Coverage caveat:** Not all 33 Ocean County municipalities use
  newjerseytaxsale.com. Some run in-person sales; some use Realauction;
  some use MicroBilt. Stafford Township confirmed its 2026-02-13 sale
  was electronic but the vendor was not named on its public page.
- **Operator unlock:**
  1. **Playwright + stealth** to bypass WAF
     (`stealth_browser_allowed: true`).
  2. **Per-municipality recon pass**: enumerate each of the 33 municipal
     tax-collector pages, capture their specific tax-sale vendor and
     URL, then build adapters per vendor (newjerseytaxsale.com /
     Realauction / MicroBilt / muni-PDF).
  3. **TCTANJ feed** (`tctanj.org/cn/webpage.cfm?tpid=14659`) — supplemental;
     thin coverage (only one Ocean County entry observed: Ocean Gate).
- **§3.3 classification when unblocked:** `tax_default` →
  `tax_default_low_priority` / `tax_foreclosure` / `tax_sale` /
  `tax_certificate` depending on the five-criteria gate verdict.

#### S8 — Ocean County Board of Taxation Tax List Search — **AMBIGUOUS** (recon-needs-deeper-probe)
- **URL:** https://tax.co.ocean.nj.us/frmTaxBoardTaxListSearch
- **Data:** Cross-municipality tax/parcel search; dropdown of all 33
  municipalities; fields = block, lot, qualifier, property class,
  location. No login or CAPTCHA observed on the search form.
- **Owner exposure:** **Not confirmed by recon.** Daniel's Law may apply
  even at the county tax board level; needs a live probe with a real
  block/lot from one of the 33 munis.
- **Role pending owner probe:**
  - If owners exposed → **`PRIMARY_OWNER_STATUS_SOURCE` + `ENRICHMENT_SOURCE`**.
  - If owners redacted → **`ENRICHMENT_SOURCE` only** (parcel join keys
    + assessment).
- **Access §2.4:** **stdlib HTTP** for the search form; probe in Phase 2.

### 2.5 ENRICHMENT_SOURCE

#### S9 — NJOGIS Parcels + MOD-IV (Ocean County extract) — **UNBLOCKED** ✅ (no owners)
- **URL:** https://njogis-newjersey.opendata.arcgis.com/datasets/parcels-and-mod-iv-of-ocean-county-nj-shp-download
- **Data:** Statewide parcel geometry + MOD-IV attributes for Ocean
  County. File Geodatabase and Shapefile downloads.
- **CRITICAL:** **Owner names redacted under Daniel's Law (P.L. 2020,
  c. 125).** Direct quote from the source: *"the NJ Office of GIS has
  redacted owner names from all hosted parcels and tax list database
  downloads."*
- **Usable fields:** PAMS_PIN, block, lot, qualifier, county code,
  municipal code, property location, property class, land value,
  improvement value, total assessed value, deed book/page,
  consideration, sale date, parcel geometry. **No owner.**
- **Access §2.4:** **stdlib HTTP**, direct shapefile/geodatabase download.
- **Role:** `ENRICHMENT_SOURCE`. Provides parcel join keys + address +
  assessment + deed pointer; owner must come from a different source.
- **Refresh:** Updated as counties submit (cadence not posted; treat as
  monthly).

#### S10 — Municipal assessor records (33 sub-sources) — **REJECTED_SOURCE** (operationally) for daily refresh
- **Reason:** 33 separate municipal sites, no uniform format, no API.
  Realistic enrichment cost > value within a single daily refresh window.
- **If S8 owners-probe redacts:** Re-classify as `BLOCKED_SOURCE` with
  unlock = OPRA bulk requests to the largest municipalities (Toms River,
  Brick, Lakewood, Jackson, Berkeley) to cover ~70% of parcels.

### 2.6 REJECTED_SOURCE (off-scope)

- **Per-municipality code-enforcement violations.** 33 separate systems,
  no public machine-readable feed; OPRA-only. Refresh-incompatible.
- **Third-party paid aggregators** (RealtyTrac, Foreclosure.com,
  Auction.com, PropertyShark, foreclosure.com, foreclosurelistings.com).
  Per framework `paid-data aggregators and reseller portals are NOT
  primary recon targets` (knowledge_base/protocols/01_county_recon.md
  §01.6 / §4.7 Layer 5).

---

## 3. §1.6 Missed-source audit

The eight source-classes the operator named:

1. ✅ County clerk / land records → **S3** (BLOCKED).
2. ✅ Sheriff sales / foreclosure auctions → **S1** (UNBLOCKED).
3. ✅ NJ Courts foreclosure case system → **S4** (BLOCKED).
4. ✅ Tax-lien sales / tax-default (municipal) → **S7** (BLOCKED, ×33 munis), **S8** (probe needed).
5. ✅ Probate / Surrogate → **S2** (UNBLOCKED).
6. ✅ NJ parcel/assessment data (MOD-IV) as enrichment → **S9** (UNBLOCKED, owner-redacted).
7. ✅ Legal notices → **S6** (UNBLOCKED, supporting only).
8. ✅ Code / condemnation → **REJECTED** (no machine-readable feed).

**Additional source not in operator's list, found in recon:**

- **S5 — NJ DCA Notice of Intention to Foreclose (NOI) Database.**
  This is the earliest legal signal in a NJ foreclosure (30 days before
  complaint can be filed) and would dramatically lift the foreclosure
  pipeline if unblocked. BLOCKED on operator partnership with Ocean
  County or a municipality.

No other classes of NJ public records identified as in-scope for v5.5.0
distress signals (mechanics liens roll into S3 land records; civil
judgments roll into S4 court access).

---

## 4. Build Eligibility Gate verdict

Per `MASTER_PROMPT.md §4.10`:

- **`p0_distress_source_required_for_shippable_build: true`** ✓ — S1
  (Sheriff foreclosures) is an unblocked P0 distress source, and S2
  (Surrogate) is an unblocked P0 owner-status/event source. **The
  shippable-build floor is met.**
- **Verdict: `PROCEED_WITH_PARTIAL_BUILD`** — sufficient for a real lead
  board on day one; punch-list every BLOCKED_SOURCE for operator unlock.
- **Predicted lead volume on day one (S1 + S2 only):** ~50–150 sheriff
  upcoming-sale leads (rolling 12-week window) + ~30–80 estate-titled
  owner leads (rolling 90-day probate filings, after parcel join). Both
  numbers are rough; locked in Phase 2.

---

## 5. Punch-list (operator unlocks, ranked by leverage)

| # | Source | Unlock | Yields | Effort |
|---|--------|--------|--------|--------|
| 1 | **S3 — County Clerk land records** | Playwright + reCAPTCHA solver OR seeded session OR alt-portal probe | All recorded distress events: lis pendens, mortgage defaults, sheriff-deed post-sale, executor deeds | Medium (1–2 day build) |
| 2 | **S4 — NJ Courts foreclosure** | Operator registers public account, supplies session cookie | Foreclosure complaints, judgments, writs of execution (the half of S1 missing today) | Low (registration + cookie capture) |
| 3 | **S7 — Per-muni tax sales** | Playwright stealth pass against newjerseytaxsale.com + per-muni recon for vendor identification | §3.3 tax-default leads across 33 munis | High (per-muni adapter fan-out) |
| 4 | **S8 — Ocean County Tax Board owner probe** | Probe one block/lot live; decide if owners are exposed | Owner enrichment join (S9 substitute) | Low (1-hour probe) |
| 5 | **S5 — NJ DCA NOI** | Ocean County or municipality partnership | Earliest foreclosure signal (pre-complaint NOI) | Very high (partnership-blocked) |

---

## 6. Recon artifacts produced (this folder)

- `recon_summary.md` — this document (headline + verdict).
- `source_of_record_matrix.json` — machine-readable source matrix.
- `access_classification.md` — per-source §2.4 access ladder verdict.
- `operator_verified_sources.yml` — stub for operator credential injection.

Per recon protocol §01.5, no further nested subdirectories are created.
Sample HTML / PDF fixtures captured during recon live in Phase 2's
`scrapers/fixtures/`, not under `recon/`.

---

## 7. Handoff

Phase 0 closes here. Phase 2 (source adapters) is authorized to begin
on **S1 (Sheriff)**, **S2 (Surrogate)**, and **S9 (NJOGIS MOD-IV)** —
the three unblocked sources. **S6 (NJPA)** is authorized as a supporting
secondary. **S8 (Tax Board owner probe)** is authorized as a 1-hour
recon spike inside Phase 2. All other sources stay in the punch-list
above pending operator unlock.
