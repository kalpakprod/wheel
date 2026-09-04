# 0009 — Regret, dependency debt and hard metrics are code, not instructions

Status: accepted
Date: 2026-09-04

## Context

An audit of what the repository promises against what it executes found three
claims that lived only in markdown. `registry/sources.yaml` held URL templates
and `skills/wheel-research/SKILL.md` told a model to look for postmortems, but
no code queried Hacker News, Reddit or a repository's own issues. Nothing
inspected a candidate's dependencies. The objective-metrics promise was carried
by release recency and star movement alone; bus factor, commit cadence and
contributor concentration were discussed and never computed.

An instruction to a model is not a measurement. It produces whatever the model
believes, and it is exactly the failure this plugin exists to prevent.

## Decision

Three stdlib-only modules, each with its own CLI subcommand, each reusing the
existing security boundary rather than opening a new one:

- `scripts/community_signals.py` — `community-signals`. Probes Hacker News
  (Algolia), Stack Overflow (Stack Exchange API), Reddit and the repository's own
  issues for regret terms.
- `scripts/dependency_debt.py` — `dependency-debt`. Transitive count from
  GitHub's SBOM, direct count from manifests, unlicensed package count.
- `scripts/hard_metrics.py` — `hard-metrics`. Bus factor, HHI concentration,
  commit cadence, release cadence.

Every network call goes through `_bounded_https_download` with a per-call host
allowlist, or through `_gh_api`. No module opens its own client.

Four defects surfaced only because the commands were run against the live API,
not because a test caught them:

- One query carrying every negative term at once matches nothing on Algolia and
  is rejected outright by GitHub search. Both now probe one term at a time, so a
  malformed query can no longer read as "no complaints found".
- GitHub search does not follow repository renames: `tiangolo/fastapi` answered 422. `_canonical_slug` resolves through `repos/{slug}` first, which does follow
  the redirect. The same query then returns 25 real issues.
- `subprocess.run(text=True)` decodes with the Windows console codepage, so any
  non-ASCII byte in a GitHub payload raised inside the reader thread and the
  caller saw an unparseable result. Every call site now pins UTF-8.
- `--json` output was written in the console codepage, so the file a program was
  meant to parse was not valid UTF-8. `_force_utf8_streams` pins both streams at
  entry.

Reddit answers 403 to unauthenticated clients from this network under every
User-Agent tried. That is reported as `blocked` with the HTTP code, never as an
empty result. Stack Overflow was added so the community half keeps more than one
reachable platform behind it.

## Consequences

The expert claims are now falsifiable: each command names its sources, reports a
per-source status, and reports `available_sources`, which a reader must treat as
"no evidence gathered" rather than "no problems exist". A deep review costs more
API calls, and the GitHub probe paces itself at 2.5 s between terms to stay under
the search rate limit, so `community-signals` takes roughly ten seconds.

Reddit stays blocked until someone runs the plugin from a network it accepts or
adds an authenticated client. The probe terms are a fixed list of four; a project
whose community complains in other words will not be caught by them.

## Evidence

- `scripts/community_signals.py`: `search_hackernews`, `search_stackexchange`,
  `search_github_issues`, `_canonical_slug`, `_dead_source_status`.
- `scripts/dependency_debt.py`: `analyze_dependency_debt`, `count_from_sbom`.
- `scripts/hard_metrics.py`: `bus_factor`, `commit_cadence`, `release_cadence`.
- `scripts/wheel.py`: `_sibling_module`, `_force_utf8_streams`.
- Live runs, 2026-09-04: `hard-metrics psf/requests` returned bus factor 3, HHI
  0.1572, 120 commits over 52 weeks, 19 releases at a 19.03-day median.
  `dependency-debt psf/requests` returned 6 direct and 30 transitive.
  `community-signals tiangolo/fastapi` returned 25 GitHub issue matches after the
  rename fix, Reddit `blocked` HTTP 403.
- 158 tests pass.
