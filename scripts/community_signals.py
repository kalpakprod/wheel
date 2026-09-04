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
import re
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


# Bind from one module object: the first getattr publishes _gh_api into this
# module's globals, and when this file is __main__ a second _wheel_core() call
# would match itself by that very attribute and return the wrong module.
_core = _wheel_core()
_bounded_https_download = getattr(_core, "_bounded_https_download")
_gh_api = getattr(_core, "_gh_api")
_timestamp = getattr(_core, "_timestamp")


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
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_DEVICE_ID = "DO_NOT_TRACK_THIS_DEVICE"
REDDIT_INSTALLED_GRANT = "https://oauth.reddit.com/grants/installed_client"
REDDIT_CLIENT_ID_ENV = "WHEEL_REDDIT_CLIENT_ID"
REDDIT_CLIENT_SECRET_ENV = "WHEEL_REDDIT_CLIENT_SECRET"
REDDIT_USER_AGENT_ENV = "WHEEL_REDDIT_USER_AGENT"
REDDIT_USERNAME_ENV = "WHEEL_REDDIT_USERNAME"
REDDIT_APP_ID = "com.kalpakprod.wheel"
REDDIT_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,20}$")
REDDIT_RATE_RESERVE = 5
_REDDIT_RATE_STATE: dict[str, float] = {}
_PLUGIN_MANIFEST = (
    Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json"
)
_REDDIT_TOKEN_CACHE: dict[str, str] = {}


def _plugin_version() -> str:
    """Read the shipped version so the agent string never claims a stale release."""
    try:
        manifest = json.loads(_PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "0.0.0"
    version = manifest.get("version")
    return version if isinstance(version, str) and version else "0.0.0"


class RedditComplianceError(RuntimeError):
    """Raised when a request would break Reddit's Data API rules if it were sent."""


def reddit_user_agent() -> str:
    """Build the agent string Reddit's Data API Wiki mandates, or refuse to call.

    Required shape: ``<platform>:<app ID>:<version> (by /u/<username>)``. Reddit
    states it throttles or blocks unidentified clients and that a generic agent is
    limited on purpose, so an install without a contact username is not allowed to
    reach the API at all rather than being throttled as an anonymous stranger.
    """
    configured = os.environ.get(REDDIT_USER_AGENT_ENV, "").strip()
    if configured:
        return configured
    username = os.environ.get(REDDIT_USERNAME_ENV, "").strip().lstrip("/")
    if username.lower().startswith("u/"):
        username = username[2:]
    if not REDDIT_USERNAME_PATTERN.match(username):
        raise RedditComplianceError(
            f"Reddit requires a contact username in the User-Agent: set {REDDIT_USERNAME_ENV} "
            f"to your Reddit handle, or set {REDDIT_USER_AGENT_ENV} to a full compliant string"
        )
    return f"python:{REDDIT_APP_ID}:v{_plugin_version()} (by /u/{username})"


def _reddit_rate_gate() -> None:
    """Stop before the free-tier budget runs out instead of discovering it with a 429.

    Reddit publishes 100 queries per minute per OAuth client id and asks callers to
    watch the x-ratelimit headers. The last response's remaining count is kept, and
    once it drops into the reserve the next call is refused until the window resets.
    """
    remaining = _REDDIT_RATE_STATE.get("remaining")
    reset_at = _REDDIT_RATE_STATE.get("reset_at", 0.0)
    if remaining is None:
        return
    if remaining > REDDIT_RATE_RESERVE:
        return
    wait = reset_at - time.time()
    if wait <= 0:
        _REDDIT_RATE_STATE.clear()
        return
    raise RedditComplianceError(
        f"Reddit rate budget exhausted: {int(remaining)} requests left, "
        f"{int(wait)}s to window reset"
    )


def _record_reddit_rate(headers: dict[str, str]) -> None:
    """Remember the published budget so the next call can honour it."""
    try:
        remaining = float(headers["x-ratelimit-remaining"])
        reset = float(headers["x-ratelimit-reset"])
    except (KeyError, TypeError, ValueError):
        return
    _REDDIT_RATE_STATE["remaining"] = remaining
    _REDDIT_RATE_STATE["reset_at"] = time.time() + reset


def _reddit_grant_payload(client_secret: str) -> dict[str, str]:
    """Pick the grant Reddit accepts for this app type instead of guessing one.

    A script or web app is a confidential client and answers to client_credentials.
    An installed app carries no secret and answers only to the installed_client grant.
    The wrong grant returns 401 with no explanation, so the choice is made from the
    one fact that distinguishes the two: whether a secret was configured.
    """
    if client_secret:
        return {"grant_type": "client_credentials"}
    return {"grant_type": REDDIT_INSTALLED_GRANT, "device_id": REDDIT_DEVICE_ID}


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
    except ImportError:
        # The client contract is standard library only. pyyaml belongs to the CI
        # catalog builder, so its absence degrades to the built-in English probes
        # rather than taking down every community-signals call on a user machine.
        return list(PROBE_TERMS[:MAX_PROBE_TERMS])

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

    agent = reddit_user_agent()
    _reddit_rate_gate()

    req = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET"
    )
    req.add_header("User-Agent", agent)
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
        _record_reddit_rate(resp_headers)
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
    client_id = os.environ.get(REDDIT_CLIENT_ID_ENV, "").strip()
    if not client_id:
        return None

    cached = _REDDIT_TOKEN_CACHE.get("access_token")
    if cached:
        return cached

    client_secret = os.environ.get(REDDIT_CLIENT_SECRET_ENV, "").strip()
    user_pass = f"{client_id}:{client_secret}".encode("utf-8")
    basic_auth = base64.b64encode(user_pass).decode("ascii")

    token_url = REDDIT_TOKEN_URL
    data = urllib.parse.urlencode(_reddit_grant_payload(client_secret)).encode("utf-8")

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


def check_reddit_credentials() -> dict[str, Any]:
    """Mint a token and report the outcome, so a misconfigured app is visible at once.

    Returns the grant that was attempted and the failure text. Credentials themselves
    are never returned, printed or logged.
    """
    client_id = os.environ.get(REDDIT_CLIENT_ID_ENV, "").strip()
    if not client_id:
        return {
            "source": "reddit",
            "status": "unconfigured",
            "grant": "",
            "reason": f"{REDDIT_CLIENT_ID_ENV} is not set",
        }
    client_secret = os.environ.get(REDDIT_CLIENT_SECRET_ENV, "").strip()
    grant = _reddit_grant_payload(client_secret)["grant_type"]
    try:
        agent = reddit_user_agent()
    except RedditComplianceError as exc:
        return {
            "source": "reddit",
            "status": "unconfigured",
            "grant": grant,
            "reason": str(exc),
        }
    _REDDIT_TOKEN_CACHE.pop("access_token", None)
    try:
        token = _get_reddit_oauth_token()
    except Exception as exc:
        return {
            "source": "reddit",
            "status": "error",
            "grant": grant,
            "reason": str(exc),
        }
    if not token:
        return {
            "source": "reddit",
            "status": "unconfigured",
            "grant": grant,
            "reason": f"{REDDIT_CLIENT_ID_ENV} is not set",
        }
    return {
        "source": "reddit",
        "status": "ok",
        "grant": grant,
        "user_agent": agent,
        "reason": "",
    }


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


def _reddit_blocked(query: str, reason: str) -> dict:
    """Reddit is either read through OAuth or not read at all."""
    return {
        "source": "reddit",
        "status": "blocked",
        "query": query,
        "reason": reason,
        "retention": "none",
        "matches": [],
    }


def search_reddit(query: str, limit: int = 25) -> dict:
    """Search Reddit for regret and migration discussions through the Data API.

    OAuth is the only route. Reddit's Responsible Builder Policy forbids masking how
    its data is reached, and its API wiki says unauthenticated traffic is blocked
    outright, so an unconfigured install reports `blocked` instead of reaching the
    same content through a managed page reader.

    Matches are returned for the caller to read now. They carry `retention: none`:
    Reddit requires deleted content to be removed from any copy held, so nothing
    from this source is written to disk by the decision path.
    """
    bounded_limit = min(limit, MAX_ITEMS_PER_SOURCE)

    try:
        oauth_token = _get_reddit_oauth_token()
    except RedditComplianceError as exc:
        return _reddit_blocked(query, str(exc))
    except Exception as exc:
        return _reddit_blocked(query, str(exc))

    if not oauth_token:
        return _reddit_blocked(
            query,
            f"{REDDIT_CLIENT_ID_ENV} is not set: register an app at "
            "https://www.reddit.com/prefs/apps and configure app-only OAuth",
        )

    try:
        data = _search_reddit_oauth(query, bounded_limit, oauth_token)
    except RedditComplianceError as exc:
        return _reddit_blocked(query, str(exc))
    except Exception as exc:
        exc_str = str(exc)
        status_val = (
            "blocked"
            if ("403" in exc_str or "429" in exc_str or "blocked" in exc_str.lower())
            else "unavailable"
        )
        return {
            "source": "reddit",
            "status": status_val,
            "query": query,
            "reason": exc_str,
            "retention": "none",
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
            "retention": "none",
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
        "retention": "none",
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
        "--slug", default=None, help="GitHub repository slug (owner/repo)"
    )
    parser.add_argument(
        "--check-reddit",
        action="store_true",
        help="Verify the configured Reddit app can mint a token, then exit",
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

    if args.check_reddit:
        report = check_reddit_credentials()
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            grant = f" ({report['grant']})" if report["grant"] else ""
            print(f"reddit: {report['status']}{grant}")
            if report["reason"]:
                print(f"  reason: {report['reason']}")
        return 0 if report["status"] == "ok" else 1

    if not args.slug:
        parser.error("--slug is required unless --check-reddit is used")

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
