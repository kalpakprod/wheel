from __future__ import annotations

import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.build_catalog import (
    REPO_LINK_PATTERN,
    RESERVED_OWNERS,
    CatalogError,
    _deduplicate,
    _is_curated,
    _known_capabilities,
    _load_registry,
    _pinned_repos,
    _require_capabilities,
    _license_id,
    _record,
    apply_momentum,
    build,
    load_previous,
    select_index,
    validate,
)

NOW = "2026-09-01T00:00:00Z"


def repository(full_name: str, stars: int = 100, **overrides: object) -> dict:
    payload = {
        "full_name": full_name,
        "stargazers_count": stars,
        "forks_count": 3,
        "open_issues_count": 1,
        "created_at": "2026-06-01T00:00:00Z",
        "pushed_at": "2026-08-20T00:00:00Z",
        "archived": False,
        "license": {"spdx_id": "MIT"},
        "topics": ["mcp", "agents"],
        "description": "A server",
    }
    payload.update(overrides)
    return payload


class RecordTests(unittest.TestCase):
    def test_record_uses_only_api_facts(self) -> None:
        record = _record(
            repository("Owner/Repo"),
            "mcp-server",
            "github-search:x",
            "mcp-servers",
            NOW,
        )
        assert record is not None
        self.assertEqual(record["slug"], "owner/repo")
        self.assertEqual(record["kind"], "mcp-server")
        self.assertEqual(record["url"], "https://github.com/owner/repo")
        self.assertEqual(record["license"], "MIT")
        self.assertEqual(record["topics"], ["agents", "mcp"])
        self.assertEqual(record["checked_at"], NOW)
        self.assertTrue(record["emerging_gem"])

    def test_record_rejects_unusable_payloads(self) -> None:
        self.assertIsNone(_record({"full_name": 42}, "repository", "s", "", NOW))
        self.assertIsNone(
            _record(
                repository("Owner/Repo", pushed_at=None), "repository", "s", "", NOW
            )
        )
        self.assertIsNone(_record(repository("not-a-slug"), "repository", "s", "", NOW))
        with self.assertRaises(CatalogError):
            _record(repository("Owner/Repo"), "not-a-kind", "s", "", NOW)

    def test_license_noassertion_is_not_a_license(self) -> None:
        self.assertEqual(_license_id({"license": {"spdx_id": "NOASSERTION"}}), "")
        self.assertEqual(_license_id({"license": None}), "")

    def test_high_star_repository_is_not_a_gem(self) -> None:
        record = _record(
            repository("Owner/Big", stars=9000), "repository", "s", "", NOW
        )
        assert record is not None
        self.assertFalse(record["emerging_gem"])


class DeduplicateTests(unittest.TestCase):
    def test_duplicate_keeps_richer_record_and_merges_sources(self) -> None:
        low = _record(
            repository("Owner/Repo", stars=10), "repository", "awesome-list:a", "", NOW
        )
        high = _record(
            repository("Owner/Repo", stars=900),
            "repository",
            "github-search:b",
            "",
            NOW,
        )
        assert low is not None and high is not None
        merged = _deduplicate([low, high], 10)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["stars"], 900)
        self.assertIn("awesome-list:a", merged[0]["source"])
        self.assertIn("github-search:b", merged[0]["source"])

    def test_output_is_deterministic_and_capped(self) -> None:
        records = [
            _record(
                repository(f"owner/repo{index}", stars=index),
                "repository",
                "s",
                "",
                NOW,
            )
            for index in range(5)
        ]
        first = _deduplicate([record for record in records if record], 3)
        second = _deduplicate([record for record in reversed(records) if record], 3)
        self.assertEqual(
            [item["slug"] for item in first], [item["slug"] for item in second]
        )
        self.assertEqual(len(first), 3)
        self.assertEqual(first[0]["stars"], 4)


class MomentumTests(unittest.TestCase):
    def test_momentum_is_measured_only_against_real_history(self) -> None:
        today = _record(repository("owner/repo", stars=150), "repository", "s", "", NOW)
        assert today is not None
        yesterday = {
            "slug": "owner/repo",
            "stars": 100,
            "checked_at": "2026-08-30T00:00:00Z",
        }
        apply_momentum([today], {"owner/repo": yesterday})
        self.assertEqual(today["stars_delta"], 50)
        self.assertEqual(today["observed_days"], 2.0)
        self.assertEqual(today["stars_per_day"], 25.0)

    def test_absent_history_stays_null_not_zero(self) -> None:
        record = _record(repository("owner/new"), "repository", "s", "", NOW)
        assert record is not None
        apply_momentum([record], {})
        self.assertIsNone(record["stars_delta"])
        self.assertIsNone(record["stars_per_day"])
        self.assertIsNone(record["observed_days"])

    def test_broken_or_future_history_is_ignored(self) -> None:
        record = _record(
            repository("owner/repo", stars=150), "repository", "s", "", NOW
        )
        assert record is not None
        apply_momentum(
            [record],
            {
                "owner/repo": {
                    "slug": "owner/repo",
                    "stars": 10,
                    "checked_at": "not-a-date",
                }
            },
        )
        self.assertIsNone(record["stars_delta"])
        apply_momentum(
            [record],
            {
                "owner/repo": {
                    "slug": "owner/repo",
                    "stars": 10,
                    "checked_at": "2026-09-02T00:00:00Z",
                }
            },
        )
        self.assertIsNone(record["stars_delta"])

    def test_previous_catalog_is_indexed_and_corruption_is_survivable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "previous.jsonl"
            self.assertEqual(load_previous(path), {})
            path.write_text('{"slug": "a/b", "stars": 5}\n', encoding="utf-8")
            self.assertEqual(set(load_previous(path)), {"a/b"})
            path.write_text('{"slug": "a/b"}\n{broken', encoding="utf-8")
            self.assertEqual(load_previous(path), {})


class SelectIndexTests(unittest.TestCase):
    """The catalog publishes movement, not a mirror; intake keeps it from ossifying."""

    def _record(
        self,
        slug: str,
        stars: int,
        source: str,
        stars_per_day: float | None = None,
        emerging_gem: bool = False,
    ) -> dict[str, object]:
        return {
            "slug": slug,
            "stars": stars,
            "source": source,
            "stars_per_day": stars_per_day,
            "stars_delta": None if stars_per_day is None else int(stars_per_day),
            "emerging_gem": emerging_gem,
        }

    def test_curated_entries_survive_regardless_of_movement(self) -> None:
        records = [
            self._record("owner/curated", 10, "awesome-list:refs"),
            self._record("owner/fast", 5000, "github-search:x", stars_per_day=90.0),
        ]
        limits = {"total_records": 2, "curated_records": 1, "movement_records": 1}
        chosen = {record["slug"] for record in select_index(records, limits)}
        self.assertEqual(chosen, {"owner/curated", "owner/fast"})

    def test_meta_counts_curated_origin_not_the_curated_tier(self) -> None:
        records = [
            self._record("owner/a", 900, "awesome-list:refs"),
            self._record("owner/b", 800, "github-search:x,awesome-list:refs"),
            self._record("owner/c", 700, "github-search:x"),
        ]
        limits = {
            "total_records": 3,
            "curated_records": 1,
            "gem_records": 0,
            "movement_records": 0,
        }
        chosen = select_index(records, limits)
        self.assertEqual(sum(1 for record in chosen if _is_curated(record)), 2)

    def test_a_gem_gets_a_slot_the_star_ranking_would_never_give_it(self) -> None:
        records = [
            self._record("owner/giant", 90000, "github-search:x"),
            self._record("owner/gem", 40, "github-search:y", emerging_gem=True),
        ]
        limits = {
            "total_records": 1,
            "curated_records": 0,
            "gem_records": 1,
            "movement_records": 0,
        }
        chosen = [record["slug"] for record in select_index(records, limits)]
        self.assertEqual(chosen, ["owner/gem"])

    def test_movement_outranks_absolute_stars(self) -> None:
        records = [
            self._record("owner/huge", 90000, "github-search:x"),
            self._record("owner/moving", 300, "github-search:y", stars_per_day=40.0),
        ]
        limits = {"total_records": 1, "curated_records": 0, "movement_records": 1}
        chosen = [record["slug"] for record in select_index(records, limits)]
        self.assertEqual(chosen, ["owner/moving"])

    def test_intake_slot_lets_a_repository_without_history_enter(self) -> None:
        records = [
            self._record("owner/moving", 100, "github-search:x", stars_per_day=9.0),
            self._record("owner/newcomer", 4000, "github-search:y"),
        ]
        limits = {"total_records": 2, "curated_records": 0, "movement_records": 1}
        chosen = {record["slug"] for record in select_index(records, limits)}
        self.assertEqual(chosen, {"owner/moving", "owner/newcomer"})

    def test_intake_prefers_a_newcomer_over_a_measurably_slow_giant(self) -> None:
        records = [
            self._record("owner/rocket", 900, "github-search:x", stars_per_day=400.0),
            self._record("owner/sleepy", 9001, "github-search:x", stars_per_day=0.5),
            self._record("owner/newcomer", 7000, "github-search:x"),
        ]
        limits = {"total_records": 2, "curated_records": 0, "movement_records": 1}
        chosen = {record["slug"] for record in select_index(records, limits)}
        self.assertEqual(chosen, {"owner/rocket", "owner/newcomer"})

    def test_total_cap_is_never_exceeded_and_output_is_sorted(self) -> None:
        records = [
            self._record("owner/a", 900, "awesome-list:refs"),
            self._record("owner/b", 800, "github-search:x", stars_per_day=50.0),
            self._record("owner/c", 700, "github-search:y"),
        ]
        limits = {"total_records": 2, "curated_records": 5, "movement_records": 5}
        chosen = [record["slug"] for record in select_index(records, limits)]
        self.assertEqual(chosen, ["owner/a", "owner/b"])

    def test_curated_origin_is_read_from_the_source_field(self) -> None:
        self.assertTrue(_is_curated({"source": "github-search:x,awesome-list:refs"}))
        self.assertFalse(_is_curated({"source": "github-search:x"}))

    def test_a_positive_total_is_required(self) -> None:
        with self.assertRaisesRegex(CatalogError, "total_records"):
            select_index([], {"total_records": 0})


class LinkExtractionTests(unittest.TestCase):
    def test_pattern_finds_repositories_and_skips_reserved_owners(self) -> None:
        readme = (
            "See https://github.com/owner/repo and https://github.com/sponsors/someone "
            "and https://github.com/topics/mcp plus https://github.com/other/tool.git"
        )
        found = [(owner, repo) for owner, repo in REPO_LINK_PATTERN.findall(readme)]
        self.assertIn(("owner", "repo"), found)
        self.assertIn(
            ("other", "tool"),
            [(owner, repo.removesuffix(".git")) for owner, repo in found],
        )
        reserved = [owner for owner, _ in found if owner in RESERVED_OWNERS]
        self.assertEqual(reserved, ["sponsors", "topics"])


class ValidateTests(unittest.TestCase):
    def test_validate_rejects_empty_and_malformed_catalogs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.jsonl"
            path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(CatalogError, "empty"):
                validate(path)
            path.write_text('{"slug": "a/b", "kind": "repository"}\n', encoding="utf-8")
            with self.assertRaisesRegex(CatalogError, "missing fields"):
                validate(path)
            path.write_text(
                json.dumps(
                    {
                        "slug": "a/b",
                        "kind": "not-a-kind",
                        "url": "https://github.com/a/b",
                        "stars": 1,
                        "pushed_at": NOW,
                        "checked_at": NOW,
                        "source": "s",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CatalogError, "unknown kind"):
                validate(path)

    def test_validate_accepts_a_produced_catalog(self) -> None:
        record = _record(repository("owner/repo"), "repository", "s", "", NOW)
        assert record is not None
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.jsonl"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            self.assertEqual(validate(path), 1)


class RegistryContractTests(unittest.TestCase):
    """The catalog registry and the client's classifier are one contract."""

    def test_a_source_without_a_capability_fails_the_build(self) -> None:
        registry = {"trends": [{"id": "t", "kind": "repository"}]}
        with self.assertRaisesRegex(CatalogError, "declares no capability"):
            _require_capabilities(registry, {"mcp-servers"})

    def test_a_capability_the_client_cannot_classify_fails_the_build(self) -> None:
        registry = {
            "awesome_lists": [{"id": "a", "kind": "reference", "capability": "ghost"}]
        }
        with self.assertRaisesRegex(CatalogError, "unknown capability"):
            _require_capabilities(registry, {"mcp-servers"})

    def test_the_shipped_registries_agree(self) -> None:
        registry_path = (
            Path(__file__).resolve().parent.parent / "registry" / "catalog_sources.yaml"
        )
        registry = _load_registry(registry_path)
        _require_capabilities(registry, _known_capabilities(registry_path))

    def test_missing_capabilities_file_is_an_error_not_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(CatalogError, "capabilities.yaml"):
                _known_capabilities(Path(temporary) / "catalog_sources.yaml")

    def test_pinned_repos_are_normalised_and_deduplicated(self) -> None:
        entry = {
            "id": "a",
            "repos": [
                "https://github.com/Owner/Repo",
                "owner/repo",
                "other/thing",
            ],
        }
        self.assertEqual(_pinned_repos(entry), ["owner/repo", "other/thing"])

    def test_an_entry_without_pinned_repos_yields_nothing(self) -> None:
        self.assertEqual(_pinned_repos({"id": "a"}), [])

    def test_an_empty_pinned_repo_fails_the_build(self) -> None:
        with self.assertRaisesRegex(CatalogError, "empty repo"):
            _pinned_repos({"id": "a", "repos": ["  "]})


class BuildTests(unittest.TestCase):
    def test_build_refuses_to_publish_an_empty_catalog(self) -> None:
        registry = {"limits": {"per_query": 1}, "trends": [], "awesome_lists": []}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                unittest.mock.patch("scripts.build_catalog._token", return_value="x"),
                unittest.mock.patch(
                    "scripts.build_catalog._load_registry", return_value=registry
                ),
                unittest.mock.patch(
                    "scripts.build_catalog._known_capabilities", return_value=set()
                ),
                self.assertRaisesRegex(CatalogError, "empty"),
            ):
                build(
                    root / "registry.yaml",
                    root / "catalog.jsonl",
                    root / "meta.json",
                )
            self.assertFalse((root / "catalog.jsonl").exists())

    def test_build_writes_sorted_jsonl_and_meta(self) -> None:
        registry = {
            "limits": {
                "per_query": 2,
                "total_records": 10,
                "curated_records": 0,
                "movement_records": 0,
            },
            "trends": [
                {
                    "id": "t",
                    "query": "topic:mcp",
                    "field": "pushed",
                    "window_days": 90,
                    "kind": "mcp-server",
                    "capability": "mcp-servers",
                }
            ],
            "awesome_lists": [],
        }
        found = [repository("owner/small", stars=5), repository("owner/big", stars=500)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                unittest.mock.patch("scripts.build_catalog._token", return_value="x"),
                unittest.mock.patch(
                    "scripts.build_catalog._load_registry", return_value=registry
                ),
                unittest.mock.patch(
                    "scripts.build_catalog._search_repositories", return_value=found
                ),
                unittest.mock.patch(
                    "scripts.build_catalog._known_capabilities",
                    return_value={"mcp-servers"},
                ),
            ):
                meta = build(
                    root / "registry.yaml", root / "catalog.jsonl", root / "meta.json"
                )
            lines = (root / "catalog.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                [json.loads(line)["slug"] for line in lines],
                ["owner/big", "owner/small"],
            )
            self.assertEqual(meta["records"], 2)
            self.assertEqual(meta["kinds"], {"mcp-server": 2})
            self.assertEqual(meta["previous_records"], 0)
            self.assertEqual(meta["with_momentum"], 0)
            self.assertEqual(validate(root / "catalog.jsonl"), 2)


if __name__ == "__main__":
    unittest.main()
