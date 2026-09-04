from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.dependency_debt import (
    SUPPORTED_MANIFESTS,
    analyze_dependency_debt,
    count_from_sbom,
    fetch_direct_manifests,
    fetch_sbom,
    main,
)


class TestDependencyDebt(unittest.TestCase):
    def test_supported_manifests(self) -> None:
        self.assertEqual(SUPPORTED_MANIFESTS["package.json"], "npm")
        self.assertEqual(SUPPORTED_MANIFESTS["requirements.txt"], "pypi")
        self.assertEqual(SUPPORTED_MANIFESTS["pyproject.toml"], "pypi")
        self.assertEqual(SUPPORTED_MANIFESTS["Cargo.toml"], "cargo")
        self.assertEqual(SUPPORTED_MANIFESTS["go.mod"], "go")
        self.assertEqual(SUPPORTED_MANIFESTS["Gemfile"], "rubygems")
        self.assertEqual(SUPPORTED_MANIFESTS["composer.json"], "packagist")

    @patch("scripts.dependency_debt._gh_api")
    def test_fetch_sbom_success(self, mock_gh_api) -> None:
        mock_gh_api.return_value = {
            "sbom": {
                "packages": [
                    {
                        "name": "pkg-a",
                        "purl": "pkg:npm/pkg-a@1.0.0",
                        "licenseConcluded": "MIT",
                    },
                    {
                        "name": "pkg-b",
                        "purl": "pkg:pypi/pkg-b@2.0.0",
                        "licenseDeclared": "Apache-2.0",
                    },
                ]
            }
        }
        res = fetch_sbom("owner/repo")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(res["packages"]), 2)

    @patch("scripts.dependency_debt._gh_api")
    def test_fetch_sbom_unavailable(self, mock_gh_api) -> None:
        mock_gh_api.return_value = None
        res = fetch_sbom("owner/repo")
        self.assertEqual(res["status"], "unavailable")
        self.assertIn("reason", res)

    def test_count_from_sbom(self) -> None:
        sbom = {
            "packages": [
                {
                    "name": "pkg-1",
                    "purl": "pkg:npm/pkg-1@1.0.0",
                    "licenseConcluded": "MIT",
                },
                {
                    "name": "pkg-2",
                    "purl": "pkg:npm/pkg-2@1.0.0",
                    "licenseConcluded": "NOASSERTION",
                    "licenseDeclared": "NONE",
                },
                {
                    "name": "pkg-3",
                    "externalRefs": [
                        {
                            "referenceType": "purl",
                            "referenceLocator": "pkg:cargo/pkg-3@0.1.0",
                        }
                    ],
                },
            ]
        }
        counted = count_from_sbom(sbom)
        self.assertEqual(counted["total"], 3)
        self.assertEqual(counted["ecosystems"]["npm"], 2)
        self.assertEqual(counted["ecosystems"]["cargo"], 1)
        self.assertEqual(counted["unlicensed"], 2)

    @patch("scripts.dependency_debt._gh_api")
    def test_fetch_direct_manifests_base64_decoding(self, mock_gh_api) -> None:
        raw_pkg_json = json.dumps(
            {
                "name": "my-pkg",
                "dependencies": {"lodash": "^4.17.21", "express": "^4.18.0"},
                "devDependencies": {"typescript": "^5.0.0"},
            }
        )
        b64_content = base64.b64encode(raw_pkg_json.encode("utf-8")).decode("utf-8")

        def mock_api(path: str):
            if path == "repos/owner/repo/contents/package.json":
                return {
                    "name": "package.json",
                    "encoding": "base64",
                    "content": b64_content,
                }
            return None

        mock_gh_api.side_effect = mock_api
        manifests = fetch_direct_manifests("owner/repo")
        self.assertEqual(len(manifests), 1)
        self.assertEqual(manifests[0]["manifest"], "package.json")
        self.assertEqual(manifests[0]["ecosystem"], "npm")
        self.assertEqual(manifests[0]["direct"], 3)

    @patch("scripts.dependency_debt._gh_api")
    def test_unparseable_manifest_yields_none(self, mock_gh_api) -> None:
        # Invalid JSON
        b64_invalid = base64.b64encode(b"not a valid json { broken ").decode("utf-8")

        def mock_api(path: str):
            if path == "repos/owner/repo/contents/package.json":
                return {
                    "name": "package.json",
                    "encoding": "base64",
                    "content": b64_invalid,
                }
            return None

        mock_gh_api.side_effect = mock_api
        manifests = fetch_direct_manifests("owner/repo")
        self.assertEqual(len(manifests), 1)
        self.assertEqual(manifests[0]["manifest"], "package.json")
        self.assertIsNone(manifests[0]["direct"])

    @patch("scripts.dependency_debt._gh_api")
    def test_analyze_dependency_debt_sbom_success(self, mock_gh_api) -> None:
        def mock_api(path: str):
            if "dependency-graph/sbom" in path:
                return {
                    "sbom": {
                        "packages": [
                            {
                                "name": "a",
                                "purl": "pkg:npm/a@1.0",
                                "licenseConcluded": "MIT",
                            }
                        ]
                    }
                }
            return None

        mock_gh_api.side_effect = mock_api
        debt = analyze_dependency_debt("owner/repo")
        self.assertEqual(debt["slug"], "owner/repo")
        self.assertEqual(debt["status"], "ok")
        self.assertEqual(debt["transitive"], 1)
        self.assertEqual(debt["unlicensed"], 0)
        self.assertEqual(debt["ecosystems"], {"npm": 1})

    @patch("scripts.dependency_debt._gh_api")
    def test_analyze_dependency_debt_fallback_partial(self, mock_gh_api) -> None:
        # SBOM fails (None / 404), manifest succeeds
        raw_reqs = "requests==2.28.1\nurllib3>=1.26\n# comment\n"
        b64_content = base64.b64encode(raw_reqs.encode("utf-8")).decode("utf-8")

        def mock_api(path: str):
            if "dependency-graph/sbom" in path:
                return None
            if path == "repos/owner/repo/contents/requirements.txt":
                return {
                    "name": "requirements.txt",
                    "encoding": "base64",
                    "content": b64_content,
                }
            return None

        mock_gh_api.side_effect = mock_api
        debt = analyze_dependency_debt("owner/repo")
        self.assertEqual(debt["slug"], "owner/repo")
        self.assertEqual(debt["status"], "partial")
        self.assertIsNone(debt["transitive"])
        self.assertIsNone(debt["unlicensed"])
        self.assertEqual(debt["direct"], 2)
        self.assertEqual(debt["ecosystems"], {"pypi": 2})

    @patch("scripts.dependency_debt.analyze_dependency_debt")
    def test_main_cli(self, mock_analyze) -> None:
        mock_analyze.return_value = {
            "slug": "foo/bar",
            "checked_at": "2026-09-01T00:00:00Z",
            "status": "ok",
            "direct": 5,
            "transitive": 25,
            "ecosystems": {"npm": 25},
            "unlicensed": 0,
            "manifests": [],
            "notes": [],
        }
        ret = main(["--slug", "foo/bar", "--json"])
        self.assertEqual(ret, 0)
        ret_human = main(["--slug", "foo/bar"])
        self.assertEqual(ret_human, 0)


if __name__ == "__main__":
    unittest.main()
