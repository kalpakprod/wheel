# Wheel catalog edge

A Cloudflare Worker that serves the daily catalog from KV, so a client has an
origin that does not depend on GitHub being reachable. jsDelivr and
`raw.githubusercontent.com` both resolve the same `data` branch: they protect
against one CDN having a bad day, not against GitHub being down.

The edge is optional. Without it the plugin behaves exactly as before.

## Why KV and not R2

R2 has to be enabled by the account owner in the dashboard before its API answers
at all; a fresh account gets `Please enable R2 through the Cloudflare Dashboard`.
KV is available immediately, and the catalog is a few hundred kilobytes against
KV's 25 MB per-value ceiling. If you would rather use R2, enable it and swap the
binding: the Worker reads two keys and nothing else.

## Deploy

```bash
npx wrangler kv namespace create wheel-catalog     # prints the namespace id
# put that id into wrangler.toml, replacing the placeholder
npx wrangler deploy
```

The Worker is read-only by construction: `GET` and `HEAD` only, 405 on anything
else, no code path that writes. The token that publishes lives in CI, never here.

Routes:

- `/catalog.jsonl` — the catalog, `application/x-ndjson`, ETag, `If-None-Match`
  answers 304, `Cache-Control: public, max-age=3600`.
- `/catalog.meta.json` — the build's metadata, same caching contract.
- `/health` — `{"status", "uploaded_at", "records", "bytes"}`, never cached.
  503 while the namespace is still empty.

## Repository secrets for the nightly upload

Set these in **Settings > Secrets and variables > Actions**. The upload step is
skipped unless all three are present, so a half-configured repository cannot fail
the nightly job:

- `CLOUDFLARE_API_TOKEN` — needs Workers KV Storage: Edit on the account.
- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_KV_NAMESPACE_ID`

`scripts/publish_edge.py` performs the upload through the Cloudflare API, so the
job needs no Node toolchain. Each object carries an ETag and the upload timestamp
as KV metadata, which is what the Worker serves.

## Client side

`WHEEL_CATALOG_EDGE_URL` is an environment variable on the machine running the
plugin, **not** a GitHub Actions secret. When set it is validated as HTTPS, tried
before the two GitHub-backed origins, and its host is allowlisted for that call
only:

```bash
set WHEEL_CATALOG_EDGE_URL=https://wheel-catalog-edge.<subdomain>.workers.dev/catalog.jsonl
python scripts/wheel.py sync-catalog --json
```

## Verify a deployment

```bash
curl -i https://<worker-url>/health
curl -i https://<worker-url>/catalog.jsonl
curl -i -H 'If-None-Match: "<etag from the previous response>"' https://<worker-url>/catalog.jsonl
```

The third call must answer `304`.
