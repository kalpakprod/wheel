# 0012 — A public archive is Reddit's second lane, not a disguise for the first

Status: accepted
Date: 2026-09-04

## Context

ADR 0011 made OAuth the only way into Reddit and deleted the page-reader
fallback, which was correct on policy and left a hole in practice: an install
without a registered Reddit app has no community evidence from the largest
engineering forum on the internet.

The `last30days` skill solves the same problem with four keyless lanes: RSS
feeds, shreddit `/svc` listing partials, shreddit comment partials, and the
Arctic Shift archive. The first three read reddit.com's own surfaces without
authentication, which is exactly the masking Reddit's Responsible Builder Policy
prohibits, and they were measured to answer 403 from ordinary contexts anyway.
The fourth is different in kind: Arctic Shift is an independent public archive
on its own infrastructure.

Measured against the live service before choosing: `title` search requires a
`subreddit` or `author` alongside it, `after` filters by date, a listing query
returns 100 rows in under a second, a title query takes about five, and bursts
answer HTTP 422 "Timeout. Maybe slow down a bit".

## Decision

The `reddit-archive` lane searches Arctic Shift and runs only when the Reddit
API lane did not return `ok`. It reports under its own source name so a reader
can never mistake an archive snapshot for a live Reddit answer.

No Reddit credential is sent to it, no Reddit rate budget is consumed, and no
Reddit account can be sanctioned for it: it is a third party, and the request
carries wheel's own agent string.

Because the archive requires a subreddit, `registry/reddit_subreddits.yaml` maps
capabilities to at most four subreddits each, with an engineering default. The
file is optional and pyyaml is optional, exactly as with probe terms.

Requests are paced 2.5 seconds apart with one retry on 422/429, and the archived
`score` is surfaced as `score_at_archive` and excluded from ranking, because it
is the count at ingest time rather than now.

The retention rule from ADR 0011 is unchanged and applies here too: these rows
are Reddit user content, so they carry `retention: none` and the decision
validators still refuse to write a reddit link into a recorded file.

## Consequences

An unconfigured install gets Reddit evidence again, at the cost of freshness and
of engagement numbers that cannot be trusted. Wheel now depends on a volunteer
archive that can disappear or rate-limit; the lane degrades to `partial` or
`unavailable` and says which subreddit failed, so its absence is visible rather
than silent.

Rejected: the shreddit `/svc` partials and the RSS feeds. They read Reddit's own
surfaces without identifying the client, which is the practice ADR 0011 removed
on purpose. Reintroducing them through a different module would be the same
violation with a new name.
