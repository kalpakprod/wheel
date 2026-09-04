# 0003 — Catalog sync is fail-closed and fetched through the shared bounded downloader

Status: accepted
Date: 2026-09-03

## Context

The first implementation of `sync_catalog` accepted any decodable HTTP 200 body
and wrote it over the cache with `Path.write_text`. An adversarial review found
three defects: a truncated response replaced a valid catalog, the write was not
atomic, and the fetch used a bare `urlopen` that follows redirects with the
default opener, so an HTTPS origin could be redirected to plain HTTP.

`scripts/wheel.py` already contained `_bounded_https_download` with an HTTPS
host allowlist, a size limit, chunked reads and `_HTTPSRedirect`, which rejects
a redirect that changes scheme or leaves the allowlist. The catalog fetcher was
a third, weaker implementation of a problem the file had already solved.

## Decision

- `_fetch_catalog_payload` delegates to `_bounded_https_download` with
  `CATALOG_MAX_BYTES`. The bespoke fetcher is deleted.
- `_require_catalog_url` accepts HTTPS only. `_catalog_allowed_hosts` authorizes
  the two built-in origins plus the host of any URL the caller named explicitly,
  so a custom mirror is opt-in and a redirect still cannot leave it.
- The payload is parsed and must be non-empty before anything is written; the
  write goes through `_atomic_text_write` under the file lock.
- A failed or unusable fetch returns `cached` when a valid cache exists and
  `unavailable` otherwise, each carrying an `error` string. Nothing is silent.

The two default origins are deliberately different services, jsDelivr and
raw.githubusercontent, so one provider's outage does not take the catalog down.

## Consequences

A bad payload can no longer destroy a good cache, and the catalog path inherits
every hardening the dependency downloader already had. `load_catalog` is now
strict: one malformed line makes the whole cache read as empty rather than
partially valid. That is the intended fail-closed behaviour, and it is the
reason `sync_catalog` refuses to overwrite on a parse failure.

## Evidence

- `scripts/wheel.py`: `_require_catalog_url`, `_catalog_allowed_hosts`,
  `_fetch_catalog_payload`, `sync_catalog`, `_bounded_https_download`,
  `_HTTPSRedirect`.
- `tests/test_wheel.py`: `test_catalog_download_is_allowlisted_and_redirect_safe`,
  `test_sync_catalog_requires_https_and_bounded_download`,
  `test_corrupt_catalog_never_replaces_valid_cache`.
