#!/bin/bash
# wheel: inject the market-first gate into session context
cat <<'EOF'
<wheel-rule>
Before implementing ANY user request that creates or changes functionality — a new project, a feature in an existing one, an integration, an automation, a UI, a script — you MUST run the `wheel` skill FIRST, before writing code or a plan.

The gate in short:
1. Classify the request: greenfield / feature-in-existing / replace. On "feature", also check whether the user's whole project is a reinvention of an existing product — that is the most commonly missed case.
2. Take stock of what is already installed (skills, plugins, tools) before searching the market.
3. Search for existing solutions in this order: ~/.claude/wheel/decisions/ (past verdicts, skip silently if absent) -> GitHub search -> awesome-lists -> web. Target 4-8 candidates.
4. Interview the user with questions DERIVED FROM THE DIFFERENCES between candidates. A question is legal only if its answer changes the candidate list. Fewer candidates -> deeper interview. Zero candidates means the interview continues, NOT that you start coding.
5. Score maturity (node scripts/maturity.mjs) and measure three gaps: functional, operational, architectural. Pick an adoption mode: deploy / package / compose / extend-core / hard-fork / assemble.
6. "Write it from scratch" is NOT a valid verdict. The worst case is `assemble` — building from other people's pieces and reference points.

Never replace a user's existing project with a third-party one without a measured cost (code size, data volume, integrations), and never present replacement without showing `extend` next to it.

Skip the gate only for trivial mechanical edits (a typo, a rename, a one-line config change) or when the user explicitly declines it.
</wheel-rule>
EOF
