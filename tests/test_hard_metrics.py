"""Unit tests for hard_metrics module."""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import MagicMock, patch

from scripts.hard_metrics import (
    bus_factor,
    collect_hard_metrics,
    commit_cadence,
    main,
    release_cadence,
)


class TestBusFactor(unittest.TestCase):
    def test_single_dominant_contributor(self):
        contributors = [
            {"login": "alice", "contributions": 999},
            {"login": "bob", "contributions": 1},
        ]
        res = bus_factor(contributors)
        self.assertEqual(res["contributors"], 2)
        self.assertEqual(res["bus_factor"], 1)
        self.assertIsNotNone(res["hhi"])
        self.assertGreater(res["hhi"], 0.99)

    def test_even_split_raises_bus_factor_and_lowers_hhi(self):
        contributors = [{"login": f"user{i}", "contributions": 10} for i in range(10)]
        res = bus_factor(contributors)
        self.assertEqual(res["contributors"], 10)
        # 10 users with 10 each = 100 total. 50% = 50. 5 users needed.
        self.assertEqual(res["bus_factor"], 5)
        # HHI: 10 * (0.1^2) = 0.1
        self.assertAlmostEqual(res["hhi"], 0.1, places=3)

    def test_empty_contributor_list(self):
        res = bus_factor([])
        self.assertEqual(res["contributors"], 0)
        self.assertEqual(res["bus_factor"], 0)
        self.assertIsNone(res["hhi"])

    def test_zero_contributions_all(self):
        res = bus_factor([{"login": "zero", "contributions": 0}])
        self.assertEqual(res["contributors"], 1)
        self.assertEqual(res["bus_factor"], 0)
        self.assertIsNone(res["hhi"])


class TestCommitCadence(unittest.TestCase):
    def test_missing_payload(self):
        res = commit_cadence(None)
        self.assertIsNone(res["weeks_observed"])
        self.assertIsNone(res["weeks_with_commits"])
        self.assertIsNone(res["commits_52w"])
        self.assertIsNone(res["median_commits_per_week"])

    def test_malformed_payload(self):
        res = commit_cadence({"all": "not-a-list"})
        self.assertIsNone(res["weeks_observed"])

        res2 = commit_cadence({})
        self.assertIsNone(res2["weeks_observed"])

        res3 = commit_cadence({"all": ["bad", "values"]})
        self.assertIsNone(res3["weeks_observed"])

    def test_valid_payload(self):
        counts = [0] * 50 + [10, 20]
        res = commit_cadence({"all": counts})
        self.assertEqual(res["weeks_observed"], 52)
        self.assertEqual(res["weeks_with_commits"], 2)
        self.assertEqual(res["commits_52w"], 30)
        self.assertEqual(res["median_commits_per_week"], 0.0)


class TestReleaseCadence(unittest.TestCase):
    def test_empty_releases(self):
        res = release_cadence([])
        self.assertEqual(res["releases"], 0)
        self.assertIsNone(res["median_interval_days"])
        self.assertIsNone(res["latest_age_days"])

    def test_single_release_yields_median_none(self):
        releases = [{"published_at": "2026-03-01T00:00:00Z"}]
        now = "2026-03-11T00:00:00Z"
        res = release_cadence(releases, now=now)
        self.assertEqual(res["releases"], 1)
        self.assertIsNone(res["median_interval_days"])
        self.assertEqual(res["latest_age_days"], 10.0)

    def test_multiple_releases_interval(self):
        releases = [
            {"published_at": "2026-03-01T00:00:00Z"},
            {"published_at": "2026-02-19T00:00:00Z"},
            {"published_at": "2026-01-30T00:00:00Z"},
        ]
        # Intervals: 2026-03-01 to 2026-02-19 = 10 days. 2026-02-19 to 2026-01-30 = 20 days.
        # Median of [10, 20] = 15.0
        now = "2026-03-06T00:00:00Z"
        res = release_cadence(releases, now=now)
        self.assertEqual(res["releases"], 3)
        self.assertEqual(res["median_interval_days"], 15.0)
        self.assertEqual(res["latest_age_days"], 5.0)


class TestCollectHardMetrics(unittest.TestCase):
    @patch("scripts.hard_metrics._gh_api")
    def test_collect_all_success(self, mock_gh):
        def gh_side_effect(path):
            if path == "repos/owner/repo":
                return {
                    "license": {"spdx_id": "MIT"},
                    "archived": False,
                    "pushed_at": "2026-03-05T00:00:00Z",
                }
            elif path == "repos/owner/repo/contributors?per_page=100":
                return [
                    {"login": "alice", "contributions": 80},
                    {"login": "bob", "contributions": 20},
                ]
            elif path == "repos/owner/repo/stats/participation":
                return {"all": [1] * 52}
            elif path == "repos/owner/repo/releases?per_page=30":
                return [
                    {"published_at": "2026-03-01T00:00:00Z"},
                    {"published_at": "2026-02-01T00:00:00Z"},
                ]
            return None

        mock_gh.side_effect = gh_side_effect
        now = "2026-03-10T00:00:00Z"
        res = collect_hard_metrics("owner/repo", now=now)

        self.assertEqual(res["slug"], "owner/repo")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["license"], "MIT")
        self.assertEqual(res["archived"], False)
        self.assertEqual(res["pushed_age_days"], 5.0)
        self.assertEqual(res["unavailable"], [])
        self.assertEqual(res["bus_factor"]["bus_factor"], 1)
        self.assertEqual(res["commit_cadence"]["commits_52w"], 52)
        self.assertEqual(res["release_cadence"]["releases"], 2)

    @patch("scripts.hard_metrics._gh_api")
    def test_collect_unavailable_calls(self, mock_gh):
        # 202 empty dict for stats, None for contributors, 404 None for releases
        def gh_side_effect(path):
            if path == "repos/owner/repo":
                return {"license": None, "archived": False}
            elif path == "repos/owner/repo/contributors?per_page=100":
                return None
            elif path == "repos/owner/repo/stats/participation":
                return {}  # GitHub 202 computing response
            elif path == "repos/owner/repo/releases?per_page=30":
                return None
            return None

        mock_gh.side_effect = gh_side_effect
        res = collect_hard_metrics("owner/repo")

        self.assertEqual(res["status"], "partial")
        self.assertIn("contributors", res["unavailable"])
        self.assertIn("participation", res["unavailable"])
        self.assertIn("releases", res["unavailable"])
        self.assertNotIn("repo", res["unavailable"])
        self.assertEqual(res["bus_factor"]["contributors"], 0)
        self.assertIsNone(res["commit_cadence"]["weeks_observed"])
        self.assertEqual(res["release_cadence"]["releases"], 0)


class TestMain(unittest.TestCase):
    @patch("scripts.hard_metrics.collect_hard_metrics")
    def test_main_json_output(self, mock_collect):
        mock_collect.return_value = {
            "slug": "foo/bar",
            "checked_at": "2026-03-10T00:00:00Z",
            "status": "ok",
            "license": "MIT",
            "archived": False,
            "pushed_age_days": 1.0,
            "bus_factor": {"contributors": 1, "bus_factor": 1, "hhi": 1.0},
            "commit_cadence": {"weeks_observed": 52},
            "release_cadence": {"releases": 1},
            "unavailable": [],
        }

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["--slug", "foo/bar", "--json"])
        self.assertEqual(code, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["slug"], "foo/bar")
        self.assertEqual(out["status"], "ok")


if __name__ == "__main__":
    unittest.main()
