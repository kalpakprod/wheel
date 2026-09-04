# 0006 — The catalog is a movement index, not a mirror of GitHub

Status: accepted
Date: 2026-09-04

## Context

The first catalog published 3000 records ranked by absolute stars. Reviewing
what it actually bought, almost every field in it — stars, license, archived,
last push, contributors — is something the live routes in
`registry/sources.yaml` return for any named slug, fresher, at request time. The
mirror duplicated live search and went stale within a day, while
`search_catalog` reparsed roughly 1.5 MB of JSONL per query.

One signal cannot be produced live at any price: how much a repository moved
since yesterday. That requires a previous snapshot, which is exactly what a
daily job owns and a live query does not.

The alternatives were: keep the mirror as it was, shrink it to a movement index,
or publish two files with two freshness states.

## Decision

The published catalog is capped at 400 records and chosen by `select_index` in
three tiers, declared in `registry/catalog_sources.yaml`:

- `curated_records` (120) — entries that came from the awesome lists, ranked by
  stars. These are the ready-made references a live search cannot assemble, so
  they keep a reserved slice regardless of movement.
- `movement_records` (180) — entries with a measured `stars_per_day`, ranked by
  that rate. This is the tier the catalog exists for.
- the remainder — intake. Repositories with no measured history come first, by
  stars, so they have a baseline tomorrow. Entries that already have history but
  lost the movement race rank last: they have had their chance and their rate is
  measurably weak.

The intake tier is not optional, and neither is its ordering. Publishing only
records that already have history would mean a repository outside today's set
can never acquire one. Ranking intake by stars alone has the same effect more
quietly: an end-to-end run showed a 9000-star repository moving one star in two
days taking the slot from a 7000-star newcomer, because the giant's measured
rate still counted as a record worth keeping. The index would ossify around
yesterday's largest names.

The consumer gained `search-catalog --moving`, which filters to measured
movement and ranks by rate instead of by stars, and every result now reports
`with_momentum`. A producer signal no client can query is not a feature.

`awesome_repos_per_list` dropped from 120 to 60, since the cap no longer
rewards collecting more than the index can publish.

Two files (option C) were rejected: it doubles the CI surface and forces the
user to reason about two freshness states for one question.

## Consequences

The catalog is roughly seven times smaller, so a query costs about 200 KB
instead of 1.5 MB, and it stops competing with live search on facts live search
answers better. Broad offline browsing of 3000 repositories is gone; that is
the price, and `maturity_for` covers a named slug on demand.

The movement tier is empty on the first run and thin on the second, because
`stars_per_day` needs a span of at least half a day between snapshots. Until
then the index is curated plus intake, which is a weaker but honest state, and
`catalog.meta.json` reports `with_momentum` so the gap is visible rather than
inferred.

## Evidence

- `scripts/build_catalog.py`: `select_index`, `_is_curated`, `_deduplicate`, `build`.
- `registry/catalog_sources.yaml`, `limits`.
- `scripts/wheel.py`: `search_catalog`, `--moving`.
- `tests/test_build_catalog.py`: `SelectIndexTests`.
- `tests/test_wheel.py`: `test_moving_filter_ranks_by_measured_rate_not_by_stars`.
