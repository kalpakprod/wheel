---
name: wheel
description: Use for any meaningful request to create or change functionality before implementation. Skip only an explicitly declined Wheel run or a trivial mechanical edit; Wheel performs live market discovery, candidate-derived questioning, verification, and an adoption verdict.
---

# Wheel

Wheel finds and verifies an existing software foundation before any implementation plan. It does not propose new code until a verified Gap remains after the Verdict.

Use Wheel for every meaningful request to create or change functionality, including a feature, behavior-changing fix, replacement, or new product. Do not require the user to name an architectural choice. Skip Wheel only when the user explicitly declines it or the request is a trivial mechanical edit, such as a typo, mechanical rename, or one-line non-functional configuration change. If a request combines mechanical edits with a meaningful functional change, run Wheel.

## Run contract

Create or resume one runtime run for the current request. Advance it in this order:

`context` -> `quick` -> `question` -> `deep` -> `verify` -> `decision` -> `recorded`

Resolve `<wheel-root>` from this loaded `SKILL.md`: its directory is `<wheel-root>/skills/wheel`. Require `<wheel-root>/scripts/wheel.py`, `<wheel-root>/registry/capabilities.yaml`, and `<wheel-root>/registry/sources.yaml` to exist. Use `python "<wheel-root>/scripts/wheel.py"` for every runtime call regardless of the current project directory, and pass the resolved runtime and registry paths to internal Wheel skills. Do not guess another install path or fall back to a project-relative `scripts/wheel.py`.

Before `doctor` and context collection, run `python "<wheel-root>/scripts/wheel.py" dependencies --ensure --check-latest --json` once unless `WHEEL_NO_BOOTSTRAP=1`. The mandatory latest-release check uses its 24-hour public metadata cache. Before the first download, report `DonSeTch 3.4.4`, `AGPL-3.0-only`, and `https://github.com/dondai44423/donsetch`. A generic install hook does not exist, so this first Wheel activation is the portable bootstrap point. With `WHEEL_NO_BOOTSTRAP=1`, do not install; collect diagnostics only.

Persist a safe dependency status in `tool_snapshot`, never raw command output, credentials, cookies, or other secrets. Preserve the dependency's reported status, tested version, installed version when present, checked time, and concise non-sensitive detail. If bootstrap fails, continue once with its dependent Source routes honestly `unavailable` or `error`; do not retry the same failure cause more than once.

Before live research, read the current request, safe host memory, applicable project instructions, relevant project and global Decision Records, and `doctor --json`. The current request overrides project decisions; project decisions override global decisions; a fresh doctor result overrides an old snapshot.

Persist the full current value of every workflow-owned field with `python "<wheel-root>/scripts/wheel.py" update-run --run-id <run-id> --input <json> [--home <wheel-home>]` at its checkpoint. An update replaces each named field; never send a partial replacement for `context`, `families`, `user_answers`, `tool_snapshot`, `candidates`, or `edges`.

Use `<wheel-root>/registry/capabilities.yaml` to classify the requested archetype (repository, library, service, skill, plugin, mcp-server, design-system, reference architecture) and derive search terms, topics, and suitable installed skills. Route research through `<wheel-root>/registry/sources.yaml`; neither registry recommends a product. When `scripts/wheel.py sync-catalog` reports `ok` or `cached`, query the offline catalog with `scripts/wheel.py search-catalog --kind <archetype> [--capability <id>] [--query <term>] [--gem] [--moving] --json`; never read `catalog.jsonl` directly. Its result carries `age_days` and `stale`: when `stale` is true, or `sync-catalog` reports `unavailable`, say so and rely on live routes only. Never describe the catalog as fresh without a successful sync in this run. Catalog records are a starting candidate list, not evidence; every candidate still needs its own verified SourceResult. The catalog publishes movement, not a mirror of GitHub: use `--moving` for momentum questions, and when the result reports `with_momentum: 0` state that no movement has been measured yet instead of reading star counts as trend.

## Orchestration

1. Create the run with `init-run`. While it remains in `context`, gather the required context and the fresh doctor result, then use `update-run` to save the complete `context` and `tool_snapshot`.
2. Transition directly from `context` to `quick`, then invoke `wheel-research` for the Quick pass. It must run before any user question. After the pass completes, `wheel-research` uses `update-run` to save the complete `families`, `candidates`, and `edges`.
3. Send the persisted Quick pass's Families, Candidates, Evidence, and decision axes to `wheel-grilling`. It alone returns the one candidate-changing question or `stable`. If it returns a question, transition to `question` and stop. Ask no second question in the same user turn.
4. On the next user answer, append the answer to the complete `user_answers` list with `update-run` before passing the persisted Candidates and answer to `wheel-grilling`. It alone may return one new candidate-changing question or declare the set stable.
5. Invoke the Deep pass only after grilling is stable or the user explicitly delegates the choice. `wheel-research` persists the complete `candidates` and `edges` with `update-run` after each completed deep checkpoint; transition through `deep` and `verify` as the passes complete.
6. Evaluate the verified Candidates with `wheel-decision`. It produces the Verdict or reports that the gate is incomplete.
7. After explicit user acceptance only, invoke `wheel-decision` to create the Decision Record and ask the runtime `record-decision` command to persist it. Transition to `recorded` only after successful persistence.

Internal skills are not alternative user-facing workflows. Do not expose them as commands or substitute them for Wheel.

## Question invariant

`wheel-grilling` is the sole owner of user questions. Ask exactly one question per user turn. A question is legal only when at least one answer changes a Family, Candidate, Core, or integration method. Each question states affected Candidates or Families and a fact-backed recommendation. If no legal question remains, grilling is stable.

## Verdict gate

A result begins with coverage state and adoption mode. It names the Core, upstream or fork, version or commit when applicable, selected donor functions and license-compatible integration, rejected alternatives, and up to three adjacent capabilities already provided by the Core. It also separates "already included, enable?" from "later, do not build".

Use the adoption modes `deploy`, `package`, `compose`, `extend-core`, `hard-fork`, and `assemble`. Preserve Candidate roles `base`, `fork`, `donor`, `plugin`, `sidecar`, and `alternative`.

No final Verdict is allowed unless the research result has the required Core gate:

- Open Core: code, documentation, license, activity, tests, and an independent usage signal.
- Closed service: official documentation, terms, cost, integrations, data-export path, and an independent usage signal.

Any planned source class without verifiable data makes coverage `PARTIAL`; this includes the `documentation` class. `COMPLETE` is allowed only when every planned class, including `documentation`, has coverage in the matrix. An incomplete Core gate makes it `PROVISIONAL`, which forbids the final Verdict. A missing Candidate does not authorize building from scratch; continue candidate-changing research.

`hard-fork` requires measured code, data, and integration migration costs and comparison with extending the current system. Prefer `plugin` or `api` integration over Core modification; name the reason when `extend-core` or `hard-fork` is necessary.

## Boundaries

During research, read only. Do not install, execute, authenticate to, import, publish, or change a Candidate. Do not persist a Decision Record or host-memory summary until the user explicitly accepts the Verdict. Do not save secrets, cookies, unverified assumptions, or rejected Candidates as preferences.

For research details, SourceResult normalization, coverage accounting, source routing, and verification procedure, use `wheel-research`. For question construction and stability, use `wheel-grilling`. For Decision Records, use `wheel-decision`.
