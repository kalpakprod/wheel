# 0005 — donsetch, an AGPL-3.0-only dependency, is used as a separate process

Status: accepted
Date: 2026-09-03

## Context

`registry/dependencies.json` declares one managed dependency: `donsetch` 3.4.4,
licensed `AGPL-3.0-only`, downloaded from its GitHub releases on first Wheel
activation. It supplies the fetch-and-extract capability several research routes
in `registry/sources.yaml` depend on.

AGPL-3.0 carries a network-use clause and copyleft that reaches derivative
works. Wheel itself is distributed under this repository's `LICENSE`, and an
automatic download of an AGPL binary onto a user's machine is a decision that
should be written down rather than discovered.

## Decision

donsetch stays, invoked strictly as a separate process over its CLI. Wheel does
not link it, import it, or vendor its source, so the two remain separate works
and the copyleft does not propagate into this repository.

The download is pinned and verified: the exact tested version, an explicit asset
per platform, a SHA-256 per asset, and an HTTPS allowlist enforced by
`_bounded_https_download`. `WHEEL_NO_BOOTSTRAP=1` disables the install entirely
and degrades the dependent routes to an honest `unavailable`, so the dependency
is never mandatory to run Wheel.

Before the first download, `skills/wheel/SKILL.md` requires reporting the name,
the version, the license `AGPL-3.0-only` and the upstream URL to the user.

## Consequences

Wheel gains the capability without inheriting AGPL obligations for its own code,
and a user who does not want the binary has a documented way to refuse it. The
cost is a supply-chain surface: an upstream release that changes behaviour is
only caught by the pinned hash failing, which is a hard stop rather than a
silent update. Any future move to import donsetch as a library, rather than
running it, invalidates this ADR and needs a new one.

## Evidence

- `registry/dependencies.json`.
- `scripts/wheel.py`: `load_dependency_manifest`, `ensure_dependency`,
  `_bounded_https_download`, `donsetch_fetch`.
- `skills/wheel/SKILL.md`, bootstrap paragraph.
