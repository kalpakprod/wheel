# 0013 — The Reddit routes that were tested and rejected

Status: accepted
Date: 2026-09-04

## Context

After ADR 0012 added the Arctic Shift lane, the obvious question was whether a
better free route exists. Every candidate below was called from this machine
before it was judged, so the record is measurements rather than reputation.

`https://old.reddit.com/search.json?q=...` answers HTTP 200 with 352 KB of HTML.
The same for `old.reddit.com/r/<sub>/.json`. The JSON endpoints are gone in
practice: what arrives is a rendered page, so a parser would be scraping the web
UI while pretending to consume an API.

`https://www.reddit.com/search.rss` answers HTTP 200 with a real Atom feed, four
entries, in under a second. It works. It is also reddit.com served to a client
that never identified itself through OAuth, which is the practice ADR 0011
removed on purpose.

`https://old.reddit.com/r/<sub>/top.rss` answers 200 with HTML containing
`shreddit-post` elements rather than a feed: the old host redirects into the new
UI, so the shreddit-partial technique and the RSS technique collapse into the
same scrape.

`https://api.pullpush.io/reddit/search/submission/` is the widest option on
paper: keyless full-text search with no subreddit required, five rows in 1.2
seconds, and a comment endpoint that returns the body text where migration
stories actually live. The second request in the same minute answered HTTP 429
with `"Rate limit exceeded. This website does not provide free scraping
resources for agents. Please contact the admin"`. The operator's own error
message refuses this use.

Arctic Shift's `/api/comments/search` answered HTTP 422 "Timeout. Maybe slow
down a bit" on every attempt across two subreddits and two retries. Comment
search is not available at this volume.

Arctic Shift's `/api/posts/search` accepts `selftext` alongside a subreddit and
returned rows in 7.9 seconds.

## Decision

Nothing new is added from reddit.com itself. The `.json` endpoints are HTML, and
the RSS feed, while functional, is the same unidentified read that ADR 0011
forbids; a working technique is not an allowed one.

PullPush is not used. A service that answers "does not provide free scraping
resources for agents" has told us what it wants, and routing around that with
pacing would be the same disrespect the Reddit fallback was deleted for.

The Arctic Shift lane gains a `selftext` sweep: for each subreddit the title is
queried first, and the body only when the title returned nothing, so the cost is
one extra request per empty subreddit rather than a doubling. Results carry
`matched_field` and are deduplicated by permalink.

Comment search stays out until the archive can serve it.

## Consequences

The free surface stops growing here. Reddit evidence is a registered app first
and an archive of post titles and bodies second, and neither reaches the comment
threads where the most useful detail sits. That gap is now documented rather
than quietly worked around, and the next person who proposes RSS or PullPush can
read what happened when they were tried.
