from __future__ import annotations

import io
import json
import sys
import unittest
import unittest.mock
from pathlib import Path
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.community_signals import (
    NEGATIVE_TERMS,
    PROBE_TERMS,
    MAX_RESPONSE_BYTES,
    MAX_ITEMS_PER_SOURCE,
    MAX_TITLE_CHARS,
    search_hackernews,
    search_reddit,
    search_stackexchange,
    search_github_issues,
    collect_regret_signals,
    load_probe_terms,
    _get_reddit_oauth_token,
    _REDDIT_TOKEN_CACHE,
    main,
)
import os


class TestCommunitySignals(unittest.TestCase):
    def setUp(self) -> None:
        # The GitHub probe paces itself to stay under the search rate limit. Real
        # sleeping belongs to the live path, not to a unit test.
        patcher = unittest.mock.patch(
            "scripts.community_signals.SEARCH_PROBE_PAUSE_SECONDS", 0
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_negative_terms(self):
        expected_terms = [
            "migrated away from",
            "moved off",
            "switching from",
            "postmortem",
            "we regret",
            "abandoned",
            "problems with",
            "alternative to",
        ]
        for term in expected_terms:
            self.assertIn(term, NEGATIVE_TERMS)

    @unittest.mock.patch("scripts.community_signals._bounded_https_download")
    def test_search_hackernews_success(self, mock_download):
        hn_payload = {
            "hits": [
                {
                    "title": "Why we migrated away from ToolX",
                    "url": "https://example.com/post1",
                    "points": 142,
                    "created_at": "2023-01-01T12:00:00Z",
                    "objectID": "12345",
                },
                {
                    "story_title": "Postmortem of our migration",
                    "url": None,
                    "points": 50,
                    "created_at": "2023-01-02T12:00:00Z",
                    "objectID": "67890",
                },
            ]
        }
        mock_download.return_value = json.dumps(hn_payload).encode("utf-8")

        result = search_hackernews("ToolX", limit=10)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source"], "hackernews")
        self.assertEqual(len(result["matches"]), 2)
        self.assertEqual(
            result["matches"][0]["title"], "Why we migrated away from ToolX"
        )
        self.assertEqual(result["matches"][0]["url"], "https://example.com/post1")
        self.assertEqual(result["matches"][0]["points"], 142)
        # Fallback URL when hit['url'] is None
        self.assertEqual(
            result["matches"][1]["url"], "https://news.ycombinator.com/item?id=67890"
        )

    @unittest.mock.patch("scripts.community_signals._bounded_https_download")
    def test_search_hackernews_failure_status(self, mock_download):
        mock_download.side_effect = urllib.error.HTTPError(
            url="https://hn.algolia.com/api/v1/search",
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=None,
        )
        result = search_hackernews("ToolX")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["matches"], [])
        self.assertIn("500", result["reason"])

    @unittest.mock.patch("scripts.community_signals._search_reddit_donsetch")
    def test_search_reddit_blocked(self, mock_donsetch):
        mock_donsetch.side_effect = RuntimeError("HTTP 403 Forbidden")
        result = search_reddit("ToolX")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["matches"], [])
        self.assertIn("403", result["reason"])

    @unittest.mock.patch("scripts.community_signals._search_reddit_donsetch")
    def test_search_reddit_success_and_timestamp_parsing(self, mock_donsetch):
        reddit_payload = {
            "data": {
                "children": [
                    {
                        "data": {
                            "title": "Leaving ToolX for Good",
                            "permalink": "/r/programming/comments/123/leaving_toolx",
                            "score": 88,
                            "created_utc": 1672531200,  # 2023-01-01T00:00:00Z
                        }
                    }
                ]
            }
        }
        mock_donsetch.return_value = reddit_payload

        result = search_reddit("ToolX")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(
            result["matches"][0]["url"],
            "https://www.reddit.com/r/programming/comments/123/leaving_toolx",
        )
        self.assertEqual(result["matches"][0]["created_at"], "2023-01-01T00:00:00Z")
        self.assertEqual(result["matches"][0]["points"], 88)

    @unittest.mock.patch("scripts.community_signals._gh_api")
    def test_search_github_issues_success(self, mock_gh):
        mock_gh.return_value = {
            "items": [
                {
                    "title": "Memory leak postmortem and fix",
                    "html_url": "https://github.com/foo/bar/issues/10",
                    "comments": 15,
                    "reactions": {"total_count": 22},
                    "created_at": "2023-03-01T08:00:00Z",
                }
            ]
        }
        result = search_github_issues("foo/bar")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source"], "github_issues")
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["points"], 22)
        self.assertEqual(
            result["matches"][0]["url"], "https://github.com/foo/bar/issues/10"
        )

    @unittest.mock.patch("scripts.community_signals._gh_api")
    def test_search_github_issues_unavailable(self, mock_gh):
        mock_gh.return_value = None
        result = search_github_issues("foo/bar")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["matches"], [])

    @unittest.mock.patch("scripts.community_signals._bounded_https_download")
    def test_probe_loop_uses_the_registry_not_the_hardcoded_tuple(
        self, mock_download
    ) -> None:
        mock_download.return_value = json.dumps({"hits": []}).encode("utf-8")
        with unittest.mock.patch(
            "scripts.community_signals.load_probe_terms",
            return_value=["alpha", "beta", "gamma"],
        ):
            result = search_hackernews("ToolX")
        self.assertEqual(result["terms_probed"], 3)
        self.assertEqual(result["terms_total"], 3)
        queried = [call.args[0] for call in mock_download.call_args_list]
        self.assertEqual(len(queried), 3)
        self.assertTrue(any("alpha" in url for url in queried))
        self.assertTrue(any("gamma" in url for url in queried))

    @unittest.mock.patch("scripts.community_signals._bounded_https_download")
    def test_extra_language_reaches_the_wire(self, mock_download) -> None:
        mock_download.return_value = json.dumps({"items": []}).encode("utf-8")
        with unittest.mock.patch(
            "scripts.community_signals.load_probe_terms",
            return_value=[
                "postmortem",
                "\u043f\u0440\u043e\u0431\u043b\u0435\u043c\u044b \u0441",
            ],
        ):
            result = search_stackexchange("ToolX")
        self.assertEqual(result["terms_total"], 2)
        queried = "".join(call.args[0] for call in mock_download.call_args_list)
        self.assertIn("%D0%BF%D1%80%D0%BE%D0%B1%D0%BB%D0%B5%D0%BC%D1%8B", queried)

    @unittest.mock.patch("scripts.community_signals._bounded_https_download")
    def test_bounding_item_count_and_title_truncation(self, mock_download):
        long_title = "A" * 300
        hits = [
            {
                "title": f"{long_title} #{i}",
                "url": f"https://example.com/story/{i}",
                "points": 1,
                "created_at": "2023-01-01T00:00:00Z",
                "objectID": str(i),
            }
            for i in range(50)
        ]
        mock_download.return_value = json.dumps({"hits": hits}).encode("utf-8")

        # Request limit 40, but bounded limit should cap at MAX_ITEMS_PER_SOURCE (25)
        result = search_hackernews("query", limit=40)
        self.assertEqual(len(result["matches"]), 25)
        self.assertEqual(len(result["matches"][0]["title"]), 200)
        self.assertEqual(result["matches"][0]["title"], long_title[:200])

    @unittest.mock.patch("scripts.community_signals._gh_api")
    def test_github_issue_probe_follows_a_repository_rename(self, mock_api) -> None:
        calls: list[str] = []

        def answer(path: str):
            calls.append(path)
            if path.startswith("repos/"):
                return {"full_name": "fastapi/fastapi"}
            return {"items": []}

        mock_api.side_effect = answer
        result = search_github_issues("tiangolo/fastapi")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["query"], "repo:fastapi/fastapi is:issue")
        self.assertTrue(
            all("tiangolo" not in path for path in calls if path.startswith("search/"))
        )

    @unittest.mock.patch("scripts.community_signals.search_github_issues")
    @unittest.mock.patch("scripts.community_signals.search_stackexchange")
    @unittest.mock.patch("scripts.community_signals.search_reddit")
    @unittest.mock.patch("scripts.community_signals.search_hackernews")
    def test_collect_regret_signals_all_fail(
        self, mock_hn, mock_reddit, mock_so, mock_gh
    ):
        mock_hn.return_value = {
            "source": "hackernews",
            "status": "unavailable",
            "matches": [],
        }
        mock_reddit.return_value = {
            "source": "reddit",
            "status": "blocked",
            "matches": [],
        }
        mock_so.return_value = {
            "source": "stackoverflow",
            "status": "error",
            "matches": [],
        }
        mock_gh.return_value = {
            "source": "github_issues",
            "status": "error",
            "matches": [],
        }

        signals = collect_regret_signals("foo/bar")
        self.assertEqual(signals["slug"], "foo/bar")
        self.assertEqual(signals["available_sources"], 0)
        self.assertEqual(signals["counts"]["hackernews"], 0)
        self.assertEqual(signals["counts"]["reddit"], 0)
        self.assertEqual(signals["counts"]["stackoverflow"], 0)
        self.assertEqual(signals["counts"]["github_issues"], 0)
        self.assertEqual(len(signals["sources"]), 4)

    @unittest.mock.patch("scripts.community_signals._bounded_https_download")
    def test_stackexchange_probes_every_term_and_deduplicates(self, mock_download):
        payload = {
            "items": [
                {
                    "title": "Why we moved off ToolX",
                    "link": "https://stackoverflow.com/q/1",
                    "score": 12,
                    "creation_date": 1700000000,
                }
            ]
        }
        mock_download.return_value = json.dumps(payload).encode("utf-8")
        result = search_stackexchange("ToolX")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["terms_probed"], len(PROBE_TERMS))
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["created_at"], "2023-11-14T22:13:20Z")

    @unittest.mock.patch("scripts.community_signals._bounded_https_download")
    def test_stackexchange_reports_http_failure_as_error(self, mock_download):
        mock_download.side_effect = urllib.error.HTTPError(
            url="https://api.stackexchange.com/2.3/search",
            code=502,
            msg="Bad Gateway",
            hdrs=None,
            fp=None,
        )
        result = search_stackexchange("ToolX")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["matches"], [])
        self.assertIn("502", result["reason"])

    @unittest.mock.patch("scripts.community_signals.collect_regret_signals")
    def test_main_cli_json(self, mock_collect):
        mock_collect.return_value = {
            "slug": "foo/bar",
            "checked_at": "2025-01-01T00:00:00Z",
            "sources": [],
            "counts": {"hackernews": 0, "reddit": 0, "github_issues": 0},
            "available_sources": 0,
        }
        stdout = io.StringIO()
        with unittest.mock.patch("sys.stdout", stdout):
            code = main(["--slug", "foo/bar", "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(stdout.getvalue())
        self.assertEqual(parsed["slug"], "foo/bar")

    @unittest.mock.patch("scripts.community_signals.collect_regret_signals")
    def test_main_cli_human(self, mock_collect):
        mock_collect.return_value = {
            "slug": "foo/bar",
            "checked_at": "2025-01-01T00:00:00Z",
            "sources": [
                {
                    "source": "hackernews",
                    "status": "ok",
                    "matches": [
                        {
                            "title": "Migrated from X",
                            "url": "https://hn.example.com",
                            "points": 42,
                            "created_at": "2025-01-01T00:00:00Z",
                        }
                    ],
                }
            ],
            "counts": {"hackernews": 1, "reddit": 0, "github_issues": 0},
            "available_sources": 1,
        }
        stdout = io.StringIO()
        with unittest.mock.patch("sys.stdout", stdout):
            code = main(["--slug", "foo/bar"])
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("Community Signals for foo/bar", output)
        self.assertIn("Migrated from X", output)

    def test_reddit_oauth_success_and_cache(self):
        _REDDIT_TOKEN_CACHE.clear()
        token_body = json.dumps({"access_token": "tok_reddit_secret_test"}).encode(
            "utf-8"
        )
        search_body = json.dumps(
            {
                "data": {
                    "children": [
                        {
                            "data": {
                                "title": "Migrated from ToolY",
                                "permalink": "/r/test/comments/456",
                                "score": 12,
                                "created_utc": 1680000000,
                            }
                        }
                    ]
                }
            }
        ).encode("utf-8")

        mock_responses = [
            (200, token_body, {}),
            (200, search_body, {}),
            (200, search_body, {}),
        ]

        with unittest.mock.patch.dict(
            os.environ,
            {
                "WHEEL_REDDIT_CLIENT_ID": "cid123",
                "WHEEL_REDDIT_CLIENT_SECRET": "csec456",
            },
        ):
            with unittest.mock.patch(
                "scripts.community_signals._reddit_http_request"
            ) as mock_http:
                mock_http.side_effect = mock_responses
                res = search_reddit("ToolY")
                self.assertEqual(res["status"], "ok")
                self.assertEqual(len(res["matches"]), 1)
                self.assertEqual(res["matches"][0]["title"], "Migrated from ToolY")
                self.assertEqual(
                    _REDDIT_TOKEN_CACHE.get("access_token"), "tok_reddit_secret_test"
                )
                # Second call should reuse cached token and make only 1 HTTP request
                mock_http.reset_mock()
                mock_http.return_value = (200, search_body, {})
                res2 = search_reddit("ToolY")
                self.assertEqual(res2["status"], "ok")
                self.assertEqual(mock_http.call_count, 1)

    def test_reddit_credential_absent_fallback(self):
        _REDDIT_TOKEN_CACHE.clear()
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with unittest.mock.patch(
                "scripts.community_signals._search_reddit_donsetch"
            ) as mock_donsetch:
                mock_donsetch.return_value = {
                    "data": {
                        "children": [
                            {
                                "data": {
                                    "title": "Postmortem ToolZ",
                                    "permalink": "/r/dev/comments/789",
                                    "score": 4,
                                    "created_utc": 1681000000,
                                }
                            }
                        ]
                    }
                }
                res = search_reddit("ToolZ")
                self.assertEqual(res["status"], "ok")
                self.assertEqual(len(res["matches"]), 1)
                self.assertEqual(res["matches"][0]["title"], "Postmortem ToolZ")
                mock_donsetch.assert_called_once()

    def test_reddit_credential_absent_fallback_failure(self):
        _REDDIT_TOKEN_CACHE.clear()
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with unittest.mock.patch(
                "scripts.community_signals._search_reddit_donsetch"
            ) as mock_donsetch:
                mock_donsetch.side_effect = RuntimeError(
                    "donsetch unavailable: browser crashed"
                )
                res = search_reddit("ToolZ")
                self.assertEqual(res["status"], "unavailable")
                self.assertIn("browser crashed", res["reason"])
                self.assertEqual(res["matches"], [])

    def test_registry_driven_term_selection(self):
        terms = load_probe_terms()
        self.assertIsInstance(terms, list)
        self.assertIn("postmortem", terms)
        self.assertIn("migrated away", terms)
        self.assertLessEqual(len(terms), 8)

    def test_wheel_probe_langs_honoured(self):
        with unittest.mock.patch.dict(os.environ, {"WHEEL_PROBE_LANGS": "ru,es"}):
            terms = load_probe_terms()
            self.assertIn("postmortem", terms)
            self.assertIn("ушёл с", terms)
            self.assertLessEqual(len(terms), 8)

    def test_probe_cap_enforced(self):
        with unittest.mock.patch.dict(
            os.environ, {"WHEEL_PROBE_LANGS": "ru,es,pt,de,fr,zh,ja"}
        ):
            terms = load_probe_terms()
            self.assertEqual(len(terms), 8)

    def test_registry_missing_fallback(self):
        from pathlib import Path

        missing_path = Path("this_file_does_not_exist_xyz.yaml")
        terms = load_probe_terms(registry_path=missing_path)
        self.assertEqual(terms, list(PROBE_TERMS[:8]))


if __name__ == "__main__":
    unittest.main()
