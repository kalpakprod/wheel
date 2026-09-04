---
name: wheel-decision
description: Internal Wheel verdict and decision-record stage. Use after deep verification to produce a Clausative Verdict, and persist an accepted decision only when wheel confirms explicit user acceptance.
---

# wheel-decision

Create a Verdict from verified research. Persist a Decision Record only after the user explicitly accepts that Verdict.

## Verdict

Refuse a final Verdict when coverage is `PROVISIONAL` or the Core gate is incomplete. Mark missing planned source coverage `PARTIAL` without hiding the affected classes.

For a valid Verdict, state:

- coverage status and adoption mode: `deploy`, `package`, `compose`, `extend-core`, `hard-fork`, or `assemble`;
- one Core, its upstream or fork, and version or commit when applicable;
- Candidate roles: `base`, `fork`, `donor`, `plugin`, `sidecar`, `alternative`;
- each donor's function, license basis, and integration method: `plugin`, `api`, `sidecar`, `cherry-pick`, or `port`;
- a gain, cost, and score comparison with rejected alternatives and reasons;
- an Explicit Sacrifice section: state clearly what the architecture is sacrificing (e.g. memory footprint, conceptual complexity, transitive dependency risk, operational overhead) by picking this solution;
- a Tradeoff Matrix comparing the top candidates across 5 axes: Performance/Resource, Complexity/DX, Maintenance Cadence, Ecosystem/Portability, and Lock-in Risk;
- a Minimal DX Code Snippet illustrating the candidate's integration interface;
- up to three adjacent capabilities already in the selected Core;
- separate sections for "already included, enable?" and "later, do not build";
- the next post-acceptance action, without implementing it.

Do not propose `hard-fork` without measured code, data, and integration migration costs, or without comparison to extending the current system. Do not copy donor code when its license does not permit the intended use.

## Decision schema

Every new decision uses the `expert` schema: `explicit_sacrifice`, `tradeoff_matrix`, `code_comparison`, and `evidence_gaps` are all mandatory and validated by `render_decision`. Supplying one of them commits the decision to that schema, so a partial expert verdict is rejected rather than silently truncated. Set `"schema": "legacy"` only for a decision that predates this contract; a legacy decision carrying any expert field is rejected.

`evidence_gaps` is a list of `{"source": ..., "reason": ...}` objects, and it is where the decision admits what it could not check. Every source you consulted that returned nothing, was blocked, rate-limited, or unavailable belongs in it, with the reason string the command itself returned, verbatim: `reddit: HTTP 403`, `hackernews: 0 matches for every probe term`, `github_issues: gh api returned no payload`. An empty list is permitted and it is a claim you must be able to defend: every source answered.

Read the commands' own output honestly. `available_sources: 0` from `community-signals` means no evidence was gathered, never that no complaints exist. A `partial` or `unavailable` status from `dependency-debt` or `hard-metrics` means the metric was not measured, and a decision that treats an unmeasured metric as a passing one is the failure this plugin exists to prevent.

## Acceptance gate

Record only when all conditions hold:

1. Reversing the decision is expensive.
2. The choice is not obvious to a future reader.
3. Real alternatives were considered and rejected for recorded reasons.
4. The user explicitly accepted this Verdict and selected global or project scope.

Do not store Wheel's recommendation as a user preference before explicit acceptance. A recommendation that fails the first three conditions remains an unpersisted result.

## Decision Record

Render a stable, Clausative Markdown record from the runtime decision JSON. Its front matter contains a stable kebab-case `name`, `coverage`, `verdict`, `picked`, `maturity`, and `date`. Its sections are:

```md
## Request

## Coverage

## Candidates

## Decided

## Core and integration

## Donors and licenses

## Rejected alternatives

## Already included, enable?

## Later, do not build
```

The record includes the accepted Core, upstream or fork, donors, integrations, licenses, rejection reasons, adjacent capabilities, and deferred functions. It contains no secrets, cookies, unverified claims, or full implementation plan.

After acceptance, prepare the decision JSON and call runtime `record-decision` with the selected `global` or `project` scope. Supply a project root or home override only when the caller provided it. Let the runtime write the portable journal under `WHEEL_HOME/decisions` for global scope or `.wheel/decisions` for project scope, update the stable-slug record, and maintain its index. Do not invent a path outside those roots.

If the host exposes safe memory writing, request a short accepted-decision summary only after `record-decision` succeeds. If it does not, the portable journal is the only persistence. Propagate persistence errors and do not claim the run is `recorded` until the runtime confirms it.
