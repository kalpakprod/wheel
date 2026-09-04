"""Community regret and postmortem signals search.

Searches Hacker News (Algolia), Reddit, and GitHub issues for negative sentiment,
migration away, and postmortem discussions for a given project/technology.
Uses existing security primitives from scripts.wheel.
"""

from __future__ import annotations

from pathlib import Path
import base64
import importlib.util
import importlib
import argparse
from datetime import datetime, timezone
import json
import os
import time
import sys
import urllib.error
import urllib.parse
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


_bounded_https_download = getattr(_wheel_core(), "_bounded_https_download")
_gh_api = getattr(_wheel_core(), "_gh_api")
_timestamp = getattr(_wheel_core(), "_timestamp")


MAX_RESPONSE_BYTES = 256 * 1024  # 256 KiB
MAX_ITEMS_PER_SOURCE = 25
MAX_TITLE_CHARS = 200

NEGATIVE_TERMS: tuple[str, ...] = (
    "migrated away from",
    "moved off",
    "switching from",
    "postmortem",
    "we regret",
    "abandoned",
    "problems with",
    "alternative to",
)

HN_ALLOWED_HOSTS: frozenset[str] = frozenset({"hn.algolia.com"})
SEARCH_PROBE_PAUSE_SECONDS = 2.5
STACKEXCHANGE_ALLOWED_HOSTS: frozenset[str] = frozenset({"api.stackexchange.com"})
REDDIT_ALLOWED_HOSTS: frozenset[str] = frozenset({"www.reddit.com"})
REDDIT_TOKEN_HOSTS: frozenset[str] = frozenset({"www.reddit.com"})
REDDIT_OAUTH_HOSTS: frozenset[str] = frozenset({"oauth.reddit.com"})

# Built-in English defaults preserved for backwards compatibility and fallback
PROBE_TERMS: tuple[str, ...] = (
    "postmortem",
    "migrated away",
    "alternative to",
    "problems with",
)

_PROBE_TERMS_REGISTRY_PATH = (
    Path(__file__).resolve().parents[1] / "registry" / "probe_terms.yaml"
)
MAX_PROBE_TERMS: int = 8
_REDDIT_TOKEN_CACHE: dict[str, str] = {}


def load_probe_terms(registry_path: Path | None = None) -> list[str]:
    """Resolve probe terms across mandatory English and optional WHEEL_PROBE_LANGS.

    Loads from registry/probe_terms.yaml if present. Falls back to built-in PROBE_TERMS
    if the registry file is absent. Total returned probe terms are capped at 8.
    """
    path = registry_path or _PROBE_TERMS_REGISTRY_PATH
    if not path.is_file():
        return list(PROBE_TERMS[:MAX_PROBE_TERMS])

    try:
        import yaml
    except ImportError as error:
        raise RuntimeError("pyyaml is required to read probe terms registry") from error

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(
            f"{path.name} must be a mapping of language codes to phrase lists"
        )

    # English is mandatory
    raw_en = data.get("en")
    en_terms = list(raw_en) if isinstance(raw_en, (list, tuple)) else list(PROBE_TERMS)
    selected_terms: list[str] = []
    for term in en_terms:
        term_str = str(term).strip()
        if term_str and term_str not in selected_terms:
            selected_terms.append(term_str)

    # Optional extra languages from WHEEL_PROBE_LANGS
    env_langs = os.environ.get("WHEEL_PROBE_LANGS", "")
    if env_langs:
        for lang_code in env_langs.split(","):
            lang = lang_code.strip().lower()
            if not lang or lang == "en":
                continue
            lang_phrases = data.get(lang)
            if isinstance(lang_phrases, (list, tuple)):
                for term in lang_phrases:
                    term_str = str(term).strip()
                    if term_str and term_str not in selected_terms:
                        selected_terms.append(term_str)
                        if len(selected_terms) >= MAX_PROBE_TERMS:
                            break
            if len(selected_terms) >= MAX_PROBE_TERMS:
                break

    return selected_terms[:MAX_PROBE_TERMS]


def _reddit_http_request(
    url: str,
    *,
    allowed_hosts: frozenset[str] | set[str],
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    max_bytes: int = 512 * 1024,
    timeout: float = 10.0,
) -> tuple[int, bytes, dict[str, str]]:
    """Bounded HTTP request helper mirroring _bounded_https_download defenses.

    Enforces: HTTPS only, strict host allowlist, response size cap, no cross-host redirects.
    Supports POST bodies and custom headers (e.g. Basic auth, Bearer tokens).
    """
    import urllib.error
    import urllib.request

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"refusing non-https url: {url}")
    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts:
        raise ValueError(f"host {host!r} not in allowed hosts: {allowed_hosts}")

    req = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET"
    )
    req.add_header("User-Agent", "wheel-plugin/1.0 (community-signals)")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    class _NoCrossHostRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            new_parsed = urllib.parse.urlparse(newurl)
            if new_parsed.scheme.lower() != "https":
                raise urllib.error.HTTPError(
                    newurl, code, "redirect to non-https", headers, fp
                )
            new_host = (new_parsed.hostname or "").lower()
            if new_host not in allowed_hosts:
                raise urllib.error.HTTPError(
                    newurl,
                    code,
                    f"redirect to unapproved host: {new_host}",
                    headers,
                    fp,
                )
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(_NoCrossHostRedirectHandler())
    with opener.open(req, timeout=timeout) as resp:
        body = resp.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError(f"response body exceeded {max_bytes} bytes")
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status, body, resp_headers


def _truncate_title(title: Any) -> str:
    if not isinstance(title, str):
        title = str(title or "")
    return title[:MAX_TITLE_CHARS]


def _dead_source_status(reasons: list[str]) -> str:
    """Name the failure precisely: the server answered, or it never did.

    Every probe failing with an HTTP status is an `error` \u2014 the service is up and
    said no. Anything else (DNS, timeout, TLS, a shape we cannot read) is
    `unavailable`. Collapsing both into one word loses the only detail that tells a
    reader whether retrying can help.
    """
    if reasons and all("HTTP " in reason for reason in reasons):
        return "error"
    return "unavailable"


def _merge_matches(
    collected: list[dict[str, Any]], new: list[dict[str, Any]], limit: int
) -> None:
    seen = {match.get("url") for match in collected}
    for match in new:
        if len(collected) >= limit:
            return
        url = match.get("url")
        if url in seen:
            continue
        seen.add(url)
        collected.append(match)


def _hn_page(query: str, limit: int) -> tuple[list[dict[str, Any]], str | None]:
    """One Algolia request. Returns (matches, error reason)."""
    params = urllib.parse.urlencode(
        {"query": query, "tags": "story", "hitsPerPage": str(limit)}
    )
    url = f"https://hn.algolia.com/api/v1/search?{params}"
    try:
        raw_bytes = _bounded_https_download(
            url, allowed_hosts=set(HN_ALLOWED_HOSTS), max_bytes=MAX_RESPONSE_BYTES
        )
    except urllib.error.HTTPError as exc:
        return [], f"HTTP {exc.code}"
    except Exception as exc:
        return [], str(exc)
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        return [], f"Invalid JSON payload: {exc}"
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        return [], "Missing or non-list hits in payload"
    matches: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        matches.append(
            {
                "title": _truncate_title(
                    hit.get("title") or hit.get("story_title") or ""
                ),
                "url": hit.get("url")
                or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}",
                "points": hit.get("points"),
                "created_at": hit.get("created_at"),
                "term": query,
            }
        )
    return matches, None


def search_hackernews(query: str, limit: int = 25) -> dict:
    """Search Hacker News for regret signals about `query`.

    Algolia ANDs every word in a single query, so one request carrying every
    negative term at once matches nothing — which reads as "no complaints found"
    when it actually means "the query was malformed". One request per probe term,
    merged and deduplicated, is what produces a real answer.
    """
    bounded_limit = min(limit, MAX_ITEMS_PER_SOURCE)
    per_term = bounded_limit
    terms = load_probe_terms()
    collected: list[dict[str, Any]] = []
    reasons: list[str] = []
    probed = 0
    for term in terms:
        matches, reason = _hn_page(f"{query} {term}", per_term)
        if reason is not None:
            reasons.append(f"{term}: {reason}")
            continue
        probed += 1
        _merge_matches(collected, matches, bounded_limit)
    if probed == 0:
        return {
            "source": "hackernews",
            "status": _dead_source_status(reasons),
            "query": query,
            "reason": "; ".join(reasons) or "no probe succeeded",
            "matches": [],
        }
    result = {
        "source": "hackernews",
        "status": "ok" if probed == len(terms) else "partial",
        "query": query,
        "terms_probed": probed,
        "terms_total": len(terms),
        "matches": collected,
    }
    if reasons:
        result["reason"] = "; ".join(reasons)
    return result


def _get_reddit_oauth_token() -> str | None:
    """Fetch an application-only Reddit OAuth token using WHEEL_REDDIT_CLIENT_ID / SECRET.

    Does NOT attempt OAuth if WHEEL_REDDIT_CLIENT_ID is unset.
    Caches token in-memory in _REDDIT_TOKEN_CACHE for the process only.
    Never prints or logs credentials.
    """
    client_id = os.environ.get("WHEEL_REDDIT_CLIENT_ID", "").strip()
    if not client_id:
        return None

    cached = _REDDIT_TOKEN_CACHE.get("access_token")
    if cached:
        return cached

    client_secret = os.environ.get("WHEEL_REDDIT_CLIENT_SECRET", "").strip()
    user_pass = f"{client_id}:{client_secret}".encode("utf-8")
    basic_auth = base64.b64encode(user_pass).decode("ascii")

    token_url = "https://www.reddit.com/api/v1/access_token"
    data = urllib.parse.urlencode(
        {
            "grant_type": "https://oauth.reddit.com/grants/installed_client",
            "device_id": "DO_NOT_TRACK_THIS_DEVICE",
        }
    ).encode("utf-8")

    headers = {
        "Authorization": f"Basic {basic_auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        status, body, _ = _reddit_http_request(
            token_url,
            allowed_hosts=REDDIT_TOKEN_HOSTS,
            headers=headers,
            data=data,
            max_bytes=64 * 1024,
            timeout=10.0,
        )
    except Exception as exc:
        raise RuntimeError(f"Reddit OAuth token request failed: {exc}") from exc

    if status != 200:
        raise RuntimeError(f"Reddit OAuth token endpoint returned status {status}")

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Reddit OAuth token response not valid JSON: {exc}"
        ) from exc

    token = payload.get("access_token")
    if not token or not isinstance(token, str):
        raise RuntimeError("Reddit OAuth token response missing access_token")

    _REDDIT_TOKEN_CACHE["access_token"] = token
    return token


def _search_reddit_oauth(query: str, limit: int, token: str) -> dict:
    """Execute search query using Reddit OAuth API."""
    params = urllib.parse.urlencode(
        {
            "q": query,
            "sort": "relevance",
            "limit": str(limit),
        }
    )
    url = f"https://oauth.reddit.com/search?{params}"
    headers = {
        "Authorization": f"bearer {token}",
    }
    status, raw_bytes, _ = _reddit_http_request(
        url,
        allowed_hosts=REDDIT_OAUTH_HOSTS,
        headers=headers,
        max_bytes=MAX_RESPONSE_BYTES,
    )
    if status != 200:
        raise RuntimeError(f"OAuth search returned HTTP status {status}")
    return json.loads(raw_bytes.decode("utf-8"))


def _search_reddit_donsetch(query: str, limit: int) -> dict:
    """Fallback reader using wheel's donsetch dynamic-page reader."""
    core = _wheel_core()
    donsetch = getattr(core, "donsetch_fetch", None)
    if not callable(donsetch):
        raise RuntimeError("donsetch reader not available in core")

    params = urllib.parse.urlencode(
        {
            "q": query,
            "sort": "relevance",
            "limit": str(limit),
        }
    )
    public_url = f"https://www.reddit.com/search.json?{params}"
    res = donsetch(public_url, allowed_hosts=REDDIT_ALLOWED_HOSTS)
    if isinstance(res, dict):
        status_field = res.get("status")
        if status_field in ("blocked", "unavailable", "error"):
            reason = (
                res.get("detail")
                or res.get("reason")
                or res.get("error")
                or f"donsetch fetch returned {status_field}"
            )
            raise RuntimeError(reason)
        # In donsetch_fetch, on success data["content"] has the text/content
        content = res.get("content")
        if not content and isinstance(res.get("data"), dict):
            content = res["data"].get("content")
        if not content:
            content = res.get("body")
        if isinstance(content, bytes):
            return json.loads(content.decode("utf-8"))
        elif isinstance(content, str) and content.strip():
            return json.loads(content)
        raise RuntimeError("donsetch returned no readable payload body")
    if hasattr(res, "status_code"):
        status_code = getattr(res, "status_code", 200)
        if status_code != 200:
            raise RuntimeError(f"HTTP {status_code}")
        body = getattr(res, "body", b"")
        if isinstance(body, bytes):
            return json.loads(body.decode("utf-8"))
        elif isinstance(body, str):
            return json.loads(body)
    raise RuntimeError(f"donsetch returned unexpected response type: {type(res)}")


def search_reddit(query: str, limit: int = 25) -> dict:
    """Search Reddit public posts for regret/migration discussions.

    Attempts app-only OAuth first when WHEEL_REDDIT_CLIENT_ID is set.
    Falls back to donsetch managed reader on unconfigured or failed OAuth.
    Returns status 'blocked' or 'unavailable' on failures, never fabricated results.
    """
    bounded_limit = min(limit, MAX_ITEMS_PER_SOURCE)

    data = None
    last_error: Exception | None = None

    oauth_token = None
    try:
        oauth_token = _get_reddit_oauth_token()
    except Exception as exc:
        last_error = exc

    if oauth_token:
        try:
            data = _search_reddit_oauth(query, bounded_limit, oauth_token)
        except Exception as exc:
            last_error = exc

    if data is None:
        try:
            data = _search_reddit_donsetch(query, bounded_limit)
        except Exception as exc:
            last_error = exc
            exc_str = str(exc)
            status_val = (
                "blocked"
                if ("403" in exc_str or "blocked" in exc_str.lower())
                else "unavailable"
            )
            return {
                "source": "reddit",
                "status": status_val,
                "query": query,
                "reason": exc_str,
                "matches": [],
            }

    children = (
        (data.get("data") or {}).get("children") if isinstance(data, dict) else None
    )
    if not isinstance(children, list):
        return {
            "source": "reddit",
            "status": "error",
            "query": query,
            "reason": "Missing or non-list children in payload",
            "matches": [],
        }

    matches: list[dict[str, Any]] = []
    for post in children[:bounded_limit]:
        pdata = post.get("data") if isinstance(post, dict) else None
        if not isinstance(pdata, dict):
            continue
        title = _truncate_title(pdata.get("title", ""))
        permalink = pdata.get("permalink")
        url_post = (
            f"https://www.reddit.com{permalink}" if permalink else pdata.get("url", "")
        )
        score = pdata.get("score")
        created_utc = pdata.get("created_utc")
        created_at = None
        if isinstance(created_utc, (int, float)):
            created_at = datetime.fromtimestamp(created_utc, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        elif isinstance(created_utc, str):
            created_at = created_utc

        matches.append(
            {
                "title": title,
                "url": url_post,
                "points": score,
                "created_at": created_at,
            }
        )

    return {
        "source": "reddit",
        "status": "ok",
        "query": query,
        "matches": matches,
    }


def search_stackexchange(query: str, limit: int = 25) -> dict:
    """Search Stack Overflow question titles for regret signals.

    Reddit answers 403 to unauthenticated clients from most networks, and a source
    that is permanently blocked is not a source. Stack Overflow's public API needs
    no key at this volume, so the community half of the evidence keeps more than
    one reachable platform behind it.
    """
    bounded_limit = min(limit, MAX_ITEMS_PER_SOURCE)
    terms = load_probe_terms()
    collected: list[dict[str, Any]] = []
    reasons: list[str] = []
    probed = 0
    for term in terms:
        params = urllib.parse.urlencode(
            {
                "order": "desc",
                "sort": "votes",
                "site": "stackoverflow",
                "pagesize": str(bounded_limit),
                "intitle": f"{query} {term}",
            }
        )
        url = f"https://api.stackexchange.com/2.3/search?{params}"
        try:
            raw_bytes = _bounded_https_download(
                url,
                allowed_hosts=set(STACKEXCHANGE_ALLOWED_HOSTS),
                max_bytes=MAX_RESPONSE_BYTES,
            )
        except urllib.error.HTTPError as exc:
            reasons.append(f"{term}: HTTP {exc.code}")
            continue
        except Exception as exc:
            reasons.append(f"{term}: {exc}")
            continue
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:
            reasons.append(f"{term}: invalid JSON payload: {exc}")
            continue
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            reasons.append(f"{term}: missing items in payload")
            continue
        probed += 1
        matches: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            created = item.get("creation_date")
            matches.append(
                {
                    "title": _truncate_title(item.get("title", "")),
                    "url": item.get("link", ""),
                    "points": item.get("score"),
                    "created_at": datetime.fromtimestamp(
                        created, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if isinstance(created, (int, float))
                    else None,
                    "term": term,
                }
            )
        _merge_matches(collected, matches, bounded_limit)
    if probed == 0:
        return {
            "source": "stackoverflow",
            "status": _dead_source_status(reasons),
            "query": query,
            "reason": "; ".join(reasons) or "no probe succeeded",
            "matches": [],
        }
    result = {
        "source": "stackoverflow",
        "status": "ok" if probed == len(terms) else "partial",
        "query": query,
        "terms_probed": probed,
        "terms_total": len(terms),
        "matches": collected,
    }
    if reasons:
        result["reason"] = "; ".join(reasons)
    return result


def _canonical_slug(slug: str) -> str:
    """Follow a repository rename before searching its issues.

    `repos/{slug}` follows GitHub's redirect, but the search API does not: it
    answers 422 "the listed repositories cannot be searched" for a renamed slug,
    which reads as "no complaints" rather than "wrong name". Resolving first keeps
    a rename from silently emptying the evidence.
    """
    data = _gh_api(f"repos/{slug}")
    if isinstance(data, dict):
        full_name = data.get("full_name")
        if isinstance(full_name, str) and "/" in full_name:
            return full_name
    return slug


def search_github_issues(slug: str, limit: int = 25) -> dict:
    """Search a repository's own issues for regret signals.

    GitHub's search API rejects a query that ORs every negative phrase together
    with the repo qualifiers, and `_gh_api` reports that only as None. Probing one
    phrase at a time keeps each query inside what the API accepts and turns a
    rejected query into a per-term reason instead of a silent empty result.
    """
    bounded_limit = min(limit, MAX_ITEMS_PER_SOURCE)
    per_term = bounded_limit
    terms = load_probe_terms()
    collected: list[dict[str, Any]] = []
    reasons: list[str] = []
    probed = 0
    slug = _canonical_slug(slug)
    for index, term in enumerate(terms):
        if index:
            # GitHub's search API allows 30 requests per minute and answers a burst
            # with an empty body that `_gh_api` can only report as None. Pacing the
            # probes keeps a throttle from being indistinguishable from "no issues".
            time.sleep(SEARCH_PROBE_PAUSE_SECONDS)
        query = f'repo:{slug} is:issue "{term}"'
        path = (
            "search/issues?q="
            + urllib.parse.quote(query, safe="")
            + f"&per_page={per_term}"
        )
        try:
            data = _gh_api(path)
        except Exception as exc:
            reasons.append(f"{term}: {exc}")
            continue
        if data is None:
            reasons.append(f"{term}: gh api returned no payload")
            continue
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            reasons.append(f"{term}: missing items in search response")
            continue
        probed += 1
        matches: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            reactions = item.get("reactions")
            points = (
                reactions.get("total_count")
                if isinstance(reactions, dict)
                else item.get("comments", 0)
            )
            matches.append(
                {
                    "title": _truncate_title(item.get("title", "")),
                    "url": item.get("html_url", ""),
                    "points": points,
                    "created_at": item.get("created_at"),
                    "term": term,
                }
            )
        _merge_matches(collected, matches, bounded_limit)
    if probed == 0:
        return {
            "source": "github_issues",
            "status": _dead_source_status(reasons),
            "query": f"repo:{slug} is:issue",
            "reason": "; ".join(reasons) or "no probe succeeded",
            "matches": [],
        }
    result = {
        "source": "github_issues",
        "status": "ok" if probed == len(terms) else "partial",
        "query": f"repo:{slug} is:issue",
        "terms_probed": probed,
        "terms_total": len(terms),
        "matches": collected,
    }
    if reasons:
        result["reason"] = "; ".join(reasons)
    return result


def collect_regret_signals(
    slug: str,
    display_name: str | None = None,
    limit: int = 25,
) -> dict:
    """Run every source and aggregate regret signals.

    `available_sources` counts only sources that answered; it is 0 when every one
    failed, and a caller must read that as "no evidence gathered", never as
    "no complaints exist".
    """
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    query_term = display_name or slug.split("/")[-1]

    # Run HN search
    hn_query = f"{query_term} alternative OR postmortem OR regret"
    hn_result = search_hackernews(hn_query, limit=limit)

    # Run Reddit search
    reddit_query = (
        f"{query_term} (migrated away OR regret OR postmortem OR alternative)"
    )
    reddit_result = search_reddit(reddit_query, limit=limit)

    # Run Stack Overflow search
    so_result = search_stackexchange(query_term, limit=limit)

    # Run GitHub issues search
    gh_result = search_github_issues(slug, limit=limit)

    sources = [hn_result, reddit_result, so_result, gh_result]
    counts = {
        "hackernews": len(hn_result.get("matches", [])),
        "reddit": len(reddit_result.get("matches", [])),
        "stackoverflow": len(so_result.get("matches", [])),
        "github_issues": len(gh_result.get("matches", [])),
    }

    available_sources = sum(1 for src in sources if src.get("status") == "ok")

    return {
        "slug": slug,
        "checked_at": checked_at,
        "sources": sources,
        "counts": counts,
        "available_sources": available_sources,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for searching regret & postmortem signals."""
    parser = argparse.ArgumentParser(
        prog="community_signals",
        description="Search Reddit, Hacker News, and GitHub issues for community regret signals.",
    )
    parser.add_argument(
        "--slug", required=True, help="GitHub repository slug (owner/repo)"
    )
    parser.add_argument(
        "--display-name",
        default=None,
        help="Optional human name of the library/tool (defaults to repo name)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum matches per source (default 25, capped at 25)",
    )
    parser.add_argument("--json", action="store_true", help="Output results as JSON")

    args = parser.parse_args(argv)

    results = collect_regret_signals(
        slug=args.slug,
        display_name=args.display_name,
        limit=args.limit,
    )

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"Community Signals for {results['slug']} at {results['checked_at']}")
    print(f"Available Sources: {results['available_sources']}/3")
    print("Counts:")
    for src_name, count in results["counts"].items():
        print(f"  - {src_name}: {count}")

    for src in results["sources"]:
        name = src.get("source", "unknown")
        status = src.get("status", "unknown")
        print(f"\n--- {name.upper()} (Status: {status}) ---")
        if status != "ok":
            print(f"  Reason: {src.get('reason', 'N/A')}")
            continue
        matches = src.get("matches", [])
        if not matches:
            print("  No matches found.")
            continue
        for idx, match in enumerate(matches, 1):
            title = match.get("title", "")
            pts = match.get("points")
            pts_str = f" ({pts} pts)" if pts is not None else ""
            url = match.get("url", "")
            created = match.get("created_at", "")
            print(f"  {idx}. {title}{pts_str}")
            print(f"     URL: {url} | Date: {created}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
