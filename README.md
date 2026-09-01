<p align="center">
  <strong>English</strong> · <a href="./README.ru.md">Русский</a>
</p>

<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="Wheel collects live evidence and selects an existing verified software core before implementation.">
</p>

<p align="center">
  <strong>Live software discovery before implementation.</strong><br>
  Quick research · one candidate-changing question · deep verification · one adoption verdict
</p>

# Wheel

## Start here

- Describe what you want to build, replace, or extend. Wheel searches for a mature existing foundation before an implementation plan exists.
- Invoke the single user-facing entry point:

  ~~~text
  /wheel a self-hosted task tracker that AI agents can maintain
  ~~~

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

## Live research

- Wheel reads the current request, safe host memory, project instructions, existing Decision Records, and a fresh `doctor` snapshot before external research.
- Quick research builds three to six solution Families before the first question.
- Deep research verifies four to eight Candidates and resolves their upstream, Forks, Plugins, Sidecars, and Donors.
- Repository Evidence comes from GitHub metadata, source, tests, releases, issues, discussions, contributors, and licenses.
- Trend Evidence comes from GitTrend, Trendshift, and current repository activity.
- Community Evidence can include Reddit, Telegram, Hacker News, X, YouTube, V2EX, and last30days when available.
- Agent Reach discovers and searches pages; managed DonSeTch `3.4.4` reads dynamic pages through an isolated CLI/JSON boundary.
- Search snippets are discovery only. Wheel reads the accessible page before creating Evidence and preserves blocked, partial, truncated, or thin results honestly.

## Install

### Codex

- Add the Wheel marketplace and install the plugin:

  ~~~bash
  codex plugin marketplace add kalpakprod/wheel --ref main
  codex plugin add wheel@wheel
  ~~~

- Start a new Codex task after updating the plugin so the current skills and manifests are loaded together.

### Prime Agent

- Install the tagged package:

  ~~~bash
  prime-agent package install git:github.com/kalpakprod/wheel@v0.6.0
  ~~~

- Start a new session or run `/reload`. Wheel resolves its runtime from the installed package, not from the current project directory.

### Claude Code

- Add the marketplace and install Wheel:

  ~~~text
  /plugin marketplace add kalpakprod/wheel
  /plugin install wheel@wheel
  ~~~

- Restart the session so the optional SessionStart adapter can run.

### Compatible SKILL.md CLI

- Copy `skills/wheel/`, `skills/wheel-research/`, `skills/wheel-grilling/`, and `skills/wheel-decision/` into the CLI skill directory.
- Load `skills/wheel/SKILL.md` as the only user-facing entry point.
- The host must support `SKILL.md`, local commands, and Python 3.11 or newer.

## Runtime

- The Python runtime uses only the standard library.
- Inspect the host and managed dependency:

  ~~~bash
  python scripts/wheel.py doctor --json
  python scripts/wheel.py dependencies --json
  ~~~

- First activation installs only the tested DonSeTch version and checks upstream metadata through a 24-hour cache:

  ~~~bash
  python scripts/wheel.py dependencies --ensure --check-latest --json
  ~~~

- Set `WHEEL_NO_BOOTSTRAP=1` to disable automatic installation while keeping diagnostics.
- Wheel downloads the official upstream asset, verifies the manifest-pinned SHA-256, allows only the exact platform payload, and checks the binary version before use.
- A newer upstream DonSeTch release reports `update_available`; Wheel keeps the tested pin until a later Wheel release verifies it.
- Read one dynamic page through the managed binary:

  ~~~bash
  python scripts/wheel.py read-url https://gittrend.io/repo/gastownhall/beads --focus "dated trend metrics" --json
  ~~~

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

  ~~~text
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
  ~~~

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
