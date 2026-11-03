"""One-shot GitHub/GitLab release importer, run from the host crontab (Plan 029).

It reads a small JSON manifest of "jobs", one GitHub or GitLab repository
each. For each job it finds at most one matching IPA and one matching APK in
the newest eligible release with a match, and downloads each to a temporary
file. It validates each file and reads the real identity and version from
the IPA's top-level `Info.plist` or the APK's `AndroidManifest.xml`. It logs
in with `/api/login` and publishes through feather's existing authenticated
HTTP API, the same way `scripts/telegram_bot_ingest.py` does. An IPA goes to
`/api/add-version` or `/api/add-app`, and an APK goes to
`/api/android/add-apk`. It also records provenance through
`/api/import-history/record`. This script never imports `app.py` and never
calls the dormant `SourceManager.add_app_from_github`. It never writes
`data/source.json`, `data/ipas/`, `data/icons/`, or any Garage object
directly. Feather stays the only writer of the catalog.

Dry-run is the default. Without `--apply`, this may only query provider
release metadata and feather's public `/api/app/<id>` and
`/fdroid/repo/index-v1.json` endpoints. It never downloads an asset, logs
in, publishes, takes the apply lock, or writes the state file. `--apply`
enables all of that. A non-blocking `fcntl.flock()` guards it, so two
crontab runs can never race each other or feather's in-process
`SourceManager._lock`.

No network call happens at import time. Only `main()` and the functions it
calls touch the network, so tests/test_release_source_ingest.py can import
and unit test this module with no network access.

See plans/029-cron-release-imports.md for the full behavioral contract.
"""

import argparse
import fnmatch
import hashlib
import ipaddress
import json
import logging
import os
import plistlib
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlparse

import requests

try:
    from .ipa_inspection import InspectionError, inspect_ipa
    from .apk_inspection import ANDROID_PACKAGE_RE, ApkInspectionError, inspect_apk
except ImportError:  # direct execution from scripts/
    from ipa_inspection import InspectionError, inspect_ipa
    from apk_inspection import ANDROID_PACKAGE_RE, ApkInspectionError, inspect_apk

try:
    import fcntl
except ImportError:  # pragma: no cover, this script runs only on Linux
    fcntl = None

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("release_source_ingest")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConfigError(RuntimeError):
    """Manifest, CLI, or environment configuration is invalid. Exit code 2."""


class ProviderError(RuntimeError):
    """A GitHub/GitLab API call, asset selection, or download failed."""


class ValidationError(RuntimeError):
    """A downloaded file failed one of the hard IPA validation checks."""


class FeatherAuthError(RuntimeError):
    """feather's /api/login (or a later authenticated call) returned 401."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_API_BASE = "https://api.github.com"
GITLAB_API_BASE = "https://gitlab.com/api/v4"
GITLAB_AUTH_HOST = (urlparse(GITLAB_API_BASE).hostname or "").lower()
GITHUB_API_VERSION = "2026-03-10"

# A ceiling, not a delay. A fast transfer returns immediately.
# RELEASE_IMPORT_TIMEOUT overrides it.
DEFAULT_TIMEOUT = 900
# 2 GiB. RELEASE_IMPORT_MAX_BYTES overrides it.
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_REDIRECTS = 5

# A cheap guard against catastrophic backtracking. Python's `re` has no
# match timeout, so a pathological regex asset pattern would hang the
# scheduler thread. Only admins supply this input (every route that accepts
# it is @requires_auth, or it comes from a locally edited manifest file), so
# this is defence in depth, not a boundary against untrusted input. It does
# not replace a real regex-safety analysis.
ASSET_PATTERN_MAX_LENGTH = 200

_APP_INFO_PLIST = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")


# ---------------------------------------------------------------------------
# Small data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """One validated manifest entry.

    `bundle_identifier` (iOS) and `package` (Android) are each optional, and
    parse_manifest_dict requires neither. A job publishes whichever
    platforms its matched assets cover, and a set identity is only an
    assertion. Both fields default to None so a Job can be built with only
    one of them. `asset_glob` stays ahead of them in field order because it
    has no dataclass default.
    """

    id: str
    provider: str
    project: str
    asset_glob: str
    bundle_identifier: str = None
    package: str = None
    asset_exclude_glob: str = None
    asset_match_mode: str = "glob"
    include_prereleases: bool = False
    create_if_missing: bool = False
    allowed_download_hosts: frozenset = field(default_factory=frozenset)
    name: str = None
    developer_name: str = None


@dataclass(frozen=True)
class ReleaseCandidate:
    """One normalized, selected release and asset, with no token-bearing headers."""

    provider: str
    project: str
    release_id: str
    release_tag: str
    release_time: str
    asset_id: str
    asset_name: str
    declared_size: object  # int or None
    download_url: str
    auth_host: str
    release_body: str = ""
    platform: str = "ios"  # "ios" or "android", inferred from the asset extension


@dataclass
class Summary:
    checked: int = 0
    would_publish: int = 0
    downloaded: int = 0
    published: int = 0
    created: int = 0
    skipped: int = 0
    failed: int = 0

    def format(self):
        return (
            f"checked={self.checked} would-publish={self.would_publish} "
            f"downloaded={self.downloaded} published={self.published} "
            f"created={self.created} skipped={self.skipped} failed={self.failed}"
        )


# ---------------------------------------------------------------------------
# Secret redaction, applied to every job-level log line. The manifest
# never carries a secret, but each run registers its tokens and password
# here once, so no log call can leak one. The telegram bot has the same
# _redact.
# ---------------------------------------------------------------------------

_ACTIVE_SECRETS = []


def _set_active_secrets(*secrets):
    global _ACTIVE_SECRETS
    _ACTIVE_SECRETS = [s for s in secrets if s]


def _redact(text):
    for secret in _ACTIVE_SECRETS:
        if secret:
            text = text.replace(secret, "<REDACTED>")
    return text


def _log_job_error(msg):
    logger.error(_redact(msg))


# ---------------------------------------------------------------------------
# Manifest and config loading
# ---------------------------------------------------------------------------

_GITHUB_PROJECT_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _require_nonempty_str(raw, key, job_id):
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        label = job_id or "<unknown>"
        raise ConfigError(f"job {label}: {key!r} must be a non-empty string")
    return value


def _validate_asset_match_mode(job_id, mode):
    """Accept only the exact strings 'glob' and 'regex'. Otherwise raise
    ConfigError naming both valid values."""
    if mode not in ("glob", "regex"):
        raise ConfigError(
            f"job {job_id}: 'assetMatchMode' must be 'glob' or 'regex', got {mode!r}"
        )
    return mode


def _validate_asset_pattern(job_id, field_name, pattern, mode):
    """Validate one assetGlob or assetExcludeGlob value when the manifest
    loads (at save time), never at run time inside the scheduler. In regex
    mode, reject patterns longer than ASSET_PATTERN_MAX_LENGTH, a cheap
    guard against catastrophic backtracking (see the constant's comment).
    Report a bad pattern as ConfigError, never as a raw re.error."""
    if mode != "regex" or pattern is None:
        return
    if len(pattern) > ASSET_PATTERN_MAX_LENGTH:
        raise ConfigError(
            f"job {job_id}: {field_name!r} pattern is too long "
            f"(max {ASSET_PATTERN_MAX_LENGTH} characters)"
        )
    try:
        re.compile(pattern)
    except re.error as e:
        raise ConfigError(f"job {job_id}: {field_name!r} is not a valid regular expression: {e}")


def _validate_download_host(job_id, host):
    if not isinstance(host, str) or not host:
        raise ConfigError(
            f"job {job_id}: allowedDownloadHosts entries must be non-empty strings"
        )
    if host != host.lower():
        raise ConfigError(
            f"job {job_id}: allowedDownloadHosts entry {host!r} must be lower-case"
        )
    if any(c in host for c in ("/", "*", "@", ":", " ")):
        raise ConfigError(
            f"job {job_id}: allowedDownloadHosts entry {host!r} must be a bare hostname"
        )
    if host == "localhost":
        raise ConfigError(
            f"job {job_id}: allowedDownloadHosts entry {host!r} is not allowed"
        )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # not an IP literal, as required
    else:
        raise ConfigError(
            f"job {job_id}: allowedDownloadHosts entry {host!r} must be a hostname, "
            "not an IP address"
        )


def parse_manifest_dict(data):
    """Validate an already-parsed manifest dict; return a list of Job."""
    if not isinstance(data, dict):
        raise ConfigError("manifest must be a JSON object")
    if data.get("schemaVersion") != 1:
        raise ConfigError("manifest 'schemaVersion' must be the integer 1")

    raw_jobs = data.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ConfigError("manifest 'jobs' must be a non-empty list")

    jobs = []
    seen_ids = set()
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            raise ConfigError("each entry in 'jobs' must be a JSON object")

        job_id = _require_nonempty_str(raw, "id", None)
        if job_id in seen_ids:
            raise ConfigError(f"duplicate job id: {job_id!r}")
        seen_ids.add(job_id)

        provider = _require_nonempty_str(raw, "provider", job_id)
        if provider not in ("github", "gitlab"):
            raise ConfigError(
                f"job {job_id}: unknown provider {provider!r} "
                "(must be 'github' or 'gitlab')"
            )

        project = _require_nonempty_str(raw, "project", job_id)
        if "://" in project:
            raise ConfigError(f"job {job_id}: 'project' must not be a URL")
        if provider == "github":
            if not _GITHUB_PROJECT_RE.match(project) or project.lower().endswith(".git"):
                raise ConfigError(
                    f"job {job_id}: github 'project' must be exactly owner/repository"
                )

        bundle_identifier_raw = raw.get("bundleIdentifier")
        if bundle_identifier_raw is not None and (
            not isinstance(bundle_identifier_raw, str) or not bundle_identifier_raw.strip()
        ):
            raise ConfigError(f"job {job_id}: 'bundleIdentifier' must be a non-empty string")
        bundle_identifier = bundle_identifier_raw.strip() if bundle_identifier_raw else None

        package_raw = raw.get("package")
        if package_raw is not None and (
            not isinstance(package_raw, str) or not package_raw.strip()
        ):
            raise ConfigError(f"job {job_id}: 'package' must be a non-empty string")
        package = package_raw.strip() if package_raw else None
        if package is not None and not ANDROID_PACKAGE_RE.match(package):
            raise ConfigError(f"job {job_id}: 'package' {package!r} is not a valid Android package name")

        # Both are optional. The importer reads the bundle identifier or
        # package from the downloaded artifact, and a configured value is
        # only an assertion, the same rule the admin UI and auto-import
        # paths follow.

        asset_glob = _require_nonempty_str(raw, "assetGlob", job_id)
        asset_exclude_glob = raw.get("assetExcludeGlob")
        if asset_exclude_glob is not None and not isinstance(asset_exclude_glob, str):
            raise ConfigError(
                f"job {job_id}: 'assetExcludeGlob' must be a string"
            )
        asset_exclude_glob = (asset_exclude_glob or "").strip() or None

        asset_match_mode = _validate_asset_match_mode(
            job_id, raw.get("assetMatchMode", "glob")
        )
        _validate_asset_pattern(job_id, "assetGlob", asset_glob, asset_match_mode)
        _validate_asset_pattern(job_id, "assetExcludeGlob", asset_exclude_glob, asset_match_mode)

        include_prereleases = bool(raw.get("includePrereleases", False))
        create_if_missing = bool(raw.get("createIfMissing", False))

        allowed_hosts_raw = raw.get("allowedDownloadHosts")
        allowed_hosts = frozenset()
        if provider == "gitlab":
            if not isinstance(allowed_hosts_raw, list) or not allowed_hosts_raw:
                raise ConfigError(
                    f"job {job_id}: gitlab jobs require a non-empty "
                    "'allowedDownloadHosts' list"
                )
            for host in allowed_hosts_raw:
                _validate_download_host(job_id, host)
            allowed_hosts = frozenset(allowed_hosts_raw)
        elif allowed_hosts_raw is not None and not isinstance(allowed_hosts_raw, list):
            raise ConfigError(f"job {job_id}: 'allowedDownloadHosts' must be a list")

        name = raw.get("name")
        developer_name = raw.get("developerName")
        if create_if_missing:
            if not isinstance(name, str) or not name.strip():
                raise ConfigError(
                    f"job {job_id}: createIfMissing requires a non-empty 'name'"
                )
            if not isinstance(developer_name, str) or not developer_name.strip():
                raise ConfigError(
                    f"job {job_id}: createIfMissing requires a non-empty 'developerName'"
                )

        jobs.append(
            Job(
                id=job_id,
                provider=provider,
                project=project,
                bundle_identifier=bundle_identifier,
                package=package,
                asset_glob=asset_glob,
                asset_exclude_glob=asset_exclude_glob,
                asset_match_mode=asset_match_mode,
                include_prereleases=include_prereleases,
                create_if_missing=create_if_missing,
                allowed_download_hosts=allowed_hosts,
                name=name,
                developer_name=developer_name,
            )
        )

    return jobs


def load_manifest(path):
    """Read and validate the manifest file at `path`. Raises ConfigError."""
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    except json.JSONDecodeError as e:
        raise ConfigError(f"config file {path} is not valid JSON: {e}")

    jobs = parse_manifest_dict(data)
    return {"schemaVersion": data.get("schemaVersion"), "jobs": jobs}


def _int_env(env, name, default):
    raw = env.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a positive integer number of seconds/bytes")
    if value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


# ---------------------------------------------------------------------------
# Provider adapters for GitHub and GitLab only. The API origins are fixed,
# with no configurable base URL in v1 (self-hosted GitLab/GitHub is out of
# scope).
# ---------------------------------------------------------------------------


def _github_headers(token):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _raise_for_provider_status(resp, provider, project, job_id):
    if resp.status_code >= 400:
        reset = (
            resp.headers.get("X-RateLimit-Reset")
            or resp.headers.get("RateLimit-Reset")
            or resp.headers.get("Retry-After")
        )
        detail = f" (rate-limit reset {reset})" if reset else ""
        raise ProviderError(
            f"job {job_id}: {provider} API returned HTTP {resp.status_code} for "
            f"{project}{detail}"
        )


def github_list_releases(session, project, token, timeout=30, job_id=None):
    url = f"{GITHUB_API_BASE}/repos/{project}/releases"
    resp = session.get(
        url, headers=_github_headers(token), params={"per_page": 100}, timeout=timeout
    )
    _raise_for_provider_status(resp, "github", project, job_id)
    try:
        return resp.json()
    except ValueError:
        raise ProviderError(f"job {job_id}: github returned a non-JSON releases response")


def _asset_match_state(job, name):
    """Return provider-neutral include/exclude selector state for one asset.

    Two modes, selected by `job.asset_match_mode`:
    - "glob" (default): fnmatch against the whole lower-cased name, as
      before regex mode existed.
    - "regex": re.fullmatch against the original name with re.IGNORECASE,
      so character classes behave predictably instead of matching a
      pre-lowercased string. fullmatch, not search, mirrors fnmatch's
      whole-name semantics. Using search would silently change what
      existing-style patterns mean.
    """
    name = name or ""
    if job.asset_match_mode == "regex":
        included = bool(re.fullmatch(job.asset_glob, name, re.IGNORECASE))
        excluded = bool(
            job.asset_exclude_glob
            and re.fullmatch(job.asset_exclude_glob, name, re.IGNORECASE)
        )
    else:
        normalized = name.lower()
        included = fnmatch.fnmatch(normalized, job.asset_glob.lower())
        excluded = bool(
            job.asset_exclude_glob
            and fnmatch.fnmatch(normalized, job.asset_exclude_glob.lower())
        )
    return {"included": included, "excluded": excluded, "matched": included and not excluded}


def _platform_for_asset(name):
    """Return "ios", "android" or None, inferred from the asset filename extension.

    A new artifact type would be added here. Keep this the only place that
    knows about file extensions.
    """
    normalized = (name or "").lower()
    if normalized.endswith(".ipa"):
        return "ios"
    if normalized.endswith(".apk"):
        return "android"
    return None


# The inverse of _platform_for_asset. It maps each platform name to the
# extension a downloaded candidate's temp file gets.
_DOWNLOAD_EXTENSION_BY_PLATFORM = {"ios": "ipa", "android": "apk"}


def _download_tmp_path(tmp_dir, platform):
    return os.path.join(tmp_dir, f"download.{_DOWNLOAD_EXTENSION_BY_PLATFORM[platform]}")


def _group_matches_by_platform(job, matches, name_of):
    """Group matched assets or links by platform. Drop any whose extension
    is neither .ipa nor .apk, so a `*` glob and checksum files coexist
    without failing selection.

    Selection is by pattern alone. It does not filter on whether the job
    configured an identity for the platform. Both `bundle_identifier` and
    `package` are optional and auto-detected from the artifact. Filtering
    here made a blank field silently match nothing and show up as "no
    eligible release had ... asset matching", which pointed the operator at
    their glob instead of the real cause. When set, the identity is an
    assertion checked after inspection. See `_process_ios_candidate`
    for iOS and `inspect_apk_metadata` for Android."""
    by_platform = {"ios": [], "android": []}
    for item in matches:
        platform = _platform_for_asset(name_of(item))
        if platform is None:
            continue
        by_platform[platform].append(item)
    return by_platform


def _github_eligible_releases(job, releases):
    eligible = [
        r for r in releases
        if not r.get("draft") and (job.include_prereleases or not r.get("prerelease"))
    ]
    eligible.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    return eligible


def github_select_candidate(job, releases):
    """Return a list of candidates, at most one per platform, for the first
    eligible release with any match, or an empty list if none matches."""
    for release in _github_eligible_releases(job, releases):
        assets = release.get("assets") or []
        matches = [a for a in assets if _asset_match_state(job, a.get("name"))["matched"]]
        by_platform = _group_matches_by_platform(job, matches, lambda a: a.get("name"))

        candidates = []
        for platform in ("ios", "android"):
            items = by_platform[platform]
            if not items:
                continue
            if len(items) > 1:
                raise ProviderError(
                    f"job {job.id}: release "
                    f"{release.get('tag_name') or release.get('id')} has {len(items)} "
                    f"{platform} assets matching {job.asset_glob!r}, expected at most one "
                    f"({', '.join(sorted(a.get('name') or '?' for a in items))}); "
                    f"narrow assetGlob or set assetExcludeGlob"
                )
            asset = items[0]
            candidates.append(ReleaseCandidate(
                provider="github",
                project=job.project,
                release_id=str(release.get("id")),
                release_tag=release.get("tag_name"),
                release_time=release.get("published_at") or "",
                asset_id=str(asset.get("id")),
                asset_name=asset.get("name") or "",
                declared_size=asset.get("size"),
                download_url=asset.get("url"),
                auth_host="api.github.com",
                release_body=release.get("body") or "",
                platform=platform,
            ))
        if candidates:
            return candidates
    return []


def _parse_iso8601(value):
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def gitlab_list_releases(session, project, token, timeout=30, job_id=None):
    encoded_project = quote(project, safe="")
    url = f"{GITLAB_API_BASE}/projects/{encoded_project}/releases"
    headers = {}
    if token:
        headers["PRIVATE-TOKEN"] = token
    resp = session.get(
        url,
        headers=headers,
        params={"per_page": 100, "order_by": "released_at", "sort": "desc"},
        timeout=timeout,
    )
    _raise_for_provider_status(resp, "gitlab", project, job_id)
    try:
        return resp.json()
    except ValueError:
        raise ProviderError(f"job {job_id}: gitlab returned a non-JSON releases response")


def _gitlab_eligible_releases(releases, now=None):
    now = now or datetime.now(timezone.utc)
    parsed = []
    for release in releases:
        dt = _parse_iso8601(release.get("released_at"))
        if dt is None or dt > now:
            continue
        parsed.append((dt, release))
    parsed.sort(key=lambda pair: pair[0], reverse=True)
    return [release for _dt, release in parsed]


def gitlab_select_candidate(job, releases, now=None):
    """Return a list of candidates, at most one per platform, for the first
    eligible release with any match, or an empty list if none matches."""
    for release in _gitlab_eligible_releases(releases, now=now):
        links = (release.get("assets") or {}).get("links") or []
        matches = [link for link in links if _asset_match_state(job, link.get("name"))["matched"]]
        by_platform = _group_matches_by_platform(job, matches, lambda link: link.get("name"))

        candidates = []
        for platform in ("ios", "android"):
            items = by_platform[platform]
            if not items:
                continue
            if len(items) > 1:
                raise ProviderError(
                    f"job {job.id}: release "
                    f"{release.get('tag_name') or release.get('id')} has {len(items)} "
                    f"{platform} asset links matching {job.asset_glob!r}, expected at most one "
                    f"({', '.join(sorted(link.get('name') or '?' for link in items))}); "
                    f"narrow assetGlob or set assetExcludeGlob"
                )
            link = items[0]
            url = link.get("url")
            host = (urlparse(url).hostname or "").lower() if url else ""
            if not url or not host or host not in job.allowed_download_hosts:
                raise ProviderError(
                    f"job {job.id}: asset link host {host!r} is not in allowedDownloadHosts"
                )
            candidates.append(ReleaseCandidate(
                provider="gitlab",
                project=job.project,
                release_id=str(release.get("id") or release.get("tag_name") or ""),
                release_tag=release.get("tag_name"),
                release_time=release.get("released_at") or "",
                asset_id=str(link.get("id") or link.get("name")),
                asset_name=link.get("name") or "",
                declared_size=None,
                download_url=url,
                # The credential belongs to the GitLab instance, not to
                # whatever host the release link points at.
                auth_host=GITLAB_AUTH_HOST,
                release_body=release.get("description") or "",
                platform=platform,
            ))
        if candidates:
            return candidates
    return []


def inspect_release_assets(job, session, tokens, timeout=30, limit=5):
    """Return sanitized selector previews without downloading an asset."""
    limit = max(0, min(int(limit), 5))
    if job.provider == "github":
        raw = github_list_releases(
            session, job.project, tokens.get("github"), timeout=timeout, job_id=job.id
        )
        releases = _github_eligible_releases(job, raw)[:limit]
        result = []
        for release in releases:
            assets = []
            for asset in (release.get("assets") or [])[:100]:
                state = _asset_match_state(job, asset.get("name"))
                assets.append({
                    "name": asset.get("name") or "",
                    "size": asset.get("size") if isinstance(asset.get("size"), int) else None,
                    **state,
                })
            result.append({
                "release": release.get("tag_name") or str(release.get("id") or ""),
                "releasedAt": release.get("published_at") or "",
                "assets": assets,
                "matchCount": sum(1 for asset in assets if asset["matched"]),
            })
        return result
    if job.provider == "gitlab":
        raw = gitlab_list_releases(
            session, job.project, tokens.get("gitlab"), timeout=timeout, job_id=job.id
        )
        releases = _gitlab_eligible_releases(raw)[:limit]
        result = []
        for release in releases:
            assets = []
            links = (release.get("assets") or {}).get("links") or []
            for link in links[:100]:
                state = _asset_match_state(job, link.get("name"))
                host = (urlparse(link.get("url") or "").hostname or "").lower()
                assets.append({
                    "name": link.get("name") or "",
                    "size": None,
                    "hostAllowed": bool(host and host in job.allowed_download_hosts),
                    **state,
                })
            result.append({
                "release": release.get("tag_name") or str(release.get("id") or ""),
                "releasedAt": release.get("released_at") or "",
                "assets": assets,
                "matchCount": sum(1 for asset in assets if asset["matched"]),
            })
        return result
    raise ConfigError(f"job {job.id}: unknown provider {job.provider!r}")


def select_candidate(job, session, tokens, timeout=30):
    """Return a list of candidates, at most one per platform, from the first
    eligible release with any match. Raise ProviderError when no eligible
    release matches at all."""
    if job.provider == "github":
        releases = github_list_releases(
            session, job.project, tokens.get("github"), timeout=timeout, job_id=job.id
        )
        candidates = github_select_candidate(job, releases)
    elif job.provider == "gitlab":
        releases = gitlab_list_releases(
            session, job.project, tokens.get("gitlab"), timeout=timeout, job_id=job.id
        )
        candidates = gitlab_select_candidate(job, releases)
    else:  # pragma: no cover, parse_manifest_dict already rejects this
        raise ConfigError(f"job {job.id}: unknown provider {job.provider!r}")

    if not candidates:
        raise ProviderError(
            f"job {job.id}: no eligible release had an .ipa or .apk asset matching "
            f"{job.asset_glob!r}"
        )
    return candidates


# ---------------------------------------------------------------------------
# Download, redirect handling, and IPA validation
# ---------------------------------------------------------------------------


def _host_allowed(provider, host, job):
    host = (host or "").lower()
    if provider == "github":
        return host in ("api.github.com", "github.com") or host.endswith(
            ".githubusercontent.com"
        )
    if provider == "gitlab":
        return host in job.allowed_download_hosts
    return False


def stream_download(
    session, candidate, job, dest_path, tokens, timeout, max_bytes, progress_cb=None
):
    """Stream `candidate`'s asset to `dest_path`, enforcing every trust rule.

    It follows redirects itself, never with `allow_redirects=True`. It
    checks that every hop is HTTPS to an allowed hostname, and drops the
    provider Authorization or PRIVATE-TOKEN header as soon as a redirect
    crosses to a different host. Returns (actual_size, sha256_hex).
    """
    headers = {}
    if candidate.provider == "github":
        headers["Accept"] = "application/octet-stream"
        headers["X-GitHub-Api-Version"] = GITHUB_API_VERSION
        token = tokens.get("github")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    else:
        token = tokens.get("gitlab")
        if token:
            headers["PRIVATE-TOKEN"] = token

    current_url = candidate.download_url
    # Provider credentials go only to the provider's own host (auth_host),
    # and never again once a redirect has left the first host. A GitLab
    # release link may point at any allowedDownloadHosts host. Sending
    # PRIVATE-TOKEN there handed the operator's PAT to a third party.
    auth_host = (getattr(candidate, "auth_host", "") or "").lower()
    first_host = (urlparse(current_url).hostname or "").lower()
    if first_host != auth_host:
        headers = {k: v for k, v in headers.items() if k not in ("Authorization", "PRIVATE-TOKEN")}
    current_headers = dict(headers)
    hops = 0
    resp = None

    while True:
        parsed = urlparse(current_url)
        if parsed.scheme != "https":
            raise ProviderError(f"job {job.id}: refusing a non-HTTPS asset URL")
        host = (parsed.hostname or "").lower()
        if not _host_allowed(candidate.provider, host, job):
            raise ProviderError(f"job {job.id}: asset host {host!r} is not allowed")

        resp = session.get(
            current_url,
            headers=current_headers,
            stream=True,
            timeout=timeout,
            allow_redirects=False,
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            hops += 1
            location = resp.headers.get("Location")
            resp.close()
            if hops > MAX_REDIRECTS:
                raise ProviderError(
                    f"job {job.id}: exceeded {MAX_REDIRECTS} redirects downloading "
                    "the asset"
                )
            if not location:
                raise ProviderError(
                    f"job {job.id}: redirect response had no Location header"
                )
            next_url = urljoin(current_url, location)
            next_host = (urlparse(next_url).hostname or "").lower()
            if next_host != host:
                # Cross-host redirect: never forward provider credentials.
                current_headers = {
                    k: v
                    for k, v in current_headers.items()
                    if k not in ("Authorization", "PRIVATE-TOKEN")
                }
            current_url = next_url
            continue

        if resp.status_code != 200:
            resp.close()
            raise ProviderError(
                f"job {job.id}: asset download returned HTTP {resp.status_code}"
            )
        break

    if candidate.declared_size is not None and candidate.declared_size > max_bytes:
        resp.close()
        raise ValidationError(
            f"job {job.id}: declared asset size {candidate.declared_size} exceeds "
            f"the configured maximum of {max_bytes} bytes"
        )

    digest = hashlib.sha256()
    total = 0
    try:
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValidationError(
                        f"job {job.id}: download exceeded the configured maximum "
                        f"of {max_bytes} bytes"
                    )
                digest.update(chunk)
                fh.write(chunk)
                if progress_cb is not None:
                    progress_cb(total, candidate.declared_size)
    finally:
        resp.close()

    if candidate.declared_size is not None and total != candidate.declared_size:
        raise ValidationError(
            f"job {job.id}: downloaded {total} bytes but the provider declared "
            f"{candidate.declared_size}"
        )

    return total, digest.hexdigest()


def inspect_ipa_metadata(path, filename, job, candidate=None):
    """Run shared IPA preflight and reject binaries explicitly marked tvOS."""
    release_ref = ""
    if candidate is not None:
        release_ref = f" (release {candidate.release_tag or candidate.release_id})"
    if not filename.lower().endswith(".ipa"):
        raise ValidationError(
            f"job {job.id}{release_ref}: asset filename {filename!r} does not end in .ipa"
        )
    label = f"job {job.id}{release_ref}"
    try:
        inspection = inspect_ipa(path, label)
    except InspectionError as exc:
        raise ValidationError(str(exc))
    if inspection.platform == "tvos":
        raise ValidationError(f"{label}: tvOS binaries are not supported")
    if inspection.platform == "unknown":
        logger.warning("%s: IPA platform could not be determined", label)
    return inspection


def inspect_apk_metadata(path, filename, job, candidate=None):
    """Run shared APK preflight and enforce the configured package match.

    This mirrors `inspect_ipa_metadata`, but Android has no separate extract
    and validate steps. There is only one identity field (`package`) to
    check, so this one function does the work that `extract_ipa_metadata`
    and `validate_and_extract_metadata` do together for iOS.
    """
    release_ref = ""
    if candidate is not None:
        release_ref = f" (release {candidate.release_tag or candidate.release_id})"
    if not filename.lower().endswith(".apk"):
        raise ValidationError(
            f"job {job.id}{release_ref}: asset filename {filename!r} does not end in .apk"
        )
    label = f"job {job.id}{release_ref}"
    try:
        inspection = inspect_apk(path, label)
    except ApkInspectionError as exc:
        raise ValidationError(str(exc))
    if job.package is not None and inspection.package != job.package:
        raise ValidationError(
            f"{label}: extracted package {inspection.package!r} does not match "
            f"configured package {job.package!r}"
        )
    return inspection


def extract_ipa_metadata(path, filename, job, candidate=None):
    """Run the six hard checks (contract Step 3), then return (bundle_id, version, name).

    `inspect_ipa` matches only the app's top-level Info.plist on purpose.
    An IPA contains an Info.plist for every bundled framework and extension,
    and a loose match would return one of those instead of the app's own
    identifier and version.

    This does not compare the extracted bundle identifier with a configured
    `job.bundle_identifier`. That comparison lives in
    `validate_and_extract_metadata`, its only caller. app.py's import routes,
    which auto-detect the bundle id, call `inspect_ipa_metadata` directly.
    """
    inspection = inspect_ipa_metadata(path, filename, job, candidate)
    return inspection.bundle_identifier, inspection.version, inspection.name


def validate_and_extract_metadata(path, filename, job, candidate=None):
    """`extract_ipa_metadata`, plus the check that the bundle id matches the configured one."""
    bundle_id, version, name = extract_ipa_metadata(path, filename, job, candidate)

    release_ref = ""
    if candidate is not None:
        release_ref = f" (release {candidate.release_tag or candidate.release_id})"

    if bundle_id != job.bundle_identifier:
        raise ValidationError(
            f"job {job.id}{release_ref}: extracted bundle identifier {bundle_id!r} "
            f"does not match configured bundleIdentifier {job.bundle_identifier!r}"
        )

    return bundle_id, version, name


# ---------------------------------------------------------------------------
# Feather HTTP client, based on the FeatherClient in
# scripts/telegram_bot_ingest.py. It never touches data/source.json,
# data/ipas/, data/icons/, or Garage directly.
# ---------------------------------------------------------------------------


class FeatherClient:
    def __init__(self, session, base_url, password, timeout=30):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.timeout = timeout
        self._logged_in = False
        self._inspection = None

    def set_preflight(self, inspection):
        self._inspection = inspection

    def get_app(self, bundle_id):
        """Public read. Returns None only for a 404 and raises on any other failure."""
        resp = self.session.get(f"{self.base_url}/api/app/{bundle_id}", timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def login(self):
        try:
            resp = self.session.post(
                f"{self.base_url}/api/login", json={"password": self.password}, timeout=30
            )
        except requests.RequestException as e:
            raise FeatherAuthError(f"cannot reach feather at {self.base_url}: {e.__class__.__name__}")
        if resp.status_code == 401:
            raise FeatherAuthError("feather /api/login returned 401 (check FEATHER_ADMIN_PASSWORD)")
        if resp.status_code == 429:
            raise FeatherAuthError(
                "feather /api/login is throttling after repeated failed logins; "
                f"retry in {resp.headers.get('Retry-After', '60')}s"
            )
        if resp.status_code >= 400:
            raise FeatherAuthError(f"feather /api/login returned HTTP {resp.status_code}")
        self._logged_in = True

    def android_index_has(self, package, version_code):
        """True if the public F-Droid index lists package@version_code, or
        None when the index cannot be read (no Android repo, or feather is
        down)."""
        try:
            resp = self.session.get(f"{self.base_url}/fdroid/repo/index-v1.json", timeout=30)
            if resp.status_code != 200:
                return None
            entries = (resp.json().get("packages") or {}).get(package) or []
        except (requests.RequestException, ValueError, AttributeError):
            return None
        return any(str(e.get("versionCode")) == str(version_code) for e in entries if isinstance(e, dict))

    def _ensure_login(self):
        if not self._logged_in:
            self.login()

    def add_version(self, bundle_id, version, path):
        self._ensure_login()
        with open(path, "rb") as fh:
            files = {"ipaFile": (os.path.basename(path), fh)}
            data = {"bundleIdentifier": bundle_id, "version": version}
            if self._inspection is not None:
                data["buildVersion"] = self._inspection.build_version
                if self._inspection.minimum_os_version:
                    data["minOSVersion"] = self._inspection.minimum_os_version
            resp = self.session.post(
                f"{self.base_url}/api/add-version",
                data=data,
                files=files,
                timeout=self.timeout,
            )
        if resp.status_code == 401:
            raise FeatherAuthError("feather /api/add-version returned 401")
        try:
            payload = resp.json()
        except ValueError:
            raise ProviderError("feather /api/add-version returned a non-JSON response")
        return bool(payload.get("success")), payload.get("error") or payload.get(
            "message"
        )

    def add_app(self, bundle_id, version, name, developer, path):
        self._ensure_login()
        with open(path, "rb") as fh:
            files = {"ipaFile": (os.path.basename(path), fh)}
            data = {
                "bundleIdentifier": bundle_id,
                "version": version,
                "name": name,
                "developerName": developer,
            }
            if self._inspection is not None:
                data["buildVersion"] = self._inspection.build_version
                if self._inspection.minimum_os_version:
                    data["minOSVersion"] = self._inspection.minimum_os_version
                if self._inspection.privacy:
                    data["privacy"] = json.dumps(self._inspection.privacy)
            resp = self.session.post(
                f"{self.base_url}/api/add-app",
                data=data,
                files=files,
                timeout=self.timeout,
            )
        if resp.status_code == 401:
            raise FeatherAuthError("feather /api/add-app returned 401")
        try:
            payload = resp.json()
        except ValueError:
            raise ProviderError("feather /api/add-app returned a non-JSON response")
        return bool(payload.get("success")), payload.get("error") or payload.get(
            "message"
        )

    def add_apk(self, path, package=None):
        """Publish an APK. `/api/android/add-apk` is already idempotent. A
        re-post of an existing version returns HTTP 200 with
        `{"success": true, "added": false, ...}`, not an error. So this
        returns `added` too, and the caller can tell "published" from
        "already present" without a second dedupe check."""
        self._ensure_login()
        with open(path, "rb") as fh:
            files = {"apkFile": (os.path.basename(path), fh)}
            data = {}
            if package:
                data["package"] = package
            resp = self.session.post(
                f"{self.base_url}/api/android/add-apk",
                data=data,
                files=files,
                timeout=self.timeout,
            )
        if resp.status_code == 401:
            raise FeatherAuthError("feather /api/android/add-apk returned 401")
        try:
            payload = resp.json()
        except ValueError:
            raise ProviderError("feather /api/android/add-apk returned a non-JSON response")
        ok = bool(payload.get("success"))
        message = payload.get("error") or payload.get("message")
        added = bool(payload.get("added"))
        return ok, message, added

    def record_import_event(self, record):
        self._ensure_login()
        resp = self.session.post(
            f"{self.base_url}/api/import-history/record",
            json=record,
            timeout=30,
        )
        if resp.status_code == 401:
            raise FeatherAuthError("feather import-history endpoint returned 401")
        resp.raise_for_status()
        return bool(resp.json().get("success"))


def _report_provenance(feather, record):
    if not hasattr(feather, "record_import_event"):
        return
    try:
        feather.record_import_event(record)
    except Exception as exc:
        logger.warning("provenance reporting failed: %s", _redact(str(exc)))


def _catalog_has_version(catalog_app, version):
    if not catalog_app or not version:
        return False
    for v in catalog_app.get("versions", []) or []:
        if v.get("version") == version:
            return True
    return False


# ---------------------------------------------------------------------------
# State is a non-secret ledger of the last verified publish per job. It is
# an optimization, never the authority. The importer always re-confirms the
# catalog.
# ---------------------------------------------------------------------------


# schemaVersion 2 (plan 083) nests a job's state entry by platform
# ({"ios": {...}, "android": {...}}) instead of one flat record. Reads still
# accept 1, because existing deployments have a schemaVersion:1 state file
# on disk. Its flat records always mean the iOS record (see
# _platform_state_record and _advance_state).
_STATE_SCHEMA_VERSIONS = (1, 2)


def load_state(path):
    if not path or not os.path.exists(path):
        return {"schemaVersion": 2, "jobs": {}}
    with open(path, "r") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or data.get("schemaVersion") not in _STATE_SCHEMA_VERSIONS:
        raise ConfigError(f"state file {path} has an unsupported schemaVersion")
    data.setdefault("jobs", {})
    return data


def save_state_atomic(path, state):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".release-import-state-", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _platform_state_record(state, job_id, platform):
    """Read the last-verified-publish record for one job and platform. Also
    accept the pre-083 flat per-job shape, which only ever recorded an iOS
    publish.

    It never mutates `state`, so it is safe to call before the dry-run gate.
    When `apply` is false, process_job must write nothing, not even a shape
    migration.
    """
    entry = state.get("jobs", {}).get(job_id)
    if not isinstance(entry, dict):
        return None
    if "releaseId" in entry:  # pre-083 flat shape, always the iOS record
        return entry if platform == "ios" else None
    return entry.get(platform)


def _advance_state(state, job, platform, candidate, version, sha256_hex, **identity):
    """Record one platform's last verified publish for `job`.

    A job's entry is keyed by platform ({"ios": {...}, "android": {...}}),
    so an iOS publish can never mask a pending Android one, or the reverse.
    A pre-083 flat entry has a top-level "releaseId". This moves it into
    `{"ios": <that record>}` instead of discarding it, since it always
    described an iOS publish.
    """
    jobs = state.setdefault("jobs", {})
    entry = jobs.get(job.id)
    if not isinstance(entry, dict):
        entry = {}
        jobs[job.id] = entry
    elif "releaseId" in entry:
        entry = {"ios": dict(entry)}
        jobs[job.id] = entry
    record = {
        "provider": job.provider,
        "project": job.project,
        "releaseId": candidate.release_id,
        "assetId": candidate.asset_id,
        "version": version,
        "sha256": sha256_hex,
        "publishedAt": _utcnow_iso(),
    }
    record.update(identity)
    entry[platform] = record


def acquire_apply_lock(state_path):
    """Take a non-blocking exclusive fcntl.flock() on a sibling lock file.

    Returns the open, locked file handle, or None if another apply run
    already holds it. The caller must flock(LOCK_UN) and close() when done.
    """
    lock_path = state_path + ".lock"
    directory = os.path.dirname(lock_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    lock_fh = open(lock_path, "a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fh.close()
        return None
    return lock_fh


# ---------------------------------------------------------------------------
# Per-job processing
# ---------------------------------------------------------------------------


def _process_ios_candidate(job, candidate, session, tokens, feather, state, apply,
                            timeout, max_bytes, summary, tmp_dir):
    """Process an iOS candidate, with the same behavior as the pre-083
    process_job.

    Returns True on success, including a clean skip or would-publish.
    Returns False on failure, and also increments summary.failed.
    """
    recorded = _platform_state_record(state, job.id, "ios")
    already_recorded = bool(
        recorded
        and recorded.get("releaseId") == candidate.release_id
        and recorded.get("assetId") == candidate.asset_id
    )

    # Without a configured bundleIdentifier, fall back to the one detected
    # on the previous run. With neither, the IPA must be downloaded first.
    known_bid = job.bundle_identifier or (recorded or {}).get("bundleIdentifier")
    catalog_app = None
    if known_bid:
        try:
            catalog_app = feather.get_app(known_bid)
        except Exception as e:
            _log_job_error(f"job {job.id}: failed to query feather for {known_bid}: {e}")
            summary.failed += 1
            return False

    if already_recorded and catalog_app and _catalog_has_version(
        catalog_app, recorded.get("version")
    ):
        summary.skipped += 1
        logger.info(
            "job %s: release/asset unchanged and catalog already has %s -- skipping",
            job.id,
            recorded.get("version"),
        )
        return True

    if known_bid and catalog_app is None and not job.create_if_missing:
        _log_job_error(
            f"job {job.id}: app {known_bid} not found in the catalog "
            "and createIfMissing is false"
        )
        summary.failed += 1
        return False

    if not apply:
        summary.would_publish += 1
        logger.info(
            "job %s: dry-run -- would download and publish release %s (ios)",
            job.id,
            candidate.release_tag or candidate.release_id,
        )
        return True

    # --apply from here: download, validate, reconcile, publish.
    tmp_path = _download_tmp_path(tmp_dir, "ios")
    try:
        _actual_size, sha256_hex = stream_download(
            session, candidate, job, tmp_path, tokens, timeout, max_bytes
        )
        summary.downloaded += 1
    except (ProviderError, ValidationError) as e:
        _log_job_error(f"job {job.id}: download failed: {e}")
        summary.failed += 1
        return False

    try:
        inspection = inspect_ipa_metadata(tmp_path, candidate.asset_name, job, candidate)
        bundle_id = inspection.bundle_identifier
        version = inspection.version
        if job.bundle_identifier and bundle_id != job.bundle_identifier:
            raise ValidationError(
                f"job {job.id}: extracted bundle identifier {bundle_id!r} does not "
                f"match configured bundleIdentifier {job.bundle_identifier!r}"
            )
    except ValidationError as e:
        _log_job_error(str(e))
        summary.failed += 1
        return False

    if bundle_id != known_bid:
        try:
            catalog_app = feather.get_app(bundle_id)
        except Exception as e:
            _log_job_error(f"job {job.id}: failed to query feather for {bundle_id}: {e}")
            summary.failed += 1
            return False
        if catalog_app is None and not job.create_if_missing:
            _log_job_error(
                f"job {job.id}: app {bundle_id} (detected from the IPA) not found in "
                "the catalog and createIfMissing is false"
            )
            summary.failed += 1
            return False

    if catalog_app and _catalog_has_version(catalog_app, version):
        _advance_state(state, job, "ios", candidate, version, sha256_hex,
                        bundleIdentifier=bundle_id)
        summary.skipped += 1
        logger.info(
            "job %s: catalog already has extracted version %s -- not "
            "re-uploading",
            job.id,
            version,
        )
        _report_provenance(feather, {
            "trigger": "standalone", "jobId": job.id, "status": "skipped",
            "stage": "publish", "provider": candidate.provider,
            "project": candidate.project, "releaseId": candidate.release_id,
            "releaseTag": candidate.release_tag, "assetId": candidate.asset_id,
            "assetName": candidate.asset_name, "bundleIdentifier": bundle_id,
            "version": version, "buildVersion": inspection.build_version,
            "platform": inspection.platform, "sha256": sha256_hex,
        })
        return True

    try:
        created = False
        if hasattr(feather, "set_preflight"):
            feather.set_preflight(inspection)
        if catalog_app is not None:
            ok, message = feather.add_version(bundle_id, version, tmp_path)
            if not ok and message == "App not found" and job.create_if_missing:
                ok, message = feather.add_app(
                    bundle_id, version, job.name, job.developer_name, tmp_path
                )
                created = ok
        else:
            ok, message = feather.add_app(
                bundle_id, version, job.name, job.developer_name, tmp_path
            )
            created = ok
    except FeatherAuthError as e:
        _log_job_error(f"job {job.id}: {e}")
        summary.failed += 1
        return False

    if not ok:
        _log_job_error(f"job {job.id}: feather publish failed: {message}")
        summary.failed += 1
        return False

    _advance_state(state, job, "ios", candidate, version, sha256_hex,
                    bundleIdentifier=bundle_id)
    if created:
        summary.created += 1
    else:
        summary.published += 1
    _report_provenance(feather, {
        "trigger": "standalone", "jobId": job.id, "status": "published",
        "stage": "publish", "provider": candidate.provider,
        "project": candidate.project, "releaseId": candidate.release_id,
        "releaseTag": candidate.release_tag, "assetId": candidate.asset_id,
        "assetName": candidate.asset_name, "bundleIdentifier": bundle_id,
        "version": version, "buildVersion": inspection.build_version,
        "platform": inspection.platform, "sha256": sha256_hex,
    })
    return True


def _process_android_candidate(job, candidate, session, tokens, feather, state, apply,
                                timeout, max_bytes, summary, tmp_dir):
    """Process an Android candidate.

    It never calls feather.get_app or _catalog_has_version, because those
    read the iOS catalog and mean nothing for an APK. It skips before
    downloading when the recorded release and asset match and
    `android_index_has` confirms the version. Otherwise dedupe relies on
    /api/android/add-apk's own idempotency (`added: false` on a re-post of
    an existing version).

    Returns True on success, including a clean would-publish or a publish
    the endpoint reports as already present. Returns False on failure, and
    also increments summary.failed.
    """
    # If the release asset is the same as last run and still in the signed
    # index, there is nothing to do and no reason to re-download an APK that
    # may be over 100 MB. If the APK was deleted from the repo since, the
    # index check fails and the importer imports it again.
    recorded = _platform_state_record(state, job.id, "android")
    if (
        recorded
        and recorded.get("releaseId") == candidate.release_id
        and recorded.get("assetId") == candidate.asset_id
        and recorded.get("package")
        and hasattr(feather, "android_index_has")
        and feather.android_index_has(recorded["package"], recorded.get("versionCode"))
    ):
        summary.skipped += 1
        logger.info(
            "job %s: release/asset unchanged and the repo already has %s versionCode %s -- skipping",
            job.id, recorded["package"], recorded.get("versionCode"),
        )
        return True

    if not apply:
        summary.would_publish += 1
        logger.info(
            "job %s: dry-run -- would download and publish release %s (android)",
            job.id,
            candidate.release_tag or candidate.release_id,
        )
        return True

    tmp_path = _download_tmp_path(tmp_dir, "android")
    try:
        _actual_size, sha256_hex = stream_download(
            session, candidate, job, tmp_path, tokens, timeout, max_bytes
        )
        summary.downloaded += 1
    except (ProviderError, ValidationError) as e:
        _log_job_error(f"job {job.id}: download failed: {e}")
        summary.failed += 1
        return False

    try:
        inspection = inspect_apk_metadata(tmp_path, candidate.asset_name, job, candidate)
    except ValidationError as e:
        _log_job_error(str(e))
        summary.failed += 1
        return False

    try:
        ok, message, added = feather.add_apk(tmp_path, package=job.package)
    except FeatherAuthError as e:
        _log_job_error(f"job {job.id}: {e}")
        summary.failed += 1
        return False

    if not ok:
        _log_job_error(f"job {job.id}: feather publish failed: {message}")
        summary.failed += 1
        return False

    _advance_state(state, job, "android", candidate, inspection.version_name, sha256_hex,
                    package=inspection.package, versionCode=inspection.version_code)
    if added:
        summary.published += 1
    else:
        summary.skipped += 1
        logger.info(
            "job %s: feather already has %s versionCode %s -- not re-uploading",
            job.id, inspection.package, inspection.version_code,
        )
    _report_provenance(feather, {
        "trigger": "standalone", "jobId": job.id,
        "status": "published" if added else "skipped",
        "stage": "publish", "provider": candidate.provider,
        "project": candidate.project, "releaseId": candidate.release_id,
        "releaseTag": candidate.release_tag, "assetId": candidate.asset_id,
        # The server's import history keeps only these field names and
        # drops unknown keys. The in-app Android paths record the package
        # the same way.
        "assetName": candidate.asset_name, "bundleIdentifier": inspection.package,
        "version": inspection.version_name, "buildVersion": str(inspection.version_code),
        "platform": "android", "sha256": sha256_hex,
    })
    return True


def process_job(job, session, tokens, feather, state, apply, timeout, max_bytes, summary):
    """Handle one job end to end, isolating its failures from other jobs.

    It selects at most one candidate per platform (see `select_candidate`)
    and processes each one separately with `_process_ios_candidate` or
    `_process_android_candidate`. A failure on one platform never blocks the
    other platform's publish in the same job and release.

    Returns True only if every candidate succeeded, including a clean skip
    or would-publish. Returns False if any failed, and then summary.failed
    has gone up once per failing candidate.
    """
    summary.checked += 1

    try:
        candidates = select_candidate(job, session, tokens, timeout=timeout)
    except (ProviderError, ConfigError) as e:
        _log_job_error(str(e))
        summary.failed += 1
        return False
    except Exception:
        logger.exception("job %s: unexpected error selecting a release", job.id)
        summary.failed += 1
        return False

    tmp_dir = tempfile.mkdtemp(prefix="release-import-") if apply else None
    try:
        job_ok = True
        for candidate in candidates:
            process = _process_android_candidate if candidate.platform == "android" else _process_ios_candidate
            try:
                candidate_ok = process(
                    job, candidate, session, tokens, feather, state, apply,
                    timeout, max_bytes, summary, tmp_dir,
                )
            except Exception as e:
                # A network error mid-download, Feather restarting mid-upload,
                # a non-JSON response or a malformed archive used to escape
                # here and end the whole run. Later jobs and this job's other
                # platform never ran, and no summary was printed.
                logger.error("job %s: %s candidate failed: %s: %s", job.id, candidate.platform,
                             type(e).__name__, _redact(str(e)))
                summary.failed += 1
                candidate_ok = False
            job_ok = job_ok and candidate_ok
        return job_ok
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="release_source_ingest.py",
        description=(
            "Poll a configured GitHub/GitLab repository for release assets and "
            "publish new IPAs to feather. Dry-run by default; pass --apply to "
            "download and publish."
        ),
    )
    parser.add_argument(
        "--config",
        help="Path to the release-sources manifest JSON (default: $RELEASE_IMPORT_CONFIG)",
    )
    parser.add_argument(
        "--state",
        help="Path to the importer's state file (default: $RELEASE_IMPORT_STATE)",
    )
    parser.add_argument("--job", help="Only process the job with this id")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Download and publish (default: dry-run, no writes)",
    )
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    env = os.environ

    try:
        config_path = args.config or env.get("RELEASE_IMPORT_CONFIG")
        state_path = args.state or env.get("RELEASE_IMPORT_STATE")
        if not config_path:
            raise ConfigError("no config path: pass --config or set RELEASE_IMPORT_CONFIG")
        if not state_path:
            raise ConfigError("no state path: pass --state or set RELEASE_IMPORT_STATE")

        manifest = load_manifest(config_path)
        jobs = manifest["jobs"]
        if args.job:
            jobs = [j for j in jobs if j.id == args.job]
            if not jobs:
                raise ConfigError(f"unknown --job id: {args.job!r}")

        github_token = env.get("GITHUB_TOKEN")
        gitlab_token = env.get("GITLAB_TOKEN")
        tokens = {"github": github_token, "gitlab": gitlab_token}

        feather_base_url = env.get("FEATHER_BASE_URL")
        if not feather_base_url:
            raise ConfigError("FEATHER_BASE_URL must be set")

        feather_password = None
        if args.apply:
            feather_password = env.get("FEATHER_ADMIN_PASSWORD")
            if not feather_password:
                raise ConfigError("FEATHER_ADMIN_PASSWORD must be set to use --apply")

        timeout = _int_env(env, "RELEASE_IMPORT_TIMEOUT", DEFAULT_TIMEOUT)
        max_bytes = _int_env(env, "RELEASE_IMPORT_MAX_BYTES", DEFAULT_MAX_BYTES)
    except ConfigError as e:
        logger.error(str(e))
        return 2

    _set_active_secrets(github_token, gitlab_token, feather_password)

    lock_fh = None
    if args.apply:
        lock_fh = acquire_apply_lock(state_path)
        if lock_fh is None:
            print("already running; skipped")
            return 0

    summary = Summary()
    try:
        provider_session = requests.Session()
        feather_session = requests.Session()
        feather = FeatherClient(
            feather_session, feather_base_url, feather_password, timeout=timeout
        )

        state = load_state(state_path)

        if args.apply:
            # Fail fast and once. A wrong password or unreachable feather
            # used to show up per job, after downloads, and each retry
            # counted against the login throttle.
            try:
                feather.login()
            except FeatherAuthError as e:
                logger.error(_redact(str(e)))
                summary.failed += 1
                print(summary.format())
                return 1

        any_failed = False
        for job in jobs:
            ok = process_job(
                job,
                provider_session,
                tokens,
                feather,
                state,
                args.apply,
                timeout,
                max_bytes,
                summary,
            )
            if not ok:
                any_failed = True
            if args.apply:
                save_state_atomic(state_path, state)

        exit_code = 1 if any_failed else 0
    finally:
        if lock_fh is not None:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()

    print(summary.format())
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
