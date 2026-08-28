"""One-shot GitHub/GitLab release importer, launched by host crontab (Plan 029).

Reads a small JSON manifest of "jobs" (one GitHub or GitLab repository each),
discovers the single configured IPA asset from the latest eligible release,
downloads and validates it to a temporary file, reads the *real* bundle
identifier and version out of the app's top-level `Info.plist`, and
publishes it through feather's existing authenticated HTTP API
(`/api/login`, `/api/add-version`, `/api/add-app`) -- exactly like
`scripts/telegram_bot_ingest.py` does. This script never imports `app.py`,
never calls the dormant `SourceManager.add_app_from_github`, and never
writes `data/source.json`, `data/ipas/`, `data/icons/`, or any Garage
object directly. Feather remains the only writer of the catalog.

Dry-run is the default: with no `--apply`, this may only query provider
release metadata and feather's public `/api/app/<id>` endpoint. It never
downloads an asset, logs in, publishes, takes the apply lock, or writes the
state file. `--apply` enables all of that, guarded by a non-blocking
`fcntl.flock()` so two crontab runs can never race each other or feather's
in-process `SourceManager._lock`.

No network call happens at import time -- only `main()` (and the functions
it calls) ever touch the network, so this module can be imported and unit
tested (see tests/test_release_source_ingest.py) with zero network access.

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
except ImportError:  # direct execution from scripts/
    from ipa_inspection import InspectionError, inspect_ipa

try:
    import fcntl
except ImportError:  # pragma: no cover - this script only ever runs on Linux
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
GITHUB_API_VERSION = "2026-03-10"

# Ceiling, not a delay -- a fast transfer returns immediately. Overridable
# via RELEASE_IMPORT_TIMEOUT.
DEFAULT_TIMEOUT = 900
# 2 GiB. Overridable via RELEASE_IMPORT_MAX_BYTES.
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_REDIRECTS = 5

_APP_INFO_PLIST = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")


# ---------------------------------------------------------------------------
# Small data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """One validated manifest entry."""

    id: str
    provider: str
    project: str
    bundle_identifier: str
    asset_glob: str
    asset_exclude_glob: str = None
    include_prereleases: bool = False
    create_if_missing: bool = False
    allowed_download_hosts: frozenset = field(default_factory=frozenset)
    name: str = None
    developer_name: str = None


@dataclass(frozen=True)
class ReleaseCandidate:
    """One normalized, selected release+asset -- no token-bearing headers."""

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
# Secret redaction -- applied to every job-level log line. The manifest
# never carries a secret, but a run's tokens/password are set here once so
# no log call can accidentally leak one. See _redact's telegram-bot analog.
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
# Manifest / config loading
# ---------------------------------------------------------------------------

_GITHUB_PROJECT_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _require_nonempty_str(raw, key, job_id):
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        label = job_id or "<unknown>"
        raise ConfigError(f"job {label}: {key!r} must be a non-empty string")
    return value


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
        pass  # not an IP literal -- good, that's required
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

        bundle_identifier = _require_nonempty_str(raw, "bundleIdentifier", job_id)
        asset_glob = _require_nonempty_str(raw, "assetGlob", job_id)
        asset_exclude_glob = raw.get("assetExcludeGlob")
        if asset_exclude_glob is not None and not isinstance(asset_exclude_glob, str):
            raise ConfigError(
                f"job {job_id}: 'assetExcludeGlob' must be a string"
            )
        asset_exclude_glob = (asset_exclude_glob or "").strip() or None

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
                asset_glob=asset_glob,
                asset_exclude_glob=asset_exclude_glob,
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
# Provider adapters -- GitHub and GitLab only. Fixed API origins; no
# configurable base URL in v1 (self-hosted GitLab/GitHub is out of scope).
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
    """Return provider-neutral include/exclude selector state for one asset."""
    normalized = (name or "").lower()
    included = fnmatch.fnmatch(normalized, job.asset_glob.lower())
    excluded = bool(
        job.asset_exclude_glob
        and fnmatch.fnmatch(normalized, job.asset_exclude_glob.lower())
    )
    return {"included": included, "excluded": excluded, "matched": included and not excluded}


def _github_eligible_releases(job, releases):
    eligible = [
        r for r in releases
        if not r.get("draft") and (job.include_prereleases or not r.get("prerelease"))
    ]
    eligible.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    return eligible


def github_select_candidate(job, releases):
    for release in _github_eligible_releases(job, releases):
        assets = release.get("assets") or []
        matches = [a for a in assets if _asset_match_state(job, a.get("name"))["matched"]]
        if not matches:
            continue
        if len(matches) > 1:
            raise ProviderError(
                f"job {job.id}: release "
                f"{release.get('tag_name') or release.get('id')} has {len(matches)} "
                f"assets matching {job.asset_glob!r}, expected exactly one"
            )
        asset = matches[0]
        return ReleaseCandidate(
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
        )
    return None


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
    for release in _gitlab_eligible_releases(releases, now=now):
        links = (release.get("assets") or {}).get("links") or []
        matches = [link for link in links if _asset_match_state(job, link.get("name"))["matched"]]
        if not matches:
            continue
        if len(matches) > 1:
            raise ProviderError(
                f"job {job.id}: release "
                f"{release.get('tag_name') or release.get('id')} has {len(matches)} "
                f"asset links matching {job.asset_glob!r}, expected exactly one"
            )
        link = matches[0]
        url = link.get("url")
        host = (urlparse(url).hostname or "").lower() if url else ""
        if not url or not host or host not in job.allowed_download_hosts:
            raise ProviderError(
                f"job {job.id}: asset link host {host!r} is not in allowedDownloadHosts"
            )
        return ReleaseCandidate(
            provider="gitlab",
            project=job.project,
            release_id=str(release.get("id") or release.get("tag_name") or ""),
            release_tag=release.get("tag_name"),
            release_time=release.get("released_at") or "",
            asset_id=str(link.get("id") or link.get("name")),
            asset_name=link.get("name") or "",
            declared_size=None,
            download_url=url,
            auth_host=host,
            release_body=release.get("description") or "",
        )
    return None


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
    if job.provider == "github":
        releases = github_list_releases(
            session, job.project, tokens.get("github"), timeout=timeout, job_id=job.id
        )
        candidate = github_select_candidate(job, releases)
    elif job.provider == "gitlab":
        releases = gitlab_list_releases(
            session, job.project, tokens.get("gitlab"), timeout=timeout, job_id=job.id
        )
        candidate = gitlab_select_candidate(job, releases)
    else:  # pragma: no cover - parse_manifest_dict already rejects this
        raise ConfigError(f"job {job.id}: unknown provider {job.provider!r}")

    if candidate is None:
        raise ProviderError(
            f"job {job.id}: no eligible release had exactly one asset matching "
            f"{job.asset_glob!r}"
        )
    return candidate


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

    Explicit redirect handling (never `allow_redirects=True`): validates
    HTTPS + an allowed hostname at *every* hop, and strips the provider
    Authorization/PRIVATE-TOKEN header the instant a redirect crosses to a
    different host. Returns (actual_size, sha256_hex).
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


def extract_ipa_metadata(path, filename, job, candidate=None):
    """The six hard checks (contract Step 3), then (bundle_id, version, name).

    The regex is deliberately exact: an IPA contains an Info.plist for every
    bundled framework/extension, and a loose match would return one of
    those instead of the app's own identifier and version.

    Does NOT compare the extracted bundle identifier against any configured
    `job.bundle_identifier` -- that comparison lives in
    `validate_and_extract_metadata` so callers that want to auto-detect the
    bundle id (e.g. the UI import route) can call this directly.
    """
    inspection = inspect_ipa_metadata(path, filename, job, candidate)
    return inspection.bundle_identifier, inspection.version, inspection.name


def validate_and_extract_metadata(path, filename, job, candidate=None):
    """`extract_ipa_metadata` plus the configured-bundle-id match check."""
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
# Feather HTTP client -- patterned on scripts/telegram_bot_ingest.py's
# FeatherClient. Never touches data/source.json, data/ipas/, data/icons/,
# or Garage directly.
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
        """Public read. Returns None only for 404; any other failure raises."""
        resp = self.session.get(f"{self.base_url}/api/app/{bundle_id}", timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def login(self):
        resp = self.session.post(
            f"{self.base_url}/api/login", json={"password": self.password}, timeout=30
        )
        if resp.status_code == 401:
            raise FeatherAuthError("feather /api/login returned 401")
        resp.raise_for_status()
        self._logged_in = True

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
# State: non-secret ledger of the last verified publish per job. An
# optimization, never the authority -- the catalog is always re-confirmed.
# ---------------------------------------------------------------------------


def load_state(path):
    if not path or not os.path.exists(path):
        return {"schemaVersion": 1, "jobs": {}}
    with open(path, "r") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
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


def _advance_state(state, job, candidate, version, sha256_hex):
    state.setdefault("jobs", {})[job.id] = {
        "provider": job.provider,
        "project": job.project,
        "releaseId": candidate.release_id,
        "assetId": candidate.asset_id,
        "bundleIdentifier": job.bundle_identifier,
        "version": version,
        "sha256": sha256_hex,
        "publishedAt": _utcnow_iso(),
    }


def acquire_apply_lock(state_path):
    """Non-blocking exclusive fcntl.flock() on a sibling lock file.

    Returns the open, locked file handle, or None if another apply run
    already holds it. The caller must flock(LOCK_UN) + close() when done.
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


def process_job(job, session, tokens, feather, state, apply, timeout, max_bytes, summary):
    """Handle one job end to end, isolating its failures from other jobs.

    Returns True on success (including a clean skip/would-publish), False
    on failure (summary.failed is also incremented in that case).
    """
    summary.checked += 1

    try:
        candidate = select_candidate(job, session, tokens, timeout=timeout)
    except (ProviderError, ConfigError) as e:
        _log_job_error(str(e))
        summary.failed += 1
        return False
    except Exception:
        logger.exception("job %s: unexpected error selecting a release", job.id)
        summary.failed += 1
        return False

    recorded = state.get("jobs", {}).get(job.id)
    already_recorded = bool(
        recorded
        and recorded.get("releaseId") == candidate.release_id
        and recorded.get("assetId") == candidate.asset_id
    )

    try:
        catalog_app = feather.get_app(job.bundle_identifier)
    except Exception as e:
        _log_job_error(f"job {job.id}: failed to query feather for {job.bundle_identifier}: {e}")
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

    if catalog_app is None and not job.create_if_missing:
        _log_job_error(
            f"job {job.id}: app {job.bundle_identifier} not found in the catalog "
            "and createIfMissing is false"
        )
        summary.failed += 1
        return False

    if not apply:
        summary.would_publish += 1
        logger.info(
            "job %s: dry-run -- would download and publish release %s",
            job.id,
            candidate.release_tag or candidate.release_id,
        )
        return True

    # --apply from here: download, validate, reconcile, publish.
    tmp_dir = tempfile.mkdtemp(prefix="release-import-")
    tmp_path = os.path.join(tmp_dir, "download.ipa")
    try:
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
            if bundle_id != job.bundle_identifier:
                raise ValidationError(
                    f"job {job.id}: extracted bundle identifier {bundle_id!r} does not "
                    f"match configured bundleIdentifier {job.bundle_identifier!r}"
                )
        except ValidationError as e:
            _log_job_error(str(e))
            summary.failed += 1
            return False

        if catalog_app and _catalog_has_version(catalog_app, version):
            _advance_state(state, job, candidate, version, sha256_hex)
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

        _advance_state(state, job, candidate, version, sha256_hex)
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
    finally:
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
