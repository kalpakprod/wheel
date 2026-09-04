# Changelog

All notable changes to Wheel. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.7.0 — 2026-09-04

The release where the plugin's claims became measurements. Everything below that
touches the network was run against the live APIs before it was written down;
where something could not be verified, this file says so.

### Added

- **`reddit-archive`, a keyless second lane.** When Reddit's API does not answer,
  `community-signals` searches [Arctic Shift](https://arctic-shift.photon-reddit.com),
  an independent public archive, under its own source name. No Reddit credential,
  no Reddit budget, no account to sanction. `registry/reddit_subreddits.yaml`
  maps capabilities to subreddits because the archive requires one alongside a
  title query; `--subreddits` and `--capability` override it. Requests are paced
  2.5s apart with one retry on the archive's 422 "slow down". Archived `score` is
  reported as `score_at_archive` and excluded from ranking: it is the count at
  ingest, not now. Verified live: 5 real threads for Kafka in r/dataengineering,
  including one titled "Kafka deleted our data and every dashboard said we were
  healthy". The shreddit `/svc` partials and RSS lanes used by `last30days` were
  rejected: they read reddit.com without identifying the client, which is the
  practice removed in this same release.

### Changed

- **Reddit is now read only through OAuth, and never stored.** Three rules from
  Reddit's Data API Wiki and Responsible Builder Policy were being broken:
  - The agent string must be `<platform>:<app ID>:<version> (by /u/<username>)`
    and unidentified clients are throttled or blocked. It is now built from
    `WHEEL_REDDIT_USERNAME`, or replaced by `WHEEL_REDDIT_USER_AGENT`. An install
    that names nobody raises before a request leaves the machine.
  - Masking how Reddit data is reached is prohibited, so the managed page-reader
    fallback is deleted. Without credentials the source reports `blocked` and the
    run has no Reddit evidence, which is the honest outcome.
  - Deleted posts must be purged from every copy held, which a decision file in
    git cannot do. `_reject_reddit_content` runs inside the same validators that
    reject secrets: any decision or run record carrying a `reddit.com` or
    `redd.it` link now fails to write. Findings are paraphrased, the source is
    cited as one to re-query.
- **The 100 QPM budget is honoured, not discovered.** `x-ratelimit-remaining` and
  `x-ratelimit-reset` are read from every Reddit response and the next call is
  refused inside a five-request reserve until the window resets.

### Fixed

- **Reddit OAuth picked the wrong grant for most apps.** The token request hard
  coded `installed_client`, which only a public installed app accepts; a script
  or web app carries a secret, is a confidential client and answers to
  `client_credentials`. Reddit rejects the mismatch with a bare 401, so the grant
  is now selected from whether `WHEEL_REDDIT_CLIENT_SECRET` is set. Verified
  against live Reddit: a deliberate wrong-credential probe returns the 401 the
  new `--check-reddit` command reports, not a silent empty result.
- **`--check-reddit`.** `scripts/community_signals.py --check-reddit` mints a
  token and prints `ok`, `unconfigured` or `error` with the grant it attempted,
  exiting non-zero on anything but `ok`. Credentials are never printed, and a
  test asserts the failure text carries neither the id nor the secret.
- **`WHEEL_REDDIT_USER_AGENT`.** Reddit throttles by agent string and every
  install previously shared one hardcoded value. The default now carries the
  version read from the plugin manifest, and an operator can name their own.
- **The helper scripts could not run as scripts.** `community_signals.py` and
  `hard_metrics.py` crashed with `AttributeError` under
  `python scripts/<name>.py`: the first `getattr` published `_gh_api` into the
  module's own globals, and the next `_wheel_core()` call matched the running
  module by that attribute and returned itself. The core module is now resolved
  once and bound from that object. A test runs all three helpers as files.

### Added

- **Daily catalog, built by CI.** `.github/workflows/sync-catalog.yml` runs
  `scripts/build_catalog.py` on a 03:00 UTC cron and publishes `catalog.jsonl`
  plus `catalog.meta.json` to an orphan `data` branch. No VPS: GitHub Actions is
  the host, `GITHUB_TOKEN` the credential. Measured cost of one run: 264 API
  requests, about 200 seconds, 400 records out of 533 observed.
- **The catalog is a movement index, not a mirror.** `select_index` publishes 400
  records in four tiers: 120 curated entries from awesome lists, 60 emerging
  gems, 180 ranked by measured `stars_per_day`, the rest fresh intake by stars so
  repositories without history get a baseline for tomorrow.
- **`sync-catalog` and `search-catalog`.** The client fetches from two origins,
  jsDelivr and `raw.githubusercontent.com`, and queries the cache locally with
  `--kind`, `--capability`, `--query`, `--gem`, `--moving` and `--limit`. Results
  carry `age_days`, `stale`, `with_momentum`, `matched` and `truncated`, so the
  catalog never enters an agent's context wholesale.
- **Five new archetypes.** `CANDIDATE_KINDS` gained `skill`, `plugin`,
  `mcp-server`, `design-system` and `reference` alongside `repository`,
  `product` and `service`, with typed ids such as `mcp-server:filesystem`.
- **`community-signals`.** Probes Hacker News, Stack Overflow, Reddit and a
  repository's own issues for regret and postmortem phrases. Every source
  reports its own status; `available_sources: 0` means no evidence was gathered,
  never that no complaints exist.
- **`dependency-debt`.** Transitive count from GitHub's SBOM, direct count from
  manifests across seven ecosystems, unlicensed package count. Falls back to
  `partial` with `transitive: null` when the SBOM is unavailable.
- **`hard-metrics`.** Bus factor, HHI contributor concentration, commit cadence
  over 52 weeks, release cadence. A metric that could not be measured lands in
  `unavailable` rather than passing as a zero.
- **Multilingual probe terms.** `registry/probe_terms.yaml` carries regret
  phrases for en, ru, es, pt, de, fr, zh and ja. English is always probed;
  `WHEEL_PROBE_LANGS` adds more, capped at eight terms per source.
- **Reddit without risking an account.** App-only OAuth via
  `WHEEL_REDDIT_CLIENT_ID` and `WHEEL_REDDIT_CLIENT_SECRET`, which involves no
  user account and so cannot get a plugin user banned. Falls back to the managed
  donsetch reader, then to an honest `blocked`.
- **Cloudflare edge as a real second origin.** `edge/worker.js` serves the
  catalog from KV, read-only, with ETag and 304. `scripts/publish_edge.py`
  uploads from CI through the Cloudflare API. The client prefers
  `WHEEL_CATALOG_EDGE_URL` when set. Optional: unset, nothing changes.
- **`evidence_gaps` in expert decisions.** A required list of
  `{source, reason}` objects naming every source that returned nothing. An empty
  list is allowed and is a claim: every source answered.
- **Objective maturity signals.** `detect_emerging_gem` and kind-aware
  `maturity_flags`, so a young low-star repository is examined rather than
  filtered out.
- **ADRs.** `.procoder/adr/0001` through `0010` record why each of these choices
  was made, including the rejected alternatives.
- **Dependabot** for the `github-actions` ecosystem, weekly.

### Changed

- Every `uses:` in both workflows is pinned to a commit SHA with the tag as a
  comment. A moving major tag can be repointed by its owner; the daily job holds
  `contents: write`.
- `registry/capabilities.yaml` grew from 19 to 22 capabilities: `devops-deploy`,
  `api-integration` and `developer-tools` were referenced by the catalog registry
  but did not exist in the classifier.
- The catalog registry and the classifier are now one contract. The build fails
  when a source declares no capability, or one the client cannot classify.
- `awesome_lists` entries accept a `repos:` list. The system-design list links to
  markdown inside itself, so link extraction returned nothing for the one source
  that was asked for by name; seven references are now pinned outright.
- README rewritten in both languages for the new architecture.

### Fixed

- `subprocess.run(text=True)` decoded with the Windows console codepage, so any
  non-ASCII byte in a GitHub payload raised inside the reader thread and the
  caller saw an unparseable result. Every call site pins UTF-8.
- `--json` output was written in the console codepage, so the file a program was
  meant to parse was not valid UTF-8. `_force_utf8_streams` pins both streams.
- `python scripts/wheel.py <command>` crashed with `ModuleNotFoundError` for the
  three new commands: that invocation puts `scripts/` on `sys.path`, not the
  repository root. `_sibling_module` resolves either way.
- GitHub's search API does not follow repository renames and answers 422;
  `_canonical_slug` resolves through `repos/{slug}` first, which does.
- One query ORing every negative term matched nothing on Algolia and was rejected
  by GitHub search, which read as "no complaints found". Both probe one term at a
  time now.
- The intake tier ranked purely by stars, so a 9000-star repository moving one
  star in two days took the slot from a 7000-star newcomer. Repositories without
  history come first.
- `meta.curated` counted awesome-list origin, not the curated tier, and reported
  180 against a cap of 120. Renamed to `from_curated_lists`.

### Known gaps

- The daily workflow has not yet completed a scheduled run; the first one creates
  the `data` branch.
- Reddit answers 403 to unauthenticated clients from many networks. Without OAuth
  credentials the source reports `blocked`, which is honest but empty.
- The catalog's movement tier is empty on the first run and thin on the second:
  `stars_per_day` needs at least half a day between snapshots.

## 0.6.0 and earlier

See the git history. This changelog starts at 0.7.0.
