from __future__ import annotations

import unittest
from unittest import mock
from unittest.mock import patch
import contextlib
import hashlib
import io
from io import BytesIO
import json
from datetime import datetime, timezone
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path
import tempfile
import tarfile
from contextlib import redirect_stdout
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch
from urllib import error as urllib_error
from urllib.error import HTTPError

from scripts import wheel
from scripts.wheel import (
    ALLOWED_TRANSITIONS,
    Candidate,
    Evidence,
    SourceResult,
    RunStore,
    _contained_path,
    _bounded_https_download,
    _HTTPSRedirect,
    _extract_required_files,
    _latest_release_status,
    _version_from_output,
    _decision_slug,
    _file_lock,
    add_candidate_role,
    candidate_graph,
    dependency_platform,
    dependency_status,
    donsetch_fetch,
    ensure_dependency,
    load_dependency_manifest,
    merge_source_results,
    load_config,
    kind_of,
    maturity_flags,
    maturity_level,
    main,
    operational_gap,
    normalize_repo_slug,
    normalize_candidate_id,
    probe_repository,
    run_doctor,
    record_decision,
    render_decision,
    save_maturity_cache,
    detect_emerging_gem,
    load_catalog,
    sync_catalog,
    _parse_catalog_payload,
    decision_schema,
    search_catalog,
    catalog_age_days,
    _require_catalog_url,
    _catalog_allowed_hosts,
    _fetch_catalog_payload,
    CATALOG_MAX_BYTES,
    DEFAULT_CATALOG_EDGE_URL,
    DEFAULT_CATALOG_FALLBACK_URL,
)


class CoreContractTests(unittest.TestCase):
    def test_normalize_repo_slug_removes_github_url_case_and_suffix(self) -> None:
        self.assertEqual(
            normalize_repo_slug("https://github.com/Owner/Repo.git"), "owner/repo"
        )

    def test_source_result_rejects_unknown_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid source status"):
            SourceResult(source="github", status="unknown", checked_at="now")  # type: ignore[arg-type]

    def test_question_can_transition_to_deep(self) -> None:
        self.assertIn("deep", ALLOWED_TRANSITIONS["question"])

    def test_candidate_rejects_string_roles_and_aliases(self) -> None:
        with self.assertRaisesRegex(ValueError, "roles"):
            Candidate(candidate_id="owner/repo", display_name="Repo", roles="base")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "aliases"):
            Candidate(candidate_id="owner/repo", display_name="Repo", aliases="repo")  # type: ignore[arg-type]

    def test_candidate_ids_distinguish_repositories_products_and_services(self) -> None:
        self.assertEqual(
            normalize_candidate_id("Owner/Repo"), ("repository", "owner/repo")
        )
        self.assertEqual(
            normalize_candidate_id("product:Open Executive"),
            ("product", "product:open-executive"),
        )
        self.assertEqual(
            normalize_candidate_id("service:Managed Search"),
            ("service", "service:managed-search"),
        )
        with self.assertRaisesRegex(ValueError, "typed"):
            normalize_candidate_id("Open Executive")

    def test_candidate_requires_known_non_empty_roles_and_matching_kind(self) -> None:
        discovered = Candidate(
            candidate_id="product:open-executive",
            display_name="OpenExecutive",
            kind="product",
        )
        self.assertEqual(discovered.roles, ["alternative"])
        add_candidate_role(discovered, "donor")
        self.assertEqual(discovered.roles, ["donor"])
        with self.assertRaisesRegex(ValueError, "roles"):
            Candidate(candidate_id="owner/repo", display_name="Repo", roles=[])
        with self.assertRaisesRegex(ValueError, "invalid candidate role"):
            Candidate(candidate_id="owner/repo", display_name="Repo", roles=["unknown"])
        with self.assertRaisesRegex(ValueError, "kind"):
            Candidate(
                candidate_id="product:open-executive",
                display_name="OpenExecutive",
                kind="service",
            )

    def test_candidate_rejects_placeholder_with_real_role(self) -> None:
        with self.assertRaisesRegex(ValueError, "alternative"):
            Candidate(
                candidate_id="owner/repo",
                display_name="Repo",
                roles=["alternative", "donor"],
            )
        evaluated = Candidate(
            candidate_id="owner/repo", display_name="Repo", roles=["donor"]
        )
        add_candidate_role(evaluated, "alternative")
        self.assertEqual(evaluated.roles, ["donor"])


class CandidateGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = Path(__file__).with_name("fixtures") / "source-results.json"
        raw = json.loads(fixture.read_text(encoding="utf-8"))
        self.results = [
            SourceResult(
                source=item["source"],
                status=item["status"],
                checked_at=item["checked_at"],
                evidence=[
                    Evidence(source=item["source"], **evidence)
                    for evidence in item["evidence"]
                ],
            )
            for item in raw["results"]
        ]

    def test_merge_canonicalizes_candidate(self) -> None:
        candidates = merge_source_results(self.results)
        self.assertEqual(list(candidates), ["owner/repo"])
        self.assertEqual(
            candidates["owner/repo"].aliases[0], "https://github.com/Owner/Repo"
        )

    def test_merge_deduplicates_evidence(self) -> None:
        candidates = merge_source_results(self.results)
        self.assertEqual(len(candidates["owner/repo"].evidence), 2)

    def test_candidate_keeps_multiple_roles(self) -> None:
        candidate = Candidate(candidate_id="owner/repo", display_name="Repo")
        add_candidate_role(candidate, "base")
        add_candidate_role(candidate, "donor")
        add_candidate_role(candidate, "base")
        self.assertEqual(candidate.roles, ["base", "donor"])

    def test_merge_preserves_typed_product_candidate_and_edge(self) -> None:
        result = SourceResult(
            source="official",
            status="ok",
            checked_at="2026-08-30T00:00:00Z",
            evidence=[
                Evidence(
                    source="official",
                    url="https://example.test/open-executive",
                    observed_at="2026-08-30T00:00:00Z",
                    claim="Hosted product",
                    candidate="product:Open Executive",
                )
            ],
            edges=[
                {
                    "kind": "integrates",
                    "source": "service:Managed Search",
                    "target": "product:Open Executive",
                    "feature": "search",
                }
            ],
        )
        candidates = merge_source_results([result])
        graph = candidate_graph(candidates, result.edges)
        self.assertEqual(candidates["product:open-executive"].kind, "product")
        self.assertEqual(graph["edges"][0]["source"], "service:managed-search")

    def test_graph_keeps_typed_edge(self) -> None:
        graph = candidate_graph(
            {"owner/repo": Candidate(candidate_id="owner/repo", display_name="Repo")},
            [
                {
                    "kind": "fork-of",
                    "source": "owner/fork",
                    "target": "owner/repo",
                    "feature": "",
                }
            ],
        )
        self.assertEqual(graph["edges"][0]["kind"], "fork-of")


class RunStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "wheel-home"
        self.store = RunStore(self.home)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_create_writes_context_record_under_home(self) -> None:
        record = self.store.create("self-hosted cinema")
        path = self.home / "runs" / f"{record.run_id}.json"
        self.assertTrue(path.is_file())
        self.assertEqual(self.store.load(record.run_id).state, "context")

    def test_save_is_atomic_and_round_trips(self) -> None:
        record = self.store.create("task")
        record.task = "changed task"
        self.store.save(record)
        self.assertEqual(self.store.load(record.run_id).task, "changed task")
        self.assertEqual(list((self.home / "runs").glob("*.tmp")), [])

    def test_transition_rejects_invalid_state_change(self) -> None:
        record = self.store.create("task")
        with self.assertRaisesRegex(ValueError, "invalid transition"):
            self.store.transition(record.run_id, "decision")

    def test_transition_persists_allowed_state_change(self) -> None:
        record = self.store.create("task")
        changed = self.store.transition(record.run_id, "quick")
        self.assertEqual(changed.state, "quick")
        self.assertEqual(self.store.load(record.run_id).state, "quick")

    def test_load_rejects_path_traversal(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid run id"):
            self.store.load("../outside")

    def test_add_source_result_persists_source_evidence(self) -> None:
        record = self.store.create("task")
        result = SourceResult(
            source="github",
            status="ok",
            checked_at="now",
            evidence=[
                Evidence(
                    source="github",
                    url="https://github.com/Owner/Repo",
                    observed_at="now",
                    claim="Tests exist",
                    candidate="Owner/Repo",
                    signal_type="code",
                )
            ],
            edges=[
                {
                    "kind": "fork-of",
                    "source": "owner/fork",
                    "target": "owner/repo",
                    "feature": "",
                }
            ],
        )
        updated = self.store.add_source_result(record.run_id, result)
        self.assertEqual(updated.source_results[0].source, "github")
        self.assertIn("owner/repo", updated.candidates)
        self.assertEqual(updated.edges[0]["kind"], "fork-of")

    def test_add_source_result_rejects_secret_before_persisting(self) -> None:
        record = self.store.create("task")
        with self.assertRaisesRegex(
            ValueError, "run record contains forbidden secret material"
        ):
            self.store.add_source_result(
                record.run_id,
                SourceResult(
                    source="github",
                    status="error",
                    checked_at="now",
                    error="adapter error: api_key=example",
                ),
            )
        saved = self.store.load(record.run_id)
        self.assertEqual(saved.source_results, [])

    def test_merging_discovery_does_not_restore_alternative_after_donor_role(
        self,
    ) -> None:
        record = self.store.create("task")
        self.store.update(
            record.run_id,
            {
                "candidates": {
                    "owner/repo": {
                        "candidate_id": "owner/repo",
                        "display_name": "Repo",
                        "roles": ["donor"],
                        "aliases": [],
                        "evidence": [],
                    }
                }
            },
        )
        result = SourceResult(
            source="github",
            status="ok",
            checked_at="now",
            evidence=[
                Evidence(
                    source="github",
                    url="https://github.com/owner/repo",
                    observed_at="now",
                    claim="Source code",
                    candidate="owner/repo",
                )
            ],
        )
        updated = self.store.add_source_result(record.run_id, result)
        self.assertEqual(updated.candidates["owner/repo"].roles, ["donor"])

    def test_update_persists_context_research_and_graph_state(self) -> None:
        record = self.store.create("task")
        updated = self.store.update(
            record.run_id,
            {
                "context": {"request": "task", "project": "wheel"},
                "families": [{"name": "portable runtimes"}],
                "user_answers": [{"question": "scope", "answer": "global"}],
                "tool_snapshot": {"commands": [{"name": "git", "status": "ok"}]},
                "candidates": {
                    "owner/repo": {
                        "candidate_id": "owner/repo",
                        "display_name": "Repo",
                        "roles": ["base"],
                        "aliases": ["Owner/Repo"],
                        "evidence": [],
                    }
                },
                "edges": [
                    {
                        "kind": "extends",
                        "source": "owner/plugin",
                        "target": "owner/repo",
                        "feature": "runtime",
                    }
                ],
            },
        )
        self.assertEqual(updated.context["project"], "wheel")
        self.assertEqual(updated.families[0]["name"], "portable runtimes")
        self.assertEqual(updated.user_answers[0]["answer"], "global")
        self.assertEqual(updated.tool_snapshot["commands"][0]["name"], "git")
        self.assertEqual(updated.candidates["owner/repo"].roles, ["base"])
        self.assertEqual(updated.edges[0]["kind"], "extends")

    def test_save_keeps_preexisting_deterministic_tmp_file(self) -> None:
        record = self.store.create("task")
        path = self.home / "runs" / f"{record.run_id}.json"
        stale = path.with_suffix(".tmp")
        stale.write_text("do not replace", encoding="utf-8")
        record.task = "changed task"
        self.store.save(record)
        self.assertEqual(stale.read_text(encoding="utf-8"), "do not replace")

    def test_cache_rejects_symlink_escape(self) -> None:
        outside = Path(self.tempdir.name) / "outside"
        outside.mkdir()
        self.home.mkdir()
        try:
            (self.home / "cache").symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "escapes WHEEL_HOME"):
            save_maturity_cache({}, self.home)

    def test_containment_rejects_resolved_escape_before_creating_parent(self) -> None:
        root = Path(self.tempdir.name) / "project"
        candidate = root.resolve() / ".wheel" / "decisions" / "record.md"
        outside = Path(self.tempdir.name) / "outside" / "record.md"
        original_resolve = Path.resolve

        def resolve(path: Path, strict: bool = False) -> Path:
            if path == candidate:
                return outside
            return original_resolve(path, strict=strict)

        with patch("scripts.wheel.Path.resolve", autospec=True, side_effect=resolve):
            with self.assertRaisesRegex(ValueError, "escapes"):
                _contained_path(
                    root, Path(".wheel") / "decisions" / "record.md", "escapes"
                )
        self.assertFalse((root / ".wheel").exists())

    def _start_lock_holder(
        self, path: Path, hold_seconds: float = 30.0
    ) -> subprocess.Popen[str]:
        program = "\n".join(
            (
                "from pathlib import Path",
                "import sys",
                "import time",
                "from scripts.wheel import _file_lock",
                "with _file_lock(Path(sys.argv[1]), timeout=5):",
                "    print('acquired', flush=True)",
                f"    time.sleep({hold_seconds!r})",
            )
        )
        process = subprocess.Popen(
            [sys.executable, "-c", program, os.fspath(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertIsNotNone(process.stdout)
        self.assertEqual(process.stdout.readline().strip(), "acquired")
        return process

    def test_file_lock_blocks_a_separate_process_until_timeout(self) -> None:
        path = Path(self.tempdir.name) / "record.json"
        holder = self._start_lock_holder(path)
        try:
            with self.assertRaisesRegex(TimeoutError, "timed out waiting for lock"):
                with _file_lock(path, timeout=0.1):
                    pass
        finally:
            holder.terminate()
            holder.communicate(timeout=5)

    def test_file_lock_releases_after_context_exit(self) -> None:
        path = Path(self.tempdir.name) / "record.json"
        with _file_lock(path):
            self.assertTrue(path.with_name(f".{path.name}.lock").is_file())
        with _file_lock(path, timeout=0):
            pass

    def test_terminated_lock_holder_does_not_block_next_acquisition(self) -> None:
        path = Path(self.tempdir.name) / "record.json"
        holder = self._start_lock_holder(path)
        try:
            holder.terminate()
            holder.communicate(timeout=5)
            with _file_lock(path, timeout=2):
                pass
        finally:
            if holder.poll() is None:
                holder.terminate()
                holder.communicate(timeout=5)

    def test_load_config_applies_defaults_and_validates_custom_values(self) -> None:
        self.assertEqual(
            load_config(self.home), {"lookback_days": 90, "telegram_channels": []}
        )
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(
            '{"lookback_days": 30, "telegram_channels": ["wheel_news"]}',
            encoding="utf-8",
        )
        self.assertEqual(
            load_config(self.home),
            {"lookback_days": 30, "telegram_channels": ["wheel_news"]},
        )


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        dependency_patcher = patch(
            "scripts.wheel.dependency_status",
            return_value={
                "id": "donsetch",
                "status": "missing",
                "path": "/managed/donsetch",
                "version": "",
                "tested_version": "3.4.4",
                "latest_version": "",
                "checked_at": "2026-01-01T00:00:00Z",
                "detail": "managed version is not installed",
            },
        )
        self.dependency_status = dependency_patcher.start()
        self.addCleanup(dependency_patcher.stop)

    @patch.dict(
        "scripts.wheel.os.environ",
        {"COMSPEC": r"C:\\Windows\\System32\\cmd.exe"},
        clear=False,
    )
    @patch("scripts.wheel.os.name", "nt")
    @patch("scripts.wheel.subprocess.run")
    @patch("scripts.wheel.shutil.which")
    def test_windows_cmd_wrapper_uses_comspec_with_only_fixed_version_args(
        self, which: object, run: object
    ) -> None:
        wrapper = r"C:\\Tools & Safe\\opencli.CMD"
        which.side_effect = lambda command: (
            wrapper if command == "opencli" else rf"C:\\Tools\\{command}.exe"
        )  # type: ignore[attr-defined]
        run.side_effect = lambda command, **_: CompletedProcess(
            command, 0, "version 1.0", ""
        )  # type: ignore[attr-defined]

        result = run_doctor()

        opencli = next(item for item in result["commands"] if item["name"] == "opencli")
        command = next(
            call.args[0]  # type: ignore[attr-defined]
            for call in run.call_args_list  # type: ignore[attr-defined]
            if call.args[0][0] == r"C:\\Windows\\System32\\cmd.exe"
        )
        resolved_wrapper = os.path.realpath(wrapper)
        self.assertEqual(opencli["status"], "ok")
        self.assertEqual(
            command,
            (
                r"C:\\Windows\\System32\\cmd.exe",
                "/d",
                "/s",
                "/c",
                subprocess.list2cmdline((resolved_wrapper, "--version")),
            ),
        )
        self.assertNotIn("untrusted", command[-1])

    @patch("scripts.wheel.subprocess.run")
    @patch("scripts.wheel.shutil.which")
    def test_reports_available_command_without_sensitive_output(
        self, which: object, run: object
    ) -> None:
        which.side_effect = lambda command: "/tools/" + command  # type: ignore[attr-defined]
        run.return_value = CompletedProcess(
            [], 0, "git version 2.48.0\ntoken=never-return", ""
        )  # type: ignore[attr-defined]
        result = run_doctor()
        git = next(item for item in result["commands"] if item["name"] == "git")
        self.assertEqual(git["status"], "ok")
        self.assertEqual(git["version"], "git version 2.48.0")
        self.assertNotIn("never-return", json.dumps(result))

    @patch("scripts.wheel.subprocess.run")
    @patch("scripts.wheel.shutil.which", return_value=None)
    def test_reports_missing_command(self, which: object, run: object) -> None:
        result = run_doctor()
        ordinary = [
            item for item in result["commands"] if item["name"] != "donsetch-managed"
        ]
        self.assertTrue(all(item["status"] == "unavailable" for item in ordinary))
        managed = next(
            item for item in result["commands"] if item["name"] == "donsetch-managed"
        )
        self.assertEqual(managed["status"], "missing")
        self.dependency_status.assert_called_once_with(None)
        run.assert_not_called()  # type: ignore[attr-defined]

    @patch(
        "scripts.wheel.subprocess.run",
        side_effect=TimeoutExpired(["git", "--version"], 5),
    )
    @patch("scripts.wheel.shutil.which", return_value="/tools/git")
    def test_reports_timeout_without_command_output(
        self, which: object, run: object
    ) -> None:
        result = run_doctor()
        git = next(item for item in result["commands"] if item["name"] == "git")
        self.assertEqual(git["status"], "error")
        self.assertEqual(git["detail"], "version command timed out")

    @patch(
        "scripts.wheel.subprocess.run",
        return_value=CompletedProcess([], 1, "", "shim failed"),
    )
    @patch(
        "scripts.wheel.shutil.which",
        side_effect=lambda name: "/broken/donsetch" if name == "donsetch" else None,
    )
    @patch(
        "scripts.wheel.dependency_status",
        return_value={
            "status": "current",
            "path": "/managed/donsetch",
            "version": "3.4.4",
            "tested_version": "3.4.4",
            "latest_version": "",
            "checked_at": "2026-01-01T00:00:00Z",
            "detail": "",
        },
    )
    def test_managed_doctor_keeps_current_binary_over_broken_global_shim(
        self, status: object, which: object, run: object
    ) -> None:
        result = run_doctor()
        managed = next(
            item for item in result["commands"] if item["name"] == "donsetch-managed"
        )
        self.assertEqual(managed["status"], "current")
        self.assertEqual(managed["path"], "/managed/donsetch")
        self.assertEqual(managed["detail"], "global command is broken and ignored")


class DependencyManifestTests(unittest.TestCase):
    def test_manifest_maps_each_supported_platform(self) -> None:
        manifest = load_dependency_manifest()
        expected = {
            ("Windows", "AMD64"),
            ("Linux", "x86_64"),
            ("Linux", "aarch64"),
            ("Darwin", "x86_64"),
            ("Darwin", "arm64"),
        }
        self.assertEqual(
            {
                tuple((item["system"], item["machine"]))
                for item in manifest["dependencies"][0]["platforms"]
            },
            expected,
        )
        for system, machine in expected:
            self.assertIsNotNone(dependency_platform(manifest, system, machine))
        self.assertIsNone(dependency_platform(manifest, "FreeBSD", "amd64"))

    def test_manifest_rejects_extra_fields_and_bad_required_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dependencies.json"
            data = load_dependency_manifest()
            data["dependencies"][0]["platforms"][0]["required_files"] = ["../escape"]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "required_files"):
                load_dependency_manifest(path)

    def test_manifest_accepts_zero_major_semver(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dependencies.json"
            data = load_dependency_manifest()
            data["dependencies"][0]["tested_version"] = "0.1.0"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(
                load_dependency_manifest(path)["dependencies"][0]["tested_version"],
                "0.1.0",
            )


class DependencyVersionTests(unittest.TestCase):
    def test_version_parser_accepts_upstream_multiline_banner_only(self) -> None:
        output = (
            "DonSeTch 3.4.4\n----------------\nbuild: release\nupdate check: disabled\n"
        )
        self.assertEqual(_version_from_output(output), "3.4.4")
        self.assertEqual(_version_from_output("3.4.4"), "3.4.4")

    def test_version_parser_rejects_unrelated_embedded_numbers(self) -> None:
        self.assertIsNone(_version_from_output("build 3.4.4 complete"))
        self.assertIsNone(_version_from_output("heading\nversion 3.4.4\n"))


class DependencyRuntimeTests(unittest.TestCase):
    def _archive(self, members: list[tuple[str, bytes, bytes | None]]) -> bytes:
        stream = BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name, content, typeflag in members:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                if typeflag is not None:
                    info.type = typeflag
                archive.addfile(info, BytesIO(content) if info.isfile() else None)
        return stream.getvalue()

    def _manifest(
        self, archive: bytes, required_files: list[str] | None = None
    ) -> dict[str, object]:
        data = load_dependency_manifest()
        platform_item = next(
            item
            for item in data["dependencies"][0]["platforms"]
            if (item["system"], item["machine"]) == ("Linux", "aarch64")
        )
        platform_item["sha256"] = hashlib.sha256(archive).hexdigest()
        if required_files is not None:
            platform_item["required_files"] = required_files
        return data

    def _write_manifest(
        self, path: Path, archive: bytes, required_files: list[str] | None = None
    ) -> None:
        path.write_text(
            json.dumps(self._manifest(archive, required_files)), encoding="utf-8"
        )

    def test_bounded_download_rejects_untrusted_redirect_and_oversized_responses(
        self,
    ) -> None:
        class Response:
            def __init__(self, payload: bytes, length: str | None = None) -> None:
                self.payload = payload
                self.offset = 0
                self.reads = 0
                self.headers = {} if length is None else {"Content-Length": length}

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self, size: int) -> bytes:
                self.reads += 1
                chunk = self.payload[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

        class Opener:
            def __init__(self, response: Response) -> None:
                self.response = response

            def open(self, _request: object, timeout: int) -> Response:
                if timeout != 30:
                    raise AssertionError("unexpected timeout")
                return self.response

        header_response = Response(b"", "11")
        with patch(
            "scripts.wheel.urllib_request.build_opener",
            return_value=Opener(header_response),
        ) as builder:
            with self.assertRaisesRegex(ValueError, "size limit"):
                _bounded_https_download("https://github.com/asset", {"github.com"}, 10)
            redirect = builder.call_args.args[0]
            self.assertIsInstance(redirect, _HTTPSRedirect)
            with self.assertRaises(HTTPError):
                redirect.redirect_request(
                    None, None, 302, "redirect", {}, "https://evil.example/asset"
                )
        self.assertEqual(header_response.reads, 0)
        streaming_response = Response(b"x" * 11)
        with patch(
            "scripts.wheel.urllib_request.build_opener",
            return_value=Opener(streaming_response),
        ):
            with self.assertRaisesRegex(ValueError, "size limit"):
                _bounded_https_download("https://github.com/asset", {"github.com"}, 10)
        self.assertGreater(streaming_response.reads, 0)

    def test_extractor_rejects_traversal_links_devices_and_extra_files(self) -> None:
        cases = {
            "traversal": [("../escape", b"x", None)],
            "absolute": [("/escape", b"x", None)],
            "nested": [("nested/donsetch", b"x", None)],
            "symlink": [("donsetch", b"", tarfile.SYMTYPE)],
            "hardlink": [("donsetch", b"", tarfile.LNKTYPE)],
            "device": [("donsetch", b"", tarfile.CHRTYPE)],
            "extra": [("donsetch", b"x", None), ("extra", b"x", None)],
            "duplicate": [("donsetch", b"x", None), ("donsetch", b"x", None)],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, members in cases.items():
                archive = root / f"{name}.tar.gz"
                archive.write_bytes(self._archive(members))
                output = root / f"out-{name}"
                output.mkdir()
                with self.assertRaises(ValueError):
                    _extract_required_files(archive, output, ["donsetch"])
                self.assertEqual(list(output.iterdir()), [])

    def test_extractor_rejects_missing_required_file_and_accepts_exact_platform_payloads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing_archive = root / "missing.tar.gz"
            missing_archive.write_bytes(self._archive([("donsetch", b"binary", None)]))
            output = root / "missing"
            output.mkdir()
            with self.assertRaises(ValueError):
                _extract_required_files(
                    missing_archive, output, ["donsetch", "libonnxruntime.so"]
                )
            self.assertEqual(list(output.iterdir()), [])
            for name, members, required in (
                (
                    "windows",
                    [
                        ("donsetch.exe", b"binary", None),
                        ("pdfium.dll", b"library", None),
                    ],
                    ["donsetch.exe", "pdfium.dll"],
                ),
                (
                    "linux",
                    [
                        ("donsetch", b"binary", None),
                        ("libonnxruntime.so", b"library", None),
                    ],
                    ["donsetch", "libonnxruntime.so"],
                ),
            ):
                archive = root / f"{name}.tar.gz"
                archive.write_bytes(self._archive(members))
                destination = root / name
                destination.mkdir()
                _extract_required_files(archive, destination, required)
                self.assertEqual(
                    {item.name for item in destination.iterdir()}, set(required)
                )

    def test_checksum_failure_does_not_publish(self) -> None:
        archive = self._archive([("donsetch", b"binary", None)])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "dependencies.json"
            self._write_manifest(manifest, archive)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["dependencies"][0]["platforms"][0]["sha256"] = "0" * 64
            manifest.write_text(json.dumps(data), encoding="utf-8")
            result = ensure_dependency(
                root / "home",
                manifest_path=manifest,
                system="Linux",
                machine="aarch64",
                download=lambda *_: archive,
            )
            self.assertEqual(result["status"], "error")
            self.assertFalse(
                (root / "home" / "dependencies" / "donsetch" / "3.4.4").exists()
            )

    def test_cancelled_install_removes_stage_and_reraises(self) -> None:
        archive = self._archive([("donsetch", b"binary", None)])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "dependencies.json"
            self._write_manifest(manifest, archive)
            with self.assertRaises(KeyboardInterrupt):
                ensure_dependency(
                    root / "home",
                    manifest_path=manifest,
                    system="Linux",
                    machine="aarch64",
                    download=lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()),
                )
            dependency_root = root / "home" / "dependencies" / "donsetch"
            self.assertFalse(
                any(
                    path.name.startswith(".3.4.4.") and path.name != ".3.4.4.lock"
                    for path in dependency_root.iterdir()
                )
            )

    @patch("scripts.wheel._probe_managed_binary", return_value=(True, "3.4.4"))
    def test_installer_publishes_staged_version_atomically(self, probe: object) -> None:
        archive = self._archive([("donsetch", b"binary", None)])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "dependencies.json"
            self._write_manifest(manifest, archive)
            requested_urls: list[str] = []

            def download(url: str, _hosts: set[str], _limit: int) -> bytes:
                requested_urls.append(url)
                return archive

            result = ensure_dependency(
                root / "home",
                manifest_path=manifest,
                system="Linux",
                machine="aarch64",
                download=download,
            )
            self.assertEqual(result["status"], "current")
            self.assertEqual(
                requested_urls,
                [
                    "https://github.com/dondai44423/donsetch/releases/download/v3.4.4/donsetch-linux-arm64.tar.gz"
                ],
            )
            version_root = root / "home" / "dependencies" / "donsetch" / "3.4.4"
            self.assertEqual((version_root / "donsetch").read_bytes(), b"binary")
            self.assertFalse(
                any(
                    path.name.startswith(".3.4.4.") and path.name != ".3.4.4.lock"
                    for path in version_root.parent.iterdir()
                )
            )

    def test_states_cover_missing_repair_current_update_and_unavailable(self) -> None:
        archive = self._archive([("donsetch", b"binary", None)])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "dependencies.json"
            self._write_manifest(manifest, archive)
            home = root / "home"
            kwargs = {
                "manifest_path": manifest,
                "system": "Linux",
                "machine": "aarch64",
            }
            self.assertEqual(dependency_status(home, **kwargs)["status"], "missing")
            version_root = home / "dependencies" / "donsetch" / "3.4.4"
            version_root.mkdir(parents=True)
            self.assertEqual(
                dependency_status(home, **kwargs)["status"], "repair_required"
            )
            binary = version_root / "donsetch"
            binary.write_bytes(b"binary")
            with patch(
                "scripts.wheel._probe_managed_binary",
                return_value=(False, "managed version mismatch"),
            ):
                self.assertEqual(
                    dependency_status(home, **kwargs)["status"], "repair_required"
                )
            with (
                patch(
                    "scripts.wheel._probe_managed_binary", return_value=(True, "3.4.4")
                ),
                patch(
                    "scripts.wheel._latest_release_status",
                    return_value={
                        "latest_version": "3.5.0",
                        "checked_at": "2026-01-01T00:00:00Z",
                        "detail": "",
                    },
                ),
            ):
                self.assertEqual(
                    dependency_status(home, check_latest=True, **kwargs)["status"],
                    "update_available",
                )
            self.assertEqual(
                dependency_status(
                    home, manifest_path=manifest, system="Other", machine="cpu"
                )["status"],
                "unavailable",
            )

    def test_required_companion_file_controls_current_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            version_root = home / "dependencies" / "donsetch" / "3.4.4"
            version_root.mkdir(parents=True)
            (version_root / "donsetch").write_bytes(b"binary")
            kwargs = {"system": "Linux", "machine": "x86_64"}
            self.assertEqual(
                dependency_status(home, **kwargs)["status"], "repair_required"
            )
            (version_root / "libonnxruntime.so").write_bytes(b"library")
            with patch(
                "scripts.wheel._probe_managed_binary", return_value=(True, "3.4.4")
            ):
                self.assertEqual(dependency_status(home, **kwargs)["status"], "current")

    @patch(
        "scripts.wheel._bounded_https_download",
        side_effect=AssertionError("network must not be used"),
    )
    def test_fresh_latest_cache_skips_network(self, download: object) -> None:
        archive = self._archive([("donsetch", b"binary", None)])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "dependencies.json"
            self._write_manifest(manifest_path, archive)
            manifest = load_dependency_manifest(manifest_path)
            cache = root / "home" / "dependencies" / "donsetch" / "latest-release.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(
                json.dumps({"version": "3.5.0", "checked_at": "2026-01-01T00:00:00Z"}),
                encoding="utf-8",
            )
            result = _latest_release_status(root / "home", manifest, now=1767225600.0)
            self.assertEqual(result["latest_version"], "3.5.0")
            download.assert_not_called()  # type: ignore[attr-defined]

    def test_dependencies_cli_default_is_json_diagnostic_without_creating_home(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "missing-home"
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(["dependencies", "--json", "--home", str(home)]), 0
                )
            report = json.loads(output.getvalue())
            self.assertEqual(report["status"], "missing")
            self.assertFalse(home.exists())

    @patch(
        "scripts.wheel.dependency_status", return_value={"status": "update_available"}
    )
    @patch("scripts.wheel.ensure_dependency", return_value={"status": "current"})
    def test_dependencies_ensure_and_check_latest_composes_status(
        self, ensure: object, status: object
    ) -> None:
        with redirect_stdout(StringIO()):
            self.assertEqual(
                main(["dependencies", "--ensure", "--check-latest", "--json"]), 0
            )
        ensure.assert_called_once_with(None)  # type: ignore[attr-defined]
        status.assert_called_once_with(None, check_latest=True)  # type: ignore[attr-defined]

    @patch(
        "scripts.wheel.ensure_dependency",
        return_value={
            "status": "error",
            "id": "donsetch",
            "version": "",
            "tested_version": "3.4.4",
            "detail": "checksum mismatch",
        },
    )
    def test_dependencies_ensure_failure_returns_nonzero(self, ensure: object) -> None:
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["dependencies", "--ensure", "--json"]), 1)
        ensure.assert_called_once_with(None)  # type: ignore[attr-defined]

    @patch(
        "scripts.wheel.donsetch_fetch",
        return_value={
            "status": "partial",
            "content_ok": True,
            "detail": "required content is incomplete",
            "data": {},
        },
    )
    def test_read_url_cli_uses_managed_fetch_helper(self, fetch: object) -> None:
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "read-url",
                        "https://example.com/repo",
                        "--focus",
                        "metrics",
                        "--require-field",
                        "stars",
                        "--require-field",
                        "period",
                        "--json",
                    ]
                ),
                0,
            )
        self.assertEqual(json.loads(output.getvalue())["status"], "partial")
        fetch.assert_called_once_with(  # type: ignore[attr-defined]
            "https://example.com/repo",
            None,
            required_fields=("stars", "period"),
            focus="metrics",
        )

    @patch("scripts.wheel.subprocess.run")
    @patch(
        "scripts.wheel.dependency_status",
        return_value={"status": "current", "path": "/managed/donsetch"},
    )
    def test_fetch_preserves_json_and_marks_missing_metric_partial(
        self, status: object, run: object
    ) -> None:
        run.return_value = CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "ok": True,
                    "content": "page text",
                    "meta": {
                        "content_ok": True,
                        "quality": "browser",
                        "via": "browser",
                        "next_offset": None,
                        "thin": False,
                        "ms": 42,
                    },
                }
            ),
            "[ghost] warning",
        )  # type: ignore[attr-defined]
        result = donsetch_fetch(
            "https://example.com/repo", required_fields=("trend_metric",)
        )
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["content_ok"])
        self.assertEqual(result["stderr_category"], "present")
        self.assertEqual(result["quality"], "browser")
        self.assertEqual(result["via"], "browser")
        self.assertEqual(result["elapsed_ms"], 42)
        command = run.call_args.args[0]  # type: ignore[attr-defined]
        self.assertEqual(
            command[:4],
            ["/managed/donsetch", "fetch", "https://example.com/repo", "--json"],
        )
        self.assertNotIn("--focus", command)
        run.reset_mock()  # type: ignore[attr-defined]
        donsetch_fetch("https://example.com/repo", focus="trend metrics")
        focused_command = run.call_args.args[0]  # type: ignore[attr-defined]
        self.assertEqual(focused_command[-2:], ["--focus", "trend metrics"])
        run.return_value = CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "ok": True,
                    "content": "page text",
                    "meta": {"content_ok": True, "next_offset": 100, "thin": False},
                }
            ),
            "",
        )  # type: ignore[attr-defined]
        paginated = donsetch_fetch("https://example.com/repo")
        self.assertEqual(paginated["status"], "partial")
        self.assertTrue(paginated["pagination"])
        self.assertTrue(paginated["truncated"])
        run.return_value = CompletedProcess(
            [],
            3,
            json.dumps(
                {
                    "ok": False,
                    "error": {"kind": "walled", "message": "ignored"},
                    "meta": {"code": "wall.challenge"},
                }
            ),
            "",
        )  # type: ignore[attr-defined]
        failed = donsetch_fetch("https://example.com/repo")
        self.assertEqual(failed["status"], "blocked")
        self.assertEqual(failed["error_kind"], "walled")
        self.assertEqual(failed["error_code"], "wall.challenge")
        run.return_value = CompletedProcess(
            [],
            2,
            json.dumps(
                {
                    "ok": False,
                    "error": {"kind": "transient", "message": "ignored"},
                    "meta": {"code": "captcha.required"},
                }
            ),
            "",
        )  # type: ignore[attr-defined]
        captcha = donsetch_fetch("https://example.com/repo")
        self.assertEqual(captcha["status"], "blocked")
        self.assertEqual(captcha["error_kind"], "transient")
        self.assertEqual(captcha["error_code"], "captcha.required")


class MaturityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = "2026-08-15T00:00:00Z"
        self.perfect = {
            "pushed_at": "2026-08-01T00:00:00Z",
            "stars": 9000,
            "releases_12mo": 12,
            "license": "MIT",
            "archived": False,
            "contributors": 40,
        }

    def test_preserves_existing_maturity_levels(self) -> None:
        self.assertEqual(
            maturity_level(maturity_flags(self.perfect, self.now), self.perfect), "A"
        )
        no_releases = {**self.perfect, "releases_12mo": 0}
        self.assertEqual(
            maturity_level(maturity_flags(no_releases, self.now), no_releases), "B"
        )
        hobby = {**no_releases, "stars": 12, "contributors": 1}
        self.assertEqual(maturity_level(maturity_flags(hobby, self.now), hobby), "C")
        dead = {**hobby, "pushed_at": "2023-01-01T00:00:00Z", "license": None}
        self.assertEqual(maturity_level(maturity_flags(dead, self.now), dead), "D")

    def test_archived_and_unlicensed_repositories_are_not_safe(self) -> None:
        archived = {**self.perfect, "archived": True}
        self.assertEqual(
            maturity_level(maturity_flags(archived, self.now), archived), "D"
        )
        self.assertFalse(maturity_flags(archived, self.now)["safe"])
        self.assertFalse(
            maturity_flags({**self.perfect, "license": None}, self.now)["safe"]
        )

    def test_operational_gap_matches_deployment_signals(self) -> None:
        self.assertEqual(operational_gap(["docker-compose.yml", "src"])["gap"], "none")
        self.assertEqual(operational_gap(["Chart.yaml"])["gap"], "none")
        self.assertEqual(operational_gap(["Dockerfile", "src"])["gap"], "small")
        self.assertEqual(operational_gap(["package.json"])["gap"], "small")
        self.assertEqual(operational_gap(["README.md", "main.c"])["gap"], "large")

    def test_skills_repositories_skip_release_cadence(self) -> None:
        self.assertEqual(kind_of(["SKILL.md", "references"]), "skills")
        self.assertEqual(kind_of([".codex-plugin", "README.md"]), "skills")
        self.assertEqual(kind_of([".claude-plugin", "README.md"]), "skills")
        self.assertEqual(
            kind_of(["skills", "charts", "docker", "package.json"]), "service"
        )
        skill_repo = {**self.perfect, "kind": "skills", "releases_12mo": 0}
        self.assertEqual(len(maturity_flags(skill_repo, self.now)), 4)
        self.assertEqual(
            maturity_level(maturity_flags(skill_repo, self.now), skill_repo), "A"
        )

    @patch("scripts.wheel._gh_api")
    def test_auxiliary_github_failures_degrade_maturity_without_aborting(
        self, gh_api: object
    ) -> None:
        gh_api.side_effect = [  # type: ignore[attr-defined]
            {
                "default_branch": "main",
                "stargazers_count": 9000,
                "pushed_at": "2026-08-01T00:00:00Z",
                "archived": False,
                "license": {"spdx_id": "MIT"},
            },
            None,
            None,
            None,
        ]
        entry = probe_repository("Owner/Repo", self.now)
        self.assertEqual(entry["slug"], "owner/repo")
        self.assertEqual(entry["releases_12mo"], 0)
        self.assertEqual(entry["contributors"], 0)
        self.assertEqual(entry["ops"]["gap"], "large")
        self.assertEqual(entry["level"], "C")
        self.assertIn("emerging_gem", entry)
        self.assertFalse(entry["emerging_gem"])
        self.assertFalse(detect_emerging_gem(entry, self.now))


class DecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "wheel-home"
        self.decision = {
            "accepted": True,
            "task": "portable market research",
            "coverage": "COMPLETE",
            "implementation_mode": "extend-core",
            "verdict": "Adopt Wheel runtime",
            "picked": "kalpakprod/wheel",
            "maturity": "C",
            "date": "2026-08-30",
            "candidates": ["kalpakprod/wheel (base)", "openexecutive (donor)"],
            "core": "https://github.com/KalpakProd/Wheel.git",
            "upstream_or_fork": "kalpakprod/wheel",
            "donors": ["OpenExecutive: source adapter"],
            "integrations": ["plugin"],
            "licenses": ["MIT"],
            "rejection_reasons": ["Alternative lacks portable state"],
            "adjacent_capabilities": ["decision journal"],
            "not_building": ["background crawler"],
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_render_decision_has_all_clausative_sections(self) -> None:
        rendered = render_decision(self.decision)
        self.assertTrue(rendered.startswith("---\n"))
        self.assertIn("name: kalpakprod-wheel", rendered)
        self.assertIn("coverage: COMPLETE", rendered)
        self.assertIn("verdict: Adopt Wheel runtime", rendered)
        for section in (
            "Request",
            "Coverage",
            "Candidates",
            "Decided",
            "Core and integration",
            "Donors and licenses",
            "Rejected alternatives",
            "Already included, enable?",
            "Later, do not build",
        ):
            self.assertIn(f"## {section}", rendered)

    def test_same_stable_slug_updates_global_decision_not_synonym(self) -> None:
        first = record_decision(self.decision, "global", home=self.home)
        updated = {**self.decision, "verdict": "Use the Wheel runtime"}
        second = record_decision(updated, "global", home=self.home)
        self.assertEqual(first, second)
        records = [
            path
            for path in (self.home / "decisions").glob("*.md")
            if path.name != "INDEX.md"
        ]
        self.assertEqual(len(records), 1)
        self.assertTrue(second.read_text(encoding="utf-8").startswith("---\n"))
        self.assertIn("Use the Wheel runtime", second.read_text(encoding="utf-8"))
        index = (self.home / "decisions" / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("[Use the Wheel runtime](kalpakprod-wheel.md)", index)

    def test_project_scope_is_contained_in_project_wheel_directory(self) -> None:
        project_root = Path(self.tempdir.name) / "project"
        path = record_decision(
            self.decision, "project", project_root=project_root, home=self.home
        )
        self.assertTrue(
            path.is_relative_to(project_root.resolve() / ".wheel" / "decisions")
        )

    def test_unaccepted_recommendation_cannot_be_recorded(self) -> None:
        with self.assertRaisesRegex(ValueError, "accepted"):
            record_decision(
                {**self.decision, "accepted": False}, "global", home=self.home
            )

    def test_decision_rejects_unknown_coverage_and_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "coverage"):
            render_decision({**self.decision, "coverage": "UNKNOWN"})
        with self.assertRaisesRegex(ValueError, "implementation mode"):
            render_decision({**self.decision, "implementation_mode": "plugin"})

    def test_decision_rejects_nested_secret_key_and_credential_pattern(self) -> None:
        with self.assertRaisesRegex(ValueError, "secret"):
            render_decision({**self.decision, "donors": [{"token": "not-a-secret"}]})
        with self.assertRaisesRegex(ValueError, "secret"):
            render_decision(
                {
                    **self.decision,
                    "rejection_reasons": ["Authorization: Bearer redacted"],
                }
            )
        with self.assertRaisesRegex(ValueError, "secret"):
            render_decision({**self.decision, "donors": ["ghp_example"]})
        with self.assertRaisesRegex(ValueError, "secret"):
            render_decision({**self.decision, "donors": ["github_pat_example"]})
        with self.assertRaisesRegex(ValueError, "secret"):
            render_decision(
                {**self.decision, "donors": ["adapter error: api_key=example"]}
            )

    def test_decision_slug_rejects_index_and_windows_device_names_before_locking(
        self,
    ) -> None:
        with patch("scripts.wheel._file_lock") as file_lock:
            with self.assertRaisesRegex(ValueError, "reserved"):
                record_decision(
                    {**self.decision, "slug": "index"}, "global", home=self.home
                )
        file_lock.assert_not_called()
        for slug in (
            "index",
            "con",
            "prn",
            "aux",
            "nul",
            "com1",
            "com9",
            "lpt1",
            "lpt9",
        ):
            with self.subTest(slug=slug):
                with self.assertRaisesRegex(ValueError, "reserved"):
                    _decision_slug({**self.decision, "slug": slug})
        with self.assertRaisesRegex(ValueError, "reserved"):
            _decision_slug({**self.decision, "core": "INDEX"})


class CliTests(unittest.TestCase):
    def test_init_run_prints_created_json_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(["init-run", "--task", "test task", "--home", temporary]), 0
                )
            path = Path(output.getvalue().strip())
            self.assertTrue(path.is_file())
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["state"], "context"
            )

    def test_add_source_inherits_source_result_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source_input = Path(temporary) / "source.json"
            source_input.write_text(
                json.dumps(
                    {
                        "source": "github",
                        "status": "ok",
                        "checked_at": "2026-08-30T00:00:00Z",
                        "evidence": [
                            {
                                "url": "https://example.test/open-executive",
                                "observed_at": "2026-08-30T00:00:00Z",
                                "claim": "Hosted product",
                                "candidate": "product:Open Executive",
                                "signal_type": "code",
                            },
                            {"malformed": True},
                        ],
                        "edges": [
                            {
                                "kind": "fork-of",
                                "source": "service:Managed Search",
                                "target": "product:Open Executive",
                                "feature": "",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(["init-run", "--task", "task", "--home", temporary]), 0
                )
            run_path = Path(output.getvalue().strip())
            run_id = json.loads(run_path.read_text(encoding="utf-8"))["run_id"]
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "add-source",
                            "--run-id",
                            run_id,
                            "--input",
                            str(source_input),
                            "--home",
                            temporary,
                        ]
                    ),
                    0,
                )
            saved = json.loads(run_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["source_results"][0]["evidence"][0]["source"], "github"
            )
            self.assertEqual(saved["source_results"][0]["status"], "partial")
            self.assertIn("evidence[1]", saved["source_results"][0]["error"])
            self.assertIn("product:open-executive", saved["candidates"])
            self.assertEqual(
                saved["candidates"]["product:open-executive"]["kind"], "product"
            )
            self.assertEqual(saved["edges"][0]["source"], "service:managed-search")
            self.assertEqual(saved["edges"][0]["kind"], "fork-of")

    def test_add_source_rejects_secret_adapter_error_or_detail_without_persisting(
        self,
    ) -> None:
        for field_name in ("error", "detail"):
            with (
                self.subTest(field_name=field_name),
                tempfile.TemporaryDirectory() as temporary,
            ):
                source_input = Path(temporary) / "source.json"
                source_input.write_text(
                    json.dumps(
                        {
                            "source": "github",
                            "status": "error",
                            "checked_at": "2026-08-30T00:00:00Z",
                            field_name: "adapter detail: api_key=example",
                        }
                    ),
                    encoding="utf-8",
                )
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(
                        main(["init-run", "--task", "task", "--home", temporary]), 0
                    )
                run_path = Path(output.getvalue().strip())
                run_id = json.loads(run_path.read_text(encoding="utf-8"))["run_id"]
                with self.assertRaisesRegex(
                    ValueError, "run record contains forbidden secret material"
                ):
                    main(
                        [
                            "add-source",
                            "--run-id",
                            run_id,
                            "--input",
                            str(source_input),
                            "--home",
                            temporary,
                        ]
                    )
                saved = json.loads(run_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["source_results"], [])
                self.assertNotIn("api_key", json.dumps(saved))

    def test_update_run_cli_validates_and_persists_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(["init-run", "--task", "task", "--home", temporary]), 0
                )
            run_path = Path(output.getvalue().strip())
            run_id = json.loads(run_path.read_text(encoding="utf-8"))["run_id"]
            update_input = Path(temporary) / "update.json"
            update_input.write_text(
                json.dumps(
                    {
                        "context": {"request": "task"},
                        "families": [{"name": "runtimes"}],
                        "candidates": {
                            "service:Managed Search": {
                                "candidate_id": "service:managed-search",
                                "display_name": "Managed Search",
                                "kind": "service",
                                "roles": ["sidecar"],
                                "aliases": [],
                                "evidence": [],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "update-run",
                            "--run-id",
                            run_id,
                            "--input",
                            str(update_input),
                            "--home",
                            temporary,
                        ]
                    ),
                    0,
                )
            saved = json.loads(run_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["context"]["request"], "task")
            self.assertEqual(saved["families"][0]["name"], "runtimes")
            self.assertEqual(
                saved["candidates"]["service:managed-search"]["kind"], "service"
            )

    def test_update_run_rejects_secret_adapter_detail_without_persisting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(["init-run", "--task", "task", "--home", temporary]), 0
                )
            run_path = Path(output.getvalue().strip())
            run_id = json.loads(run_path.read_text(encoding="utf-8"))["run_id"]
            update_input = Path(temporary) / "update.json"
            update_input.write_text(
                json.dumps({"context": {"adapter": {"detail": "api_key=example"}}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "run record contains forbidden secret material"
            ):
                main(
                    [
                        "update-run",
                        "--run-id",
                        run_id,
                        "--input",
                        str(update_input),
                        "--home",
                        temporary,
                    ]
                )
            saved = json.loads(run_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["context"], {})
            self.assertNotIn("api_key", json.dumps(saved))


class ExpertWheelExtensionTests(unittest.TestCase):
    def test_new_candidate_kinds_and_normalization(self) -> None:
        self.assertEqual(
            normalize_candidate_id("skill:Prompt Router"),
            ("skill", "skill:prompt-router"),
        )
        self.assertEqual(
            normalize_candidate_id("plugin:Vim Mode"),
            ("plugin", "plugin:vim-mode"),
        )
        self.assertEqual(
            normalize_candidate_id("mcp-server:PostgreSQL MCP"),
            ("mcp-server", "mcp-server:postgresql-mcp"),
        )
        self.assertEqual(
            normalize_candidate_id("design-system:Ant Design"),
            ("design-system", "design-system:ant-design"),
        )
        self.assertEqual(
            normalize_candidate_id("reference:Awesome System Design"),
            ("reference", "reference:awesome-system-design"),
        )

    @staticmethod
    def _expert_decision() -> dict:
        return {
            "accepted": True,
            "task": "build scalable event system",
            "coverage": "COMPLETE",
            "implementation_mode": "extend-core",
            "verdict": "Adopt Apache Kafka with custom sidecar",
            "picked": "apache/kafka",
            "maturity": "A",
            "date": "2026-08-30",
            "candidates": ["apache/kafka (base)"],
            "core": "https://github.com/apache/kafka.git",
            "upstream_or_fork": "apache/kafka",
            "donors": ["rabbitmq: consumer adapter"],
            "integrations": ["sidecar"],
            "licenses": ["Apache-2.0"],
            "rejection_reasons": ["RabbitMQ lacks log compaction"],
            "adjacent_capabilities": ["schema registry"],
            "not_building": ["custom broker"],
            "code_comparison": "```ts\nconst stream = kafka.consume('events');\n```",
            "evidence_gaps": [
                {"source": "reddit", "reason": "HTTP 403"},
            ],
            "explicit_sacrifice": ["High initial RAM overhead"],
            "tradeoff_matrix": [
                {
                    "axis": "Resource / RAM",
                    "candidate": "Heavy (JVM)",
                    "alternative": "Light (Erlang)",
                    "assessment": "Accepted for throughput",
                }
            ],
        }

    def test_candidate_id_rejects_malformed_typed_and_repo_values(self) -> None:
        with self.assertRaises(ValueError):
            normalize_candidate_id("unknown:thing")
        with self.assertRaises(ValueError):
            normalize_candidate_id("skill:")
        with self.assertRaises(ValueError):
            normalize_candidate_id("skill:" + "x" * 200)
        with self.assertRaises(ValueError):
            normalize_candidate_id("mcp-server")
        with self.assertRaises(ValueError):
            normalize_candidate_id("owner/re:po")
        with self.assertRaises(ValueError):
            normalize_candidate_id("own er/repo")
        with self.assertRaises(ValueError):
            normalize_candidate_id("-owner/repo")
        self.assertEqual(
            normalize_candidate_id("Ashishps1/awesome-system.design_resources"),
            ("repository", "ashishps1/awesome-system.design_resources"),
        )

    def test_sync_catalog_requires_https_and_bounded_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for hostile in (
                "file:///etc/passwd",
                "http://127.0.0.1/catalog.jsonl",
                "https:///catalog.jsonl",
            ):
                with self.assertRaisesRegex(ValueError, "https"):
                    sync_catalog(home=Path(temporary), edge_url=hostile)

    def test_catalog_download_is_allowlisted_and_redirect_safe(self) -> None:
        self.assertEqual(
            _require_catalog_url(DEFAULT_CATALOG_EDGE_URL), DEFAULT_CATALOG_EDGE_URL
        )
        default_hosts = _catalog_allowed_hosts(
            [DEFAULT_CATALOG_EDGE_URL, DEFAULT_CATALOG_FALLBACK_URL]
        )
        self.assertEqual(
            default_hosts, {"cdn.jsdelivr.net", "raw.githubusercontent.com"}
        )
        self.assertIn(
            "mirror.example",
            _catalog_allowed_hosts(["https://mirror.example/catalog.jsonl"]),
        )

        with unittest.mock.patch(
            "scripts.wheel._bounded_https_download", return_value=b"{}\n"
        ) as download:
            _fetch_catalog_payload(DEFAULT_CATALOG_EDGE_URL, set(default_hosts))
        url, hosts, limit = download.call_args.args
        self.assertEqual(url, DEFAULT_CATALOG_EDGE_URL)
        self.assertEqual(hosts, default_hosts)
        self.assertEqual(limit, CATALOG_MAX_BYTES)

        with self.assertRaisesRegex(ValueError, "allowlisted HTTPS"):
            _fetch_catalog_payload(
                "https://evil.example/catalog.jsonl", set(default_hosts)
            )

    def test_sync_catalog_cli_reports_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            buffer = io.StringIO()
            with (
                unittest.mock.patch(
                    "scripts.wheel._bounded_https_download",
                    side_effect=urllib_error.URLError("offline"),
                ),
                contextlib.redirect_stdout(buffer),
            ):
                code = main(
                    [
                        "sync-catalog",
                        "--home",
                        temporary,
                        "--edge-url",
                        "https://example.invalid/catalog.jsonl",
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["status"], "unavailable")
            self.assertEqual(payload["count"], 0)
            self.assertIn("offline", payload["error"])

    def test_render_decision_rejects_unsafe_matrix_and_snippet(self) -> None:
        escaped = self._expert_decision()
        escaped["tradeoff_matrix"] = [
            {
                "axis": "RAM | CPU",
                "candidate": "Heavy",
                "alternative": "Light",
                "assessment": "ok",
            }
        ]
        rendered = render_decision(escaped)
        self.assertIn("RAM \\| CPU", rendered)
        table_rows = [
            line for line in rendered.splitlines() if line.startswith("| RAM")
        ]
        self.assertEqual(len(table_rows), 1)
        self.assertEqual(table_rows[0].count(" | "), 3)

        multiline = self._expert_decision()
        multiline["tradeoff_matrix"] = [
            {
                "axis": "RAM\n## Injected",
                "candidate": "a",
                "alternative": "b",
                "assessment": "c",
            }
        ]
        with self.assertRaisesRegex(ValueError, "single-line"):
            render_decision(multiline)

        wrong_type = self._expert_decision()
        wrong_type["tradeoff_matrix"] = [
            {
                "axis": {"nested": 1},
                "candidate": "a",
                "alternative": "b",
                "assessment": "c",
            }
        ]
        with self.assertRaisesRegex(ValueError, "non-empty string"):
            render_decision(wrong_type)

        missing_column = self._expert_decision()
        missing_column["tradeoff_matrix"] = [{"axis": "a", "candidate": "b"}]
        with self.assertRaisesRegex(ValueError, "missing fields"):
            render_decision(missing_column)

        empty_matrix = self._expert_decision()
        empty_matrix["tradeoff_matrix"] = []
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            render_decision(empty_matrix)

        empty_sacrifice = self._expert_decision()
        empty_sacrifice["explicit_sacrifice"] = []
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            render_decision(empty_sacrifice)

        bad_snippet = self._expert_decision()
        bad_snippet["code_comparison"] = {"ts": "x"}
        with self.assertRaisesRegex(ValueError, "code_comparison"):
            render_decision(bad_snippet)

        yaml_sample = self._expert_decision()
        yaml_sample["code_comparison"] = "```yaml\n---\nkey: value\n```"
        self.assertIn("key: value", render_decision(yaml_sample))

    @staticmethod
    def _catalog_lines() -> str:
        rows = [
            {
                "slug": "punkpeye/awesome-mcp-servers",
                "kind": "reference",
                "capability": "mcp-servers",
                "display_name": "punkpeye/awesome-mcp-servers",
                "description": "Curated list of MCP servers",
                "url": "https://github.com/punkpeye/awesome-mcp-servers",
                "stars": 70000,
                "topics": ["mcp", "awesome"],
                "emerging_gem": False,
                "checked_at": "2026-08-30T00:00:00Z",
            },
            {
                "slug": "tiny/fresh-mcp",
                "kind": "mcp-server",
                "capability": "mcp-servers",
                "display_name": "tiny/fresh-mcp",
                "description": "Postgres bridge",
                "url": "https://github.com/tiny/fresh-mcp",
                "stars": 40,
                "topics": ["mcp", "postgres"],
                "emerging_gem": True,
                "checked_at": "2026-08-31T00:00:00Z",
            },
            {
                "slug": "big/design-kit",
                "kind": "design-system",
                "capability": "design-systems",
                "display_name": "big/design-kit",
                "description": "Component library",
                "url": "https://github.com/big/design-kit",
                "stars": 9000,
                "topics": ["design-system"],
                "emerging_gem": False,
                "checked_at": "2026-08-29T00:00:00Z",
            },
        ]
        return "".join(json.dumps(row) + "\n" for row in rows)

    def _catalog_home(self, temporary: str) -> Path:
        home = Path(temporary)
        cache_file = home / "cache" / "catalog.jsonl"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(self._catalog_lines(), encoding="utf-8")
        return home

    def test_search_catalog_filters_without_loading_everything(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = self._catalog_home(temporary)

            by_kind = search_catalog(kind="mcp-server", home=home)
            self.assertEqual(
                [item["slug"] for item in by_kind["matches"]], ["tiny/fresh-mcp"]
            )
            self.assertEqual(by_kind["catalog_records"], 3)

            by_capability = search_catalog(capability="mcp-servers", home=home)
            self.assertEqual(by_capability["matched"], 2)
            self.assertEqual(
                by_capability["matches"][0]["slug"], "punkpeye/awesome-mcp-servers"
            )

            by_topic = search_catalog(query="POSTGRES", home=home)
            self.assertEqual(
                [item["slug"] for item in by_topic["matches"]], ["tiny/fresh-mcp"]
            )

            by_description = search_catalog(query="component", home=home)
            self.assertEqual(
                [item["slug"] for item in by_description["matches"]], ["big/design-kit"]
            )

            gems = search_catalog(gem_only=True, home=home)
            self.assertEqual(
                [item["slug"] for item in gems["matches"]], ["tiny/fresh-mcp"]
            )

            capped = search_catalog(limit=1, home=home)
            self.assertTrue(capped["truncated"])
            self.assertEqual(len(capped["matches"]), 1)
            self.assertEqual(
                capped["matches"][0]["slug"], "punkpeye/awesome-mcp-servers"
            )

            self.assertEqual(
                search_catalog(query="nothing-matches-this", home=home)["matched"], 0
            )

    def test_search_catalog_rejects_bad_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = self._catalog_home(temporary)
            with self.assertRaisesRegex(ValueError, "unknown candidate kind"):
                search_catalog(kind="not-a-kind", home=home)
            with self.assertRaisesRegex(ValueError, "positive integer"):
                search_catalog(limit=0, home=home)

    def test_moving_filter_ranks_by_measured_rate_not_by_stars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            cache = home / "cache" / "catalog.jsonl"
            cache.parent.mkdir(parents=True, exist_ok=True)
            rows = [
                {
                    "slug": "owner/huge",
                    "display_name": "Huge",
                    "kind": "repository",
                    "stars": 90000,
                    "checked_at": "2026-09-03T00:00:00Z",
                    "stars_per_day": None,
                },
                {
                    "slug": "owner/fast",
                    "display_name": "Fast",
                    "kind": "repository",
                    "stars": 400,
                    "checked_at": "2026-09-03T00:00:00Z",
                    "stars_per_day": 51.5,
                },
                {
                    "slug": "owner/steady",
                    "display_name": "Steady",
                    "kind": "repository",
                    "stars": 9000,
                    "checked_at": "2026-09-03T00:00:00Z",
                    "stars_per_day": 3.0,
                },
            ]
            cache.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )

            everything = search_catalog(home=home)
            self.assertEqual(everything["matched"], 3)
            self.assertEqual(everything["with_momentum"], 2)
            self.assertEqual(everything["matches"][0]["slug"], "owner/huge")

            moving = search_catalog(moving_only=True, home=home)
            self.assertEqual(moving["matched"], 2)
            self.assertEqual(
                [record["slug"] for record in moving["matches"]],
                ["owner/fast", "owner/steady"],
            )

    def test_catalog_age_marks_a_dead_cron_as_stale(self) -> None:
        records = _parse_catalog_payload(self._catalog_lines())
        self.assertEqual(catalog_age_days(records, "2026-09-01T00:00:00Z"), 1.0)
        self.assertIsNone(catalog_age_days([{"slug": "a/b"}]))
        self.assertIsNone(catalog_age_days([]))

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            cache_file = home / "cache" / "catalog.jsonl"
            cache_file.parent.mkdir(parents=True, exist_ok=True)

            abandoned = {
                "slug": "a/b",
                "kind": "repository",
                "stars": 1,
                "checked_at": "2020-01-01T00:00:00Z",
            }
            cache_file.write_text(json.dumps(abandoned) + "\n", encoding="utf-8")
            self.assertTrue(search_catalog(home=home)["stale"])

            fresh = {
                **abandoned,
                "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            cache_file.write_text(json.dumps(fresh) + "\n", encoding="utf-8")
            fresh_result = search_catalog(home=home)
            self.assertFalse(fresh_result["stale"])
            self.assertLess(fresh_result["age_days"], 1)

            self.assertTrue(search_catalog(home=home / "missing")["stale"])

    def test_search_catalog_cli_reports_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = self._catalog_home(temporary)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = main(
                    [
                        "search-catalog",
                        "--home",
                        str(home),
                        "--kind",
                        "mcp-server",
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["matched"], 1)
            self.assertEqual(payload["matches"][0]["slug"], "tiny/fresh-mcp")

            text = io.StringIO()
            with contextlib.redirect_stdout(text):
                main(["search-catalog", "--home", str(home), "--gem"])
            printed = text.getvalue()
            self.assertIn("tiny/fresh-mcp", printed)
            self.assertIn("gem", printed)

    def test_expert_decision_must_declare_its_evidence_gaps(self) -> None:
        decision = self._expert_decision()
        decision.pop("evidence_gaps")
        with self.assertRaisesRegex(ValueError, "evidence_gaps"):
            render_decision(decision)

    def test_an_empty_gap_list_is_a_claim_that_every_source_answered(self) -> None:
        decision = self._expert_decision()
        decision["evidence_gaps"] = []
        rendered = render_decision(decision)
        self.assertIn("## Evidence gaps", rendered)
        self.assertIn("Every source consulted answered", rendered)

    def test_gaps_are_rendered_one_line_per_silent_source(self) -> None:
        decision = self._expert_decision()
        decision["evidence_gaps"] = [
            {"source": "reddit", "reason": "HTTP 403"},
            {"source": "hackernews", "reason": "0 matches for every probe term"},
        ]
        rendered = render_decision(decision)
        self.assertIn("- reddit: HTTP 403", rendered)
        self.assertIn("- hackernews: 0 matches for every probe term", rendered)

    def test_malformed_gap_entries_are_rejected(self) -> None:
        decision = self._expert_decision()
        for broken, pattern in (
            ("not a list", "must be a list"),
            (["reddit"], "must be objects"),
            ([{"source": "reddit"}], "missing fields"),
            ([{"source": "", "reason": "x"}], "non-empty string"),
            ([{"source": "reddit", "reason": "line\nbreak"}], "single-line"),
        ):
            decision["evidence_gaps"] = broken
            with self.assertRaisesRegex(ValueError, pattern):
                render_decision(decision)

    def test_a_standard_decision_does_not_need_evidence_gaps(self) -> None:
        decision = self._expert_decision()
        for field_name in (
            "explicit_sacrifice",
            "tradeoff_matrix",
            "code_comparison",
            "evidence_gaps",
        ):
            decision.pop(field_name)
        self.assertEqual(decision_schema(decision), "legacy")
        self.assertNotIn("Evidence gaps", render_decision(decision))

    def test_expert_schema_is_detected_and_enforced(self) -> None:
        legacy = self._expert_decision()
        for field_name in (
            "explicit_sacrifice",
            "tradeoff_matrix",
            "code_comparison",
            "evidence_gaps",
        ):
            legacy.pop(field_name)
        self.assertEqual(decision_schema(legacy), "legacy")
        self.assertNotIn("## Tradeoff matrix", render_decision(legacy))

        partial = self._expert_decision()
        partial.pop("explicit_sacrifice")
        self.assertEqual(decision_schema(partial), "expert")
        with self.assertRaisesRegex(ValueError, "expert decision missing fields"):
            render_decision(partial)

        forced = self._expert_decision()
        for field_name in ("explicit_sacrifice", "tradeoff_matrix"):
            forced.pop(field_name)
        forced["schema"] = "expert"
        with self.assertRaisesRegex(ValueError, "expert decision missing fields"):
            render_decision(forced)

        mislabelled = self._expert_decision()
        mislabelled["schema"] = "legacy"
        with self.assertRaisesRegex(
            ValueError, "legacy decision cannot carry expert fields"
        ):
            render_decision(mislabelled)

        unknown = self._expert_decision()
        unknown["schema"] = "hybrid"
        with self.assertRaisesRegex(ValueError, "legacy or expert"):
            render_decision(unknown)

        complete = self._expert_decision()
        complete["code_comparison"] = "```ts\nconst a = 1;\n```"
        self.assertEqual(decision_schema(complete), "expert")
        rendered = render_decision(complete)
        self.assertIn("## Explicit sacrifice", rendered)
        self.assertIn("## Tradeoff matrix", rendered)
        self.assertIn("## Code comparison", rendered)

    def test_decision_index_escapes_markdown_verdict(self) -> None:
        decision = self._expert_decision()
        decision["verdict"] = "Adopt [kafka](https://evil.example)"
        with tempfile.TemporaryDirectory() as temporary:
            record_decision(decision, "global", home=temporary)
            index = (Path(temporary) / "decisions" / "INDEX.md").read_text(
                encoding="utf-8"
            )
        self.assertIn("\\[kafka\\]", index)

    def test_kind_of_and_detect_emerging_gem(self) -> None:
        self.assertEqual(kind_of(["mcp.json", "src"]), "mcp-server")
        now = "2026-08-15T00:00:00Z"
        gem_repo = {
            "pushed_at": "2026-08-10T00:00:00Z",
            "stars": 42,
            "license": "MIT",
            "archived": False,
            "kind": "mcp-server",
        }
        self.assertTrue(detect_emerging_gem(gem_repo, now))
        flags = maturity_flags(gem_repo, now)
        self.assertNotIn("sustained", flags)
        self.assertTrue(flags["alive"])
        self.assertFalse(flags["adopted"])

        old_repo = {**gem_repo, "pushed_at": "2024-01-01T00:00:00Z"}
        self.assertFalse(detect_emerging_gem(old_repo, now))

    def test_catalog_load_and_local_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.assertEqual(load_catalog(home), [])
            cache_file = home / "cache" / "catalog.jsonl"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                '{"slug": "ashishps1/awesome-system-design-resources", "kind": "reference"}\n',
                encoding="utf-8",
            )
            items = load_catalog(home)
            self.assertEqual(len(items), 1)
            self.assertEqual(
                items[0]["slug"], "ashishps1/awesome-system-design-resources"
            )

            with unittest.mock.patch(
                "scripts.wheel._bounded_https_download",
                side_effect=urllib_error.URLError("offline"),
            ) as download:
                res = sync_catalog(
                    home=home, edge_url="https://example.invalid/catalog.jsonl"
                )
            download.assert_called_once()
            self.assertEqual(res["status"], "cached")
            self.assertEqual(res["count"], 1)
            self.assertIn("offline", res["error"])

    def test_corrupt_catalog_never_replaces_valid_cache(self) -> None:
        with self.assertRaises(ValueError):
            _parse_catalog_payload('{"slug": "a/b"}\n{trunca')
        with self.assertRaises(ValueError):
            _parse_catalog_payload('["not", "an", "object"]')
        self.assertEqual(_parse_catalog_payload(""), [])

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            cache_file = home / "cache" / "catalog.jsonl"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            valid = '{"slug": "ashishps1/awesome-system-design-resources"}\n'
            cache_file.write_text(valid, encoding="utf-8")

            with unittest.mock.patch(
                "scripts.wheel._bounded_https_download",
                return_value=b'{"slug": "a/b"}\n{trunca',
            ) as download:
                result = sync_catalog(
                    home=home, edge_url="https://example.invalid/catalog.jsonl"
                )
            download.assert_called_once()
            self.assertEqual(result["status"], "cached")
            self.assertEqual(result["count"], 1)
            self.assertEqual(cache_file.read_text(encoding="utf-8"), valid)

    def test_render_decision_expert_adr_extensions(self) -> None:
        base_decision = {
            "accepted": True,
            "task": "build scalable event system",
            "coverage": "COMPLETE",
            "implementation_mode": "extend-core",
            "verdict": "Adopt Apache Kafka with custom sidecar",
            "picked": "apache/kafka",
            "maturity": "A",
            "date": "2026-08-30",
            "candidates": ["apache/kafka (base)", "rabbitmq (alternative)"],
            "core": "https://github.com/apache/kafka.git",
            "upstream_or_fork": "apache/kafka",
            "donors": ["rabbitmq: consumer adapter"],
            "integrations": ["sidecar"],
            "licenses": ["Apache-2.0"],
            "rejection_reasons": ["RabbitMQ lacks log compaction"],
            "adjacent_capabilities": ["schema registry"],
            "not_building": ["custom broker"],
            "explicit_sacrifice": ["High initial RAM overhead (JVM 2GB min)"],
            "tradeoff_matrix": [
                {
                    "axis": "Resource / RAM",
                    "candidate": "Heavy (JVM)",
                    "alternative": "Light (Erlang)",
                    "assessment": "Accepted for throughput",
                }
            ],
            "code_comparison": "```ts\n// Consumer example\nconst stream = kafka.consume('events');\n```",
            "evidence_gaps": [],
        }
        rendered = render_decision(base_decision)
        self.assertIn("## Explicit sacrifice", rendered)
        self.assertIn("- High initial RAM overhead (JVM 2GB min)", rendered)
        self.assertIn("## Tradeoff matrix", rendered)
        self.assertIn("Resource / RAM", rendered)
        self.assertIn("## Code comparison", rendered)
        self.assertIn("const stream = kafka.consume('events');", rendered)

    def test_catalog_candidate_urls_default_precedence(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            urls = wheel._catalog_candidate_urls()
            self.assertEqual(
                urls,
                [wheel.DEFAULT_CATALOG_EDGE_URL, wheel.DEFAULT_CATALOG_FALLBACK_URL],
            )

    def test_catalog_candidate_urls_env_precedence(self) -> None:
        custom_edge = "https://custom-edge.worker.dev/catalog.jsonl"
        with mock.patch.dict(
            os.environ, {wheel.WHEEL_CATALOG_EDGE_URL_ENV: custom_edge}
        ):
            urls = wheel._catalog_candidate_urls()
            self.assertEqual(
                urls,
                [
                    custom_edge,
                    wheel.DEFAULT_CATALOG_EDGE_URL,
                    wheel.DEFAULT_CATALOG_FALLBACK_URL,
                ],
            )

    def test_catalog_candidate_urls_rejects_non_https(self) -> None:
        with mock.patch.dict(
            os.environ,
            {wheel.WHEEL_CATALOG_EDGE_URL_ENV: "http://insecure.dev/catalog.jsonl"},
        ):
            with self.assertRaises(ValueError):
                wheel._catalog_candidate_urls()

    def test_cli_subcommands_registration(self) -> None:
        parser = wheel.build_parser()
        args = parser.parse_args(
            ["community-signals", "--slug", "pallets/flask", "--limit", "10", "--json"]
        )
        self.assertEqual(args.command, "community-signals")
        self.assertEqual(args.slug, "pallets/flask")
        self.assertEqual(args.limit, 10)
        self.assertTrue(args.as_json)

        args = parser.parse_args(
            ["dependency-debt", "--slug", "pallets/flask", "--json"]
        )
        self.assertEqual(args.command, "dependency-debt")
        self.assertEqual(args.slug, "pallets/flask")
        self.assertTrue(args.as_json)

        args = parser.parse_args(["hard-metrics", "--slug", "pallets/flask", "--json"])
        self.assertEqual(args.command, "hard-metrics")
        self.assertEqual(args.slug, "pallets/flask")
        self.assertTrue(args.as_json)

    def test_cli_subcommands_dispatch_lazy(self) -> None:
        with mock.patch(
            "scripts.community_signals.collect_regret_signals"
        ) as mock_comm:
            mock_comm.return_value = {
                "slug": "test/repo",
                "available_sources": 1,
                "sources": [],
            }
            out = io.StringIO()
            with redirect_stdout(out):
                rc = wheel.main(["community-signals", "--slug", "test/repo", "--json"])
            self.assertEqual(rc, 0)
            mock_comm.assert_called_once_with("test/repo", limit=25)

        with mock.patch("scripts.dependency_debt.analyze_dependency_debt") as mock_dep:
            mock_dep.return_value = {"slug": "test/repo", "status": "ok"}
            out = io.StringIO()
            with redirect_stdout(out):
                rc = wheel.main(["dependency-debt", "--slug", "test/repo", "--json"])
            self.assertEqual(rc, 0)
            mock_dep.assert_called_once_with("test/repo")

        with mock.patch("scripts.hard_metrics.collect_hard_metrics") as mock_hard:
            mock_hard.return_value = {"slug": "test/repo", "status": "ok"}
            out = io.StringIO()
            with redirect_stdout(out):
                rc = wheel.main(["hard-metrics", "--slug", "test/repo", "--json"])
            self.assertEqual(rc, 0)
            mock_hard.assert_called_once_with("test/repo")


if __name__ == "__main__":
    unittest.main()
