# 0002 — The catalog is built by GitHub Actions and published as JSONL on a data branch

Status: accepted
Date: 2026-09-03

## Context

Wheel needed a candidate catalog that refreshes daily without the user renting
a VPS. Two hosts were considered: GitHub Actions and a Cloudflare Worker.

A Worker on the free tier is capped at 10 to 50 ms of CPU per request. Parsing
several hundred API responses, validating them and computing derived fields
exceeds that budget, and the failure mode is a truncated result rather than an
error. GitHub Actions gives a full VM, a scheduled trigger, and a credential
(`GITHUB_TOKEN`) that is already scoped to the repository.

## Decision

`.github/workflows/sync-catalog.yml` runs `scripts/build_catalog.py` on a daily
cron plus manual dispatch, validates the output with the consumer's own parser,
and commits `catalog.jsonl` and `catalog.meta.json` to the orphan branch `data`.
The client fetches that file over HTTPS.

JSONL, not a database file, is the transport: it diffs in git, so every daily
change is reviewable, and it needs no schema migration to read.

## Consequences

No server to run, no secret to store beyond the token Actions injects. The
catalog is a plain file any client can fetch, and its history is the branch's
git history. In exchange the client parses text rather than querying an index,
which is recorded as debt in `search_catalog`, and freshness is bounded by the
cron period, which the client surfaces as `age_days` and `stale`.

## Evidence

- `.github/workflows/sync-catalog.yml`.
- `scripts/build_catalog.py`, `scripts/wheel.py` `sync_catalog`, `search_catalog`.
