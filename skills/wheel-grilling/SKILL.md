---
name: wheel-grilling
description: Internal Wheel questioning stage that reduces live-researched Families and Candidates. Use only from wheel after its quick pass or after one user answer; emit exactly one legal next question or declare the Candidate set stable.
---

# wheel-grilling

Turn the researcher's Families, Candidates, Evidence, and decision axes into one user decision at a time. `wheel-grilling` is the sole owner of question construction and emission. Facts are the researcher's job. User preferences decide among fact-backed options.

## Legal question

A question is legal only when at least one possible answer changes the surviving Families, Candidates, possible Core, or integration method. Never ask about an implementation detail already supplied by every remaining Candidate.

Emit exactly one question per user turn. It includes:

- the choice in plain language;
- answer options;
- Families and Candidates affected by every option;
- a recommendation grounded in a verified fact;
- the Candidate-count consequence when known.

```text
Question: Managed service or self-hosted Core?
Managed service: affects Family A; keeps Candidate 1 and Candidate 2.
Self-hosted: affects Family B; keeps Candidate 3 and Candidate 4.
Recommendation: self-hosted, because the project instruction requires local data control.
```

Do not present a recommendation as a user preference. Do not ask a second question while waiting for an answer.

## Flow

1. After the Quick pass, derive and emit the next legal question from the supplied three to six Families, Candidates, Evidence, and decision axes.
2. After each answer, update requirements and request bounded research only for the affected branch.
3. Recompute the remaining Candidates, Candidate roles, and integration methods before proposing another question.
4. Continue only while a legal answer can change the final set.
5. Declare grilling stable when no remaining answer changes the final Candidate set, or when the user explicitly delegates the choice to Wheel.

If no Candidate remains, keep grilling and hand every new constraint, synonym, adjacent niche, or English term to `wheel-research` as a new search lead. Zero Candidates never authorizes a new implementation.

## Handoff

Return one of:

- `question`: one legal question with affected Families and Candidates;
- `stable`: remaining Candidates, answered constraints, eliminated Candidates and reasons, and any unresolved verification.

Wheel starts deep research only from `stable`.
