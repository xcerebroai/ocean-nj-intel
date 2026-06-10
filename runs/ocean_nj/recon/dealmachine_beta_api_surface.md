# DealMachine Beta API — Surface Probe (Ocean County, NJ)

Date: 2026-06-09
Key: `dm_sk_live_BYu…` (43 chars, stored gitignored at `runs/ocean_nj/.dealmachine_key`, Bearer)

## Goal
Determine whether the supplied beta key exposes a **READ** endpoint returning
owner / skip-trace / property data for a given address or APN (vs the known
WRITE-only public CRM-sync API).

## What exists (infrastructure)
DNS: only `api.dealmachine.com` and `app.dealmachine.com` resolve (same
Cloudflare IPs `104.20.46.164 / 172.66.146.145`). All of these are **NXDOMAIN**:
`developer.`, `data.`, `api2.`, `gateway.`, `api-beta.`, `beta.dealmachine.com`.
No separate beta/developer host exists.

## The only documented API: `/public/v1/` (CRM sync)
Source: live Postman collection JSON (`documenter.getpostman.com/view/10528472/TzzBrbnN`).
Base `https://api.dealmachine.com/public/v1/`. Endpoints:

| Method | Path | Kind |
|---|---|---|
| GET | `/leads/?limit&after` | read (your own leads) |
| GET | `/leads/:lead_id` | read (one of your leads) |
| POST | `/leads/` | write (add lead) |
| DELETE | `/leads/:lead_id` | write |
| POST | `/leads/:id/lead-status`, `/custom-field`, `/assign-lead`, `/add-to-list`, `/remove-from-list`, `/add-tags`, `/remove-tags`, `/create-note`, `/start-mailer-campaign`, `/pause-mail-sequence`, `/end-mail-sequence` | write |
| GET | `/lead-statuses/`, `/custom-fields/`, `/mail-sequences/`, `/lists/`, `/tags/`, `/team-members/` | read (your own config objects) |

There is **NO** property-lookup, address-lookup, APN-lookup, skip-trace, owner,
search, comps, or parcels endpoint. Guessed paths all 404:
`/properties/ /property/ /property-lookup/ /skip-trace/ /skip-traces/ /owners/ /lookup/ /search/ /comps/ /parcels/` → **HTTP 404**.

## The supplied key is REJECTED by this API
Every documented endpoint returns `{"error":{"code":100,"message":"Invalid api key."},"data":[]}`
(HTTP 200 envelope) for this key, across ALL auth formats tried:
`Authorization: Bearer <key>`, raw `Authorization: <key>`, `x-api-key`,
`api-key` header, and `?api_key=` query param. The key is not recognized by
`/public/v1/`.

No alternate API version exists: `/beta/v1/`, `/v2/`, `/public/v2/`, `/api/v1/` → 404.

## Conclusion
1. The `dm_sk_live_` beta key does **not** authenticate against the only
   reachable DealMachine API (`/public/v1/`), under any auth scheme.
2. Even if it did, that API has **no read endpoint that returns owner /
   skip-trace / property / enrichment data by address or APN**. Its only reads
   are of *your own* previously-created leads and account config objects.
   DealMachine's skip-trace / owner-lookup is an **in-app product feature**, not
   an exposed data-lookup API.
3. No beta/developer/data host or beta API version exists in DNS or on the
   gateway.

**Net: there is no enrichment-read capability to build against with this key.**
Blocked on the operator/DealMachine side: (a) confirm the key is valid and which
product/endpoint it is provisioned for, or (b) confirm a private beta base URL +
docs. Until then, no enricher is buildable.

---

## ROUND 2 (2026-06-09) — probing the claimed `/v1/enrichment/*` beta endpoint

Operator supplied: base `https://api.dealmachine.com/v1/`, `POST /v1/enrichment/address`
and `POST /v1/enrichment/apn`, Bearer auth, same key. Findings:

### The claimed endpoints do not exist
`POST /v1/enrichment/address` and `/v1/enrichment/apn` → **Apache origin 404**
("The requested URL was not found on this server"), on `api.` and `app.dealmachine.com`,
under every auth header (Bearer / raw / `X-DM-Client-Key` / `client-security-token` /
`x-api-key`), trailing-slash variants, and ~25 alt route names. A known-good public
route answers every method with app JSON; these answer GET/POST with web-server 404 →
the route is not deployed at that path.

### BUT a real, undocumented `/v1/` data app exists (token-param auth)
Discovered by JSON-vs-Apache signature. Real routes (return app JSON, not HTML):
- `GET /v1/credits/`  → `{"error":"Invalid token.","valid":"invalid","results":[]}`
- `GET /v1/lookup/`   → `{"error":"You must provide the token parameter.",...}`
- `GET /v1/user/`     → `{"error":"Invalid token.",...}`

Auth model: a `token` **query parameter** (`/v1/credits/?token=…` is read and validated).
`/v1/lookup/` is the most likely property/enrichment endpoint. `/v1/enrichment/*` is NOT
a route in this app.

### The key is rejected as invalid by BOTH DealMachine apps
- `/public/v1/` (lead-sync) → `{"error":{"code":100,"message":"Invalid api key."}}`
- `/v1/` (data app) → `{"error":"Invalid token.","valid":"invalid"}`

`dm_sk_live_BYuCvC5fI4WTOYZWrfivxGawdgK5miw6` authenticates to neither. The `/v1/` app
appears to expect a *session token*, not a `dm_sk_live_` secret key. `/v1/lookup/` would
not even read the token from query/body/header/form in any placement tried — it kept
returning "must provide the token parameter," so its token-passing convention is unknown
without docs.

### The official MCP server does not exist
Operator supplied `npx -y @dealmachine/mcp`. **Not published on npm** (404), nor under
`dealmachine-mcp`, `@deal-machine/mcp`, `mcp-dealmachine`, `@dealmachine/mcp-server`,
`@dealmachinedev/mcp`. `claude mcp add` would create a config entry whose server cannot
start. NOT added.

## ROUND 2 CONCLUSION
Three operator-supplied facts do not check out against the live service: the
`/v1/enrichment/*` endpoint (404 / not a route), the `@dealmachine/mcp` package
(unpublished), and the key's validity (rejected by both apps). A real undocumented
`/v1/` data API with a `/v1/lookup/` endpoint DOES exist and is the plausible target —
but it needs (a) a credential it accepts and (b) its token-passing + request/response
contract. **No enricher was built or run; no credits spent; nothing fabricated.**
Unblock requires, from DealMachine/operator: a valid token for the `/v1/` data app, the
exact working request for `/v1/lookup/` (or the real enrichment route), OR a real MCP
package/URL.

---

## ROUND 3 (2026-06-09) — RESOLVED: correct host is api.v2.dealmachine.com

The real production API is `https://api.v2.dealmachine.com/v1` (NOT api.dealmachine.com).
The supplied key authenticates there. Official CLI `@dealmachine/cli` (bin `dm`) works:
`dm enrich address|apn` returns owner/property/contacts. Full enricher built at
`scrapers/dealmachine_enrich.py` (APN-preferred, address fallback, contacts via
`include_contacts:true`+`contact_audience:owners`, §3.8 provenance, Daniel's-Law backfill,
incremental cache, resumable checkpoint, circuit breaker). Wired into daily_refresh.yml.

NJ APN format (derived + live-verified): `{district:02}-{block_whole:05}-{block_frac}-{lot_whole:05}[-{lot_frac}]`
where district = cd_code[-2:], block_frac = "0000" if integer block. e.g.
`1507_44.01_16`→`07-00044-01-00016`, `1519_1.348_21`→`19-00001-348-00021`,
`1508_392_15.11`→`08-00392-0000-00015-11`.

Cost model (live): 1 property credit/match + 1 people credit/contact; unmatched &
in-cycle duplicates FREE. Account: 120k credits.

### KNOWN ISSUE — DealMachine API instability (2026-06-09 ~19:40–20:15+ ET)
The v2 API began returning `Invalid prisma.$queryRawUnsafe() invocation: Timed out
fetching a new connection from [pool]` (server-side DB connection-pool exhaustion) on
the majority of calls — flapping then sustained-down. Smoke test (20 leads) succeeded
fully when healthy (18/20 matched). Full 536-lead pass is BLOCKED by this outage.
12/454 unique keys resolved so far. `scripts/dm_enrich_poller.sh` retries every 120s and
converges automatically when the API recovers; the daily refresh will also keep retrying
(errors are never cached). Dashboard + §7 STATIC_OK verified with the partial data.
