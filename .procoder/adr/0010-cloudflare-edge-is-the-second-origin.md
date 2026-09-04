# 0010 — The Cloudflare edge is the second origin, and it is optional

Status: accepted
Date: 2026-09-04

## Context

The delivery design was described as a tandem: GitHub Actions builds the catalog,
an edge serves it. The client did fetch from two URLs, `cdn.jsdelivr.net` and
`raw.githubusercontent.com`, but both resolve the same `data` branch in the same
GitHub repository. That is protection against one CDN having a bad day, not
against GitHub being unavailable, and no Cloudflare code existed anywhere in the
tree. The documents named a component that was never built.

## Decision

`edge/worker.js` serves `catalog.jsonl` and `catalog.meta.json` from an R2
bucket, read-only: `GET` and `HEAD` only, ETag with `If-None-Match` answering
304, `/health` reporting the upload timestamp, 405 on anything that writes.

`.github/workflows/sync-catalog.yml` uploads both files to R2 after validation.
The step is skipped unless `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` and
`CLOUDFLARE_R2_BUCKET` are all present — guarding on the token alone would let a
half-configured repository fail the nightly job.

The client reads `WHEEL_CATALOG_EDGE_URL` from the environment. When set, it is
validated as HTTPS, tried first, and its host is added to a per-call allowlist
rather than to the module-level default, so an operator's URL cannot widen the
allowlist for any other download. When unset, nothing changes.

The edge is optional by construction. Without it the plugin behaves exactly as
before, which is why it ships unconfigured rather than as a required secret.

## Consequences

An operator who deploys the Worker gets an origin that survives GitHub being
down; one who does not is no worse off than yesterday. The cost is a second
deployment target with its own credentials, and a Worker whose contents can
silently diverge from the `data` branch if an upload fails after a successful
commit. `/health` is the only thing that would show that, and nothing polls it.

`WHEEL_CATALOG_EDGE_URL` is a consumer-side environment variable, not an Actions
secret. It was documented once as the latter, which would have sent an operator
looking for a setting that does nothing.

## Evidence

- `edge/worker.js`, `edge/wrangler.toml`, `edge/README.md`.
- `.github/workflows/sync-catalog.yml`, the `publish to cloudflare r2` step.
- `scripts/wheel.py`: `_catalog_candidate_urls`, `_catalog_allowed_hosts`,
  `_require_catalog_url`.
