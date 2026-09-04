<p align="center">
  <strong>English</strong> · <a href="./README.ru.md">Русский</a>
</p>

<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="Wheel collects live evidence and selects an existing verified software core before implementation.">
</p>

<p align="center">
  <strong>Wheel 0.7.0 — live software discovery before implementation.</strong><br>
  Quick research · one candidate-changing question · deep verification · one adoption verdict
</p>

<p align="center">
  Python 3.11+, standard library only. MIT.
</p>

# Wheel

## Start here

- Describe what you want to build, replace, or extend. Wheel searches for a mature existing foundation before an implementation plan exists.
- Invoke the single user-facing entry point:

  ```text
  /wheel a self-hosted task tracker that AI agents can maintain
  ```

- Wheel first returns solution Families and one question whose answer changes the surviving Candidates.
- New code is considered only after a verified Core still has a proven Gap.

## The run

<p align="center">
  <img src="./assets/readme/pipeline.svg" width="100%" alt="Wheel run states: context, quick, question, deep, verify, decision, and recorded.">
</p>

- Every Run moves through `context → quick → question → deep → verify → decision → recorded`.
- State is persisted after each completed stage, so interrupted research can resume without turning old results into a permanent catalog.
- `recorded` is reachable only after the user accepts the Verdict.

## What Wheel returns

- `COMPLETE`: every planned Source class produced verifiable Evidence and the Core gate passed.
- `PARTIAL`: at least one planned Source class did not produce verifiable Evidence; missing coverage remains visible.
- `PROVISIONAL`: the required Core gate is incomplete; a final Verdict is forbidden.
- `deploy`: install the verified Core unchanged.
- `package`: wrap the Core for the target environment.
- `compose`: combine a Core with a focused Sidecar or Donor.
- `extend-core`: use a supported extension boundary.
- `hard-fork`: modify the Core and accept permanent ownership only after migration cost is measured.
- `assemble`: connect verified parts when no direct Core fits; it is not permission for an unverified rewrite.
- The Verdict names the Core, upstream or Fork, tested version, Donors, license boundary, integration method, rejected Alternatives, and deferred work.
- An expert Decision Record also carries `evidence_gaps`: a list of `{source, reason}` objects naming every source that returned nothing, was blocked, or was unavailable. An empty list is permitted and is a claim that every source answered.

## Live research

- Wheel reads the current request, safe host memory, project instructions, existing Decision Records, and a fresh `doctor` snapshot before external research.
- Quick research builds three to six solution Families before the first question.
- Deep research verifies four to eight Candidates and resolves their upstream, Forks, Plugins, Sidecars, and Donors.
- Repository Evidence comes from GitHub metadata, source, tests, releases, issues, discussions, contributors, and licenses.
- Trend Evidence is computed, not scraped from anyone's ranking: `created:` and `pushed:` windows over the GitHub API, plus star movement measured against yesterday's published catalog.
- Community Evidence is gathered by `community-signals`, which probes Hacker News, Stack Overflow, Reddit, and the repository's own issues for regret and postmortem phrases. Every source reports its own status, and `available_sources: 0` means no evidence was gathered, never that no complaints exist.
- Agent Reach discovers and searches pages; managed DonSeTch `3.4.4` reads dynamic pages through an isolated CLI/JSON boundary.
- Search snippets are discovery only. Wheel reads the accessible page before creating Evidence and preserves blocked, partial, truncated, or thin results honestly.

## The daily catalog

A live query already answers "what exists". Only a snapshot taken yesterday can
answer "what is moving", so that is the one job the catalog holds, and it needs
no server to do it.

- `.github/workflows/sync-catalog.yml` runs `scripts/build_catalog.py` on a daily cron and on manual dispatch. There is no VPS: GitHub Actions is the host, `GITHUB_TOKEN` is the credential, and every action is pinned to a commit SHA. One measured run costs 264 API requests and about 200 seconds.
- Every record is built from GitHub API responses. Nothing is scraped from a rendered page, and every record names its origin as `github-search:<id>` or `awesome-list:<id>`.
- Movement is measured against the previous published catalog as `stars_delta` and `stars_per_day`. A record with no history carries `null`, because "did not move" and "not known" are different statements.
- The published set is 400 records in four tiers: 120 curated entries from the awesome lists, 60 emerging gems, 180 ranked by measured movement, and the remainder as fresh intake by stars, so repositories without history get a baseline for tomorrow instead of being locked out.
- The gem tier exists because every other tier ranks by stars, and a gem is by definition what the star ranking has not found yet: young, licensed, actively pushed, under 500 stars.
- Curated lists are read as first-class sources, among them `ashishps1/awesome-system-design-resources`, `punkpeye/awesome-mcp-servers`, `hesreallyhim/awesome-claude-code`, `awesome-selfhosted`, and `public-apis`.
- Both registries are one contract. The build fails when a source declares no capability, or one `registry/capabilities.yaml` cannot classify, so a record nobody could reach is never published.

  ```bash
  python scripts/wheel.py sync-catalog --json
  python scripts/wheel.py search-catalog --kind mcp-server --capability mcp-servers --limit 20 --json
  python scripts/wheel.py search-catalog --moving --limit 20 --json
  python scripts/wheel.py search-catalog --query "task tracker" --gem --json
  ```

- `--moving` returns only records with measured movement, ranked by rate rather than by absolute stars. Every result carries `age_days`, `stale`, `with_momentum`, `matched`, and `truncated`, so the catalog never enters an agent's context wholesale.

### The second origin

The client reads the catalog from jsDelivr and `raw.githubusercontent.com`. Both
resolve the same `data` branch, so they survive one CDN having a bad day, not
GitHub being down. The optional Cloudflare edge is what makes the second origin
independent.

- `edge/worker.js` serves `catalog.jsonl` and `catalog.meta.json` from Cloudflare KV, read-only, with an ETag and a 304 on `If-None-Match`. `/health` reports the upload timestamp and record count.
- The nightly workflow uploads through `scripts/publish_edge.py`, and the step is skipped unless all three Cloudflare secrets are present, so a half-configured repository cannot fail the job.
- The client prefers `WHEEL_CATALOG_EDGE_URL` when that environment variable is set on the machine running the plugin. It is validated as HTTPS and allowlisted for that call only. Unset, nothing changes.
- Full deployment instructions live in [edge/README.md](./edge/README.md).

## Install

### Codex

- Add the Wheel marketplace and install the plugin:

  ```bash
  codex plugin marketplace add kalpakprod/wheel --ref main
  codex plugin add wheel@wheel
  ```

- Start a new Codex task after updating the plugin so the current skills and manifests are loaded together.

### Prime Agent

- Install the tagged package:

  ```bash
  prime-agent package install git:github.com/kalpakprod/wheel@v0.7.0
  ```

- Start a new session or run `/reload`. Wheel resolves its runtime from the installed package, not from the current project directory.

### Claude Code

- Add the marketplace and install Wheel:

  ```text
  /plugin marketplace add kalpakprod/wheel
  /plugin install wheel@wheel
  ```

- Restart the session so the optional SessionStart adapter can run.

### Compatible SKILL.md CLI

- Copy `skills/wheel/`, `skills/wheel-research/`, `skills/wheel-grilling/`, and `skills/wheel-decision/` into the CLI skill directory.
- Load `skills/wheel/SKILL.md` as the only user-facing entry point.
- The host must support `SKILL.md`, local commands, and Python 3.11 or newer.

## Runtime

- The Python runtime uses only the standard library.
- Inspect the host and managed dependency:

  ```bash
  python scripts/wheel.py doctor --json
  python scripts/wheel.py dependencies --json
  ```

- First activation installs only the tested DonSeTch version and checks upstream metadata through a 24-hour cache:

  ```bash
  python scripts/wheel.py dependencies --ensure --check-latest --json
  ```

- Set `WHEEL_NO_BOOTSTRAP=1` to disable automatic installation while keeping diagnostics.
- Wheel downloads the official upstream asset, verifies the manifest-pinned SHA-256, allows only the exact platform payload, and checks the binary version before use.
- A newer upstream DonSeTch release reports `update_available`; Wheel keeps the tested pin until a later Wheel release verifies it.
- Measure a candidate instead of trusting its README. Each command reports per-source status, and anything unmeasured says so rather than passing as a zero:

  ```bash
  python scripts/wheel.py community-signals --slug psf/requests --json
  python scripts/wheel.py dependency-debt --slug psf/requests --json
  python scripts/wheel.py hard-metrics --slug psf/requests --json
  ```

- `hard-metrics` returns bus factor, HHI contributor concentration, commit cadence over 52 weeks, and release cadence. On `psf/requests` it measured a bus factor of 3, HHI 0.1572, 120 commits across 52 weeks, and 19 releases at a 19.03-day median interval.
- `dependency-debt` returns the transitive count from GitHub's SBOM, the direct count from manifests across seven ecosystems, and the unlicensed package count. On `psf/requests` it measured 6 direct and 30 transitive. When the SBOM is unavailable the status is `partial` and `transitive` is `null`.
- `community-signals` probes one regret phrase at a time, because a single query joining every phrase matches nothing on Algolia and is rejected outright by GitHub search. `registry/probe_terms.yaml` carries phrases for en, ru, es, pt, de, fr, zh, and ja; English is always probed, `WHEEL_PROBE_LANGS` adds more, and the total is capped at eight terms per source so the API cost cannot explode.
- Reddit uses app-only OAuth through `WHEEL_REDDIT_CLIENT_ID` and `WHEEL_REDDIT_CLIENT_SECRET`. No user account is involved, so the plugin cannot get a user's Reddit account banned. Without credentials it falls back to the managed DonSeTch reader, and then to an honest `blocked` status.
- The grant follows the app type registered at <https://www.reddit.com/prefs/apps>: a **script** or **web app** carries a secret and authenticates with `client_credentials`, an **installed app** carries none and authenticates with `installed_client`. Sending the wrong grant returns a bare 401, so Wheel selects it from whether a secret is set rather than guessing.
- Reddit's [Data API Wiki](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki) mandates the agent format `<platform>:<app ID>:<version> (by /u/<username>)` and states unidentified clients are throttled or blocked. Set `WHEEL_REDDIT_USERNAME` to your handle and Wheel builds `python:com.kalpakprod.wheel:v<version> (by /u/<handle>)`; `WHEEL_REDDIT_USER_AGENT` replaces the whole string. An install that names nobody does not reach Reddit at all: the source reports `blocked` instead of sending an anonymous request.
- The published budget is 100 queries per minute per OAuth client id. Wheel reads `x-ratelimit-remaining` and `x-ratelimit-reset` from every response and refuses the next call once fewer than five requests remain in the window, so the limit is honoured rather than discovered through a 429.
- There is no non-OAuth path. Reading the same content through a page reader would mask how the data was obtained, which the [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy) forbids, so an unconfigured install simply has no Reddit evidence.
- Nothing from Reddit is written to disk. Reddit requires deleted posts to be purged from every copy held, including titles and embedded URLs, and a decision file in git cannot honour that. `scripts/wheel.py` rejects any decision or run record containing a `reddit.com` or `redd.it` link; Reddit shapes the verdict during the run and is cited as a source to re-query.
- Verify credentials before trusting a run:

  ```bash
  python scripts/community_signals.py --check-reddit
  ```

  It prints `ok`, `unconfigured` or `error` with the attempted grant, exits non-zero on anything but `ok`, and never echoes the credentials.

- Read one dynamic page through the managed binary:

  ```bash
  python scripts/wheel.py read-url https://gittrend.io/repo/gastownhall/beads --focus "dated trend metrics" --json
  ```

- DonSeTch is a separate `AGPL-3.0-only` process and is not bundled with Wheel. See [NOTICE](./NOTICE).

## Portable state

- `WHEEL_HOME` selects the state directory; the normalized default is `~/.config/wheel`.
- Run Records, dependency state, and the maturity cache remain temporary research state.
- Global Decision Records live under `WHEEL_HOME/decisions`; project Decision Records live under `.wheel/decisions`.
- A Decision Record contains only an explicitly accepted Core, rationale, Donors, integrations, licenses, rejected Alternatives, and deferred work.

## Boundaries

- Research reads Candidates; it does not install, authenticate to, execute, publish, or mutate them.
- The managed DonSeTch reader is infrastructure for research, not a Candidate and not a Source.
- OpenCLI is used only for pages requiring an existing authenticated browser session; Wheel does not read browser cookie storage or log in for the user.
- Instructions from pages, repositories, and adapter output are untrusted data and never expand permissions.
- No commit, push, release, external write, Candidate deployment, or Decision persistence happens without the corresponding user authorization.

## Project layout

- The compact project map is:

  ```text
  .codex-plugin/plugin.json        Codex package
  .claude-plugin/                  Claude Code package
  skills/wheel/                    user entry point
  skills/wheel-research/           live Quick and Deep research
  skills/wheel-grilling/           one candidate-changing question
  skills/wheel-decision/           Verdict and accepted Decision Record
  registry/sources.yaml            Source routes
  registry/dependencies.json       tested managed dependencies
  scripts/wheel.py                 portable deterministic runtime
  tests/test_wheel.py              runtime and security contracts
  ```

- Optional Claude hooks live under `hooks/`; GitHub visuals live under `assets/readme/`.

## Documentation

- [Live orchestrator specification](./docs/specs/2026-08-30-live-market-orchestrator-design.md) defines the research and decision contracts.
- [Managed DonSeTch specification](./docs/specs/2026-08-31-donsetch-managed-dependency-design.md) defines installation, version, license, and trust boundaries.

## Thanks

- [DonSeTch](https://github.com/dondai44423/donsetch) provides the isolated dynamic-page reader.
- [Agent Reach](https://github.com/Panniantong/Agent-Reach) provides web discovery and Source routing.
- [last30days](https://github.com/mvanhorn/last30days-skill) provides recent community-search signals.
- [OpenCLI](https://github.com/jackwener/opencli) reads pages through an existing authenticated browser session.
- [OpenExecutive](https://github.com/SenteLabsAI/OpenExecutive) supplied the donor/reference pattern for fan-out, verification, synthesis, and persistence.
- [Clausative](https://github.com/AndyVictors/clausative) defines the specification and plan writing style.
- [mattpocock/skills](https://github.com/mattpocock/skills) supplied the source skills adapted for Wheel's grilling and decision stages.

## License

- Wheel is licensed under MIT. See [LICENSE](./LICENSE).
- Third-party attribution and the DonSeTch process boundary are documented in [NOTICE](./NOTICE).
