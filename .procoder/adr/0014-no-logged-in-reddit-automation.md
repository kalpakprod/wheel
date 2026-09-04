# 0014 — A logged-in browser session is not a Reddit route

Status: accepted
Date: 2026-09-04

## Context

`opencli` 1.8.7 is installed on this machine and ships a Reddit site adapter with
`login`, `search`, `subreddit`, `read`, `user-comments`, `saved`, and the write
verbs `upvote`, `comment`, `reply`, `subscribe`. It drives a real Chrome profile,
so `read <post-id>` returns a post together with its comment tree: exactly the
gap ADR 0013 documented as unreachable, since the archive's comment search times
out and Reddit's own comment endpoints are OAuth-only.

`agent-reach` 1.5.0 is installed as well and is already the adapter for the
`web`, `documentation`, `github`, `gittrend`, `trendshift` and `hackernews`
routes, with `browser_capability: opencli` and
`browser_session: existing-authenticated` on the two page-reading classes.

The temptation is obvious: the tooling is present, it works, and it closes the
one hole in the evidence chain.

HKUDS/CLI-Anything (49k stars, Apache-2.0) was checked in the same pass. It is a
hub of agent-native CLIs for local software — Blender, GIMP, Obsidian, OBS,
Zotero — and carries no Reddit surface. It is irrelevant to this decision.

## Decision

Wheel does not read Reddit through a logged-in browser session, and no skill
instruction may tell an agent to.

Three lines of Reddit's Responsible Builder Policy decide it. "You must not
misrepresent or mask how or why you are accessing Reddit data": an automated
Chrome session presenting itself as a human is that mask. "Apps must register
and create a developer profile to get an App profile label. Apps must not
circumvent any labeling performed by Reddit": a browser session carries no app
label. "App accounts should solely be used to perform app functions (no mixed
use accounts)": the session belongs to a person who also reads Reddit as a
person.

The enforcement paragraph names the price: suspending the app, the account, and
associated accounts. The account at risk is the user's own, and the entire
reason app-only OAuth was chosen in ADR 0011 was that no human account should be
exposed by a research tool.

The write verbs are refused independently and permanently: the policy's
prohibited-activities section names vote and karma manipulation, and a research
gate has no business casting a vote.

This is recorded in `registry/sources.yaml` next to the reddit route and in
`skills/wheel-research/SKILL.md`, so an agent that finds `opencli reddit` on the
machine reads the prohibition before it reasons its way into using it.

## Consequences

Comment threads stay out of reach. Wheel sees Reddit titles and post bodies and
never the thread where someone explains what broke in production, which is a
real loss of evidence quality and is stated as such rather than papered over.

`opencli` and `agent-reach` remain fully in use for everything they are allowed
to do: GitHub, GitTrend, Trendshift, Hacker News, documentation pages, and any
page whose owner expects an authenticated read by its own user.

If Reddit ever offers an approved path for this kind of tool, or the user
accepts the account risk deliberately for a one-off manual check, that is a
human decision executed by hand outside the gate, not a lane wired into the
automated evidence chain.
