"""Publish the built catalog to the Cloudflare KV edge.

Runs in CI only (.github/workflows/sync-catalog.yml). The edge is the second
origin: without it a client still reads the `data` branch through jsDelivr and
raw.githubusercontent.com, both of which are GitHub. With it there is an origin
that survives GitHub being unavailable.

Uploading through the Cloudflare API rather than wrangler keeps the step free of
a Node toolchain, and keeps the failure surface to one HTTPS call per object.

The API token is read from the environment and is never logged, echoed, or
written anywhere: only its absence is ever reported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_ROOT = "https://api.cloudflare.com/client/v4"
MAX_UPLOAD_BYTES = 24 * 1024 * 1024  # KV rejects values above 25 MB.
TIMEOUT_SECONDS = 60


class EdgeError(RuntimeError):
    """The upload could not be completed, with a reason that names no secret."""


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise EdgeError(f"{name} is not set")
    return value


def _multipart(value: bytes, metadata: dict[str, Any]) -> tuple[bytes, str]:
    """Build the value plus metadata body KV expects, without a dependency."""
    boundary = "----wheel" + hashlib.sha256(value).hexdigest()[:24]
    parts: list[bytes] = []
    for name, payload, content_type in (
        ("value", value, "application/octet-stream"),
        (
            "metadata",
            json.dumps(metadata, sort_keys=True).encode("utf-8"),
            "application/json",
        ),
    ):
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(
            f'Content-Disposition: form-data; name="{name}"\r\n'.encode("utf-8")
        )
        parts.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        parts.append(payload)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), boundary


def put_object(key: str, path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Write one file into the KV namespace and return what the API reported."""
    account = _env("CLOUDFLARE_ACCOUNT_ID")
    namespace = _env("CLOUDFLARE_KV_NAMESPACE_ID")
    token = _env("CLOUDFLARE_API_TOKEN")

    payload = path.read_bytes()
    if not payload:
        raise EdgeError(f"{path.name} is empty; refusing to publish")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise EdgeError(f"{path.name} exceeds the {MAX_UPLOAD_BYTES} byte KV limit")

    body, boundary = _multipart(payload, metadata)
    url = (
        f"{API_ROOT}/accounts/{account}/storage/kv/namespaces/{namespace}/values/{key}"
    )
    request = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise EdgeError(f"KV rejected {key}: HTTP {error.code} {detail}") from error
    except OSError as error:
        raise EdgeError(f"KV upload failed for {key}: {error}") from error
    if not answer.get("success"):
        messages = [str(item.get("message")) for item in (answer.get("errors") or [])]
        raise EdgeError(f"KV rejected {key}: {'; '.join(messages) or 'unknown error'}")
    return answer


def publish(catalog: Path, meta: Path) -> dict[str, Any]:
    """Upload both objects, tagging each with an ETag the Worker can serve."""
    uploaded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    records = sum(
        1 for line in catalog.read_text(encoding="utf-8").splitlines() if line.strip()
    )
    report: dict[str, Any] = {"uploaded_at": uploaded_at, "records": records}
    for key, path in (("catalog.jsonl", catalog), ("catalog.meta.json", meta)):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        put_object(
            key,
            path,
            {"etag": digest, "uploaded_at": uploaded_at, "records": records},
        )
        report[key] = {"bytes": path.stat().st_size, "etag": digest}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="publish_edge", description="publish the catalog to the Cloudflare KV edge"
    )
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--meta", required=True)
    args = parser.parse_args(argv)

    try:
        report = publish(Path(args.catalog), Path(args.meta))
    except EdgeError as error:
        print(f"edge publish failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
