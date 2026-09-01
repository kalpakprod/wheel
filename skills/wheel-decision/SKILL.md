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
- up to three adjacent capabilities already in the selected Core;
- separate sections for "already included, enable?" and "later, do not build";
- the next post-acceptance action, without implementing it.

Do not propose `hard-fork` without measured code, data, and integration migration costs, or without comparison to extending the current system. Do not copy donor code when its license does not permit the intended use.

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
