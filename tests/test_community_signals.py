from __future__ import annotations

import io
import json
import sys
import subprocess
import unittest
import unittest.mock
from pathlib import Path
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import community_signals
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

    @unittest.mock.patch("scripts.community_signals._get_reddit_oauth_token")
    @unittest.mock.patch("scripts.community_signals._search_reddit_oauth")
    def test_search_reddit_blocked(self, mock_search, mock_token):
        mock_token.return_value = "t0ken"
        mock_search.side_effect = RuntimeError("HTTP 403 Forbidden")
        result = search_reddit("ToolX")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["matches"], [])
        self.assertIn("403", result["reason"])

    @unittest.mock.patch("scripts.community_signals._get_reddit_oauth_token")
    def test_unconfigured_reddit_is_blocked_not_read_another_way(self, mock_token):
        mock_token.return_value = None
        result = search_reddit("ToolX")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["matches"], [])
        self.assertIn("WHEEL_REDDIT_CLIENT_ID", result["reason"])
        self.assertEqual(result["retention"], "none")

    @unittest.mock.patch("scripts.community_signals._get_reddit_oauth_token")
    def test_rate_budget_refusal_is_reported_as_blocked(self, mock_token):
        mock_token.side_effect = community_signals.RedditComplianceError(
            "Reddit rate budget exhausted: 2 requests left, 30s to window reset"
        )
        result = search_reddit("ToolX")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("rate budget", result["reason"])

    @unittest.mock.patch("scripts.community_signals._get_reddit_oauth_token")
    @unittest.mock.patch("scripts.community_signals._search_reddit_oauth")
    def test_search_reddit_success_and_timestamp_parsing(self, mock_search, mock_token):
        mock_token.return_value = "t0ken"
        mock_donsetch = mock_search
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

    @unittest.mock.patch("scripts.community_signals.search_reddit_archive")
    @unittest.mock.patch("scripts.community_signals.search_github_issues")
    @unittest.mock.patch("scripts.community_signals.search_stackexchange")
    @unittest.mock.patch("scripts.community_signals.search_reddit")
    @unittest.mock.patch("scripts.community_signals.search_hackernews")
    def test_collect_regret_signals_all_fail(
        self, mock_hn, mock_reddit, mock_so, mock_gh, mock_archive
    ):
        mock_archive.return_value = {
            "source": "reddit-archive",
            "status": "unavailable",
            "matches": [],
        }
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
        self.assertEqual(len(signals["sources"]), 5)
        self.assertEqual(signals["sources"][4]["source"], "reddit-archive")

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

    def test_matches_are_marked_as_not_retained(self):
        payload = {"data": {"children": []}}
        with unittest.mock.patch.object(
            community_signals, "_get_reddit_oauth_token", return_value="t0ken"
        ):
            with unittest.mock.patch.object(
                community_signals, "_search_reddit_oauth", return_value=payload
            ):
                result = search_reddit("ToolX")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["retention"], "none")


class RedditAuthTests(unittest.TestCase):
    """The Reddit app type decides the grant; the wrong one fails with a bare 401."""

    def setUp(self) -> None:
        community_signals._REDDIT_TOKEN_CACHE.pop("access_token", None)

    def tearDown(self) -> None:
        community_signals._REDDIT_TOKEN_CACHE.pop("access_token", None)

    def _named(self, **extra: str) -> unittest.mock._patch_dict:
        env = {"WHEEL_REDDIT_USER_AGENT": "", "WHEEL_REDDIT_USERNAME": "wheelbot"}
        env.update(extra)
        return unittest.mock.patch.dict(os.environ, env, clear=False)

    def test_secret_selects_client_credentials(self) -> None:
        payload = community_signals._reddit_grant_payload("s3cret")
        self.assertEqual(payload, {"grant_type": "client_credentials"})

    def test_no_secret_selects_installed_client_with_device_id(self) -> None:
        payload = community_signals._reddit_grant_payload("")
        self.assertEqual(
            payload["grant_type"], community_signals.REDDIT_INSTALLED_GRANT
        )
        self.assertEqual(payload["device_id"], community_signals.REDDIT_DEVICE_ID)

    def test_token_request_sends_the_selected_grant_and_hides_credentials(self) -> None:
        seen: dict[str, object] = {}

        def fake_request(url, *, allowed_hosts, headers=None, data=None, **kwargs):
            seen["url"] = url
            seen["data"] = data.decode("utf-8")
            seen["headers"] = headers or {}
            return 200, b'{"access_token": "t0ken"}', {}

        env = {
            "WHEEL_REDDIT_CLIENT_ID": "id-abc",
            "WHEEL_REDDIT_CLIENT_SECRET": "secret-xyz",
        }
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            with unittest.mock.patch.object(
                community_signals, "_reddit_http_request", fake_request
            ):
                token = community_signals._get_reddit_oauth_token()

        self.assertEqual(token, "t0ken")
        self.assertEqual(seen["url"], community_signals.REDDIT_TOKEN_URL)
        self.assertIn("grant_type=client_credentials", seen["data"])
        self.assertNotIn("secret-xyz", str(seen["url"]))
        self.assertNotIn("secret-xyz", str(seen["data"]))
        self.assertTrue(seen["headers"]["Authorization"].startswith("Basic "))

    def test_user_agent_is_overridable(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {"WHEEL_REDDIT_USER_AGENT": "acme/9 (by /u/acme)"}, clear=False
        ):
            self.assertEqual(
                community_signals.reddit_user_agent(), "acme/9 (by /u/acme)"
            )

    def test_user_agent_follows_the_format_reddit_documents(self) -> None:
        env = {"WHEEL_REDDIT_USER_AGENT": "", "WHEEL_REDDIT_USERNAME": "u/maxim_dev"}
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            agent = community_signals.reddit_user_agent()
        self.assertTrue(agent.startswith("python:com.kalpakprod.wheel:v"))
        self.assertTrue(agent.endswith("(by /u/maxim_dev)"))
        self.assertNotIn(":v0.0.0", agent)

    def test_an_unnamed_install_is_refused_rather_than_sent_anonymously(self) -> None:
        env = {"WHEEL_REDDIT_USER_AGENT": "", "WHEEL_REDDIT_USERNAME": ""}
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(community_signals.RedditComplianceError):
                community_signals.reddit_user_agent()

    def test_a_bad_username_is_refused(self) -> None:
        env = {"WHEEL_REDDIT_USER_AGENT": "", "WHEEL_REDDIT_USERNAME": "no spaces!"}
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(community_signals.RedditComplianceError):
                community_signals.reddit_user_agent()

    def test_rate_gate_refuses_once_the_published_budget_is_spent(self) -> None:
        community_signals._REDDIT_RATE_STATE.clear()
        try:
            community_signals._record_reddit_rate(
                {"x-ratelimit-remaining": "2", "x-ratelimit-reset": "45"}
            )
            with self.assertRaises(community_signals.RedditComplianceError) as caught:
                community_signals._reddit_rate_gate()
            self.assertIn("rate budget", str(caught.exception))

            community_signals._record_reddit_rate(
                {"x-ratelimit-remaining": "80", "x-ratelimit-reset": "45"}
            )
            community_signals._reddit_rate_gate()
        finally:
            community_signals._REDDIT_RATE_STATE.clear()

    def test_rate_state_ignores_a_response_without_the_headers(self) -> None:
        community_signals._REDDIT_RATE_STATE.clear()
        community_signals._record_reddit_rate({"content-type": "application/json"})
        self.assertEqual(community_signals._REDDIT_RATE_STATE, {})
        community_signals._reddit_rate_gate()

    def test_check_reports_unconfigured_without_touching_the_network(self) -> None:
        def explode(*args, **kwargs):
            raise AssertionError("no request may be made without a client id")

        with unittest.mock.patch.dict(
            os.environ, {"WHEEL_REDDIT_CLIENT_ID": ""}, clear=False
        ):
            with unittest.mock.patch.object(
                community_signals, "_reddit_http_request", explode
            ):
                report = community_signals.check_reddit_credentials()

        self.assertEqual(report["status"], "unconfigured")
        self.assertEqual(report["grant"], "")

    def test_check_reports_failure_without_leaking_the_secret(self) -> None:
        def fake_request(url, *, allowed_hosts, headers=None, data=None, **kwargs):
            return 401, b"", {}

        env = {
            "WHEEL_REDDIT_CLIENT_ID": "id-abc",
            "WHEEL_REDDIT_CLIENT_SECRET": "secret-xyz",
            "WHEEL_REDDIT_USER_AGENT": "",
            "WHEEL_REDDIT_USERNAME": "wheelbot",
        }
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            with unittest.mock.patch.object(
                community_signals, "_reddit_http_request", fake_request
            ):
                report = community_signals.check_reddit_credentials()

        self.assertEqual(report["status"], "error")
        self.assertEqual(report["grant"], "client_credentials")
        self.assertIn("401", report["reason"])
        self.assertNotIn("secret-xyz", report["reason"])
        self.assertNotIn("id-abc", report["reason"])

    def test_check_reports_ok_when_a_token_is_minted(self) -> None:
        def fake_request(url, *, allowed_hosts, headers=None, data=None, **kwargs):
            return 200, b'{"access_token": "t0ken"}', {}

        env = {
            "WHEEL_REDDIT_CLIENT_ID": "id-abc",
            "WHEEL_REDDIT_CLIENT_SECRET": "",
            "WHEEL_REDDIT_USER_AGENT": "",
            "WHEEL_REDDIT_USERNAME": "wheelbot",
        }
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            with unittest.mock.patch.object(
                community_signals, "_reddit_http_request", fake_request
            ):
                report = community_signals.check_reddit_credentials()

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["grant"], community_signals.REDDIT_INSTALLED_GRANT)
        self.assertEqual(report["reason"], "")
        self.assertIn("(by /u/wheelbot)", report["user_agent"])

    def test_check_refuses_an_install_that_cannot_identify_itself(self) -> None:
        env = {
            "WHEEL_REDDIT_CLIENT_ID": "id-abc",
            "WHEEL_REDDIT_CLIENT_SECRET": "",
            "WHEEL_REDDIT_USER_AGENT": "",
            "WHEEL_REDDIT_USERNAME": "",
        }
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            report = community_signals.check_reddit_credentials()
        self.assertEqual(report["status"], "unconfigured")
        self.assertIn("WHEEL_REDDIT_USERNAME", report["reason"])


class ScriptEntryPointTests(unittest.TestCase):
    """Each helper is documented as a plain script, so it must run as one."""

    def test_every_script_runs_as_a_file_not_only_as_a_module(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for name in ("community_signals", "dependency_debt", "hard_metrics"):
            with self.subTest(script=name):
                completed = subprocess.run(
                    [sys.executable, str(repo / "scripts" / f"{name}.py"), "--help"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr[-400:])


class RedditArchiveTests(unittest.TestCase):
    """The archive is a third party: no Reddit credentials, no Reddit budget."""

    def _payload(self, rows):
        return 200, json.dumps({"data": rows}).encode("utf-8"), {}

    def test_the_request_goes_to_the_archive_and_carries_no_reddit_credential(
        self,
    ) -> None:
        seen: dict[str, object] = {}

        def fake_request(url, *, allowed_hosts, user_agent, **kwargs):
            seen["url"] = url
            seen["hosts"] = set(allowed_hosts)
            seen["agent"] = user_agent
            seen["headers"] = kwargs.get("headers")
            return self._payload([])

        env = {
            "WHEEL_REDDIT_CLIENT_ID": "id-abc",
            "WHEEL_REDDIT_CLIENT_SECRET": "secret-xyz",
        }
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            with unittest.mock.patch.object(
                community_signals, "_bounded_http_request", fake_request
            ):
                result = community_signals.search_reddit_archive(
                    "kafka", limit=5, subreddits=["dataengineering"]
                )

        self.assertEqual(result["source"], "reddit-archive")
        self.assertIn("arctic-shift.photon-reddit.com", str(seen["url"]))
        self.assertEqual(seen["hosts"], {"arctic-shift.photon-reddit.com"})
        self.assertNotIn("secret-xyz", str(seen["url"]))
        self.assertIsNone(seen["headers"])
        self.assertTrue(str(seen["agent"]).startswith("wheel/"))

    def test_rows_are_normalized_and_never_ranked_by_the_archived_score(self) -> None:
        rows = [
            {
                "title": "We migrated away from ToolX",
                "permalink": "/r/dataengineering/comments/abc/we_migrated/",
                "subreddit": "dataengineering",
                "score": 3,
                "created_utc": 1672531200,
            }
        ]
        with unittest.mock.patch.object(
            community_signals,
            "_bounded_http_request",
            lambda *a, **k: self._payload(rows),
        ):
            result = community_signals.search_reddit_archive(
                "ToolX", limit=5, subreddits=["dataengineering"]
            )

        self.assertEqual(result["status"], "ok")
        match = result["matches"][0]
        self.assertEqual(match["matched_field"], "title")
        self.assertEqual(match["created_at"], "2023-01-01T00:00:00Z")
        self.assertEqual(match["score_at_archive"], 3)
        self.assertNotIn("points", match)
        self.assertEqual(
            match["url"],
            "https://www.reddit.com/r/dataengineering/comments/abc/we_migrated/",
        )
        self.assertEqual(result["retention"], "none")

    def test_a_slow_down_answer_is_retried_once_then_reported(self) -> None:
        calls = {"n": 0}

        def fake_request(*args, **kwargs):
            calls["n"] += 1
            raise RuntimeError("archive returned HTTP 422")

        with unittest.mock.patch.object(community_signals, "time") as fake_time:
            fake_time.time.return_value = 0.0
            with unittest.mock.patch.object(
                community_signals, "_bounded_http_request", fake_request
            ):
                result = community_signals.search_reddit_archive(
                    "ToolX", limit=5, subreddits=["dataengineering"]
                )

        # title and selftext, each retried once after the archive says slow down
        self.assertEqual(calls["n"], 4)
        self.assertIn("422", result["reason"])
        self.assertEqual(result["matches"], [])

    def test_partial_when_one_subreddit_answers_and_another_does_not(self) -> None:
        rows = [{"title": "t", "permalink": "/r/a/comments/1/t/", "subreddit": "a"}]
        answers = [self._payload(rows), RuntimeError("archive returned HTTP 500")]

        def fake_request(*args, **kwargs):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with unittest.mock.patch.object(community_signals, "time") as fake_time:
            fake_time.time.return_value = 0.0
            with unittest.mock.patch.object(
                community_signals, "_bounded_http_request", fake_request
            ):
                result = community_signals.search_reddit_archive(
                    "t", limit=5, subreddits=["a", "b"]
                )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["matches"]), 1)
        self.assertIn("500", result["reason"])

    def test_registry_maps_a_capability_and_falls_back_to_defaults(self) -> None:
        self.assertEqual(
            community_signals.load_subreddits("mcp-servers"), ["mcp", "ClaudeAI"]
        )
        self.assertEqual(
            community_signals.load_subreddits("no-such-capability"),
            list(community_signals.DEFAULT_SUBREDDITS),
        )

    def test_the_archive_lane_is_skipped_when_reddit_itself_answered(self) -> None:
        ok_reddit = {"source": "reddit", "status": "ok", "matches": []}
        empty = {"source": "x", "status": "error", "matches": []}
        with unittest.mock.patch.object(
            community_signals, "search_reddit", return_value=ok_reddit
        ):
            with unittest.mock.patch.object(
                community_signals, "search_hackernews", return_value=empty
            ):
                with unittest.mock.patch.object(
                    community_signals, "search_stackexchange", return_value=empty
                ):
                    with unittest.mock.patch.object(
                        community_signals, "search_github_issues", return_value=empty
                    ):
                        with unittest.mock.patch.object(
                            community_signals, "search_reddit_archive"
                        ) as mock_archive:
                            signals = collect_regret_signals("foo/bar")

        mock_archive.assert_not_called()
        self.assertNotIn("reddit_archive", signals["counts"])


class ArchiveFieldSweepTests(unittest.TestCase):
    """A migration story lives in the body as often as in the headline."""

    def _rows(self, permalink):
        return [
            {
                "title": "t",
                "permalink": permalink,
                "subreddit": "devops",
                "created_utc": 1672531200,
            }
        ]

    def test_the_body_is_swept_only_when_the_title_found_nothing(self) -> None:
        seen: list[str] = []

        def fake_request(url, **kwargs):
            seen.append("selftext=" if "selftext=" in url else "title=")
            rows = [] if "title=" in url else self._rows("/r/devops/comments/2/b/")
            return 200, json.dumps({"data": rows}).encode("utf-8"), {}

        with unittest.mock.patch.object(community_signals, "time") as fake_time:
            fake_time.time.return_value = 0.0
            with unittest.mock.patch.object(
                community_signals, "_bounded_http_request", fake_request
            ):
                result = community_signals.search_reddit_archive(
                    "ToolX", limit=5, subreddits=["devops"]
                )

        self.assertEqual(seen, ["title=", "selftext="])
        self.assertEqual(result["matches"][0]["matched_field"], "selftext")

    def test_a_title_hit_skips_the_body_sweep(self) -> None:
        seen: list[str] = []

        def fake_request(url, **kwargs):
            seen.append("selftext=" if "selftext=" in url else "title=")
            return (
                200,
                json.dumps({"data": self._rows("/r/devops/comments/1/a/")}).encode(
                    "utf-8"
                ),
                {},
            )

        with unittest.mock.patch.object(community_signals, "time") as fake_time:
            fake_time.time.return_value = 0.0
            with unittest.mock.patch.object(
                community_signals, "_bounded_http_request", fake_request
            ):
                result = community_signals.search_reddit_archive(
                    "ToolX", limit=5, subreddits=["devops"]
                )

        self.assertEqual(seen, ["title="])
        self.assertEqual(len(result["matches"]), 1)

    def test_the_same_thread_is_not_reported_twice(self) -> None:
        def fake_request(url, **kwargs):
            rows = [] if "title=" in url else self._rows("/r/devops/comments/3/c/")
            if "selftext=" in url:
                rows = rows + rows
            return 200, json.dumps({"data": rows}).encode("utf-8"), {}

        with unittest.mock.patch.object(community_signals, "time") as fake_time:
            fake_time.time.return_value = 0.0
            with unittest.mock.patch.object(
                community_signals, "_bounded_http_request", fake_request
            ):
                result = community_signals.search_reddit_archive(
                    "ToolX", limit=5, subreddits=["devops"]
                )

        self.assertEqual(len(result["matches"]), 1)

    def test_an_unsupported_field_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            community_signals._archive_request("devops", "x", 5, field="author")


if __name__ == "__main__":
    unittest.main()
