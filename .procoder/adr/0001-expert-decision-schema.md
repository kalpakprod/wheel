# 0001 — Two decision schemas, expert by default

Status: accepted
Date: 2026-09-03

## Context

`skills/wheel-decision/SKILL.md` requires an Explicit Sacrifice section, a
Tradeoff Matrix and a code snippet in every verdict. `render_decision` in
`scripts/wheel.py` rendered those three blocks only when the caller happened to
supply them, and `DECISION_REQUIRED_FIELDS` did not list them. A decision with
none of the three passed validation and was written to disk, so the honesty
contract lived in the prompt only.

Making the three fields unconditionally required would invalidate every decision
record written before this contract and every existing test fixture.

## Decision

`decision_schema(decision)` classifies each decision as `legacy` or `expert`.

- Any of `explicit_sacrifice`, `tradeoff_matrix`, `code_comparison` present, and
  the decision is `expert`; then all three are required.
- None present, and it is `legacy`, rendered exactly as before.
- An explicit `"schema": "expert"` demands all three even when none are present.
- An explicit `"schema": "legacy"` carrying an expert field is rejected as a
  contradiction.

New decisions use `expert`. `legacy` exists for records that predate this ADR.

## Consequences

A partial expert verdict now fails loudly instead of being silently truncated,
which is the point. Backwards compatibility holds: no existing record or test
had to change. The cost is two validation paths in one function, and a later
migration step once no legacy records remain, at which point the three fields
move into `DECISION_REQUIRED_FIELDS` and `decision_schema` is deleted.

## Evidence

- `scripts/wheel.py`, `decision_schema`, `render_decision`.
- `tests/test_wheel.py`, `test_expert_schema_is_detected_and_enforced`.
