# 0007 — GitHub Actions are pinned by commit SHA

Status: accepted
Date: 2026-09-04

## Context

Both workflows referenced `actions/checkout@v4` and `actions/setup-python@v5`.
A major tag is a movable pointer: the upstream owner can repoint it at any
commit, and a run picks the new code up silently on the next trigger.

`sync-catalog.yml` runs unattended on a daily cron, holds `contents: write`,
and is handed a token. A compromised upstream tag would execute in that job
with those rights. That is the highest-value target in this repository, and it
is protected by nothing but trust in an account we do not control.

## Decision

Every `uses:` reference is pinned to a full commit SHA with the human-readable
tag kept as a trailing comment:

- `actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4`
- `actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5`

A SHA cannot be repointed, so what ran yesterday is what runs tomorrow.

`.github/dependabot.yml` subscribes the `github-actions` ecosystem to a weekly
check, so an upstream release arrives as a reviewable pull request rather than
as an unannounced change or not at all.

## Consequences

The daily job can no longer be changed by anyone but us, and every upstream
change becomes a diff a human approves. The cost is that security fixes stop
arriving automatically: without Dependabot the pins would rot, which is why the
two parts of this decision are not separable. The comment after each SHA is the
only thing keeping the file readable, and it is not verified by anything — a
comment that disagrees with its SHA is a lie the tooling will not catch.

## Evidence

- `.github/workflows/checks.yml`, `.github/workflows/sync-catalog.yml`.
- `.github/dependabot.yml`.
- SHAs resolved from `repos/actions/checkout/git/ref/tags/v4` and
  `repos/actions/setup-python/git/ref/tags/v5`.
