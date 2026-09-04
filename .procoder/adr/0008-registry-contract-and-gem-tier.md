# 0008 — The two registries are one contract, and gems get a tier

Status: accepted
Date: 2026-09-04

## Context

The catalog builder had never been run against the live GitHub API. A dry run
with the real registry, shrunk caps and a real token produced 60 records in 37
seconds and exposed three defects that no unit test could have caught, because
each of them lives in the agreement between two files rather than inside one.

- 42 of 60 records carried `capability: ""`. None of the seven entries in
  `trends` declared a capability, and `_collect_trends` read it with
  `entry.get("capability", "")`. Every trend record was therefore invisible to
  `search-catalog --capability`, which is the client's main filter.
- `registry/catalog_sources.yaml` used the capability ids `devops-deploy` and
  `api-integration`. Neither existed in `registry/capabilities.yaml`, so the
  client's classifier could never produce them and the records tagged with them
  were unreachable by construction.
- `emerging_gem` was true for zero published records. `detect_emerging_gem`
  requires `stars <= 500`, while every tier in `select_index` ranked by stars.
  A gem is by definition what the star ranking has not found yet, so
  `search-catalog --gem` was guaranteed to return an empty list forever.

The third one contradicts a stated requirement: young repositories are to be
examined more closely, not filtered out.

## Decision

`_known_capabilities` reads `registry/capabilities.yaml` next to the catalog
registry, and `_require_capabilities` fails the build when any source declares
no capability or one the client cannot classify. A missing capabilities file is
an error, not a skipped check. The seven trend entries now declare their
capability, and `devops-deploy`, `api-integration` and `developer-tools` were
added to the classifier, since no existing entry covered those intents.

`select_index` gained a `gem_records` tier of 60, placed after curated and
before movement, ranked by movement then stars. It is the only tier that does
not rank primarily by absolute stars.

`awesome_lists` entries gained an optional `repos:` list. Link extraction
returns nothing for `ashishps1/awesome-system-design-resources`, because that
README links to markdown inside its own repository rather than to other repos,
so the source the user asked for contributed only its own record. Seven
references are now named outright, each verified against the API before being
written into the registry.

## Consequences

A source that drifts from the classifier now fails the nightly build instead of
publishing records nobody can reach; the cost is that adding a source means
adding its capability first. Gems occupy 60 of 400 slots, which is 60 slots the
movement and intake tiers no longer have — that is the price of the flag being
real rather than decorative. `repos:` is a manual list: it does not update
itself, and a repository that dies there stays there until someone looks.

Two full runs at shipped caps have now exercised the live path end to end: 400
records from 533 observed, 264 API requests, about 200 s each, and a second run
against the first measured movement for 360 of 400 records. The 45-minute
timeout holds with a wide margin, and 264 requests sit under the 1000 per hour
that Actions grants GITHUB_TOKEN.

The request count is instrumented in the builder, not read from GitHub: the
`rate_limit` endpoint answered a static 5000 remaining before and after a run
that fetched hundreds of repositories, so its counter cannot be used as evidence
here.

## Evidence

- Dry run against the live API, 2026-09-04: `empty capability 0` (was 42 of 60),
  `gems 9` (was 0), pinned system-design references present.
- Full run, shipped caps: 400 records, 337 KB, 73 gems, 9 capabilities, 0 empty.
- Second full run against the first: `with_momentum` 360, `observed_days` 2.004,
  rates from 0.0 to 3.493, 264 requests, 197 s.
- `scripts/build_catalog.py`: `_known_capabilities`, `_require_capabilities`,
  `_pinned_repos`, `select_index`.
- `registry/catalog_sources.yaml`, `registry/capabilities.yaml`.
- `tests/test_build_catalog.py`: `RegistryContractTests`,
  `test_a_gem_gets_a_slot_the_star_ranking_would_never_give_it`.
