"""Objective engineering metrics derived from GitHub API responses.

Measures bus factor, commit cadence, and release cadence directly from GitHub
facts rather than subjective heuristics or opaque composite scores.
"""

from __future__ import annotations

from pathlib import Path
import importlib.util
import importlib
import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from typing import Any


def _wheel_core() -> Any:
    """Return wheel.py's module object however this file was reached.

    `python scripts/wheel.py <command>` runs wheel.py as `__main__` with scripts/ on
    sys.path and the repository root absent, so `import scripts.wheel` fails and a
    path import would execute a second copy of the CLI. Preferring an already loaded
    module keeps exactly one wheel module alive in every invocation.
    """
    for name in ("scripts.wheel", "wheel", "__main__"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "_gh_api"):
            return module
    try:
        return importlib.import_module("scripts.wheel")
    except ImportError:
        pass
    path = Path(__file__).resolve().parent / "wheel.py"
    spec = importlib.util.spec_from_file_location("_wheel_core", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load wheel.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DAY_SECONDS = getattr(_wheel_core(), "DAY_SECONDS")
_gh_api = getattr(_wheel_core(), "_gh_api")
_timestamp = getattr(_wheel_core(), "_timestamp")


def bus_factor(contributors: list[dict]) -> dict:
    """Compute bus factor and HHI from contributor contribution counts.

    Bus factor is the smallest number of contributors whose combined contributions
    reach 50% of the total contributions.
    HHI is the Herfindahl-Hirschman index of contribution shares (sum of squared
    shares, 0..1).
    Empty list returns zeros and None hhi.
    """
    if not contributors:
        return {
            "contributors": 0,
            "bus_factor": 0,
            "hhi": None,
        }

    counts = [int(c.get("contributions", 0)) for c in contributors]
    total = sum(counts)
    if total <= 0:
        return {
            "contributors": len(contributors),
            "bus_factor": 0,
            "hhi": None,
        }

    # Sort descending to find minimal set reaching 50%
    sorted_counts = sorted(counts, reverse=True)
    target = total * 0.5
    accumulated = 0
    k = 0
    for count in sorted_counts:
        accumulated += count
        k += 1
        if accumulated >= target:
            break

    shares = [c / total for c in counts]
    hhi = round(sum(s * s for s in shares), 4)

    return {
        "contributors": len(contributors),
        "bus_factor": k,
        "hhi": hhi,
    }


def commit_cadence(participation: dict | None) -> dict:
    """Given stats/participation ({'all': [52 weekly counts]}), return cadence metrics.

    Missing or malformed payload yields all-None fields.
    """
    fields = {
        "weeks_observed": None,
        "weeks_with_commits": None,
        "commits_52w": None,
        "median_commits_per_week": None,
    }
    if not isinstance(participation, dict):
        return fields

    counts = participation.get("all")
    if not isinstance(counts, list) or not counts:
        return fields

    valid_counts: list[int] = []
    for c in counts:
        if not isinstance(c, (int, float)):
            return fields
        valid_counts.append(int(c))

    weeks_observed = len(valid_counts)
    weeks_with_commits = sum(1 for c in valid_counts if c > 0)
    commits_52w = sum(valid_counts)
    median_val = round(float(statistics.median(valid_counts)), 2)

    return {
        "weeks_observed": weeks_observed,
        "weeks_with_commits": weeks_with_commits,
        "commits_52w": commits_52w,
        "median_commits_per_week": median_val,
    }


def _parse_iso(ts: str) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def release_cadence(releases: list[dict] | None, now: str | None = None) -> dict:
    """Given releases entries with 'published_at', return release cadence facts.

    Fewer than two releases means median_interval_days is None, not zero.
    """
    res = {
        "releases": 0,
        "median_interval_days": None,
        "latest_age_days": None,
    }
    if not isinstance(releases, list):
        return res

    now_dt = _parse_iso(now) if now else datetime.now(timezone.utc)
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)

    dates: list[datetime] = []
    for r in releases:
        if not isinstance(r, dict):
            continue
        pub = r.get("published_at")
        if not pub:
            continue
        dt = _parse_iso(pub)
        if dt is not None:
            dates.append(dt)

    if not dates:
        return res

    # Sort descending (latest first)
    dates.sort(reverse=True)
    res["releases"] = len(dates)

    latest = dates[0]
    age_sec = (now_dt - latest).total_seconds()
    res["latest_age_days"] = max(0.0, round(age_sec / DAY_SECONDS, 2))

    if len(dates) >= 2:
        intervals = [
            (dates[i] - dates[i + 1]).total_seconds() / DAY_SECONDS
            for i in range(len(dates) - 1)
        ]
        res["median_interval_days"] = max(
            0.0, round(float(statistics.median(intervals)), 2)
        )

    return res


def collect_hard_metrics(slug: str, now: str | None = None) -> dict:
    """Fetch repo, contributors, participation, and releases through _gh_api."""
    checked_at = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    unavailable: list[str] = []

    # 1. repos/{slug}
    repo_data = _gh_api(f"repos/{slug}")
    if repo_data is None or not isinstance(repo_data, dict):
        unavailable.append("repo")
        repo_data = {}

    # 2. repos/{slug}/contributors?per_page=100
    contrib_data = _gh_api(f"repos/{slug}/contributors?per_page=100")
    if contrib_data is None or not isinstance(contrib_data, list):
        unavailable.append("contributors")
        bus_metrics = bus_factor([])
    else:
        bus_metrics = bus_factor(contrib_data)

    # 3. repos/{slug}/stats/participation
    # GitHub answers 202 with an empty body while computing statistics:
    # treat empty/None as unavailable rather than as zero.
    part_data = _gh_api(f"repos/{slug}/stats/participation")
    commit_metrics = commit_cadence(part_data)
    if commit_metrics.get("weeks_observed") is None:
        # None here means the payload was absent, still computing, or shaped in a way
        # this code cannot read. All three are "not measured", and a metric that is
        # not measured has to say so instead of riding along in an "ok" status.
        unavailable.append("participation")

    # 4. repos/{slug}/releases?per_page=30
    rel_data = _gh_api(f"repos/{slug}/releases?per_page=30")
    if rel_data is None or not isinstance(rel_data, list):
        unavailable.append("releases")
        rel_metrics = release_cadence([], now=checked_at)
    else:
        rel_metrics = release_cadence(rel_data, now=checked_at)

    status = (
        "ok"
        if not unavailable
        else ("partial" if len(unavailable) < 4 else "unavailable")
    )

    license_info = None
    if isinstance(repo_data.get("license"), dict):
        license_info = repo_data["license"].get("spdx_id") or repo_data["license"].get(
            "name"
        )

    archived = repo_data.get("archived") if "archived" in repo_data else None

    pushed_age_days = None
    pushed_at = repo_data.get("pushed_at")
    if pushed_at:
        pushed_dt = _parse_iso(pushed_at)
        now_dt = _parse_iso(checked_at) or datetime.now(timezone.utc)
        if pushed_dt:
            age_sec = (now_dt - pushed_dt).total_seconds()
            pushed_age_days = max(0.0, round(age_sec / DAY_SECONDS, 2))

    return {
        "slug": slug,
        "checked_at": checked_at,
        "status": status,
        "license": license_info,
        "archived": archived,
        "pushed_age_days": pushed_age_days,
        "bus_factor": bus_metrics,
        "commit_cadence": commit_metrics,
        "release_cadence": rel_metrics,
        "unavailable": unavailable,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for hard metrics inspection."""
    parser = argparse.ArgumentParser(
        description="Collect hard engineering metrics from GitHub."
    )
    parser.add_argument(
        "--slug", required=True, help="GitHub repository slug (owner/repo)"
    )
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args(argv)

    metrics = collect_hard_metrics(args.slug)

    if args.json:
        sys.stdout.write(json.dumps(metrics, indent=2) + "\n")
    else:
        sys.stdout.write(f"Hard Metrics: {metrics['slug']}\n")
        sys.stdout.write(f"Status: {metrics['status']}\n")
        sys.stdout.write(f"License: {metrics['license']}\n")
        sys.stdout.write(f"Archived: {metrics['archived']}\n")
        sys.stdout.write(f"Pushed age (days): {metrics['pushed_age_days']}\n")
        sys.stdout.write(f"Bus factor: {metrics['bus_factor']}\n")
        sys.stdout.write(f"Commit cadence: {metrics['commit_cadence']}\n")
        sys.stdout.write(f"Release cadence: {metrics['release_cadence']}\n")
        if metrics["unavailable"]:
            sys.stdout.write(
                f"Unavailable calls: {', '.join(metrics['unavailable'])}\n"
            )

    return 0 if metrics["status"] in ("ok", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
