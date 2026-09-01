from __future__ import annotations

from dataclasses import asdict, dataclass, field
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from typing import Any, Callable, Iterator, Literal, Sequence
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse
from uuid import uuid4

SourceStatus = Literal["ok", "partial", "unavailable", "blocked", "error"]
SignalType = Literal["official", "code", "usage", "trend", "discussion", "risk"]
CandidateKind = Literal["repository", "product", "service"]
RunState = Literal[
    "context", "quick", "question", "deep", "verify", "decision", "recorded"
]

SOURCE_STATUSES: frozenset[str] = frozenset(
    {"ok", "partial", "unavailable", "blocked", "error"}
)
SIGNAL_TYPES: frozenset[str] = frozenset(
    {"official", "code", "usage", "trend", "discussion", "risk"}
)
RUN_STATES: frozenset[str] = frozenset(
    {"context", "quick", "question", "deep", "verify", "decision", "recorded"}
)
EDGE_KINDS: frozenset[str] = frozenset(
    {"fork-of", "donates", "extends", "integrates", "replaces"}
)
CANDIDATE_KINDS: frozenset[str] = frozenset({"repository", "product", "service"})
CANDIDATE_ROLES: frozenset[str] = frozenset(
    {"base", "fork", "donor", "plugin", "sidecar", "alternative"}
)
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "context": {"quick"},
    "quick": {"question", "deep"},
    "question": {"question", "deep"},
    "deep": {"verify"},
    "verify": {"decision"},
    "decision": {"recorded"},
    "recorded": set(),
}


def _require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return value


def _require_json_value(value: Any, field_name: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be JSON-serializable") from error


def _require_json_object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} must be an object with string keys")
    _require_json_value(value, field_name)
    return value


def _require_json_object_list(value: Any, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of objects")
    return [_require_json_object(item, field_name) for item in value]


@dataclass(frozen=True)
class Evidence:
    source: str
    url: str
    observed_at: str
    claim: str
    candidate: str = ""
    signal_type: SignalType = "official"

    def __post_init__(self) -> None:
        _require_non_empty_string(self.source, "evidence source")
        _require_non_empty_string(self.url, "evidence url")
        _require_non_empty_string(self.observed_at, "evidence observed_at")
        _require_non_empty_string(self.claim, "evidence claim")
        if not isinstance(self.candidate, str):
            raise ValueError("evidence candidate must be a string")
        if self.signal_type not in SIGNAL_TYPES:
            raise ValueError(f"invalid signal type: {self.signal_type}")


@dataclass
class SourceResult:
    source: str
    status: SourceStatus
    checked_at: str
    query: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    error: str = ""
    edges: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        _require_non_empty_string(self.source, "source")
        _require_non_empty_string(self.checked_at, "checked_at")
        if not isinstance(self.query, str) or not isinstance(self.error, str):
            raise ValueError("source query and error must be strings")
        if self.status not in SOURCE_STATUSES:
            raise ValueError(f"invalid source status: {self.status}")
        if not isinstance(self.evidence, list) or not all(
            isinstance(item, Evidence) for item in self.evidence
        ):
            raise ValueError("source evidence must be Evidence objects")
        self.edges = [_normalize_edge(edge) for edge in self.edges]


@dataclass
class Candidate:
    candidate_id: str
    display_name: str
    kind: CandidateKind = "repository"
    roles: list[str] = field(default_factory=lambda: ["alternative"])
    aliases: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)

    def __post_init__(self) -> None:
        _require_non_empty_string(self.candidate_id, "candidate_id")
        _require_non_empty_string(self.display_name, "display_name")
        if self.kind not in CANDIDATE_KINDS:
            raise ValueError("invalid candidate kind")
        actual_kind, canonical_id = normalize_candidate_id(self.candidate_id)
        if actual_kind != self.kind or canonical_id != self.candidate_id:
            raise ValueError("candidate_id must match its typed canonical kind")
        _require_string_list(self.roles, "roles")
        if not self.roles:
            raise ValueError("candidate roles must not be empty")
        if any(role not in CANDIDATE_ROLES for role in self.roles):
            raise ValueError("invalid candidate role")
        if "alternative" in self.roles and len(self.roles) != 1:
            raise ValueError("alternative role cannot coexist with an evaluated role")
        _require_string_list(self.aliases, "aliases")
        if not isinstance(self.evidence, list) or not all(
            isinstance(item, Evidence) for item in self.evidence
        ):
            raise ValueError("candidate evidence must be Evidence objects")


@dataclass
class RunRecord:
    schema_version: int
    run_id: str
    state: RunState
    task: str
    source_results: list[SourceResult] = field(default_factory=list)
    candidates: dict[str, Candidate] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    families: list[dict[str, Any]] = field(default_factory=list)
    user_answers: list[dict[str, Any]] = field(default_factory=list)
    tool_snapshot: dict[str, Any] = field(default_factory=dict)
    edges: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, int) or self.schema_version < 1:
            raise ValueError("schema_version must be a positive integer")
        _require_non_empty_string(self.run_id, "run_id")
        _require_non_empty_string(self.task, "task")
        if self.state not in RUN_STATES:
            raise ValueError(f"invalid run state: {self.state}")
        if not isinstance(self.source_results, list) or not all(
            isinstance(item, SourceResult) for item in self.source_results
        ):
            raise ValueError("source_results must contain SourceResult objects")
        if not isinstance(self.candidates, dict) or not all(
            isinstance(key, str) and isinstance(value, Candidate) and key == value.candidate_id
            for key, value in self.candidates.items()
        ):
            raise ValueError("candidates must map ids to Candidate objects")
        _require_json_object(self.context, "context")
        _require_json_object_list(self.families, "families")
        _require_json_object_list(self.user_answers, "user_answers")
        _require_json_object(self.tool_snapshot, "tool_snapshot")
        self.edges = [_normalize_edge(edge) for edge in self.edges]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_repo_slug(value: str) -> str:
    """Return a lowercase GitHub owner/repository identifier."""
    raw = value.strip()
    if not raw:
        raise ValueError("repository slug is empty")
    ssh_match = re.fullmatch(r"git@github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?/?", raw, re.I)
    if ssh_match:
        owner, repo = ssh_match.groups()
        return f"{owner}/{repo}".lower()
    parsed = urlparse(raw)
    if parsed.scheme:
        if parsed.netloc.lower() != "github.com":
            raise ValueError(f"not a GitHub repository: {value}")
        parts = [part for part in parsed.path.split("/") if part]
    else:
        parts = [part for part in raw.strip("/").split("/") if part]
    if len(parts) != 2:
        raise ValueError(f"invalid GitHub repository: {value}")
    owner, repo = parts
    repo = re.sub(r"\.git$", "", repo, flags=re.I)
    if not owner or not repo:
        raise ValueError(f"invalid GitHub repository: {value}")
    return f"{owner}/{repo}".lower()


def _normalize_typed_slug(value: str, kind: CandidateKind) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug or len(slug) > 80:
        raise ValueError(f"invalid {kind} candidate id")
    return f"{kind}:{slug}"


def normalize_candidate_id(value: str) -> tuple[CandidateKind, str]:
    """Return a canonical repository id or an explicitly typed non-repository id."""
    raw = _require_non_empty_string(value, "candidate_id").strip()
    typed = re.fullmatch(r"(product|service):(.*)", raw, re.I)
    if typed:
        kind = typed.group(1).casefold()
        if kind == "product":
            return "product", _normalize_typed_slug(typed.group(2), "product")
        return "service", _normalize_typed_slug(typed.group(2), "service")
    try:
        return "repository", normalize_repo_slug(raw)
    except ValueError as error:
        raise ValueError(
            "non-repository candidates require a typed product: or service: id"
        ) from error


def evidence_key(evidence: Evidence) -> tuple[str, str, str, str]:
    """Return the provenance key used to collapse duplicate evidence."""
    claim = " ".join(evidence.claim.split()).casefold()
    return (
        evidence.source.casefold(),
        evidence.url.rstrip("/").casefold(),
        claim,
        evidence.signal_type,
    )


def add_candidate_role(candidate: Candidate, role: str) -> None:
    _require_non_empty_string(role, "candidate role")
    if role not in CANDIDATE_ROLES:
        raise ValueError("invalid candidate role")
    if role == "alternative" and candidate.roles != ["alternative"]:
        return
    if role != "alternative":
        candidate.roles[:] = [existing for existing in candidate.roles if existing != "alternative"]
    if role not in candidate.roles:
        candidate.roles.append(role)


def merge_source_results(results: list[SourceResult]) -> dict[str, Candidate]:
    """Canonicalize candidate identities and keep only unique evidence."""
    merged: dict[str, Candidate] = {}
    seen: dict[str, set[tuple[str, str, str, str]]] = {}
    for result in results:
        for evidence in result.evidence:
            if not evidence.candidate:
                continue
            candidate_kind, candidate_id = normalize_candidate_id(evidence.candidate)
            candidate = merged.setdefault(
                candidate_id,
                Candidate(
                    candidate_id=candidate_id,
                    display_name=candidate_id,
                    kind=candidate_kind,
                ),
            )
            if evidence.candidate not in candidate.aliases:
                candidate.aliases.append(evidence.candidate)
            key = evidence_key(evidence)
            if key not in seen.setdefault(candidate_id, set()):
                candidate.evidence.append(evidence)
                seen[candidate_id].add(key)
    return merged


def candidate_graph(
    candidates: dict[str, Candidate], edges: list[dict[str, str]]
) -> dict[str, Any]:
    """Return a JSON-serializable graph with explicitly typed edges."""
    if any(candidate_id != candidate.candidate_id for candidate_id, candidate in candidates.items()):
        raise ValueError("candidate graph ids must match candidates")
    normalized_edges = [_normalize_edge(edge) for edge in edges]
    return {
        "candidates": {candidate_id: asdict(candidate) for candidate_id, candidate in candidates.items()},
        "edges": normalized_edges,
    }


def _normalize_edge(edge: Any) -> dict[str, str]:
    required = {"kind", "source", "target", "feature"}
    if not isinstance(edge, dict) or set(edge) != required:
        raise ValueError("invalid candidate graph edge")
    if not all(isinstance(edge[name], str) for name in required):
        raise ValueError("invalid candidate graph edge")
    if edge["kind"] not in EDGE_KINDS:
        raise ValueError(f"invalid candidate graph edge kind: {edge['kind']}")
    return {
        "kind": edge["kind"],
        "source": normalize_candidate_id(edge["source"])[1],
        "target": normalize_candidate_id(edge["target"])[1],
        "feature": edge["feature"],
    }


def _merge_candidate(target: Candidate, incoming: Candidate) -> None:
    for role in incoming.roles:
        add_candidate_role(target, role)
    for alias in incoming.aliases:
        if alias not in target.aliases:
            target.aliases.append(alias)
    seen = {evidence_key(evidence) for evidence in target.evidence}
    for evidence in incoming.evidence:
        if evidence_key(evidence) not in seen:
            target.evidence.append(evidence)
            seen.add(evidence_key(evidence))


def _merge_edges(existing: list[dict[str, str]], incoming: list[dict[str, str]]) -> list[dict[str, str]]:
    merged = [_normalize_edge(edge) for edge in existing]
    seen = {tuple(edge[name] for name in ("kind", "source", "target", "feature")) for edge in merged}
    for edge in incoming:
        normalized = _normalize_edge(edge)
        key = tuple(normalized[name] for name in ("kind", "source", "target", "feature"))
        if key not in seen:
            merged.append(normalized)
            seen.add(key)
    return merged


def wheel_home(value: str | Path | None = None) -> Path:
    """Resolve the portable Wheel state directory without string path tricks."""
    configured = value if value is not None else os.environ.get("WHEEL_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".config" / "wheel"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolved_root(value: str | Path, *, create: bool = False) -> Path:
    root = Path(value).expanduser()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _contained_path(root: Path, relative: Path, message: str) -> Path:
    resolved_root = root.resolve()
    candidate = resolved_root / relative
    resolved_candidate = candidate.resolve(strict=False)
    if not resolved_candidate.is_relative_to(resolved_root):
        raise ValueError(message)
    parent = candidate.parent
    parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = parent.resolve()
    if not resolved_parent.is_relative_to(resolved_root):
        raise ValueError(message)
    path = resolved_parent / candidate.name
    if not path.resolve(strict=False).is_relative_to(resolved_root):
        raise ValueError(message)
    return path


@contextmanager
def _file_lock(path: Path, timeout: float = 10.0) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    deadline = time.monotonic() + timeout
    handle = lock_path.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for lock: {path.name}") from None
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        try:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


# ponytail: one coarse advisory lock per record/cache file; split only if measured throughput requires it.
def _atomic_text_write(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_json_write(path: Path, value: Any) -> None:
    _require_json_value(value, "file content")
    _atomic_text_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _candidate_from_dict(candidate: Any) -> Candidate:
    if not isinstance(candidate, dict):
        raise ValueError("candidate must contain an object")
    evidence = candidate.get("evidence", [])
    if not isinstance(evidence, list):
        raise ValueError("candidate evidence must be a list")
    return Candidate(
        candidate_id=candidate["candidate_id"],
        display_name=candidate["display_name"],
        kind=candidate.get("kind", "repository"),
        roles=candidate.get("roles", []),
        aliases=candidate.get("aliases", []),
        evidence=[Evidence(**item) for item in evidence],
    )


def _record_from_dict(data: dict[str, Any]) -> RunRecord:
    if not isinstance(data, dict):
        raise ValueError("run record must contain an object")
    raw_results = data.get("source_results", [])
    if not isinstance(raw_results, list):
        raise ValueError("source_results must be a list")
    results = [
        SourceResult(
            source=item["source"],
            status=item["status"],
            checked_at=item["checked_at"],
            query=item.get("query", ""),
            evidence=[Evidence(**evidence) for evidence in item.get("evidence", [])],
            error=item.get("error", ""),
            edges=item.get("edges", []),
        )
        for item in raw_results
    ]
    raw_candidates = data.get("candidates", {})
    if not isinstance(raw_candidates, dict):
        raise ValueError("candidates must contain an object")
    candidates = {
        candidate_id: _candidate_from_dict(candidate)
        for candidate_id, candidate in raw_candidates.items()
    }
    return RunRecord(
        schema_version=data["schema_version"],
        run_id=data["run_id"],
        state=data["state"],
        task=data["task"],
        source_results=results,
        candidates=candidates,
        context=data.get("context", {}),
        families=data.get("families", []),
        user_answers=data.get("user_answers", []),
        tool_snapshot=data.get("tool_snapshot", {}),
        edges=data.get("edges", []),
    )


class RunStore:
    """Filesystem-backed run records with atomic writes below one trusted home."""

    def __init__(self, home: str | Path | None = None) -> None:
        self.home = _resolved_root(wheel_home(home))
        self.runs_dir = self.home / "runs"

    def _run_path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27}", run_id):
            raise ValueError("invalid run id")
        return _contained_path(
            self.home, Path("runs") / f"{run_id}.json", "run path escapes WHEEL_HOME"
        )

    def create(self, task: str) -> RunRecord:
        if not task.strip():
            raise ValueError("task is empty")
        record = RunRecord(
            schema_version=1,
            run_id=str(uuid4()),
            state="context",
            task=task,
        )
        self.save(record)
        return record

    def load(self, run_id: str) -> RunRecord:
        path = self._run_path(run_id)
        return self._load_path(path)

    def _load_path(self, path: Path) -> RunRecord:
        with path.open(encoding="utf-8") as handle:
            return _record_from_dict(json.load(handle))

    def save(self, record: RunRecord) -> Path:
        path = self._run_path(record.run_id)
        with _file_lock(path):
            self._save_path(path, record)
        return path

    def _save_path(self, path: Path, record: RunRecord) -> None:
        _reject_run_secrets(record.to_dict())
        _atomic_json_write(path, record.to_dict())

    def transition(self, run_id: str, target: RunState) -> RunRecord:
        path = self._run_path(run_id)
        with _file_lock(path):
            record = self._load_path(path)
            if target not in ALLOWED_TRANSITIONS[record.state]:
                raise ValueError(f"invalid transition: {record.state} -> {target}")
            record.state = target
            self._save_path(path, record)
        return record

    def add_source_result(self, run_id: str, result: SourceResult) -> RunRecord:
        _reject_run_secrets(asdict(result))
        path = self._run_path(run_id)
        with _file_lock(path):
            record = self._load_path(path)
            record.source_results.append(result)
            for candidate_id, incoming in merge_source_results([result]).items():
                current = record.candidates.get(candidate_id)
                if current is None:
                    record.candidates[candidate_id] = incoming
                else:
                    _merge_candidate(current, incoming)
            record.edges = _merge_edges(record.edges, result.edges)
            self._save_path(path, record)
        return record

    def update(self, run_id: str, update: dict[str, Any]) -> RunRecord:
        validated = _validate_run_update(update)
        path = self._run_path(run_id)
        with _file_lock(path):
            record = self._load_path(path)
            for field_name, value in validated.items():
                setattr(record, field_name, value)
            self._save_path(path, record)
        return record


def load_config(home: str | Path | None = None) -> dict[str, Any]:
    path = wheel_home(home) / "config.json"
    config: dict[str, Any] = {"lookback_days": 90, "telegram_channels": []}
    if not path.exists():
        return config
    with path.open(encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError("config.json must contain an object")
    lookback_days = loaded.get("lookback_days", config["lookback_days"])
    channels = loaded.get("telegram_channels", config["telegram_channels"])
    if isinstance(lookback_days, bool) or not isinstance(lookback_days, int) or lookback_days < 1:
        raise ValueError("lookback_days must be a positive integer")
    if not isinstance(channels, list) or not all(isinstance(channel, str) for channel in channels):
        raise ValueError("telegram_channels must be a list of strings")
    return {"lookback_days": lookback_days, "telegram_channels": channels}


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEPENDENCY_MANIFEST_PATH = REPOSITORY_ROOT / "registry" / "dependencies.json"
DONSETCH_ID = "donsetch"
DONSETCH_DOWNLOAD_LIMIT_BYTES = 128 * 1024 * 1024
DONSETCH_VERSION_TIMEOUT_SECONDS = 10.0
DONSETCH_FETCH_TIMEOUT_SECONDS = 30.0
DONSETCH_LATEST_CACHE_NAME = "latest-release.json"
SEMVER_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
DONSETCH_PLATFORM_FILES: dict[tuple[str, str], tuple[str, str, tuple[str, ...]]] = {
    ("Windows", "AMD64"): ("donsetch-win32-x64.tar.gz", "donsetch.exe", ("donsetch.exe", "pdfium.dll")),
    ("Linux", "x86_64"): ("donsetch-linux-x64.tar.gz", "donsetch", ("donsetch", "libonnxruntime.so")),
    ("Linux", "aarch64"): ("donsetch-linux-arm64.tar.gz", "donsetch", ("donsetch",)),
    ("Darwin", "x86_64"): ("donsetch-darwin-x64.tar.gz", "donsetch", ("donsetch",)),
    ("Darwin", "arm64"): ("donsetch-darwin-arm64.tar.gz", "donsetch", ("donsetch",)),
}


def _require_exact_keys(value: Any, keys: set[str], field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{field_name} has an invalid schema")
    return value


def _is_semver(value: Any) -> bool:
    return isinstance(value, str) and SEMVER_PATTERN.fullmatch(value) is not None


def _is_https_url(value: Any) -> bool:
    parsed = urlparse(value) if isinstance(value, str) else None
    return bool(parsed and parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password)


def _safe_manifest_component(value: Any) -> bool:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        return False
    path = Path(value)
    return len(path.parts) == 1 and path.name == value and value not in {".", ".."}


def load_dependency_manifest(path: str | Path = DEPENDENCY_MANIFEST_PATH) -> dict[str, Any]:
    """Load the single, pinned dependency manifest without accepting extensions."""
    with Path(path).expanduser().open(encoding="utf-8") as handle:
        raw = json.load(handle)
    envelope = _require_exact_keys(raw, {"schema_version", "dependencies"}, "dependencies manifest")
    if envelope["schema_version"] != 1 or not isinstance(envelope["dependencies"], list):
        raise ValueError("dependencies manifest has an invalid schema")
    dependencies = envelope["dependencies"]
    if len(dependencies) != 1:
        raise ValueError("dependencies manifest must contain exactly one dependency")
    dependency = _require_exact_keys(
        dependencies[0],
        {
            "id", "tested_version", "source_repository", "release_base_url",
            "license", "license_url", "check_interval_seconds", "platforms",
        },
        "dependency",
    )
    if dependency["id"] != DONSETCH_ID:
        raise ValueError("unsupported managed dependency")
    if not _is_semver(dependency["tested_version"]):
        raise ValueError("dependency tested_version must be SemVer")
    if not isinstance(dependency["source_repository"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", dependency["source_repository"]):
        raise ValueError("dependency source_repository must be owner/repository")
    if not _is_https_url(dependency["release_base_url"]) or not _is_https_url(dependency["license_url"]):
        raise ValueError("dependency URLs must use HTTPS")
    if dependency["license"] != "AGPL-3.0-only":
        raise ValueError("dependency license is invalid")
    if isinstance(dependency["check_interval_seconds"], bool) or not isinstance(dependency["check_interval_seconds"], int) or dependency["check_interval_seconds"] < 1:
        raise ValueError("dependency check_interval_seconds must be positive")
    if not isinstance(dependency["platforms"], list) or not dependency["platforms"]:
        raise ValueError("dependency platforms must be a non-empty list")
    seen: set[tuple[str, str]] = set()
    normalized_platforms: list[dict[str, str]] = []
    for item in dependency["platforms"]:
        platform_item = _require_exact_keys(item, {"system", "machine", "asset", "binary", "required_files", "sha256"}, "dependency platform")
        if not all(isinstance(platform_item[key], str) and platform_item[key] for key in ("system", "machine")):
            raise ValueError("dependency platform system and machine must be strings")
        key = (platform_item["system"], platform_item["machine"])
        if key in seen:
            raise ValueError("dependency platform tuple is duplicated")
        expected_files = DONSETCH_PLATFORM_FILES.get(key)
        if expected_files is None:
            raise ValueError("dependency platform tuple is unsupported")
        seen.add(key)
        if not _safe_manifest_component(platform_item["asset"]) or not platform_item["asset"].endswith(".tar.gz"):
            raise ValueError("dependency asset name is invalid")
        if not _safe_manifest_component(platform_item["binary"]):
            raise ValueError("dependency binary name is invalid")
        required_files = platform_item["required_files"]
        if (
            not isinstance(required_files, list)
            or not required_files
            or not all(_safe_manifest_component(name) for name in required_files)
            or len(set(required_files)) != len(required_files)
            or platform_item["binary"] not in required_files
        ):
            raise ValueError("dependency required_files is invalid")
        if (
            platform_item["asset"], platform_item["binary"], tuple(required_files)
        ) != expected_files:
            raise ValueError("dependency platform files are unexpected")
        if not isinstance(platform_item["sha256"], str) or SHA256_PATTERN.fullmatch(platform_item["sha256"]) is None:
            raise ValueError("dependency sha256 is invalid")
        normalized_platforms.append(dict(platform_item))
    if seen != set(DONSETCH_PLATFORM_FILES):
        raise ValueError("dependencies manifest must contain every supported platform")
    result = {"schema_version": 1, "dependencies": [{**dependency, "platforms": normalized_platforms}]}
    _require_json_value(result, "dependencies manifest")
    return result


def dependency_platform(manifest: dict[str, Any], system: str | None = None, machine: str | None = None) -> dict[str, Any] | None:
    """Return the explicitly pinned asset for a supported host tuple."""
    current_system = system if system is not None else platform.system()
    current_machine = machine if machine is not None else platform.machine()
    dependency = manifest["dependencies"][0]
    for item in dependency["platforms"]:
        if item["system"] == current_system and item["machine"] == current_machine:
            return dict(item)
    return None


def _dependency_base(home: str | Path | None, manifest: dict[str, Any]) -> Path:
    return _resolved_root(wheel_home(home)) / "dependencies" / manifest["dependencies"][0]["id"]


def _managed_binary_path(home: str | Path | None, manifest: dict[str, Any], platform_item: dict[str, Any]) -> Path:
    dependency = manifest["dependencies"][0]
    return _dependency_base(home, manifest) / dependency["tested_version"] / platform_item["binary"]


def _valid_managed_layout(version_root: Path, required_files: Sequence[str]) -> bool:
    try:
        entries = list(version_root.iterdir())
    except OSError:
        return False
    expected = set(required_files)
    return (
        {entry.name for entry in entries} == expected
        and all(entry.name in expected and entry.is_file() and not entry.is_symlink() for entry in entries)
    )


def _version_from_output(value: str) -> str | None:
    for line in value.splitlines():
        match = re.fullmatch(r"DonSeTch\s+v?(" + SEMVER_PATTERN.pattern + r")", line.strip(), re.I)
        if match:
            return match.group(1)
    if "\n" not in value and "\r" not in value:
        match = re.fullmatch(r"v?(" + SEMVER_PATTERN.pattern + r")", value.strip(), re.I)
        if match:
            return match.group(1)
    return None


def _probe_managed_binary(binary: Path, expected_version: str, timeout: float = DONSETCH_VERSION_TIMEOUT_SECONDS) -> tuple[bool, str]:
    environment = dict(os.environ)
    environment["NO_COLOR"] = "1"
    try:
        completed = subprocess.run(
            [str(binary), "--version"], check=False, capture_output=True, text=True,
            timeout=timeout, shell=False, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "managed version probe failed"
    if completed.returncode != 0:
        return False, "managed version probe failed"
    version = _version_from_output(completed.stdout)
    if version != expected_version:
        return False, "managed version mismatch"
    return True, version


def _latest_cache_path(home: str | Path | None, manifest: dict[str, Any]) -> Path:
    return _dependency_base(home, manifest) / DONSETCH_LATEST_CACHE_NAME


def _read_latest_cache(path: Path, interval: int, now: float) -> dict[str, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"version", "checked_at"} or not _is_semver(data["version"]) or not isinstance(data["checked_at"], str):
            return None
        if now - _timestamp(data["checked_at"]) > interval:
            return None
        return {"version": data["version"], "checked_at": data["checked_at"]}
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _github_latest_url(repository: str) -> str:
    return f"https://api.github.com/repos/{repository}/releases/latest"


class _HTTPSRedirect(urllib_request.HTTPRedirectHandler):
    def __init__(self, hosts: set[str]) -> None:
        super().__init__()
        self._hosts = hosts

    def redirect_request(self, request: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname not in self._hosts:
            raise urllib_error.HTTPError(newurl, code, "unsafe redirect", headers, fp)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _bounded_https_download(url: str, allowed_hosts: set[str], max_bytes: int = DONSETCH_DOWNLOAD_LIMIT_BYTES) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise ValueError("dependency download URL is not allowlisted HTTPS")
    opener = urllib_request.build_opener(_HTTPSRedirect(allowed_hosts))
    request = urllib_request.Request(url, headers={"User-Agent": "wheel-managed-dependency"})
    with opener.open(request, timeout=30) as response:
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > max_bytes):
            raise ValueError("dependency download exceeds size limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("dependency download exceeds size limit")
            chunks.append(chunk)
    return b"".join(chunks)


def _extract_required_files(archive: Path, output: Path, required_files: Sequence[str]) -> None:
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            expected = set(required_files)
            if len(members) != len(expected):
                raise ValueError("dependency archive contains unexpected files")
            found: set[str] = set()
            for member in members:
                member_path = Path(member.name)
                if (
                    member.name not in expected
                    or member.name in found
                    or member_path.is_absolute()
                    or ".." in member_path.parts
                    or not member.isfile()
                    or member.issym()
                    or member.islnk()
                    or member.isdev()
                ):
                    raise ValueError("dependency archive contains an unsafe member")
                found.add(member.name)
            if found != expected:
                raise ValueError("dependency archive is missing required files")
            for member in members:
                source = tar.extractfile(member)
                if source is None:
                    raise ValueError("dependency archive member is unreadable")
                with source, (output / member.name).open("xb") as target:
                    shutil.copyfileobj(source, target)
    except tarfile.TarError as error:
        raise ValueError("dependency archive is invalid") from error


def dependency_status(
    home: str | Path | None = None,
    *,
    manifest_path: str | Path = DEPENDENCY_MANIFEST_PATH,
    system: str | None = None,
    machine: str | None = None,
    check_latest: bool = False,
    now: float | None = None,
) -> dict[str, str]:
    """Report only public managed-dependency status; it never selects PATH binaries."""
    checked_at = _utc_now()
    base = {"id": DONSETCH_ID, "status": "unavailable", "path": "", "version": "", "tested_version": "", "latest_version": "", "checked_at": checked_at, "detail": ""}
    try:
        manifest = load_dependency_manifest(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {**base, "detail": "dependency manifest is unavailable"}
    dependency = manifest["dependencies"][0]
    base.update(id=dependency["id"], tested_version=dependency["tested_version"])
    platform_item = dependency_platform(manifest, system, machine)
    if platform_item is None:
        return {**base, "detail": "unsupported platform"}
    binary = _managed_binary_path(home, manifest, platform_item)
    base["path"] = str(binary)
    version_root = binary.parent
    if not version_root.exists():
        result = {**base, "status": "missing", "detail": "managed version is not installed"}
    elif not binary.is_file() or not _valid_managed_layout(version_root, platform_item["required_files"]):
        result = {**base, "status": "repair_required", "detail": "managed version is incomplete"}
    else:
        valid, version_or_detail = _probe_managed_binary(binary, dependency["tested_version"])
        result = {**base, "status": "current" if valid else "repair_required", "version": version_or_detail if valid else "", "detail": "" if valid else version_or_detail}
    if check_latest:
        result.update(_latest_release_status(home, manifest, now=now))
        if result["status"] == "current" and _semver_is_newer(result["latest_version"], dependency["tested_version"]):
            result["status"] = "update_available"
    return result


def _semver_is_newer(candidate: str, tested: str) -> bool:
    if not _is_semver(candidate) or not _is_semver(tested):
        return False
    def stable_parts(version: str) -> tuple[int, int, int]:
        return tuple(int(piece) for piece in version.split("-", 1)[0].split("+", 1)[0].split("."))  # type: ignore[return-value]
    return stable_parts(candidate) > stable_parts(tested)


def _latest_release_status(home: str | Path | None, manifest: dict[str, Any], *, now: float | None = None) -> dict[str, str]:
    dependency = manifest["dependencies"][0]
    timestamp = time.time() if now is None else now
    cache_path = _latest_cache_path(home, manifest)
    cached = _read_latest_cache(cache_path, dependency["check_interval_seconds"], timestamp)
    if cached is not None:
        return {"latest_version": cached["version"], "checked_at": cached["checked_at"], "detail": ""}
    try:
        payload = _bounded_https_download(_github_latest_url(dependency["source_repository"]), {"api.github.com"}, 1024 * 1024)
        data = json.loads(payload)
        tag = data.get("tag_name") if isinstance(data, dict) else None
        version = tag[1:] if isinstance(tag, str) and tag.startswith("v") else tag
        if not _is_semver(version):
            raise ValueError("invalid latest release")
    except (OSError, ValueError, urllib_error.URLError, urllib_error.HTTPError, json.JSONDecodeError):
        return {"latest_version": "", "checked_at": _utc_now(), "detail": "latest release check failed"}
    checked_at = _utc_now()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json_write(cache_path, {"version": version, "checked_at": checked_at})
    return {"latest_version": version, "checked_at": checked_at, "detail": ""}


def ensure_dependency(
    home: str | Path | None = None,
    *,
    manifest_path: str | Path = DEPENDENCY_MANIFEST_PATH,
    system: str | None = None,
    machine: str | None = None,
    download: Callable[[str, set[str], int], bytes] = _bounded_https_download,
) -> dict[str, str]:
    """Install only the manifest-tested release, after checksum and staged probing."""
    manifest = load_dependency_manifest(manifest_path)
    dependency = manifest["dependencies"][0]
    platform_item = dependency_platform(manifest, system, machine)
    if platform_item is None:
        return dependency_status(home, manifest_path=manifest_path, system=system, machine=machine)
    before = dependency_status(home, manifest_path=manifest_path, system=system, machine=machine)
    if before["status"] == "current":
        return before
    version_root = _managed_binary_path(home, manifest, platform_item).parent
    if version_root.exists():
        return {**before, "status": "repair_required", "detail": "managed version is incomplete; refusing to overwrite"}
    base = _dependency_base(home, manifest)
    base.mkdir(parents=True, exist_ok=True)
    lock_path = base / dependency["tested_version"]
    with _file_lock(lock_path):
        before = dependency_status(home, manifest_path=manifest_path, system=system, machine=machine)
        if before["status"] == "current":
            return before
        if version_root.exists():
            return {**before, "status": "repair_required", "detail": "managed version is incomplete; refusing to overwrite"}
        stage = Path(tempfile.mkdtemp(prefix=f".{dependency['tested_version']}.", dir=base))
        try:
            asset_url = (
                dependency["release_base_url"].rstrip("/")
                + "/v" + dependency["tested_version"]
                + "/" + platform_item["asset"]
            )
            allowed_hosts = {urlparse(dependency["release_base_url"]).hostname or ""}
            if "github.com" in allowed_hosts:
                allowed_hosts.update({"release-assets.githubusercontent.com", "objects.githubusercontent.com"})
            payload = download(asset_url, allowed_hosts, DONSETCH_DOWNLOAD_LIMIT_BYTES)
            if hashlib.sha256(payload).hexdigest() != platform_item["sha256"]:
                raise ValueError("dependency checksum mismatch")
            archive = stage / platform_item["asset"]
            archive.write_bytes(payload)
            _extract_required_files(archive, stage, platform_item["required_files"])
            archive.unlink()
            binary = stage / platform_item["binary"]
            if os.name != "nt":
                binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            valid, detail = _probe_managed_binary(binary, dependency["tested_version"])
            if not valid:
                raise ValueError(detail)
            os.replace(stage, version_root)
        except BaseException as error:
            shutil.rmtree(stage, ignore_errors=True)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return {**before, "status": "error", "detail": _redact_text(str(error))}
    return dependency_status(home, manifest_path=manifest_path, system=system, machine=machine)


def _content_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, dict):
        values = [_content_text(value) for key, value in payload.items() if key in {"content", "text", "markdown", "body", "structuredContent"}]
        return "\n".join(value for value in values if value).strip()
    if isinstance(payload, list):
        return "\n".join(_content_text(value) for value in payload).strip()
    return ""


def donsetch_fetch(
    url: str,
    home: str | Path | None = None,
    *,
    manifest_path: str | Path = DEPENDENCY_MANIFEST_PATH,
    required_fields: Sequence[str] = (),
    focus: str | None = None,
    timeout: float = DONSETCH_FETCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Read one validated public URL through the managed binary with JSON-only stdout."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {"status": "error", "content_ok": False, "detail": "invalid fetch URL", "stderr_category": "", "data": {}}
    normalized_focus = ""
    if focus is not None:
        if not isinstance(focus, str) or not focus.strip() or len(focus.strip()) > 200:
            return {"status": "error", "content_ok": False, "detail": "invalid fetch focus", "stderr_category": "", "data": {}}
        normalized_focus = focus.strip()
    status = dependency_status(home, manifest_path=manifest_path)
    if status["status"] not in {"current", "update_available"}:
        return {"status": "unavailable", "content_ok": False, "detail": "managed dependency is unavailable", "stderr_category": "", "data": {}}
    command = [status["path"], "fetch", url, "--json", "--deadline-ms", "15000", "--max-chars", "50000"]
    if normalized_focus:
        command.extend(("--focus", normalized_focus))
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        return {"status": "error", "content_ok": False, "detail": "fetch timed out", "stderr_category": "", "data": {}}
    except OSError:
        return {"status": "error", "content_ok": False, "detail": "fetch could not start", "stderr_category": "", "data": {}}
    stderr_category = "present" if completed.stderr.strip() else ""
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        detail = "fetch command failed" if completed.returncode != 0 else "fetch returned invalid JSON"
        return {"status": "error", "content_ok": False, "detail": detail, "stderr_category": stderr_category, "data": {}}
    if not isinstance(data, dict):
        return {"status": "error", "content_ok": False, "detail": "fetch returned invalid JSON envelope", "stderr_category": stderr_category, "data": {}}
    error_value = data.get("error")
    error_kind = ""
    if isinstance(error_value, dict) and isinstance(error_value.get("kind"), str):
        error_kind = error_value["kind"]
    if data.get("ok") is not True:
        meta_value = data.get("meta")
        error_code = meta_value.get("code", "") if isinstance(meta_value, dict) and isinstance(meta_value.get("code"), str) else ""
        detail = f"fetch returned error: {error_kind}" if error_kind else "fetch returned an error"
        blocked = error_kind.casefold() in {"blocked", "walled", "captcha"} or error_code.casefold().startswith(("wall.", "captcha"))
        return {"status": "blocked" if blocked else "error", "content_ok": False, "detail": detail, "stderr_category": stderr_category, "data": data, "error_kind": error_kind, "error_code": error_code}
    if completed.returncode != 0:
        return {"status": "error", "content_ok": False, "detail": "fetch command failed", "stderr_category": stderr_category, "data": data}
    meta = data.get("meta")
    if not isinstance(meta, dict) or meta.get("content_ok") is not True:
        return {"status": "partial", "content_ok": False, "detail": "fetch did not confirm content", "stderr_category": stderr_category, "data": data}
    content = data.get("content")
    if not isinstance(content, str):
        content = ""
    content = content.strip()
    if not content:
        return {"status": "partial", "content_ok": True, "detail": "fetch content is empty", "stderr_category": stderr_category, "data": data}
    missing = [
        field for field in required_fields
        if not _content_text(meta.get(field)) and field.casefold() not in content.casefold()
    ]
    next_offset = meta.get("next_offset")
    truncated = bool(meta.get("truncated")) or next_offset is not None
    pagination = next_offset is not None
    thin_content = bool(meta.get("thin"))
    incomplete = bool(truncated or thin_content or missing)
    return {
        "status": "partial" if incomplete else "ok", "content_ok": True,
        "detail": "required content is incomplete" if incomplete else "",
        "stderr_category": stderr_category, "data": data,
        "quality": meta.get("quality"), "via": meta.get("via"),
        "escalation": meta.get("escalation"), "truncated": truncated,
        "pagination": pagination, "thin_content": thin_content, "elapsed_ms": meta.get("ms"),
        "next_offset": next_offset,
    }


DOCTOR_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", ("python", "--version")),
    ("gh", ("gh", "version")),
    ("agent-reach", ("agent-reach", "--version")),
    ("opencli", ("opencli", "--version")),
    ("curl", ("curl", "--version")),
    ("git", ("git", "--version")),
)


def _redact_text(value: str) -> str:
    return re.sub(
        r"(?i)\b(token|cookie|password|authorization)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        value,
    )


def _doctor_version_command(executable: str, argv: tuple[str, ...]) -> tuple[str, ...]:
    """Build a fixed-argument, shell-free doctor version command."""
    resolved_executable = os.path.realpath(executable)
    version_args = argv[1:]
    if os.name != "nt" or os.path.splitext(resolved_executable)[1].casefold() not in {".bat", ".cmd"}:
        return (resolved_executable, *version_args)
    comspec = os.environ.get("COMSPEC")
    if not comspec:
        raise OSError("COMSPEC is not configured")
    return (
        comspec,
        "/d",
        "/s",
        "/c",
        subprocess.list2cmdline((resolved_executable, *version_args)),
    )


def _global_donsetch_detail(timeout: float) -> str:
    """Return a short diagnostic for PATH only; callers never execute it as managed."""
    executable = shutil.which(DONSETCH_ID)
    if executable is None:
        return ""
    try:
        completed = subprocess.run(
            _doctor_version_command(executable, (DONSETCH_ID, "--version")),
            check=False, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "global command is broken and ignored"
    if completed.returncode != 0 or _version_from_output(completed.stdout) is None:
        return "global command is broken and ignored"
    return "global command is ignored"


def run_doctor(timeout: float = 5.0, home: str | Path | None = None) -> dict[str, Any]:
    """Probe only safe executable version commands and return no raw output."""
    checked_at = _utc_now()
    commands: list[dict[str, str]] = []
    for name, argv in DOCTOR_COMMANDS:
        executable = shutil.which(name)
        result: dict[str, str] = {
            "name": name,
            "status": "unavailable",
            "path": executable or "",
            "version": "",
            "checked_at": checked_at,
            "detail": "command not found" if executable is None else "",
        }
        if executable is None:
            commands.append(result)
            continue
        try:
            completed = subprocess.run(
                _doctor_version_command(executable, argv),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            result.update(status="error", detail="version command timed out")
        except OSError:
            result.update(status="error", detail="version command could not start")
        else:
            if completed.returncode == 0:
                first_line = (completed.stdout or completed.stderr).splitlines()
                result.update(
                    status="ok",
                    version=_redact_text(first_line[0].strip()) if first_line else "",
                )
            else:
                result.update(status="error", detail="version command failed")
        commands.append(result)
    managed = dependency_status(home)
    managed_entry = {
        "name": "donsetch-managed",
        "status": managed["status"],
        "path": managed["path"],
        "version": managed["version"],
        "tested_version": managed["tested_version"],
        "latest_version": managed["latest_version"],
        "checked_at": managed["checked_at"],
        "detail": managed["detail"],
    }
    global_detail = _global_donsetch_detail(timeout)
    if global_detail:
        managed_entry["detail"] = "; ".join(
            part for part in (managed_entry["detail"], global_detail) if part
        )
    commands.append(managed_entry)
    return {"checked_at": checked_at, "commands": commands}


DAY_SECONDS = 86_400
MATURITY_CACHE_TTL_SECONDS = 7 * DAY_SECONDS


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def kind_of(paths: list[str]) -> str:
    """Classify repositories of instructions without penalizing release cadence."""
    skillish = any(
        re.fullmatch(r"(?:SKILL\.md|skills|\.claude-plugin|\.codex-plugin|commands)", path, re.I)
        for path in paths
    )
    ops = operational_gap(paths)
    return "skills" if skillish and not any(ops[key] for key in ("compose", "helm", "k8s", "docker")) else "service"


def maturity_flags(record: dict[str, Any], now: str | float | None = None) -> dict[str, bool]:
    now_timestamp = _timestamp(now) if isinstance(now, str) else (now if now is not None else datetime.now(timezone.utc).timestamp())
    flags = {
        "alive": now_timestamp - _timestamp(record["pushed_at"]) < 90 * DAY_SECONDS,
        "adopted": record["stars"] > 500,
        "sustained": record["releases_12mo"] >= 2,
        "safe": bool(record["license"]) and not record["archived"],
        "bus": record["contributors"] >= 3,
    }
    if record.get("kind") == "skills":
        del flags["sustained"]
    return flags


def maturity_level(flags: dict[str, bool], record: dict[str, Any]) -> str:
    if record["archived"]:
        return "D"
    total = len(flags)
    positive = sum(flags.values())
    if positive == total:
        return "A"
    if positive == total - 1:
        return "B"
    if positive >= 2:
        return "C"
    return "D"


def operational_gap(paths: list[str]) -> dict[str, bool | str]:
    def has(pattern: str) -> bool:
        return any(re.fullmatch(pattern, path, re.I) is not None for path in paths)

    signals: dict[str, bool] = {
        "compose": has(r"(?:docker-)?compose\.ya?ml"),
        "helm": has(r"(?:charts?|helm)") or has(r"Chart\.ya?ml"),
        "k8s": has(r"(?:k8s|kubernetes|manifests|deploy)"),
        "docker": has(r"Dockerfile.*"),
        "pkg": has(r"(?:package\.json|pyproject\.toml|Cargo\.toml|go\.mod|composer\.json)"),
    }
    gap = "none" if signals["compose"] or signals["helm"] else "small" if signals["docker"] or signals["pkg"] else "large"
    return {**signals, "gap": gap}


def _gh_api(path: str, *, required: bool = False) -> Any | None:
    try:
        completed = subprocess.run(
            ("gh", "api", path),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as error:
        if required:
            raise RuntimeError("gh CLI not found") from error
        return None
    except subprocess.TimeoutExpired as error:
        if required:
            raise RuntimeError(f"gh api timed out for {path}") from error
        return None
    if completed.returncode != 0:
        if required:
            raise RuntimeError(f"cannot read GitHub API path: {path}")
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        if required:
            raise RuntimeError(f"GitHub API returned invalid JSON for {path}") from error
        return None


def probe_repository(slug: str, now: str | float | None = None) -> dict[str, Any]:
    canonical_slug = normalize_repo_slug(slug)
    now_timestamp = _timestamp(now) if isinstance(now, str) else (now if now is not None else datetime.now(timezone.utc).timestamp())
    repository = _gh_api(f"repos/{canonical_slug}", required=True)
    if not isinstance(repository, dict):
        raise RuntimeError(f"cannot read repos/{canonical_slug}")
    releases = _gh_api(f"repos/{canonical_slug}/releases?per_page=100")
    contributors = _gh_api(f"repos/{canonical_slug}/contributors?per_page=10")
    root = _gh_api(f"repos/{canonical_slug}/contents?ref={repository['default_branch']}")
    release_items = releases if isinstance(releases, list) else []
    contributor_items = contributors if isinstance(contributors, list) else []
    root_items = root if isinstance(root, list) else []
    paths = [item["name"] for item in root_items if isinstance(item, dict) and isinstance(item.get("name"), str)]
    record: dict[str, Any] = {
        "slug": canonical_slug,
        "kind": kind_of(paths),
        "stars": repository["stargazers_count"],
        "pushed_at": repository["pushed_at"],
        "archived": repository["archived"],
        "license": (repository.get("license") or {}).get("spdx_id"),
        "releases_12mo": sum(
            1
            for release in release_items
            if isinstance(release, dict)
            and isinstance(release.get("published_at") or release.get("created_at"), str)
            and now_timestamp - _timestamp(release.get("published_at") or release["created_at"])
            < 365 * DAY_SECONDS
        ),
        "contributors": len(contributor_items),
    }
    if record["license"] == "NOASSERTION":
        record["license"] = None
    record["flags"] = maturity_flags(record, now_timestamp)
    record["level"] = maturity_level(record["flags"], record)
    record["ops"] = {"gap": "n/a"} if record["kind"] == "skills" else operational_gap(paths)
    return record


def _maturity_cache_path(home: str | Path | None = None) -> Path:
    root = _resolved_root(wheel_home(home))
    return _contained_path(root, Path("cache") / "maturity.json", "cache path escapes WHEEL_HOME")


def load_maturity_cache(home: str | Path | None = None) -> dict[str, dict[str, Any]]:
    path = _maturity_cache_path(home)
    return _load_maturity_cache_path(path)


def _load_maturity_cache_path(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        cache = json.load(handle)
    if not isinstance(cache, dict):
        raise ValueError("maturity cache must contain an object")
    return cache


def save_maturity_cache(cache: dict[str, dict[str, Any]], home: str | Path | None = None) -> Path:
    path = _maturity_cache_path(home)
    with _file_lock(path):
        _atomic_json_write(path, cache)
    return path


def maturity_for(slugs: list[str], home: str | Path | None = None) -> list[dict[str, Any]]:
    now_timestamp = datetime.now(timezone.utc).timestamp()
    path = _maturity_cache_path(home)
    with _file_lock(path):
        cache = _load_maturity_cache_path(path)
        result: list[dict[str, Any]] = []
        for slug in slugs:
            canonical_slug = normalize_repo_slug(slug)
            entry = cache.get(canonical_slug)
            if entry is None or now_timestamp - _timestamp(entry["checked_at"]) >= MATURITY_CACHE_TTL_SECONDS:
                entry = {**probe_repository(canonical_slug, now_timestamp), "checked_at": _utc_now()}
                cache[canonical_slug] = entry
            result.append(entry)
        _atomic_json_write(path, cache)
    return result


DECISION_LIST_FIELDS: tuple[tuple[str, str], ...] = (
    ("Donors", "donors"),
    ("Integrations", "integrations"),
    ("Licenses", "licenses"),
    ("Rejected alternatives", "rejection_reasons"),
    ("Already included, enable?", "adjacent_capabilities"),
    ("Later, do not build", "not_building"),
)
DECISION_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {
        "accepted",
        "task",
        "coverage",
        "implementation_mode",
        "verdict",
        "picked",
        "maturity",
        "date",
        "candidates",
        "core",
        "upstream_or_fork",
        *(field for _, field in DECISION_LIST_FIELDS),
    }
)
DECISION_COVERAGE: frozenset[str] = frozenset({"COMPLETE", "PARTIAL"})
DECISION_MODES: frozenset[str] = frozenset(
    {"deploy", "package", "compose", "extend-core", "hard-fork", "assemble"}
)
DECISION_MATURITY: frozenset[str] = frozenset({"A", "B", "C", "D"})
SECRET_KEY_PATTERN = re.compile(
    r"(?i)(?:token|cookie|password|authorization|secret|api[_-]?key|private[_-]?key|access[_-]?key)"
)
CREDENTIAL_PATTERN = re.compile(
    r"(?i)(?:\bghp_[a-z0-9_-]+|\bgithub_pat_[a-z0-9_-]+|\bsk-[a-z0-9_-]+|\bbearer\s+\S+|(?:token|cookie|password|authorization|secret|api[_-]?key|private[_-]?key|access[_-]?key)\s*[:=]\s*\S+)"
)
WINDOWS_RESERVED_DECISION_SLUGS: frozenset[str] = frozenset(
    {"index", "con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)


def _decision_list(value: Any, field: str) -> list[str]:
    return _require_string_list(value, f"decision field {field}")


def _reject_secret_material(value: Any, subject: str) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str) or SECRET_KEY_PATTERN.search(key):
                raise ValueError(f"{subject} contains forbidden secret material")
            _reject_secret_material(nested, subject)
    elif isinstance(value, list):
        for nested in value:
            _reject_secret_material(nested, subject)
    elif isinstance(value, str) and CREDENTIAL_PATTERN.search(value):
        raise ValueError(f"{subject} contains forbidden secret material")


def _reject_decision_secrets(value: Any) -> None:
    _reject_secret_material(value, "decision")


def _reject_run_secrets(value: Any) -> None:
    _reject_secret_material(value, "run record")


def _decision_slug(decision: dict[str, Any]) -> str:
    explicit = decision.get("slug")
    if explicit is not None:
        if not isinstance(explicit, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", explicit):
            raise ValueError("invalid decision slug")
        slug = explicit
    else:
        core = decision["core"]
        if not isinstance(core, str) or not core.strip():
            raise ValueError("decision core must be a non-empty string")
        try:
            slug = normalize_repo_slug(core).replace("/", "-")
        except ValueError:
            slug = re.sub(r"[^a-z0-9]+", "-", core.casefold()).strip("-")
            if not slug:
                raise ValueError("decision core cannot produce a slug")
            slug = slug[:80]
    if slug.casefold() in WINDOWS_RESERVED_DECISION_SLUGS:
        raise ValueError("decision slug is reserved")
    return slug


def render_decision(decision: dict[str, Any]) -> str:
    """Render the accepted verdict as stable, complete Markdown."""
    _reject_decision_secrets(decision)
    missing = DECISION_REQUIRED_FIELDS.difference(decision)
    if missing:
        raise ValueError(f"decision missing fields: {', '.join(sorted(missing))}")
    if decision["accepted"] is not True:
        raise ValueError("decision must be explicitly accepted before recording")
    scalar_fields = (
        "task",
        "coverage",
        "implementation_mode",
        "verdict",
        "picked",
        "maturity",
        "date",
        "core",
        "upstream_or_fork",
    )
    if not all(isinstance(decision[field], str) and decision[field].strip() for field in scalar_fields):
        raise ValueError("decision scalar fields must be non-empty strings")
    if decision["coverage"] not in DECISION_COVERAGE:
        raise ValueError("invalid decision coverage")
    if decision["implementation_mode"] not in DECISION_MODES:
        raise ValueError("invalid decision implementation mode")
    if decision["maturity"] not in DECISION_MATURITY:
        raise ValueError("invalid decision maturity")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", decision["date"]):
        raise ValueError("decision date must use YYYY-MM-DD")
    if any("\n" in decision[field] or "\r" in decision[field] for field in ("verdict", "picked", "maturity", "date")):
        raise ValueError("decision front matter values must be single-line")
    candidates = _decision_list(decision["candidates"], "candidates")
    slug = _decision_slug(decision)
    sections = [
        "---",
        f"name: {slug}",
        f"coverage: {decision['coverage']}",
        f"verdict: {decision['verdict']}",
        f"picked: {decision['picked']}",
        f"maturity: {decision['maturity']}",
        f"date: {decision['date']}",
        "---",
        "",
        "## Request",
        decision["task"],
        "",
        "## Coverage",
        decision["coverage"],
        "",
        "## Candidates",
        *([f"- {candidate}" for candidate in candidates] or ["- None"]),
        "",
        "## Decided",
        decision["verdict"],
        "",
        "## Core and integration",
        f"- Core: {decision['core']}",
        f"- Upstream or fork: {decision['upstream_or_fork']}",
        f"- Mode: {decision['implementation_mode']}",
    ]
    for heading, field in DECISION_LIST_FIELDS[:3]:
        values = _decision_list(decision[field], field)
        sections.extend([] if heading == "Donors" else [""])
        if heading == "Donors":
            sections.extend(["", "## Donors and licenses"])
        else:
            sections.append(f"### {heading}")
        sections.extend([f"- {value}" for value in values] or ["- None"])
    for heading, field in DECISION_LIST_FIELDS[3:]:
        values = _decision_list(decision[field], field)
        sections.extend(["", f"## {heading}"])
        sections.extend([f"- {value}" for value in values] or ["- None"])
    return "\n".join(sections) + "\n"


def _front_matter_value(path: Path, field: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[1:8]:
            if line.startswith(f"{field}: "):
                return line.removeprefix(f"{field}: ")
    except OSError:
        return path.stem
    return path.stem


def _render_decision_index(directory: Path) -> str:
    records = sorted(path for path in directory.glob("*.md") if path.name != "INDEX.md")
    lines = ["# Wheel decisions", ""]
    lines.extend(
        f"- [{_front_matter_value(record, 'verdict')}]({record.name})" for record in records
    )
    return "\n".join(lines) + "\n"


def record_decision(
    decision: dict[str, Any],
    scope: Literal["global", "project"],
    *,
    project_root: str | Path | None = None,
    home: str | Path | None = None,
) -> Path:
    rendered = render_decision(decision)
    if scope == "global":
        root = _resolved_root(wheel_home(home))
        relative_directory = Path("decisions")
    elif scope == "project":
        if project_root is None:
            raise ValueError("project_root is required for project decisions")
        root = _resolved_root(project_root)
        relative_directory = Path(".wheel") / "decisions"
    else:
        raise ValueError("scope must be global or project")
    path = _contained_path(
        root,
        relative_directory / f"{_decision_slug(decision)}.md",
        "decision path escapes its directory",
    )
    index_path = _contained_path(
        root, relative_directory / "INDEX.md", "decision path escapes its directory"
    )
    with _file_lock(index_path):
        with _file_lock(path):
            _atomic_text_write(path, rendered)
            _atomic_text_write(index_path, _render_decision_index(path.parent))
    return path


def _source_result_from_input(path: str) -> SourceResult:
    with Path(path).expanduser().open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("source input must contain an object")
    _reject_run_secrets(data)
    source = _require_non_empty_string(data["source"], "source")
    errors: list[str] = []
    evidence: list[Evidence] = []
    raw_evidence = data.get("evidence", [])
    if not isinstance(raw_evidence, list):
        raw_evidence = []
        errors.append("evidence must be a list")
    for index, item in enumerate(raw_evidence):
        try:
            if not isinstance(item, dict):
                raise ValueError("must be an object")
            payload = dict(item)
            payload.setdefault("source", source)
            normalized = Evidence(**payload)
            if normalized.candidate:
                normalize_candidate_id(normalized.candidate)
            evidence.append(normalized)
        except (KeyError, TypeError, ValueError) as error:
            errors.append(f"evidence[{index}]: {error}")
    edges: list[dict[str, str]] = []
    raw_edges = data.get("edges", [])
    if not isinstance(raw_edges, list):
        raw_edges = []
        errors.append("edges must be a list")
    for index, item in enumerate(raw_edges):
        try:
            edges.append(_normalize_edge(item))
        except ValueError as error:
            errors.append(f"edges[{index}]: {error}")
    error_text = "; ".join(filter(None, [data.get("error", ""), *errors]))
    status = data["status"]
    if errors:
        status = "partial" if evidence or edges else "error"
    return SourceResult(
        source=source,
        status=status,
        checked_at=data["checked_at"],
        query=data.get("query", ""),
        evidence=evidence,
        error=error_text,
        edges=edges,
    )


RUN_UPDATE_FIELDS: frozenset[str] = frozenset(
    {"context", "families", "user_answers", "tool_snapshot", "candidates", "edges"}
)


def _validate_run_update(update: Any) -> dict[str, Any]:
    update = _require_json_object(update, "run update")
    _reject_run_secrets(update)
    if not update:
        raise ValueError("run update must not be empty")
    unknown = set(update).difference(RUN_UPDATE_FIELDS)
    if unknown:
        raise ValueError(f"unsupported run update fields: {', '.join(sorted(unknown))}")
    validated: dict[str, Any] = {}
    if "context" in update:
        validated["context"] = _require_json_object(update["context"], "context")
    if "families" in update:
        validated["families"] = _require_json_object_list(update["families"], "families")
    if "user_answers" in update:
        validated["user_answers"] = _require_json_object_list(update["user_answers"], "user_answers")
    if "tool_snapshot" in update:
        validated["tool_snapshot"] = _require_json_object(update["tool_snapshot"], "tool_snapshot")
    if "candidates" in update:
        raw_candidates = _require_json_object(update["candidates"], "candidates")
        candidates: dict[str, Candidate] = {}
        for candidate_id, raw_candidate in raw_candidates.items():
            candidate_kind, canonical_id = normalize_candidate_id(candidate_id)
            candidate = _candidate_from_dict(raw_candidate)
            if candidate.candidate_id != canonical_id or candidate.kind != candidate_kind:
                raise ValueError("candidate id must match its canonical map key")
            candidates[canonical_id] = candidate
        validated["candidates"] = candidates
    if "edges" in update:
        if not isinstance(update["edges"], list):
            raise ValueError("edges must be a list")
        validated["edges"] = [_normalize_edge(edge) for edge in update["edges"]]
    return validated


def _load_json_object(path: str, message: str) -> dict[str, Any]:
    with Path(path).expanduser().open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(message)
    return data


def _add_home_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", help="WHEEL_HOME override")


def _json_output(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Portable deterministic Wheel runtime")
    commands = parser.add_subparsers(dest="command", required=True)

    init_run = commands.add_parser("init-run")
    init_run.add_argument("--task", required=True)
    _add_home_argument(init_run)

    add_source = commands.add_parser("add-source")
    add_source.add_argument("--run-id", required=True)
    add_source.add_argument("--input", required=True)
    _add_home_argument(add_source)

    transition = commands.add_parser("transition")
    transition.add_argument("--run-id", required=True)
    transition.add_argument("--to", choices=sorted(RUN_STATES), required=True)
    _add_home_argument(transition)

    validate = commands.add_parser("validate-run")
    validate.add_argument("--run-id", required=True)
    _add_home_argument(validate)

    update = commands.add_parser("update-run")
    update.add_argument("--run-id", required=True)
    update.add_argument("--input", required=True)
    _add_home_argument(update)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    _add_home_argument(doctor)

    dependencies = commands.add_parser("dependencies")
    dependencies.add_argument("--ensure", action="store_true")
    dependencies.add_argument("--check-latest", action="store_true")
    dependencies.add_argument("--json", action="store_true", dest="as_json")
    _add_home_argument(dependencies)

    read_url = commands.add_parser("read-url")
    read_url.add_argument("url")
    read_url.add_argument("--focus")
    read_url.add_argument("--require-field", action="append", default=[], dest="required_fields")
    read_url.add_argument("--json", action="store_true", dest="as_json")
    _add_home_argument(read_url)

    maturity = commands.add_parser("maturity")
    maturity.add_argument("slugs", nargs="+")
    maturity.add_argument("--json", action="store_true", dest="as_json")
    _add_home_argument(maturity)

    decision = commands.add_parser("record-decision")
    decision.add_argument("--input", required=True)
    decision.add_argument("--scope", choices=("global", "project"), required=True)
    decision.add_argument("--project-root")
    _add_home_argument(decision)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init-run":
        record = RunStore(args.home).create(args.task)
        print(RunStore(args.home)._run_path(record.run_id))
    elif args.command == "add-source":
        record = RunStore(args.home).add_source_result(args.run_id, _source_result_from_input(args.input))
        _json_output(record.to_dict())
    elif args.command == "transition":
        record = RunStore(args.home).transition(args.run_id, args.to)
        _json_output(record.to_dict())
    elif args.command == "validate-run":
        _json_output(RunStore(args.home).load(args.run_id).to_dict())
    elif args.command == "update-run":
        record = RunStore(args.home).update(
            args.run_id, _load_json_object(args.input, "run update input must contain an object")
        )
        _json_output(record.to_dict())
    elif args.command == "doctor":
        report = run_doctor(home=args.home)
        if args.as_json:
            _json_output(report)
        else:
            for command in report["commands"]:
                version = command["version"] or command["detail"]
                print(f"{command['name']}: {command['status']} {version}".rstrip())
    elif args.command == "dependencies":
        report = ensure_dependency(args.home) if args.ensure else dependency_status(
            args.home, check_latest=args.check_latest
        )
        if args.ensure and args.check_latest and report["status"] in {"current", "update_available"}:
            report = dependency_status(args.home, check_latest=True)
        if args.as_json:
            _json_output(report)
        else:
            version = report["version"] or report["tested_version"]
            detail = f" {report['detail']}" if report["detail"] else ""
            print(f"{report['id']}: {report['status']} {version}{detail}".rstrip())
        if args.ensure and report["status"] in {"error", "repair_required", "unavailable"}:
            return 1
    elif args.command == "read-url":
        report = donsetch_fetch(
            args.url,
            args.home,
            required_fields=tuple(args.required_fields),
            focus=args.focus,
        )
        if args.as_json:
            _json_output(report)
        else:
            detail = f" {report['detail']}" if report["detail"] else ""
            print(f"donsetch: {report['status']}{detail}")
    elif args.command == "maturity":
        entries = maturity_for(args.slugs, args.home)
        if args.as_json:
            _json_output(entries)
        else:
            for entry in entries:
                enabled = " ".join(name for name, value in entry["flags"].items() if value) or "-"
                print(f"{entry['level']}  {entry['slug']}  ★{entry['stars']}  deploy-gap:{entry['ops']['gap']}  [{enabled}]")
    elif args.command == "record-decision":
        decision = _load_json_object(args.input, "decision input must contain an object")
        print(record_decision(decision, args.scope, project_root=args.project_root, home=args.home))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
