# Upstash provisioning — Custom Mix candidate pool

Everything in this document is a **manual action for you**. No account, database, key or
credential has been created by the code in this repository, and no dashboard click has been
performed on your behalf. The code is written and tested against mocks; that is *readiness
for provisioning*, not production readiness.

Work top to bottom. Do not skip to step 12.

---

## 1. Create or select an Upstash Redis database

Upstash console → **Redis** → *Create database*. A single database is enough: Production,
Sandbox and Preview are already namespaced apart by `parseRedis` in `api/_lib/env.ts`, and
the pool keys carry their own namespace and schema version.

Choose the **Regional** (not Global) tier unless you have a reason not to — the pool is read
from one Vercel region, so global replication adds cost without adding latency benefit.

## 2. Region

The repository does **not** declare a Vercel function region: `vercel.json` contains only
header rules, no `regions` key. So the region is whatever the Vercel project is set to.

- Open the Vercel project → **Settings → Functions → Function Region** and read the value.
- Create the Upstash database in the matching region. If the Vercel project is on the
  default **Washington, D.C. (`iad1`)**, choose Upstash **`us-east-1`**.

A mismatched region still works; it just adds a cross-region round trip to every Pro
request, which is exactly the latency the read timeout budget cannot spare.

## 3. Obtain the REST URL

Database → **REST API** → copy the endpoint. It must be an `https://…upstash.io` URL with no
query string and no fragment; `resolveUpstashCredentials` rejects anything else, including a
plaintext `http://` URL, so a token can never go out in the clear.

## 4. Obtain the tokens

Upstash exposes two REST tokens per database:

| Token | Scope | Used by |
|---|---|---|
| `UPSTASH_REDIS_REST_TOKEN` | read **and** write | the publisher |
| `UPSTASH_REDIS_REST_READ_ONLY_TOKEN` | read only | the API |

**Use both, and do not swap them.** Give the API the *read-only* token: the API never needs
to write, and a read-only credential in a serverless function is one that cannot corrupt or
delete a day's pool even if the function is compromised.

> **Known limitation.** Upstash's read-only token is read-only for the *whole database*, not
> scoped to a key prefix. There is no finer-grained credential available on this provider, so
> the API's token can read every key in the database. That is acceptable while the database
> holds only Mix Pool artifacts. If you later store anything else there, give Custom Mix its
> own database rather than trying to scope the token.

## 5. Add the API **read** variables to Vercel

Vercel project → **Settings → Environment Variables**. Exact names:

| Variable | Value | Environments |
|---|---|---|
| `KV_REST_API_URL` | the REST URL from step 3 | Production, Preview, Development |
| `KV_REST_API_TOKEN` | the **read-only** token from step 4 | Production, Preview, Development |

These names are not new — `api/_lib/env.ts` has required them for Production since Phase 3C-1,
and Preview is allowed to run store-less. Mark both **Sensitive** so they cannot be read back
out of the dashboard.

## 6. Add the publisher **write** secrets to GitHub Actions

Repository → **Settings → Secrets and variables → Actions → New repository secret**. Exact names:

| Secret | Value |
|---|---|
| `KV_REST_API_URL` | the same REST URL |
| `KV_REST_API_WRITE_TOKEN` | the **read/write** token from step 4 |

The write variable is deliberately named differently from the API's `KV_REST_API_TOKEN`. The
two credentials are then impossible to confuse when pasting, and either can be rotated on its
own without touching the other.

## 7. Confirm no secret reaches the iOS client

Nothing here is bundled, and nothing needs to be:

- the app talks only to `/api/edition`, authenticated with a Signals token
- the pool is read **server-side**, inside the Vercel function
- `api/_tests/vercel-runtime.test.ts` asserts that no `KV_REST_API*` name appears in any
  client-visible response body

After adding the variables, re-run `npm run test:api` and confirm that assertion still passes.
Never put a REST token in the iOS project, in `Info.plist`, or in `vercel.json`.

## 8. Non-production smoke test

Build one real Editorial Mix Pool to a temporary path first:

```bash
python3 pipeline/editorial_mix_pool_cli.py build \
  --input pipeline/candidates.json \
  --date "$(date -u +%F)" \
  --generated-at "$(date -u +%FT%TZ)" \
  --output /tmp/editorial-pool.json
```

It prints counts and identity prefixes only. If it reports fewer than 15 surviving
candidates it writes nothing — that is the contract working, not a failure to route around.

## 9–11. Publish, verify, clean up one test key

```bash
KV_REST_API_URL='…' KV_REST_API_WRITE_TOKEN='…' \
  python3 pipeline/upstash_smoke_test.py \
    --artifact /tmp/editorial-pool.json \
    --test-date 2026-01-01 \
    --i-understand-this-is-not-production \
    --delete-after
```

This publishes to `signals:smoke:editorial-mix-pool:v1:<test-date>` — a namespace neither the
reader nor the daily publisher consults, with a one-hour TTL — then reads it back and reports:

- `bytesIdentical`, `sha256Identical`, `canonicalIdentical`
- `selectorIdentityMatches`, `editorialIdentityMatches`, both **re-derived** from the
  retrieved candidates rather than read out of the envelope

**All five must be `true`.** Any `false` means the transport altered the bytes, and
publication must not be enabled until that is understood.

`--delete-after` removes that one exact key. If you would rather not use a token that can
delete, omit the flag and delete the key by hand in the Upstash console — the printed
`cleanup` field gives the exact key string. Nothing scans, patterns or flushes.

To confirm the **TypeScript** side reads the same bytes, the parity is already asserted by
`api/_tests/upstash-pool-store.test.ts`, which drives real Python-published bytes through the
real TypeScript store.

## 12. Enable workflow publication

No edit is required. `.github/workflows/daily-auto-publish.yml` already carries the Custom Mix
block; it skips with a notice while the secrets are absent and starts publishing the first
morning after step 6. Watch the first scheduled run and confirm:

- the standard edition steps behaved exactly as before
- `Custom Mix — publish the Editorial Mix Pool to Upstash` succeeded
- `Custom Mix — assert nothing was added to the repository` succeeded
- no token appears anywhere in the run log

If the publish step fails, the edition is already merged and safe — the failure is loud and
isolated, and Custom Mix is simply unavailable for that day.

## 13. `/api/edition` is now connected (Phase 3E-1)

`/api/edition` reads the published Editorial Mix Pool and returns a five-signal `SignalsFeed`
for an eligible Pro caller. Every other outcome is `503 {"status":"unavailable","code":
"custom_mix_unavailable"}` — one body for every cause, so the storage layer cannot be probed
through the route.

### Operational requirement — the API runtime needs the READ-ONLY variables

The route builds its store from the read-only credential only:

| Variable | Where it must exist | Used by |
| --- | --- | --- |
| `KV_REST_API_URL` | Vercel env (Production **and** Preview) | API runtime |
| `KV_REST_API_TOKEN` | Vercel env (Production **and** Preview) | API runtime, **read-only** |
| `KV_REST_API_WRITE_TOKEN` | GitHub Actions secret **only** | daily publisher |

`KV_REST_API_WRITE_TOKEN` is never read by any API module. A test asserts the name appears
nowhere in the executable request path.

**Preview deployments.** `parseRedis` allows a Preview deployment to run without storage. When
it does, `createProductionPoolStore` returns `null` and every Custom Mix request answers
`custom_mix_unavailable`. This is the intended fail-safe, not a bug — but it means:

> **Testing Custom Mix in Preview requires the same read-only variables the API runtime uses in
> Production: `KV_REST_API_URL` and `KV_REST_API_TOKEN`, scoped to the Preview environment.**
> Do not copy the write token into Preview, and do not hardcode Production credentials there.

Until those are set, Custom Mix is Production-only and Preview exercises the failure path.

### Date window

The route serves **UTC today ± 1 calendar day** only. A date outside that window is refused
before any storage read, so a client cannot walk the key space by varying the date.

---

## Rollback

Custom Mix has no rollback coupling to the standard edition. To stop publication: delete the
`KV_REST_API_WRITE_TOKEN` GitHub secret and the block skips again the next morning. Existing
keys expire on their own within 9 days.
