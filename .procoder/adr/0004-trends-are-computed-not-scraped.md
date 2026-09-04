# 0004 — Trends are computed from the GitHub API, not scraped from Trendshift or GitTrend

Status: accepted
Date: 2026-09-03

## Context

The requirement was live, continuously updated collections of software, with
Trendshift and GitTrend named as the model. Both render their rankings in HTML
and neither publishes a documented API for it. Scraping them means a parser that
breaks on any layout change, and redistributing a ranking those services compute
is a claim we cannot substantiate from primary data.

An early version of `registry/sources.yaml` declared `trendshift` and `gittrend`
as operations of the catalog route. No code implemented them. That declaration
was removed as a false capability.

## Decision

Trend is computed from facts the GitHub API returns: `created:>` and `pushed:>`
windows ranked by stars, declared per archetype in
`registry/catalog_sources.yaml`, plus day-over-day movement (`stars_delta`,
`stars_per_day`) measured against the previous published catalog.

Every record names its origin as `github-search:<id>` or `awesome-list:<id>`. No
record claims a source it did not come from.

## Consequences

The ranking is ours and reproducible from primary data, and it survives any
redesign of a third-party site. It will not match Trendshift's list, because
Trendshift weighs signals we do not have. Movement needs at least two runs
before it says anything, and records without history carry `null` rather than
zero, because "did not move" and "not known" are different statements.

## Evidence

- `registry/catalog_sources.yaml`, `registry/sources.yaml` route `catalog-sync`.
- `scripts/build_catalog.py`: `_collect_trends`, `apply_momentum`.
- `tests/test_build_catalog.py`: `MomentumTests`.
