"""Dependency debt analyzer for repositories.

Measures transitive dependency debt via GitHub SBOM or falls back to direct
manifest parsing.
"""

from __future__ import annotations

from pathlib import Path
import importlib.util
import importlib
import argparse
import base64
import json
import re
import sys
import tomllib
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


_core = _wheel_core()
_gh_api = getattr(_core, "_gh_api")


SUPPORTED_MANIFESTS: dict[str, str] = {
    "package.json": "npm",
    "requirements.txt": "pypi",
    "pyproject.toml": "pypi",
    "Cargo.toml": "cargo",
    "go.mod": "go",
    "Gemfile": "rubygems",
    "composer.json": "packagist",
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_sbom(slug: str) -> dict[str, Any]:
    """Fetch SBOM from GitHub API.

    Returns {'status': 'ok', 'packages': [...]} on success.
    On 404/403/None returns {'status': 'unavailable', 'reason': ...}.
    """
    path = f"repos/{slug}/dependency-graph/sbom"
    try:
        data = _gh_api(path)
    except Exception as exc:
        return {"status": "unavailable", "reason": f"gh api error: {exc}"}

    if not isinstance(data, dict):
        return {
            "status": "unavailable",
            "reason": "SBOM endpoint unavailable or empty response",
        }

    sbom = data.get("sbom")
    if not isinstance(sbom, dict):
        return {
            "status": "unavailable",
            "reason": "malformed SBOM payload (missing sbom root)",
        }

    packages = sbom.get("packages")
    if not isinstance(packages, list):
        return {
            "status": "unavailable",
            "reason": "malformed SBOM payload (packages is not a list)",
        }

    return {
        "status": "ok",
        "packages": packages,
        "documentDescribes": sbom.get("documentDescribes"),
        "sbom": sbom,
    }


def _parse_purl_ecosystem(purl: str) -> str | None:
    if not purl or not isinstance(purl, str):
        return None
    # PURL format: pkg:<type>/...
    match = re.match(r"^pkg:([a-zA-Z0-9_-]+)/", purl.strip())
    if match:
        return match.group(1).lower()
    return None


def count_from_sbom(sbom: dict[str, Any]) -> dict[str, Any]:
    """Count packages, ecosystems, and unlicensed packages derived only from SBOM."""
    packages = sbom.get("packages", []) if isinstance(sbom, dict) else []
    if not isinstance(packages, list):
        packages = []

    # Exclude root package before counting:
    # Use the SBOM's `documentDescribes` list and the SPDXID it names,
    # and additionally skip a package whose SPDXID matches the SPDXRef-Repository prefix.
    root_spdxids: set[str] = set()
    doc_describes = sbom.get("documentDescribes") if isinstance(sbom, dict) else None
    if (
        doc_describes is None
        and isinstance(sbom, dict)
        and isinstance(sbom.get("sbom"), dict)
    ):
        doc_describes = sbom["sbom"].get("documentDescribes")

    if isinstance(doc_describes, list):
        for item in doc_describes:
            if isinstance(item, str):
                root_spdxids.add(item)
    elif isinstance(doc_describes, str):
        root_spdxids.add(doc_describes)

    filtered_packages: list[dict[str, Any]] = []
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        spdx_id = pkg.get("SPDXID")
        if isinstance(spdx_id, str):
            if spdx_id in root_spdxids or spdx_id.startswith("SPDXRef-Repository"):
                continue
        filtered_packages.append(pkg)

    total = len(filtered_packages)
    ecosystems: dict[str, int] = {}
    unlicensed = 0

    for pkg in filtered_packages:
        if not isinstance(pkg, dict):
            continue

        # Check license
        license_concluded = pkg.get("licenseConcluded")
        license_declared = pkg.get("licenseDeclared")
        has_license = False
        for lic in (license_concluded, license_declared):
            if (
                lic
                and isinstance(lic, str)
                and lic.strip()
                and lic.strip().upper() not in {"NOASSERTION", "NONE"}
            ):
                has_license = True
                break
        if not has_license:
            unlicensed += 1

        # Ecosystem from purl or externalRefs
        eco: str | None = None
        purl = pkg.get("purl")
        if purl:
            eco = _parse_purl_ecosystem(purl)

        if not eco:
            ext_refs = pkg.get("externalRefs")
            if isinstance(ext_refs, list):
                for ref in ext_refs:
                    if isinstance(ref, dict) and ref.get("referenceType") == "purl":
                        eco = _parse_purl_ecosystem(ref.get("referenceLocator", ""))
                        if eco:
                            break

        if not eco:
            eco = "unknown"

        ecosystems[eco] = ecosystems.get(eco, 0) + 1

    return {
        "total": total,
        "ecosystems": ecosystems,
        "unlicensed": unlicensed,
    }


def _parse_package_json(content: str) -> int | None:
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            return None
        deps = set()
        for key in (
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
        ):
            section = data.get(key)
            if isinstance(section, dict):
                deps.update(section.keys())
        return len(deps)
    except Exception:
        return None


def _parse_requirements_txt(content: str) -> int | None:
    try:
        count = 0
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(
                ("-r", "--requirement", "-f", "--find-links", "-i", "--index-url")
            ):
                continue
            count += 1
        return count
    except Exception:
        return None


def _parse_pyproject_toml(content: str) -> int | None:
    try:
        data = tomllib.loads(content)
        deps = set()
        project = data.get("project", {})
        if isinstance(project, dict):
            std_deps = project.get("dependencies", [])
            if isinstance(std_deps, list):
                deps.update(str(d) for d in std_deps)
            opt_deps = project.get("optional-dependencies", {})
            if isinstance(opt_deps, dict):
                for group, dlist in opt_deps.items():
                    if isinstance(dlist, list):
                        deps.update(str(d) for d in dlist)

        poetry = (
            data.get("tool", {}).get("poetry", {})
            if isinstance(data.get("tool"), dict)
            else {}
        )
        if isinstance(poetry, dict):
            p_deps = poetry.get("dependencies", {})
            if isinstance(p_deps, dict):
                deps.update(k for k in p_deps.keys() if k.lower() != "python")
            p_dev = poetry.get("dev-dependencies", {})
            if isinstance(p_dev, dict):
                deps.update(p_dev.keys())
            group = poetry.get("group", {})
            if isinstance(group, dict):
                for _, gdata in group.items():
                    if isinstance(gdata, dict) and isinstance(
                        gdata.get("dependencies"), dict
                    ):
                        deps.update(gdata["dependencies"].keys())

        return len(deps)
    except Exception:
        return None


def _parse_cargo_toml(content: str) -> int | None:
    try:
        data = tomllib.loads(content)
        deps = set()
        for key in ("dependencies", "dev-dependencies", "build-dependencies"):
            section = data.get(key)
            if isinstance(section, dict):
                deps.update(section.keys())
        target = data.get("target")
        if isinstance(target, dict):
            for _, tdata in target.items():
                if isinstance(tdata, dict):
                    for key in (
                        "dependencies",
                        "dev-dependencies",
                        "build-dependencies",
                    ):
                        section = tdata.get(key)
                        if isinstance(section, dict):
                            deps.update(section.keys())
        return len(deps)
    except Exception:
        return None


def _parse_go_mod(content: str) -> int | None:
    try:
        count = 0
        in_require = False
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            if line.startswith("require ("):
                in_require = True
                continue
            if in_require:
                if line == ")":
                    in_require = False
                else:
                    count += 1
                continue
            if line.startswith("require "):
                count += 1
        return count
    except Exception:
        return None


def _parse_gemfile(content: str) -> int | None:
    try:
        count = 0
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if re.match(r"^gem\s+['\"][^'\"]+['\"]", line):
                count += 1
        return count
    except Exception:
        return None


def _parse_composer_json(content: str) -> int | None:
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            return None
        deps = set()
        for key in ("require", "require-dev"):
            section = data.get(key)
            if isinstance(section, dict):
                deps.update(k for k in section.keys() if k.lower() != "php")
        return len(deps)
    except Exception:
        return None


_MANIFEST_PARSERS = {
    "package.json": _parse_package_json,
    "requirements.txt": _parse_requirements_txt,
    "pyproject.toml": _parse_pyproject_toml,
    "Cargo.toml": _parse_cargo_toml,
    "go.mod": _parse_go_mod,
    "Gemfile": _parse_gemfile,
    "composer.json": _parse_composer_json,
}


def _decode_manifest_content(entry: dict[str, Any]) -> str | None:
    content = entry.get("content")
    encoding = entry.get("encoding")
    if not content or not isinstance(content, str):
        return None
    if encoding == "base64":
        try:
            raw = base64.b64decode(content.encode("utf-8"))
            return raw.decode("utf-8")
        except Exception:
            return None
    elif encoding is None or encoding == "":
        return content
    return None


def fetch_direct_manifests(slug: str) -> list[dict[str, Any]]:
    """Fetch direct manifests from repository contents.

    For each name in SUPPORTED_MANIFESTS, calls _gh_api, decodes content,
    and counts direct dependencies with a conservative parser.
    Returns [{'manifest': ..., 'ecosystem': ..., 'direct': int | None}].
    """
    results: list[dict[str, Any]] = []
    for manifest_name, ecosystem in SUPPORTED_MANIFESTS.items():
        path = f"repos/{slug}/contents/{manifest_name}"
        res = _gh_api(path)
        if not isinstance(res, dict) or "content" not in res:
            continue

        raw_text = _decode_manifest_content(res)
        parser = _MANIFEST_PARSERS.get(manifest_name)
        direct_count = None
        if raw_text is not None and parser is not None:
            direct_count = parser(raw_text)

        results.append(
            {
                "manifest": manifest_name,
                "ecosystem": ecosystem,
                "direct": direct_count,
            }
        )
    return results


def analyze_dependency_debt(slug: str) -> dict[str, Any]:
    """Analyze dependency debt for repository slug.

    Prefers the SBOM; falls back to manifests with status 'partial' and transitive None.
    Returns:
      {'slug', 'checked_at', 'status', 'direct', 'transitive', 'ecosystems',
       'unlicensed', 'manifests', 'notes': [...]}
    """
    checked_at = _iso_now()
    notes: list[str] = []

    sbom_res = fetch_sbom(slug)
    manifests = fetch_direct_manifests(slug)

    # Compute direct count if any manifest has a count.
    # D4: Make top-level direct None whenever any discovered manifest has direct=None,
    # and record the reason in notes.
    if any(m.get("direct") is None for m in manifests):
        total_direct = None
        unparseable = [
            m.get("manifest", "unknown") for m in manifests if m.get("direct") is None
        ]
        notes.append(
            f"Incomplete direct dependency count: manifests could not be parsed ({', '.join(unparseable)})"
        )
    elif manifests:
        total_direct = sum(m.get("direct", 0) for m in manifests)
    else:
        total_direct = 0

    if sbom_res.get("status") == "ok":
        counts = count_from_sbom(sbom_res)
        transitive = counts["total"]
        ecosystems = counts["ecosystems"]
        unlicensed = counts["unlicensed"]
        status = "ok"
        notes.append("Transitive dependency counts derived from GitHub SBOM")
    else:
        status = "partial"
        transitive = None
        notes.append(f"SBOM unavailable: {sbom_res.get('reason', 'unknown reason')}")
        # Build ecosystem map from parsed manifests
        eco_map: dict[str, int] = {}
        for m in manifests:
            eco = m.get("ecosystem", "unknown")
            cnt = m.get("direct")
            if cnt is not None:
                eco_map[eco] = eco_map.get(eco, 0) + cnt
        ecosystems = eco_map
        unlicensed = None

    return {
        "slug": slug,
        "checked_at": checked_at,
        "status": status,
        "direct": total_direct,
        "transitive": transitive,
        "ecosystems": ecosystems,
        "unlicensed": unlicensed,
        "manifests": manifests,
        "notes": notes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure transitive and direct dependency debt for a repository."
    )
    parser.add_argument("--slug", required=True, help="Repository slug (owner/repo)")
    parser.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args(argv)
    result = analyze_dependency_debt(args.slug)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Dependency Debt: {result['slug']}")
        print(f"Status: {result['status']}")
        print(f"Direct dependencies: {result['direct']}")
        print(f"Transitive dependencies: {result['transitive']}")
        print(f"Unlicensed dependencies: {result['unlicensed']}")
        print(f"Ecosystems: {result['ecosystems']}")
        if result["manifests"]:
            print("Manifests found:")
            for m in result["manifests"]:
                print(f"  - {m['manifest']} ({m['ecosystem']}): direct={m['direct']}")
        if result["notes"]:
            print("Notes:")
            for note in result["notes"]:
                print(f"  - {note}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
