# 0011 — Reddit is read live through OAuth and never stored

Status: accepted
Date: 2026-09-04

## Context

Reddit's Data API Wiki and Responsible Builder Policy set three rules that the
first implementation broke.

The agent string must be `<platform>:<app ID>:<version> (by /u/<username>)`, and
Reddit states it throttles or blocks unidentified clients. Wheel sent one shared
hardcoded agent for every installation.

Traffic that does not use OAuth is blocked, and masking how Reddit data is
reached is prohibited. Wheel fell back to a managed page reader whenever OAuth
was unconfigured or failed, which is exactly that mask.

Content deleted on Reddit must be removed from every copy held, including
titles, bodies and embedded URLs; retention is a violation even when anonymized.
Wheel wrote Reddit titles and permalinks into decision files that live in git
forever, where a deletion can never be honoured.

## Decision

OAuth is the only route. The page-reader fallback for Reddit is deleted; an
unconfigured install reports `blocked` with the reason and simply has no Reddit
evidence.

The agent string is built from `WHEEL_REDDIT_USERNAME` in the documented format,
or replaced wholesale by `WHEEL_REDDIT_USER_AGENT`. An install that names nobody
raises before a request is made rather than sending an anonymous one.

The published budget of 100 queries per minute per client id is honoured from
the `x-ratelimit-remaining` and `x-ratelimit-reset` headers, refusing the next
call inside a five-request reserve.

Reddit content is not persisted. `_reject_reddit_content` runs inside the same
validators that already reject secrets, so any decision or run record carrying a
`reddit.com` or `redd.it` link fails to write. Findings are paraphrased and the
source is cited as one to re-query.

## Consequences

Reddit evidence now costs a registered app and a declared username, and users
who configure neither lose the source entirely instead of getting it through a
side door. Reddit findings cannot be audited from a stored link later; the run
that used them is the only place they existed, and a re-check means a re-query.

In exchange every rule that has an enforcement mechanism is enforced by code
rather than by a sentence in the README: the wrong agent cannot be sent, the
budget cannot be overrun silently, and the retention rule cannot be broken by an
agent that decides a permalink would look good in an ADR.
