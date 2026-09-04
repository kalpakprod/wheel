"""Produce wheel's offline candidate catalog from verifiable GitHub facts.

Runs in CI (.github/workflows/sync-catalog.yml), never on a user's machine.
Every record is built from GitHub API responses; nothing is inferred from a
rendered web page and nothing is invented. The output is JSONL, one object per
line, readable by scripts.wheel._parse_catalog_payload.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.wheel import (
    CANDIDATE_KINDS,
    _parse_catalog_payload,
    detect_emerging_gem,
    normalize_repo_slug,
)

API_ROOT = "https://api.github.com"
RAW_CONTENT_ROOT = "https://raw.githubusercontent.com/"
USER_AGENT = "wheel-catalog-builder"
SEARCH_PAGE_SIZE = 50
MAX_ATTEMPTS = 4
README_MAX_BYTES = 8 * 1024 * 1024
RESERVED_OWNERS = frozenset(
    {
        "sponsors",
        "apps",
        "topics",
        "orgs",
        "users",
        "features",
        "marketplace",
        "settings",
        "collections",
        "about",
        "pricing",
        "readme",
    }
)
REPO_LINK_PATTERN = re.compile(
    r"https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/([A-Za-z0-9._-]{1,100})"
)


class CatalogError(RuntimeError):
    """Raised when the catalog cannot be produced from real data."""


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise CatalogError(
            "GITHUB_TOKEN is required; refusing to build an unauthenticated catalog"
        )
    return token


def _request(url: str, token: str) -> Any:
    """GET a JSON document, honouring secondary rate limits, never logging the token."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib_request.Request(url)
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", USER_AGENT)
        try:
            with urllib_request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as error:
            if error.code in {403, 429} and attempt < MAX_ATTEMPTS:
                retry_after = (
                    error.headers.get("Retry-After") if error.headers else None
                )
                delay = (
                    int(retry_after)
                    if isinstance(retry_after, str) and retry_after.isdigit()
                    else 30 * attempt
                )
                print(f"rate limited, sleeping {delay}s", file=sys.stderr)
                time.sleep(delay)
                continue
            if error.code == 404:
                return None
            raise CatalogError(f"github api {error.code} for {_redact(url)}") from error
        except (urllib_error.URLError, TimeoutError, json.JSONDecodeError) as error:
            if attempt < MAX_ATTEMPTS:
                time.sleep(5 * attempt)
                continue
            raise CatalogError(
                f"github api unreachable for {_redact(url)}: {error}"
            ) from error
    raise CatalogError(f"github api exhausted retries for {_redact(url)}")


def _redact(url: str) -> str:
    return url.split("?", 1)[0]


def _search_repositories(query: str, token: str, limit: int) -> list[dict[str, Any]]:
    encoded = urllib_parse.urlencode(
        {
            "q": query,
            "sort": "stars",
            "order": "desc",
            "per_page": min(limit, SEARCH_PAGE_SIZE),
        }
    )
    payload = _request(f"{API_ROOT}/search/repositories?{encoded}", token)
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    return (
        [item for item in items if isinstance(item, dict)][:limit]
        if isinstance(items, list)
        else []
    )


def _repository(slug: str, token: str) -> dict[str, Any] | None:
    payload = _request(f"{API_ROOT}/repos/{slug}", token)
    return payload if isinstance(payload, dict) else None


def _readme_text(slug: str, token: str) -> str:
    """Return the README body, falling back to raw download for files over 1 MB."""
    payload = _request(f"{API_ROOT}/repos/{slug}/readme", token)
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    if isinstance(content, str) and payload.get("encoding") == "base64":
        try:
            return base64.b64decode(content).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return ""
    download_url = payload.get("download_url")
    if not isinstance(download_url, str) or not download_url.startswith(
        RAW_CONTENT_ROOT
    ):
        return ""
    request = urllib_request.Request(download_url)
    request.add_header("User-Agent", USER_AGENT)
    try:
        with urllib_request.urlopen(request, timeout=30) as response:
            body = response.read(README_MAX_BYTES + 1)
    except (urllib_error.URLError, TimeoutError, OSError) as error:
        print(f"readme download failed for {slug}: {error}", file=sys.stderr)
        return ""
    if len(body) > README_MAX_BYTES:
        print(f"readme too large for {slug}", file=sys.stderr)
        return ""
    return body.decode("utf-8", errors="replace")


def _license_id(repository: dict[str, Any]) -> str:
    license_block = repository.get("license")
    if isinstance(license_block, dict):
        spdx = license_block.get("spdx_id")
        if isinstance(spdx, str) and spdx not in {"NOASSERTION", ""}:
            return spdx
    return ""


def _record(
    repository: dict[str, Any], kind: str, source: str, capability: str, checked_at: str
) -> dict[str, Any] | None:
    full_name = repository.get("full_name")
    if not isinstance(full_name, str):
        return None
    try:
        slug = normalize_repo_slug(full_name)
    except ValueError:
        return None
    if kind not in CANDIDATE_KINDS:
        raise CatalogError(f"unknown candidate kind in registry: {kind}")
    pushed_at = repository.get("pushed_at")
    if not isinstance(pushed_at, str) or not pushed_at:
        return None
    topics = repository.get("topics")
    record = {
        "slug": slug,
        "kind": kind,
        "capability": capability,
        "display_name": full_name,
        "url": f"https://github.com/{slug}",
        "description": (repository.get("description") or "")[:300],
        "stars": int(repository.get("stargazers_count") or 0),
        "forks": int(repository.get("forks_count") or 0),
        "open_issues": int(repository.get("open_issues_count") or 0),
        "created_at": repository.get("created_at") or "",
        "pushed_at": pushed_at,
        "license": _license_id(repository),
        "archived": bool(repository.get("archived")),
        "topics": sorted(topic for topic in topics if isinstance(topic, str))
        if isinstance(topics, list)
        else [],
        "source": source,
        "checked_at": checked_at,
    }
    record["emerging_gem"] = detect_emerging_gem(record, checked_at)
    return record


def _load_registry(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as error:  # pragma: no cover - CI installs pyyaml
        raise CatalogError("pyyaml is required to read the catalog registry") from error
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CatalogError(f"{path.name} must be a mapping")
    return data


def _known_capabilities(registry_path: Path) -> set[str]:
    """Read the capability ids the client actually classifies requests into.

    The catalog registry and registry/capabilities.yaml are two files that have to
    agree: a record tagged with a capability the client never produces is
    unreachable through `search-catalog --capability`, and nothing in either file
    would report the drift.
    """
    try:
        import yaml
    except ImportError as error:  # pragma: no cover - CI installs pyyaml
        raise CatalogError(
            "pyyaml is required to read the capability registry"
        ) from error
    path = registry_path.parent / "capabilities.yaml"
    if not path.exists():
        raise CatalogError(f"capabilities.yaml not found next to {registry_path.name}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise CatalogError("capabilities.yaml must be a list of entries")
    ids = {
        entry["id"]
        for entry in data
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }
    if not ids:
        raise CatalogError("capabilities.yaml declares no capability ids")
    return ids


def _require_capabilities(registry: dict[str, Any], known: set[str]) -> None:
    """Fail the build on a source whose capability the client cannot classify."""
    for section in ("trends", "awesome_lists"):
        for entry in registry.get(section) or []:
            if not isinstance(entry, dict):
                continue
            capability = entry.get("capability")
            if not isinstance(capability, str) or not capability:
                raise CatalogError(
                    f"{section} entry {entry.get('id', '?')} declares no capability"
                )
            if capability not in known:
                raise CatalogError(
                    f"{section} entry {entry.get('id', '?')} uses unknown capability "
                    f"{capability!r}; add it to registry/capabilities.yaml"
                )


def _window(field: str, days: int, now: datetime) -> str:
    if field not in {"created", "pushed"}:
        raise CatalogError(f"trend field must be created or pushed, got {field}")
    since = (now - timedelta(days=int(days))).date().isoformat()
    return f"{field}:>{since}"


def _collect_trends(
    registry: dict[str, Any],
    token: str,
    limits: dict[str, Any],
    now: datetime,
    checked_at: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    per_query = int(limits.get("per_query", SEARCH_PAGE_SIZE))
    for entry in registry.get("trends") or []:
        if not isinstance(entry, dict):
            continue
        query = f"{entry['query']} {_window(entry.get('field', 'pushed'), entry.get('window_days', 90), now)}"
        for repository in _search_repositories(query, token, per_query):
            record = _record(
                repository,
                entry["kind"],
                f"github-search:{entry['id']}",
                entry.get("capability", ""),
                checked_at,
            )
            if record is not None:
                records.append(record)
    return records


def _pinned_repos(entry: dict[str, Any]) -> list[str]:
    """Slugs a curated entry names outright, independent of its README.

    Some curated lists index documents rather than repositories:
    ashishps1/awesome-system-design-resources links to markdown inside itself, so
    link extraction returns nothing and the source contributes only its own record.
    `repos:` is how such an entry still delivers the ready-made references it was
    added for.
    """
    pinned = entry.get("repos")
    if pinned is None:
        return []
    if not isinstance(pinned, list):
        raise CatalogError(
            f"awesome_lists entry {entry.get('id', '?')} repos must be a list"
        )
    slugs: list[str] = []
    for value in pinned:
        if not isinstance(value, str) or not value.strip():
            raise CatalogError(
                f"awesome_lists entry {entry.get('id', '?')} declares an empty repo"
            )
        slug = normalize_repo_slug(value)
        if slug not in slugs:
            slugs.append(slug)
    return slugs


def _collect_awesome(
    registry: dict[str, Any], token: str, limits: dict[str, Any], checked_at: str
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    budget = int(limits.get("awesome_repos_per_list", 100))
    for entry in registry.get("awesome_lists") or []:
        if not isinstance(entry, dict):
            continue
        list_slug = normalize_repo_slug(entry["repo"])
        list_repository = _repository(list_slug, token)
        if list_repository is None:
            print(f"curated list unavailable: {list_slug}", file=sys.stderr)
            continue
        list_record = _record(
            list_repository,
            "reference",
            f"awesome-list:{entry['id']}",
            entry.get("capability", ""),
            checked_at,
        )
        if list_record is not None:
            records.append(list_record)
        readme = _readme_text(list_slug, token)
        if not readme:
            print(f"no readme for {list_slug}", file=sys.stderr)
            for slug in _pinned_repos(entry):
                repository = _repository(slug, token)
                if repository is None or repository.get("archived"):
                    continue
                record = _record(
                    repository,
                    entry["kind"],
                    f"awesome-list:{entry['id']}",
                    entry.get("capability", ""),
                    checked_at,
                )
                if record is not None:
                    records.append(record)
            continue
        seen: list[str] = []
        for owner, repo in REPO_LINK_PATTERN.findall(readme):
            if owner.lower() in RESERVED_OWNERS:
                continue
            slug = f"{owner}/{repo}".lower().removesuffix(".git")
            if slug == list_slug or slug in seen:
                continue
            seen.append(slug)
            if len(seen) >= budget:
                break
        print(f"{list_slug}: {len(seen)} linked repositories", file=sys.stderr)
        for slug in _pinned_repos(entry):
            if slug not in seen and slug != list_slug:
                seen.append(slug)
        for slug in seen:
            repository = _repository(slug, token)
            if repository is None or repository.get("archived"):
                continue
            record = _record(
                repository,
                entry["kind"],
                f"awesome-list:{entry['id']}",
                entry.get("capability", ""),
                checked_at,
            )
            if record is not None:
                records.append(record)
    return records


def load_previous(path: Path) -> dict[str, dict[str, Any]]:
    """Index yesterday's catalog by slug so today's run can measure movement."""
    if not path.exists():
        return {}
    try:
        records = _parse_catalog_payload(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        print(f"previous catalog unusable: {error}", file=sys.stderr)
        return {}
    return {
        record["slug"]: record
        for record in records
        if isinstance(record.get("slug"), str)
    }


def apply_momentum(
    records: list[dict[str, Any]], previous: dict[str, dict[str, Any]]
) -> None:
    """Attach measured star movement; absent history stays null, never zero."""
    for record in records:
        earlier = previous.get(record["slug"])
        if earlier is None:
            record["stars_delta"] = None
            record["stars_per_day"] = None
            record["observed_days"] = None
            continue
        try:
            span_seconds = _seconds_between(
                earlier.get("checked_at"), record["checked_at"]
            )
        except ValueError:
            record["stars_delta"] = None
            record["stars_per_day"] = None
            record["observed_days"] = None
            continue
        delta = record["stars"] - int(earlier.get("stars") or 0)
        days = span_seconds / 86400.0
        record["stars_delta"] = delta
        record["observed_days"] = round(days, 3)
        record["stars_per_day"] = round(delta / days, 3) if days >= 0.5 else None


def _seconds_between(earlier: Any, later: str) -> float:
    if not isinstance(earlier, str) or not earlier:
        raise ValueError("missing earlier timestamp")
    start = datetime.strptime(earlier, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    end = datetime.strptime(later, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    span = (end - start).total_seconds()
    if span <= 0:
        raise ValueError("previous catalog is not older than this run")
    return span


def _deduplicate(
    records: Iterable[dict[str, Any]], total_cap: int | None = None
) -> list[dict[str, Any]]:
    """Merge records that describe one repository; the cap belongs to select_index."""
    best: dict[str, dict[str, Any]] = {}
    for record in records:
        slug = record["slug"]
        current = best.get(slug)
        if current is None:
            best[slug] = record
        elif record["stars"] > current["stars"]:
            record["source"] = f"{current['source']},{record['source']}"
            best[slug] = record
        else:
            current["source"] = f"{current['source']},{record['source']}"
    ordered = sorted(best.values(), key=lambda item: (-item["stars"], item["slug"]))
    return ordered if total_cap is None else ordered[:total_cap]


def _is_curated(record: dict[str, Any]) -> bool:
    return any(
        source.startswith("awesome-list:") for source in record["source"].split(",")
    )


def select_index(
    records: list[dict[str, Any]], limits: dict[str, Any]
) -> list[dict[str, Any]]:
    """Choose what the catalog publishes: curated lists, movement, then fresh intake.

    A live GitHub query already answers "what exists". Only a previous snapshot can
    answer "what is moving", so the published set is small and organised around that:

    - curated: entries from the awesome lists, the ready-made references a live
      search cannot assemble, ranked by stars.
    - gems: young, licensed, actively pushed repositories under 500 stars. They
      need their own tier because every other tier ranks by stars, and a gem is by
      definition something the star ranking has not found yet. Without the tier
      `search-catalog --gem` returns nothing, however many gems were observed.
    - movement: entries with measured `stars_per_day`, ranked by it. This is the
      tier the catalog exists for.
    - intake: repositories with no measured history first, by stars, so they get a
      baseline and can win a movement slot tomorrow. Entries that already have
      history but lost the movement race rank last: they have had their chance and
      their rate is measurably weak. Ranking intake by stars alone would hand every
      slot back to yesterday's largest names and the index would ossify.
    """
    total = int(limits.get("total_records", 400))
    curated_cap = int(limits.get("curated_records", 120))
    gem_cap = int(limits.get("gem_records", 60))
    movement_cap = int(limits.get("movement_records", 180))
    if total < 1:
        raise CatalogError("total_records must be a positive integer")

    chosen: dict[str, dict[str, Any]] = {}

    curated = sorted(
        (record for record in records if _is_curated(record)),
        key=lambda record: (-record["stars"], record["slug"]),
    )
    for record in curated[:curated_cap]:
        chosen[record["slug"]] = record

    gems = sorted(
        (
            record
            for record in records
            if record["slug"] not in chosen and record.get("emerging_gem")
        ),
        key=lambda record: (
            -float(record.get("stars_per_day") or 0.0),
            -record["stars"],
            record["slug"],
        ),
    )
    for record in gems[:gem_cap]:
        if len(chosen) >= total:
            break
        chosen[record["slug"]] = record

    moving = sorted(
        (
            record
            for record in records
            if record["slug"] not in chosen and record.get("stars_per_day") is not None
        ),
        key=lambda record: (-float(record["stars_per_day"]), record["slug"]),
    )
    for record in moving[:movement_cap]:
        if len(chosen) >= total:
            break
        chosen[record["slug"]] = record

    intake = sorted(
        (record for record in records if record["slug"] not in chosen),
        key=lambda record: (
            record.get("stars_per_day") is not None,
            -record["stars"],
            record["slug"],
        ),
    )
    for record in intake:
        if len(chosen) >= total:
            break
        chosen[record["slug"]] = record

    return sorted(
        chosen.values(), key=lambda record: (-record["stars"], record["slug"])
    )


def build(
    registry_path: Path,
    out_path: Path,
    meta_path: Path,
    previous_path: Path | None = None,
) -> dict[str, Any]:
    token = _token()
    registry = _load_registry(registry_path)
    _require_capabilities(registry, _known_capabilities(registry_path))
    limits = registry.get("limits") if isinstance(registry.get("limits"), dict) else {}
    now = datetime.now(timezone.utc)
    checked_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    records = _collect_trends(registry, token, limits, now, checked_at)
    records.extend(_collect_awesome(registry, token, limits, checked_at))
    merged = _deduplicate(records)
    if not merged:
        raise CatalogError("catalog is empty; refusing to publish")
    previous = load_previous(previous_path) if previous_path is not None else {}
    apply_momentum(merged, previous)
    catalog = select_index(merged, limits)
    if not catalog:
        raise CatalogError("catalog is empty; refusing to publish")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in catalog
        ),
        encoding="utf-8",
    )
    meta = {
        "schema_version": 1,
        "generated_at": checked_at,
        "records": len(catalog),
        "kinds": {
            kind: sum(1 for record in catalog if record["kind"] == kind)
            for kind in sorted({record["kind"] for record in catalog})
        },
        "emerging_gems": sum(1 for record in catalog if record["emerging_gem"]),
        "with_momentum": sum(
            1 for record in catalog if record.get("stars_delta") is not None
        ),
        "previous_records": len(previous),
        "observed_records": len(merged),
        "from_curated_lists": sum(1 for record in catalog if _is_curated(record)),
        "observed_gems": sum(1 for record in merged if record.get("emerging_gem")),
        "sources": sorted(
            {source for record in catalog for source in record["source"].split(",")}
        ),
    }
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return meta


def validate(path: Path) -> int:
    """Prove the consumer can read what the producer wrote."""
    records = _parse_catalog_payload(path.read_text(encoding="utf-8"))
    if not records:
        raise CatalogError("validated catalog is empty")
    for number, record in enumerate(records, start=1):
        missing = {
            "slug",
            "kind",
            "url",
            "stars",
            "pushed_at",
            "checked_at",
            "source",
        }.difference(record)
        if missing:
            raise CatalogError(
                f"record {number} missing fields: {', '.join(sorted(missing))}"
            )
        if record["kind"] not in CANDIDATE_KINDS:
            raise CatalogError(f"record {number} has unknown kind {record['kind']}")
    print(f"catalog valid: {len(records)} records")
    return len(records)


def _force_utf8_streams() -> None:
    """Emit UTF-8 whatever the console codepage is.

    On a non-UTF-8 Windows console `print` encodes with the active codepage, so a
    repository title carrying an em dash or an emoji either raises or lands in the
    output as mojibake. `--json` exists to be parsed by another program; output that
    is not valid UTF-8 is not parseable, so the streams are pinned here rather than
    at every call site.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()
    parser = argparse.ArgumentParser(
        description="build wheel's offline candidate catalog"
    )
    parser.add_argument("--registry", default="registry/catalog_sources.yaml")
    parser.add_argument("--out", default="dist/catalog.jsonl")
    parser.add_argument("--meta", default="dist/catalog.meta.json")
    parser.add_argument(
        "--previous", help="prior catalog.jsonl used to measure star movement"
    )
    parser.add_argument("--validate", metavar="PATH")
    args = parser.parse_args(argv)

    try:
        if args.validate:
            validate(Path(args.validate))
            return 0
        meta = build(
            Path(args.registry),
            Path(args.out),
            Path(args.meta),
            Path(args.previous) if args.previous else None,
        )
    except CatalogError as error:
        print(f"catalog build failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(meta, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
