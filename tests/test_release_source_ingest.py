"""Tests for the cron release importer (Plan 029).

No test in this file touches the network, GitHub, GitLab, Feather, or
Docker -- provider/download HTTP is served by a hand-rolled FakeSession
(recording calls, returning canned FakeResponse objects), and feather
publication is served by a hand-rolled FakeFeatherClient, following the
FakeBotAPI/FakeFeatherClient pattern in tests/test_telegram_bot_ingest.py.
Real temporary ZIP/plist fixtures are used for IPA validation.

See plans/029-cron-release-imports.md Step 8 for the 21 required tests.
"""

import fcntl
import io
import json
import os
import plistlib
import tempfile
import zipfile
from urllib.parse import quote

import pytest

from scripts import release_source_ingest as ingest


# ---------------------------------------------------------------------------
# Fakes -- record calls, never touch the network.
# ---------------------------------------------------------------------------


class FakeResponse:
    """Records only what the importer touches -- status/json/headers/body."""

    def __init__(self, status_code=200, json_data=None, headers=None, content_chunks=None):
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {}
        self._content_chunks = content_chunks if content_chunks is not None else []
        self.closed = False

    def json(self):
        if self._json_data is None:
            raise ValueError("no json configured on this FakeResponse")
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024 * 1024):
        for chunk in self._content_chunks:
            yield chunk

    def close(self):
        self.closed = True


class FakeSession:
    """Maps an exact URL to a queue of FakeResponse objects.

    Records every call (url, kwargs) so tests can assert on headers sent
    (e.g. that provider auth was stripped on a cross-host redirect).
    """

    def __init__(self):
        self.responses = {}
        self.calls = []

    def add_response(self, url, response):
        self.responses.setdefault(url, []).append(response)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        queue = self.responses.get(url)
        if not queue:
            raise AssertionError(f"no fake response configured for GET {url}")
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        queue = self.responses.get(url)
        if not queue:
            raise AssertionError(f"no fake response configured for POST {url}")
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]


class FakeFeatherClient:
    """Stands in for FeatherClient -- records calls, never touches HTTP.

    add_version/add_app read the file at `path` into memory *at call time*
    (bundle_id, version, content_bytes) rather than storing the path, since
    process_job deletes its temp directory before a test can inspect it.
    """

    def __init__(self, apps=None, add_version_result=(True, "ok"), add_app_result=(True, "ok"),
                 get_app_error=None, add_apk_result=(True, "Added", True)):
        self.apps = {k: dict(v) for k, v in (apps or {}).items()}
        for app in self.apps.values():
            app.setdefault("versions", [])
        self.login_calls = 0
        self.get_app_calls = []
        self.add_version_calls = []
        self.add_app_calls = []
        self.add_apk_calls = []
        self.add_version_result = add_version_result
        self.add_app_result = add_app_result
        self.get_app_error = get_app_error
        # (ok, message, added), matching FeatherClient.add_apk's return shape.
        # Reassign between calls (`feather.add_apk_result = (...)`) to
        # simulate the endpoint's own idempotency on a second run.
        self.add_apk_result = add_apk_result

    def get_app(self, bundle_id):
        self.get_app_calls.append(bundle_id)
        if self.get_app_error is not None:
            raise self.get_app_error
        return self.apps.get(bundle_id)

    def add_version(self, bundle_id, version, path):
        self.login_calls += 1
        with open(path, "rb") as fh:
            content = fh.read()
        self.add_version_calls.append((bundle_id, version, content))
        ok, message = self.add_version_result
        if ok:
            app = self.apps.setdefault(bundle_id, {"bundleIdentifier": bundle_id, "versions": []})
            app["versions"].insert(0, {"version": version})
        return ok, message

    def add_app(self, bundle_id, version, name, developer, path):
        self.login_calls += 1
        with open(path, "rb") as fh:
            content = fh.read()
        self.add_app_calls.append((bundle_id, version, name, developer, content))
        ok, message = self.add_app_result
        if ok:
            self.apps[bundle_id] = {"bundleIdentifier": bundle_id, "versions": [{"version": version}]}
        return ok, message

    def add_apk(self, path, package=None):
        self.login_calls += 1
        with open(path, "rb") as fh:
            content = fh.read()
        self.add_apk_calls.append((path, package, content))
        return self.add_apk_result


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def make_job(**overrides):
    defaults = dict(
        id="job1",
        provider="github",
        project="owner/repo",
        bundle_identifier="com.example.app",
        asset_glob="*.ipa",
        include_prereleases=False,
        create_if_missing=False,
        allowed_download_hosts=frozenset(),
        name=None,
        developer_name=None,
    )
    defaults.update(overrides)
    return ingest.Job(**defaults)


def make_candidate(**overrides):
    defaults = dict(
        provider="github",
        project="owner/repo",
        release_id="1",
        release_tag="v1.0.0",
        release_time="2026-01-01T00:00:00Z",
        asset_id="1",
        asset_name="App.ipa",
        declared_size=None,
        download_url="https://api.github.com/repos/owner/repo/releases/assets/1",
        auth_host="api.github.com",
    )
    defaults.update(overrides)
    return ingest.ReleaseCandidate(**defaults)


def build_ipa_bytes(bundle_id="com.example.app", version="1.0.0", extra_plists=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        plist = {"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version}
        zf.writestr("Payload/App.app/Info.plist", plistlib.dumps(plist, fmt=plistlib.FMT_BINARY))
        if extra_plists:
            for name, pdict in extra_plists.items():
                zf.writestr(name, plistlib.dumps(pdict, fmt=plistlib.FMT_BINARY))
    return buf.getvalue()


DEFAULT_RELEASE_ID = 501
DEFAULT_ASSET_ID = 9001


def make_github_session(
    ipa_bytes,
    asset_size=None,
    asset_name="App.ipa",
    release_id=DEFAULT_RELEASE_ID,
    asset_id=DEFAULT_ASSET_ID,
    tag="v1.0.0",
    published_at="2026-01-01T00:00:00Z",
    project="owner/repo",
):
    releases_url = f"https://api.github.com/repos/{project}/releases"
    asset_url = f"https://api.github.com/repos/{project}/releases/assets/{asset_id}"
    session = FakeSession()
    session.add_response(
        releases_url,
        FakeResponse(
            200,
            json_data=[
                {
                    "id": release_id,
                    "tag_name": tag,
                    "draft": False,
                    "prerelease": False,
                    "published_at": published_at,
                    "assets": [
                        {"id": asset_id, "name": asset_name, "size": asset_size, "url": asset_url}
                    ],
                }
            ],
        ),
    )
    session.add_response(asset_url, FakeResponse(200, content_chunks=[ipa_bytes]))
    return session


def make_github_session_dual(
    ipa_bytes,
    apk_bytes,
    ipa_name="App.ipa",
    apk_name="App.apk",
    release_id=DEFAULT_RELEASE_ID,
    ipa_asset_id=DEFAULT_ASSET_ID,
    apk_asset_id=DEFAULT_ASSET_ID + 1,
    tag="v1.0.0",
    published_at="2026-01-01T00:00:00Z",
    project="owner/repo",
):
    """Like make_github_session, but for a release with both an .ipa and an
    .apk asset -- for plan 083's cross-platform-in-one-release tests."""
    releases_url = f"https://api.github.com/repos/{project}/releases"
    ipa_url = f"https://api.github.com/repos/{project}/releases/assets/{ipa_asset_id}"
    apk_url = f"https://api.github.com/repos/{project}/releases/assets/{apk_asset_id}"
    session = FakeSession()
    session.add_response(
        releases_url,
        FakeResponse(
            200,
            json_data=[{
                "id": release_id, "tag_name": tag, "draft": False, "prerelease": False,
                "published_at": published_at,
                "assets": [
                    {"id": ipa_asset_id, "name": ipa_name, "size": None, "url": ipa_url},
                    {"id": apk_asset_id, "name": apk_name, "size": None, "url": apk_url},
                ],
            }],
        ),
    )
    session.add_response(ipa_url, FakeResponse(200, content_chunks=[ipa_bytes]))
    session.add_response(apk_url, FakeResponse(200, content_chunks=[apk_bytes]))
    return session


def _valid_github_job(**overrides):
    job = {
        "id": "gh",
        "provider": "github",
        "project": "owner/repo",
        "bundleIdentifier": "com.example.app",
        "assetGlob": "*.ipa",
    }
    job.update(overrides)
    return job


NO_TOKENS = {"github": None, "gitlab": None}


# ---------------------------------------------------------------------------
# 1. test_load_manifest_accepts_github_and_gitlab_jobs
# ---------------------------------------------------------------------------


def test_load_manifest_accepts_github_and_gitlab_jobs(tmp_path):
    cfg = tmp_path / "sources.json"
    cfg.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "jobs": [
                    {
                        "id": "gh",
                        "provider": "github",
                        "project": "owner/repo",
                        "bundleIdentifier": "com.example.app",
                        "assetGlob": "*.ipa",
                        "includePrereleases": True,
                    },
                    {
                        "id": "gl",
                        "provider": "gitlab",
                        "project": "group/project",
                        "bundleIdentifier": "com.example.other",
                        "assetGlob": "*.ipa",
                        "allowedDownloadHosts": ["gitlab.com"],
                        "createIfMissing": True,
                        "name": "Other",
                        "developerName": "Dev",
                    },
                ],
            }
        )
    )

    manifest_result = ingest.load_manifest(str(cfg))
    jobs = {j.id: j for j in manifest_result["jobs"]}

    assert jobs["gh"].provider == "github"
    assert jobs["gh"].project == "owner/repo"
    assert jobs["gh"].include_prereleases is True
    assert jobs["gh"].create_if_missing is False

    assert jobs["gl"].provider == "gitlab"
    assert jobs["gl"].allowed_download_hosts == frozenset({"gitlab.com"})
    assert jobs["gl"].create_if_missing is True
    assert jobs["gl"].name == "Other"
    assert jobs["gl"].developer_name == "Dev"

    # The shipped example manifest must also parse cleanly.
    example_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "release-sources.example.json",
    )
    example = ingest.load_manifest(example_path)
    assert len(example["jobs"]) == 2


def test_asset_exclude_glob_round_trips_and_rejects_non_string():
    job = ingest.parse_manifest_dict({
        "schemaVersion": 1,
        "jobs": [_valid_github_job(assetExcludeGlob=" *-tvOS.ipa ")],
    })[0]
    assert job.asset_exclude_glob == "*-tvOS.ipa"
    with pytest.raises(ingest.ConfigError, match="assetExcludeGlob"):
        ingest.parse_manifest_dict({
            "schemaVersion": 1,
            "jobs": [_valid_github_job(assetExcludeGlob=["*-tvOS.ipa"])],
        })


def test_github_selector_excludes_ambiguous_tvos_asset():
    job = ingest.Job(
        id="gh", provider="github", project="owner/repo",
        bundle_identifier="com.example.app", asset_glob="SceneBox-*.ipa",
        asset_exclude_glob="*-tvOS.ipa",
    )
    releases = [{
        "id": 1, "tag_name": "v1", "draft": False, "prerelease": False,
        "published_at": "2026-01-01T00:00:00Z",
        "assets": [
            {"id": 1, "name": "SceneBox-1.0.ipa", "size": 10, "url": "https://api.github.com/a"},
            {"id": 2, "name": "SceneBox-1.0-tvOS.ipa", "size": 11, "url": "https://api.github.com/b"},
        ],
    }]
    candidates = ingest.github_select_candidate(job, releases)
    assert len(candidates) == 1
    assert candidates[0].asset_name == "SceneBox-1.0.ipa"
    assert candidates[0].platform == "ios"


def test_inspect_release_assets_is_sanitized_ordered_and_capped():
    releases = []
    for number in range(7):
        releases.append({
            "id": number,
            "tag_name": f"v{number}",
            "draft": False,
            "prerelease": False,
            "published_at": f"2026-01-{number + 1:02d}T00:00:00Z",
            "assets": [{
                "id": number,
                "name": f"App-{number}.ipa",
                "size": number,
                "url": f"https://api.github.com/secret/{number}",
            }],
        })
    session = FakeSession()
    session.add_response(
        "https://api.github.com/repos/owner/repo/releases",
        FakeResponse(200, json_data=releases),
    )
    job = ingest.Job(
        id="gh", provider="github", project="owner/repo",
        bundle_identifier="com.example.app", asset_glob="*.ipa",
    )
    preview = ingest.inspect_release_assets(job, session, {"github": "secret"})
    assert len(preview) == 5
    assert preview[0]["release"] == "v6"
    assert preview[0]["matchCount"] == 1
    encoded = json.dumps(preview)
    assert "url" not in encoded.lower()
    assert "secret" not in encoded


def test_gitlab_inspection_reports_host_allowlist_without_url():
    session = FakeSession()
    session.add_response(
        "https://gitlab.com/api/v4/projects/group%2Frepo/releases",
        FakeResponse(200, json_data=[{
            "tag_name": "v1", "released_at": "2026-01-01T00:00:00Z",
            "assets": {"links": [
                {"name": "App.ipa", "url": "https://gitlab.com/download/App.ipa"},
                {"name": "Mirror.ipa", "url": "https://evil.example/App.ipa"},
            ]},
        }]),
    )
    job = ingest.Job(
        id="gl", provider="gitlab", project="group/repo",
        bundle_identifier="com.example.app", asset_glob="App.ipa",
        allowed_download_hosts=frozenset({"gitlab.com"}),
    )
    preview = ingest.inspect_release_assets(job, session, {"gitlab": None})
    assert preview[0]["assets"][0]["hostAllowed"] is True
    assert preview[0]["assets"][1]["hostAllowed"] is False
    assert "https://" not in json.dumps(preview)


# ---------------------------------------------------------------------------
# 2. test_load_manifest_rejects_unknown_duplicate_or_unsafe_jobs
# ---------------------------------------------------------------------------


def test_load_manifest_rejects_unknown_duplicate_or_unsafe_jobs():
    bad_manifests = [
        {"schemaVersion": 2, "jobs": [_valid_github_job()]},
        {"schemaVersion": 1, "jobs": []},
        {"schemaVersion": 1, "jobs": [_valid_github_job(provider="bitbucket")]},
        {"schemaVersion": 1, "jobs": [_valid_github_job(), _valid_github_job()]},
        {"schemaVersion": 1, "jobs": [_valid_github_job(project="https://github.com/owner/repo")]},
        {"schemaVersion": 1, "jobs": [_valid_github_job(project="owner/repo.git")]},
        {"schemaVersion": 1, "jobs": [_valid_github_job(project="just-one-segment")]},
        {
            "schemaVersion": 1,
            "jobs": [
                {
                    "id": "gl",
                    "provider": "gitlab",
                    "project": "group/project",
                    "bundleIdentifier": "com.example.app",
                    "assetGlob": "*.ipa",
                }
            ],
        },
        {
            "schemaVersion": 1,
            "jobs": [
                {
                    "id": "gl",
                    "provider": "gitlab",
                    "project": "group/project",
                    "bundleIdentifier": "com.example.app",
                    "assetGlob": "*.ipa",
                    "allowedDownloadHosts": ["10.0.0.1"],
                }
            ],
        },
        {
            "schemaVersion": 1,
            "jobs": [
                {
                    "id": "gl",
                    "provider": "gitlab",
                    "project": "group/project",
                    "bundleIdentifier": "com.example.app",
                    "assetGlob": "*.ipa",
                    "allowedDownloadHosts": ["GitLab.com"],
                }
            ],
        },
        {"schemaVersion": 1, "jobs": [_valid_github_job(createIfMissing=True)]},
        {"schemaVersion": 1, "jobs": [_valid_github_job(id="")]},
    ]

    for manifest_dict in bad_manifests:
        with pytest.raises(ingest.ConfigError):
            ingest.parse_manifest_dict(manifest_dict)


# ---------------------------------------------------------------------------
# Job identity: bundleIdentifier (iOS) / package (Android) -- plan 083
# ---------------------------------------------------------------------------


def test_manifest_with_only_bundle_identifier_behaves_exactly_as_before():
    """Backward-compatibility gate (plan 083 done criteria): an iOS-only
    manifest specifying only bundleIdentifier must keep parsing exactly as
    it did before `package` was introduced."""
    job = ingest.parse_manifest_dict({
        "schemaVersion": 1,
        "jobs": [_valid_github_job()],
    })[0]
    assert job.bundle_identifier == "com.example.app"
    assert job.package is None
    assert job.asset_glob == "*.ipa"


def test_manifest_accepts_android_only_job_via_package():
    job = ingest.parse_manifest_dict({
        "schemaVersion": 1,
        "jobs": [_valid_github_job(bundleIdentifier=None, package="org.example.app",
                                    assetGlob="*.apk")],
    })[0]
    assert job.bundle_identifier is None
    assert job.package == "org.example.app"


def test_manifest_accepts_job_with_both_identities():
    job = ingest.parse_manifest_dict({
        "schemaVersion": 1,
        "jobs": [_valid_github_job(package="org.example.app", assetGlob="*.{ipa,apk}")],
    })[0]
    assert job.bundle_identifier == "com.example.app"
    assert job.package == "org.example.app"


def test_manifest_requires_bundle_identifier_or_package():
    with pytest.raises(ingest.ConfigError, match="bundleIdentifier.*package|package.*bundleIdentifier"):
        ingest.parse_manifest_dict({
            "schemaVersion": 1,
            "jobs": [_valid_github_job(bundleIdentifier=None)],
        })


def test_manifest_rejects_invalid_package_name():
    with pytest.raises(ingest.ConfigError, match="package"):
        ingest.parse_manifest_dict({
            "schemaVersion": 1,
            "jobs": [_valid_github_job(package="not_a_valid_package")],
        })


def test_manifest_rejects_non_string_package():
    with pytest.raises(ingest.ConfigError, match="package"):
        ingest.parse_manifest_dict({
            "schemaVersion": 1,
            "jobs": [_valid_github_job(package=123)],
        })


# ---------------------------------------------------------------------------
# 3. test_config_errors_and_logs_never_expose_tokens
# ---------------------------------------------------------------------------


def test_config_errors_and_logs_never_expose_tokens(tmp_path, monkeypatch, caplog):
    cfg = tmp_path / "sources.json"
    cfg.write_text(json.dumps({"schemaVersion": 1, "jobs": [_valid_github_job()]}))
    state_path = tmp_path / "state.json"

    fake_session = FakeSession()
    fake_session.add_response(
        "https://api.github.com/repos/owner/repo/releases",
        FakeResponse(403, headers={"X-RateLimit-Reset": "1700000000"}),
    )
    monkeypatch.setattr(ingest.requests, "Session", lambda: fake_session)

    github_secret = "ghp_super-secret-token-value"
    feather_secret = "feather-admin-password-secret"
    monkeypatch.setenv("GITHUB_TOKEN", github_secret)
    monkeypatch.setenv("FEATHER_BASE_URL", "http://feather.example")
    monkeypatch.setenv("FEATHER_ADMIN_PASSWORD", feather_secret)

    with caplog.at_level("INFO"):
        exit_code = ingest.main(["--config", str(cfg), "--state", str(state_path), "--apply"])

    assert exit_code == 1
    assert github_secret not in caplog.text
    assert feather_secret not in caplog.text


# ---------------------------------------------------------------------------
# 4. test_github_selects_latest_published_release_with_one_matching_asset
# ---------------------------------------------------------------------------


def test_github_selects_latest_published_release_with_one_matching_asset():
    job = make_job(asset_glob="*.ipa")
    releases = [
        {
            "id": 1,
            "tag_name": "v0.9.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2025-01-01T00:00:00Z",
            "assets": [
                {
                    "id": 10,
                    "name": "App.ipa",
                    "size": 111,
                    "url": "https://api.github.com/repos/owner/repo/releases/assets/10",
                }
            ],
        },
        {
            "id": 2,
            "tag_name": "v1.0.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-01-01T00:00:00Z",
            "assets": [
                {
                    "id": 20,
                    "name": "README.md",
                    "size": 5,
                    "url": "https://api.github.com/repos/owner/repo/releases/assets/20",
                },
                {
                    "id": 21,
                    "name": "App.ipa",
                    "size": 222,
                    "url": "https://api.github.com/repos/owner/repo/releases/assets/21",
                },
            ],
        },
    ]

    candidates = ingest.github_select_candidate(job, releases)
    assert len(candidates) == 1
    candidate = candidates[0]

    assert candidate.release_id == "2"
    assert candidate.release_tag == "v1.0.0"
    assert candidate.asset_id == "21"
    assert candidate.asset_name == "App.ipa"
    assert candidate.declared_size == 222
    assert candidate.auth_host == "api.github.com"
    assert candidate.platform == "ios"


# ---------------------------------------------------------------------------
# 5. test_github_prerelease_requires_opt_in
# ---------------------------------------------------------------------------


def test_github_prerelease_requires_opt_in():
    prerelease_only = [
        {
            "id": 1,
            "tag_name": "v1.0.0-beta",
            "draft": False,
            "prerelease": True,
            "published_at": "2026-01-01T00:00:00Z",
            "assets": [
                {
                    "id": 10,
                    "name": "App.ipa",
                    "size": 111,
                    "url": "https://api.github.com/repos/owner/repo/releases/assets/10",
                }
            ],
        }
    ]

    job_default = make_job(include_prereleases=False)
    assert ingest.github_select_candidate(job_default, prerelease_only) == []

    job_opt_in = make_job(include_prereleases=True)
    candidates = ingest.github_select_candidate(job_opt_in, prerelease_only)
    assert len(candidates) == 1
    assert candidates[0].release_tag == "v1.0.0-beta"

    # Drafts are excluded regardless of includePrereleases.
    draft_only = [
        {
            "id": 2,
            "tag_name": "v1.0.0",
            "draft": True,
            "prerelease": False,
            "published_at": "2026-02-01T00:00:00Z",
            "assets": [
                {
                    "id": 11,
                    "name": "App.ipa",
                    "size": 50,
                    "url": "https://api.github.com/repos/owner/repo/releases/assets/11",
                }
            ],
        }
    ]
    assert ingest.github_select_candidate(job_opt_in, draft_only) == []


# ---------------------------------------------------------------------------
# 6. test_gitlab_selects_latest_released_asset_link
# ---------------------------------------------------------------------------


def test_gitlab_selects_latest_released_asset_link():
    job = make_job(provider="gitlab", allowed_download_hosts=frozenset({"gitlab.com"}))
    releases = [
        {
            "id": 1,
            "tag_name": "v0.9.0",
            "released_at": "2025-01-01T00:00:00Z",
            "assets": {
                "links": [
                    {
                        "id": 10,
                        "name": "App.ipa",
                        "url": "https://gitlab.com/group/project/-/releases/v0.9.0/downloads/App.ipa",
                    }
                ]
            },
        },
        {
            "id": 2,
            "tag_name": "v1.0.0",
            "released_at": "2026-01-01T00:00:00Z",
            "assets": {
                "links": [
                    {
                        "id": 20,
                        "name": "App.ipa",
                        "url": "https://gitlab.com/group/project/-/releases/v1.0.0/downloads/App.ipa",
                    }
                ]
            },
        },
        {
            # Future release -- excluded regardless of matching assets.
            "id": 3,
            "tag_name": "v2.0.0",
            "released_at": "2099-01-01T00:00:00Z",
            "assets": {
                "links": [
                    {
                        "id": 30,
                        "name": "App.ipa",
                        "url": "https://gitlab.com/group/project/-/releases/v2.0.0/downloads/App.ipa",
                    }
                ]
            },
        },
        {
            # Only a generated source archive -- assets.sources must never
            # be treated as a candidate.
            "id": 4,
            "tag_name": "v3.0.0",
            "released_at": "2026-02-01T00:00:00Z",
            "assets": {"sources": [{"format": "zip", "url": "https://gitlab.com/x.zip"}], "links": []},
        },
    ]
    now = ingest._parse_iso8601("2026-06-01T00:00:00Z")

    candidates = ingest.gitlab_select_candidate(job, releases, now=now)
    assert len(candidates) == 1
    candidate = candidates[0]

    assert candidate.release_id == "2"
    assert candidate.release_tag == "v1.0.0"
    assert candidate.asset_id == "20"
    assert candidate.download_url.endswith("v1.0.0/downloads/App.ipa")
    assert candidate.platform == "ios"


# ---------------------------------------------------------------------------
# 7. test_provider_rejects_zero_or_ambiguous_matching_assets
# ---------------------------------------------------------------------------


def test_provider_rejects_zero_or_ambiguous_matching_assets():
    job = make_job()
    no_match_releases = [
        {
            "id": 1,
            "tag_name": "v1.0.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-01-01T00:00:00Z",
            "assets": [
                {
                    "id": 10,
                    "name": "README.md",
                    "size": 1,
                    "url": "https://api.github.com/repos/owner/repo/releases/assets/10",
                }
            ],
        }
    ]
    assert ingest.github_select_candidate(job, no_match_releases) == []

    ambiguous_releases = [
        {
            "id": 2,
            "tag_name": "v1.0.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-01-01T00:00:00Z",
            "assets": [
                {
                    "id": 20,
                    "name": "App-arm64.ipa",
                    "size": 1,
                    "url": "https://api.github.com/repos/owner/repo/releases/assets/20",
                },
                {
                    "id": 21,
                    "name": "App-x86.ipa",
                    "size": 1,
                    "url": "https://api.github.com/repos/owner/repo/releases/assets/21",
                },
            ],
        }
    ]
    with pytest.raises(ingest.ProviderError):
        ingest.github_select_candidate(job, ambiguous_releases)

    # Through the full dispatcher, zero eligible matches across every
    # release raises ProviderError rather than silently returning nothing.
    session = FakeSession()
    session.add_response(
        "https://api.github.com/repos/owner/repo/releases",
        FakeResponse(200, json_data=no_match_releases),
    )
    with pytest.raises(ingest.ProviderError):
        ingest.select_candidate(job, session, NO_TOKENS, timeout=30)


# ---------------------------------------------------------------------------
# 7b. Cross-platform selection -- at most one candidate per platform (083)
# ---------------------------------------------------------------------------


def _release_with_assets(asset_specs, release_id=1, tag="v1.0.0"):
    return {
        "id": release_id, "tag_name": tag, "draft": False, "prerelease": False,
        "published_at": "2026-01-01T00:00:00Z",
        "assets": [
            {"id": i, "name": name, "size": 1, "url": f"https://api.github.com/a{i}"}
            for i, name in enumerate(asset_specs, start=1)
        ],
    }


def test_github_selection_yields_one_candidate_per_platform():
    job = make_job(asset_glob="App*", package="org.example.app")
    releases = [_release_with_assets(["App.ipa", "App.apk"])]
    candidates = ingest.github_select_candidate(job, releases)
    assert len(candidates) == 2
    by_platform = {c.platform: c for c in candidates}
    assert by_platform["ios"].asset_name == "App.ipa"
    assert by_platform["android"].asset_name == "App.apk"


def test_github_selection_rejects_two_apks_for_one_platform():
    job = make_job(asset_glob="App*", package="org.example.app")
    releases = [_release_with_assets(["App-arm64.apk", "App-x86.apk"])]
    with pytest.raises(ingest.ProviderError, match="android"):
        ingest.github_select_candidate(job, releases)


def test_github_selection_does_not_filter_on_configured_identity():
    """Selection is by pattern alone. Both identity fields are optional and
    auto-detected from the artifact, so a job with neither must still select
    both assets -- filtering here previously made a blank field match nothing
    and surface as a misleading "no ... asset matching <glob>" error."""
    job = make_job(asset_glob="App*")  # neither bundleIdentifier nor package
    releases = [_release_with_assets(["App.ipa", "App.apk"])]
    candidates = ingest.github_select_candidate(job, releases)
    assert {c.platform for c in candidates} == {"ios", "android"}
    assert {c.asset_name for c in candidates} == {"App.ipa", "App.apk"}


def test_github_selection_finds_an_apk_with_no_package_configured():
    """Regression for the reported failure: an APK-only release with the
    Android Package field left blank selected zero candidates and reported
    'no eligible release had ... asset matching', pointing at the glob."""
    job = make_job(asset_glob="tsuzuku-release.apk")
    releases = [_release_with_assets(["tsuzuku-release.apk"])]
    candidates = ingest.github_select_candidate(job, releases)
    assert len(candidates) == 1
    assert candidates[0].platform == "android"
    assert candidates[0].asset_name == "tsuzuku-release.apk"


def test_github_selection_ignores_checksum_file_alongside_ipa_and_apk():
    job = make_job(asset_glob="*", package="org.example.app")
    releases = [_release_with_assets(["App.ipa", "App.apk", "App.sha256"])]
    candidates = ingest.github_select_candidate(job, releases)
    assert len(candidates) == 2
    names = {c.asset_name for c in candidates}
    assert names == {"App.ipa", "App.apk"}


def test_gitlab_selection_yields_one_candidate_per_platform():
    job = make_job(
        provider="gitlab", asset_glob="App*", package="org.example.app",
        allowed_download_hosts=frozenset({"gitlab.com"}),
    )
    releases = [{
        "id": 1, "tag_name": "v1.0.0", "released_at": "2026-01-01T00:00:00Z",
        "assets": {"links": [
            {"id": 1, "name": "App.ipa", "url": "https://gitlab.com/dl/App.ipa"},
            {"id": 2, "name": "App.apk", "url": "https://gitlab.com/dl/App.apk"},
        ]},
    }]
    now = ingest._parse_iso8601("2026-06-01T00:00:00Z")
    candidates = ingest.gitlab_select_candidate(job, releases, now=now)
    assert len(candidates) == 2
    assert {c.platform for c in candidates} == {"ios", "android"}


# ---------------------------------------------------------------------------
# 8. test_download_strips_provider_auth_on_cross_host_redirect
# ---------------------------------------------------------------------------


def test_download_strips_provider_auth_on_cross_host_redirect():
    job = make_job(allowed_download_hosts=frozenset({"gitlab.com", "assets.example.com"}))
    ipa_bytes = build_ipa_bytes()

    # -- GitHub: api.github.com redirects to a *.githubusercontent.com host --
    gh_candidate = make_candidate(
        provider="github",
        download_url="https://api.github.com/repos/owner/repo/releases/assets/9001",
        auth_host="api.github.com",
    )
    gh_session = FakeSession()
    gh_session.add_response(
        gh_candidate.download_url,
        FakeResponse(302, headers={"Location": "https://release-assets.githubusercontent.com/x"}),
    )
    gh_session.add_response(
        "https://release-assets.githubusercontent.com/x",
        FakeResponse(200, content_chunks=[ipa_bytes]),
    )

    dest = tempfile.mktemp()
    try:
        ingest.stream_download(
            gh_session,
            gh_candidate,
            job,
            dest,
            {"github": "secret-gh-token", "gitlab": None},
            timeout=30,
            max_bytes=10_000_000,
        )
    finally:
        if os.path.exists(dest):
            os.remove(dest)

    first_call, second_call = gh_session.calls[0], gh_session.calls[1]
    assert "Authorization" in first_call[1]["headers"]
    assert "Authorization" not in second_call[1]["headers"]

    # -- GitLab: gitlab.com redirects to an allowed external asset host --
    gl_candidate = make_candidate(
        provider="gitlab",
        download_url="https://gitlab.com/group/project/-/releases/v1.0.0/downloads/App.ipa",
        auth_host="gitlab.com",
    )
    gl_session = FakeSession()
    gl_session.add_response(
        gl_candidate.download_url,
        FakeResponse(302, headers={"Location": "https://assets.example.com/App.ipa"}),
    )
    gl_session.add_response(
        "https://assets.example.com/App.ipa",
        FakeResponse(200, content_chunks=[ipa_bytes]),
    )

    dest2 = tempfile.mktemp()
    try:
        ingest.stream_download(
            gl_session,
            gl_candidate,
            job,
            dest2,
            {"github": None, "gitlab": "secret-gl-token"},
            timeout=30,
            max_bytes=10_000_000,
        )
    finally:
        if os.path.exists(dest2):
            os.remove(dest2)

    gl_first, gl_second = gl_session.calls[0], gl_session.calls[1]
    assert "PRIVATE-TOKEN" in gl_first[1]["headers"]
    assert "PRIVATE-TOKEN" not in gl_second[1]["headers"]


# ---------------------------------------------------------------------------
# 9. test_dry_run_never_downloads_logs_in_or_writes_state
# ---------------------------------------------------------------------------


def test_dry_run_never_downloads_logs_in_or_writes_state(tmp_path, monkeypatch):
    job = make_job(create_if_missing=False)
    ipa_bytes = build_ipa_bytes()
    session = make_github_session(ipa_bytes)
    feather = FakeFeatherClient(
        apps={"com.example.app": {"bundleIdentifier": "com.example.app", "versions": [{"version": "0.9.0"}]}}
    )
    state_path = str(tmp_path / "state.json")
    state = ingest.load_state(state_path)
    summary = ingest.Summary()

    def boom(*a, **k):
        raise AssertionError("stream_download must not be called in dry-run")

    monkeypatch.setattr(ingest, "stream_download", boom)

    ok = ingest.process_job(job, session, NO_TOKENS, feather, state, False, 30, 1_000_000, summary)

    assert ok is True
    assert summary.would_publish == 1
    assert summary.downloaded == 0
    assert feather.add_version_calls == []
    assert feather.add_app_calls == []
    assert feather.login_calls == 0
    assert not os.path.exists(state_path)
    assert state["jobs"] == {}


# ---------------------------------------------------------------------------
# 10. test_same_release_and_catalog_version_skips_without_download
# ---------------------------------------------------------------------------


def test_same_release_and_catalog_version_skips_without_download(tmp_path, monkeypatch):
    job = make_job()
    ipa_bytes = build_ipa_bytes(version="1.0.0")
    session = make_github_session(ipa_bytes)
    feather = FakeFeatherClient(
        apps={"com.example.app": {"bundleIdentifier": "com.example.app", "versions": [{"version": "1.0.0"}]}}
    )
    state = {
        "schemaVersion": 1,
        "jobs": {
            job.id: {
                "releaseId": str(DEFAULT_RELEASE_ID),
                "assetId": str(DEFAULT_ASSET_ID),
                "version": "1.0.0",
            }
        },
    }
    summary = ingest.Summary()

    def boom(*a, **k):
        raise AssertionError("must not download when state+catalog already match")

    monkeypatch.setattr(ingest, "stream_download", boom)

    ok = ingest.process_job(job, session, NO_TOKENS, feather, state, True, 30, 1_000_000, summary)

    assert ok is True
    assert summary.skipped == 1
    assert summary.downloaded == 0
    assert feather.add_version_calls == []


# ---------------------------------------------------------------------------
# 11. test_download_streams_and_enforces_declared_and_configured_sizes
# ---------------------------------------------------------------------------


def test_download_streams_and_enforces_declared_and_configured_sizes():
    job = make_job()

    # Declared size exceeds the configured maximum -- rejected before any
    # byte is written.
    candidate = make_candidate(declared_size=1000)
    session = FakeSession()
    session.add_response(candidate.download_url, FakeResponse(200, content_chunks=[b"x" * 20]))
    dest = tempfile.mktemp()
    with pytest.raises(ingest.ValidationError):
        ingest.stream_download(session, candidate, job, dest, NO_TOKENS, timeout=30, max_bytes=10)
    assert not os.path.exists(dest)

    # No declared size, but the actual stream exceeds the configured max.
    candidate2 = make_candidate(
        declared_size=None,
        download_url="https://api.github.com/repos/owner/repo/releases/assets/2",
    )
    session2 = FakeSession()
    session2.add_response(
        candidate2.download_url, FakeResponse(200, content_chunks=[b"a" * 5, b"b" * 5, b"c" * 5])
    )
    dest2 = tempfile.mktemp()
    try:
        with pytest.raises(ingest.ValidationError):
            ingest.stream_download(session2, candidate2, job, dest2, NO_TOKENS, timeout=30, max_bytes=8)
    finally:
        if os.path.exists(dest2):
            os.remove(dest2)

    # Declared size present but the actual bytes differ once streaming
    # completes.
    candidate3 = make_candidate(
        declared_size=999,
        download_url="https://api.github.com/repos/owner/repo/releases/assets/3",
    )
    session3 = FakeSession()
    session3.add_response(candidate3.download_url, FakeResponse(200, content_chunks=[b"z" * 10]))
    dest3 = tempfile.mktemp()
    try:
        with pytest.raises(ingest.ValidationError):
            ingest.stream_download(
                session3, candidate3, job, dest3, NO_TOKENS, timeout=30, max_bytes=1_000_000
            )
    finally:
        if os.path.exists(dest3):
            os.remove(dest3)


# ---------------------------------------------------------------------------
# 12. test_validate_rejects_non_ipa_and_missing_payload
# ---------------------------------------------------------------------------


def test_validate_rejects_non_ipa_and_missing_payload(tmp_path):
    job = make_job()

    non_ipa = tmp_path / "notes.txt"
    non_ipa.write_bytes(b"hello")
    with pytest.raises(ingest.ValidationError):
        ingest.validate_and_extract_metadata(str(non_ipa), "notes.txt", job)

    not_a_zip = tmp_path / "fake.ipa"
    not_a_zip.write_bytes(b"not a real zip")
    with pytest.raises(ingest.ValidationError):
        ingest.validate_and_extract_metadata(str(not_a_zip), "fake.ipa", job)

    no_payload = tmp_path / "no-payload.ipa"
    with zipfile.ZipFile(no_payload, "w") as zf:
        zf.writestr("readme.txt", "not an ipa")
    with pytest.raises(ingest.ValidationError):
        ingest.validate_and_extract_metadata(str(no_payload), "no-payload.ipa", job)

    no_plist = tmp_path / "no-plist.ipa"
    with zipfile.ZipFile(no_plist, "w") as zf:
        zf.writestr("Payload/App.app/notaplist.txt", "nope")
    with pytest.raises(ingest.ValidationError):
        ingest.validate_and_extract_metadata(str(no_plist), "no-plist.ipa", job)


# ---------------------------------------------------------------------------
# 12b. inspect_apk_metadata (plan 083)
# ---------------------------------------------------------------------------


def _patch_pyaxmlparser_apk(monkeypatch, **fields):
    """Stand in for pyaxmlparser.APK, mirroring tests/test_apk_inspection.py's
    _FakeApk -- a real binary AndroidManifest cannot be authored by hand."""
    import pyaxmlparser

    class _FakeApk:
        def __init__(self, path):
            self.package = fields.get("package", "org.example.app")
            self.version_code = fields.get("version_code", "1")
            self.version_name = fields.get("version_name", "1.0")

        def is_valid_APK(self):
            return True

        def get_min_sdk_version(self):
            return 21

        def get_target_sdk_version(self):
            return 34

        def get_app_name(self):
            return "Example"

    monkeypatch.setattr(pyaxmlparser, "APK", _FakeApk)


def test_inspect_apk_metadata_rejects_non_apk_filename(tmp_path):
    job = make_job(bundle_identifier=None, package="org.example.app")
    path = tmp_path / "notes.txt"
    path.write_bytes(b"hello")
    with pytest.raises(ingest.ValidationError, match="does not end in .apk"):
        ingest.inspect_apk_metadata(str(path), "notes.txt", job)


def test_inspect_apk_metadata_rejects_package_mismatch(tmp_path, monkeypatch):
    job = make_job(bundle_identifier=None, package="org.example.app")
    path = tmp_path / "App.apk"
    path.write_bytes(b"fake-apk-bytes")
    _patch_pyaxmlparser_apk(monkeypatch, package="org.other.app")
    with pytest.raises(ingest.ValidationError, match="does not match configured package"):
        ingest.inspect_apk_metadata(str(path), "App.apk", job)


def test_inspect_apk_metadata_accepts_happy_path(tmp_path, monkeypatch):
    job = make_job(bundle_identifier=None, package="org.example.app")
    path = tmp_path / "App.apk"
    path.write_bytes(b"fake-apk-bytes")
    _patch_pyaxmlparser_apk(
        monkeypatch, package="org.example.app", version_code="7", version_name="1.7"
    )
    inspection = ingest.inspect_apk_metadata(str(path), "App.apk", job)
    assert inspection.package == "org.example.app"
    assert inspection.version_code == 7
    assert inspection.version_name == "1.7"


def test_inspect_apk_metadata_accepts_when_job_has_no_package_configured(tmp_path, monkeypatch):
    """A job may leave `package` unset (auto-detect); the extracted package
    is trusted without a match check in that case."""
    job = make_job(package=None)
    path = tmp_path / "App.apk"
    path.write_bytes(b"fake-apk-bytes")
    _patch_pyaxmlparser_apk(monkeypatch, package="org.example.app")
    inspection = ingest.inspect_apk_metadata(str(path), "App.apk", job)
    assert inspection.package == "org.example.app"


# ---------------------------------------------------------------------------
# 13. test_extract_metadata_uses_only_top_level_app_plist
# ---------------------------------------------------------------------------


def test_extract_metadata_uses_only_top_level_app_plist(tmp_path):
    job = make_job(bundle_identifier="com.example.app")
    path = tmp_path / "App.ipa"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "Payload/App.app/Info.plist",
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "com.example.app",
                    "CFBundleShortVersionString": "1.2.3",
                    "CFBundleDisplayName": "Example",
                },
                fmt=plistlib.FMT_BINARY,
            ),
        )
        # A bundled framework's own Info.plist -- must never be mistaken
        # for the app's.
        zf.writestr(
            "Payload/App.app/Frameworks/Foo.framework/Info.plist",
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "com.example.app.Foo",
                    "CFBundleShortVersionString": "9.9.9",
                },
                fmt=plistlib.FMT_BINARY,
            ),
        )

    bundle_id, version, name = ingest.validate_and_extract_metadata(str(path), "App.ipa", job)

    assert bundle_id == "com.example.app"
    assert version == "1.2.3"
    assert name == "Example"


# ---------------------------------------------------------------------------
# 14. test_bundle_mismatch_fails_before_feather_login
# ---------------------------------------------------------------------------


def test_bundle_mismatch_fails_before_feather_login(tmp_path):
    job = make_job(bundle_identifier="com.example.app")
    ipa_bytes = build_ipa_bytes(bundle_id="com.other.app", version="1.0.0")
    session = make_github_session(ipa_bytes)
    feather = FakeFeatherClient(
        apps={"com.example.app": {"bundleIdentifier": "com.example.app", "versions": []}}
    )
    state = ingest.load_state(str(tmp_path / "state.json"))
    summary = ingest.Summary()

    ok = ingest.process_job(job, session, NO_TOKENS, feather, state, True, 30, 1_000_000, summary)

    assert ok is False
    assert summary.failed == 1
    assert feather.add_version_calls == []
    assert feather.add_app_calls == []
    assert feather.login_calls == 0
    assert job.id not in state["jobs"]


# ---------------------------------------------------------------------------
# 14b. FeatherClient.add_apk (plan 083)
# ---------------------------------------------------------------------------


def test_feather_client_add_apk_reports_added_and_message(tmp_path):
    apk_path = tmp_path / "App.apk"
    apk_path.write_bytes(b"fake-apk-bytes")

    session = FakeSession()
    session.add_response(
        "https://feather.example/api/login", FakeResponse(200, json_data={"success": True})
    )
    session.add_response(
        "https://feather.example/api/android/add-apk",
        FakeResponse(200, json_data={
            "success": True, "added": True, "message": "Added org.example.app version 7",
            "package": "org.example.app", "versionCode": 7,
        }),
    )

    feather = ingest.FeatherClient(session, "https://feather.example", "secret")
    ok, message, added = feather.add_apk(str(apk_path), package="org.example.app")

    assert ok is True
    assert added is True
    assert "Added" in message
    # Multipart file + package form field were sent on the add-apk call.
    post_url, post_kwargs = session.calls[-1]
    assert post_url == "https://feather.example/api/android/add-apk"
    assert post_kwargs["data"]["package"] == "org.example.app"
    assert post_kwargs["files"]["apkFile"][0] == "App.apk"


def test_feather_client_add_apk_reports_already_present_as_not_added(tmp_path):
    apk_path = tmp_path / "App.apk"
    apk_path.write_bytes(b"fake-apk-bytes")

    session = FakeSession()
    session.add_response(
        "https://feather.example/api/login", FakeResponse(200, json_data={"success": True})
    )
    session.add_response(
        "https://feather.example/api/android/add-apk",
        FakeResponse(200, json_data={
            "success": True, "added": False,
            "message": "Already present: org.example.app versionCode 7",
        }),
    )

    feather = ingest.FeatherClient(session, "https://feather.example", "secret")
    ok, message, added = feather.add_apk(str(apk_path))

    assert ok is True
    assert added is False
    assert "Already present" in message


def test_feather_client_add_apk_raises_on_401(tmp_path):
    apk_path = tmp_path / "App.apk"
    apk_path.write_bytes(b"fake-apk-bytes")

    session = FakeSession()
    session.add_response(
        "https://feather.example/api/login", FakeResponse(200, json_data={"success": True})
    )
    session.add_response(
        "https://feather.example/api/android/add-apk", FakeResponse(401)
    )
    feather = ingest.FeatherClient(session, "https://feather.example", "secret")
    with pytest.raises(ingest.FeatherAuthError):
        feather.add_apk(str(apk_path))


# ---------------------------------------------------------------------------
# 15. test_existing_app_publishes_multipart_add_version
# ---------------------------------------------------------------------------


def test_existing_app_publishes_multipart_add_version(tmp_path):
    job = make_job()
    ipa_bytes = build_ipa_bytes(version="1.0.0")
    session = make_github_session(ipa_bytes)
    feather = FakeFeatherClient(
        apps={"com.example.app": {"bundleIdentifier": "com.example.app", "versions": [{"version": "0.9.0"}]}}
    )
    state = ingest.load_state(str(tmp_path / "state.json"))
    summary = ingest.Summary()

    ok = ingest.process_job(job, session, NO_TOKENS, feather, state, True, 30, 1_000_000, summary)

    assert ok is True
    assert summary.published == 1
    assert len(feather.add_version_calls) == 1
    bundle_id, version, content = feather.add_version_calls[0]
    assert bundle_id == "com.example.app"
    assert version == "1.0.0"
    assert content == ipa_bytes
    assert feather.add_app_calls == []
    # State is keyed per platform (plan 083) so an Android publish can never
    # mask a pending iOS one, or vice versa.
    assert state["jobs"][job.id]["ios"]["version"] == "1.0.0"
    assert state["jobs"][job.id]["ios"]["releaseId"] == str(DEFAULT_RELEASE_ID)
    assert state["jobs"][job.id]["ios"]["assetId"] == str(DEFAULT_ASSET_ID)


# ---------------------------------------------------------------------------
# 16. test_missing_app_creation_requires_explicit_config
# ---------------------------------------------------------------------------


def test_missing_app_creation_requires_explicit_config(tmp_path, monkeypatch):
    job_no_create = make_job(create_if_missing=False)
    session_a = make_github_session(build_ipa_bytes())
    feather_a = FakeFeatherClient(apps={})
    state_a = ingest.load_state(str(tmp_path / "state-a.json"))
    summary_a = ingest.Summary()

    with monkeypatch.context() as mp:
        def boom(*a, **k):
            raise AssertionError("must not download when createIfMissing is false")

        mp.setattr(ingest, "stream_download", boom)
        ok = ingest.process_job(
            job_no_create, session_a, NO_TOKENS, feather_a, state_a, True, 30, 1_000_000, summary_a
        )

    assert ok is False
    assert summary_a.failed == 1
    assert feather_a.add_app_calls == []

    job_create = make_job(create_if_missing=True, name="Example App", developer_name="Example Dev")
    session_b = make_github_session(build_ipa_bytes())
    feather_b = FakeFeatherClient(apps={})
    state_b = ingest.load_state(str(tmp_path / "state-b.json"))
    summary_b = ingest.Summary()

    ok_b = ingest.process_job(
        job_create, session_b, NO_TOKENS, feather_b, state_b, True, 30, 1_000_000, summary_b
    )
    assert ok_b is True
    assert summary_b.created == 1
    assert len(feather_b.add_app_calls) == 1


# ---------------------------------------------------------------------------
# 17. test_create_fallback_triggers_only_on_exact_app_not_found
# ---------------------------------------------------------------------------


def test_create_fallback_triggers_only_on_exact_app_not_found(tmp_path):
    job = make_job(create_if_missing=True, name="Example App", developer_name="Example Dev")

    # Case A: add_version fails with exactly "App not found" -> fallback creates.
    session_a = make_github_session(build_ipa_bytes())
    feather_a = FakeFeatherClient(
        apps={"com.example.app": {"bundleIdentifier": "com.example.app", "versions": []}},
        add_version_result=(False, "App not found"),
        add_app_result=(True, "created"),
    )
    state_a = ingest.load_state(str(tmp_path / "state-a.json"))
    summary_a = ingest.Summary()
    ok_a = ingest.process_job(job, session_a, NO_TOKENS, feather_a, state_a, True, 30, 1_000_000, summary_a)
    assert ok_a is True
    assert summary_a.created == 1
    assert len(feather_a.add_app_calls) == 1
    assert len(feather_a.add_version_calls) == 1

    # Case B: add_version fails with a different error -> no fallback.
    session_b = make_github_session(build_ipa_bytes())
    feather_b = FakeFeatherClient(
        apps={"com.example.app": {"bundleIdentifier": "com.example.app", "versions": []}},
        add_version_result=(False, "Failed to save source data"),
    )
    state_b = ingest.load_state(str(tmp_path / "state-b.json"))
    summary_b = ingest.Summary()
    ok_b = ingest.process_job(job, session_b, NO_TOKENS, feather_b, state_b, True, 30, 1_000_000, summary_b)
    assert ok_b is False
    assert summary_b.failed == 1
    assert feather_b.add_app_calls == []


# ---------------------------------------------------------------------------
# 18. test_state_advances_only_after_verified_publish
# ---------------------------------------------------------------------------


def test_state_advances_only_after_verified_publish(tmp_path):
    job = make_job()

    session_fail = make_github_session(build_ipa_bytes(version="1.0.0"))
    feather_fail = FakeFeatherClient(
        apps={"com.example.app": {"bundleIdentifier": "com.example.app", "versions": []}},
        add_version_result=(False, "boom"),
    )
    state_fail = ingest.load_state(str(tmp_path / "state-fail.json"))
    summary_fail = ingest.Summary()
    ok = ingest.process_job(
        job, session_fail, NO_TOKENS, feather_fail, state_fail, True, 30, 1_000_000, summary_fail
    )
    assert ok is False
    assert job.id not in state_fail["jobs"]

    session_ok = make_github_session(build_ipa_bytes(version="1.0.0"))
    feather_ok = FakeFeatherClient(
        apps={"com.example.app": {"bundleIdentifier": "com.example.app", "versions": []}}
    )
    state_ok = ingest.load_state(str(tmp_path / "state-ok.json"))
    summary_ok = ingest.Summary()
    ok2 = ingest.process_job(
        job, session_ok, NO_TOKENS, feather_ok, state_ok, True, 30, 1_000_000, summary_ok
    )
    assert ok2 is True
    assert state_ok["jobs"][job.id]["ios"]["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# 19. test_second_run_is_idempotent_after_state_or_catalog_recovery
# ---------------------------------------------------------------------------


def test_second_run_is_idempotent_after_state_or_catalog_recovery(tmp_path):
    job = make_job()
    feather = FakeFeatherClient(
        apps={"com.example.app": {"bundleIdentifier": "com.example.app", "versions": []}}
    )
    state = ingest.load_state(str(tmp_path / "state.json"))
    summary = ingest.Summary()

    session1 = make_github_session(build_ipa_bytes(version="1.0.0"))
    ok1 = ingest.process_job(job, session1, NO_TOKENS, feather, state, True, 30, 1_000_000, summary)
    assert ok1 is True
    assert summary.published == 1
    assert len(feather.add_version_calls) == 1

    # Second run, same state, same release/asset -> skips without a
    # second upload.
    session2 = make_github_session(build_ipa_bytes(version="1.0.0"))
    ok2 = ingest.process_job(job, session2, NO_TOKENS, feather, state, True, 30, 1_000_000, summary)
    assert ok2 is True
    assert summary.skipped == 1
    assert len(feather.add_version_calls) == 1

    # Lost state (as if the state file were deleted) but the catalog still
    # has the version -- reconciled via the catalog, never a duplicate
    # upload.
    fresh_state = {"schemaVersion": 1, "jobs": {}}
    session3 = make_github_session(build_ipa_bytes(version="1.0.0"))
    ok3 = ingest.process_job(job, session3, NO_TOKENS, feather, fresh_state, True, 30, 1_000_000, summary)
    assert ok3 is True
    assert len(feather.add_version_calls) == 1
    assert fresh_state["jobs"][job.id]["ios"]["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# 20. test_overlapping_run_exits_without_publishing
# ---------------------------------------------------------------------------


def test_overlapping_run_exits_without_publishing(tmp_path, monkeypatch, capsys):
    state_path = str(tmp_path / "state.json")
    lock1 = ingest.acquire_apply_lock(state_path)
    assert lock1 is not None
    try:
        cfg = tmp_path / "sources.json"
        cfg.write_text(json.dumps({"schemaVersion": 1, "jobs": [_valid_github_job()]}))
        monkeypatch.setenv("FEATHER_BASE_URL", "http://feather.example")
        monkeypatch.setenv("FEATHER_ADMIN_PASSWORD", "test-password-not-a-real-secret")

        def boom(*a, **k):
            raise AssertionError("must not touch the network when the lock is held")

        monkeypatch.setattr(ingest.requests, "Session", boom)

        exit_code = ingest.main(["--config", str(cfg), "--state", state_path, "--apply"])
        captured = capsys.readouterr()

        assert exit_code == 0
        assert "already running; skipped" in captured.out
    finally:
        fcntl.flock(lock1, fcntl.LOCK_UN)
        lock1.close()


# ---------------------------------------------------------------------------
# 21. test_one_job_failure_does_not_block_remaining_jobs_and_sets_exit_one
# ---------------------------------------------------------------------------


def test_one_job_failure_does_not_block_remaining_jobs_and_sets_exit_one(tmp_path, monkeypatch):
    cfg = tmp_path / "sources.json"
    cfg.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "jobs": [
                    {
                        "id": "job-a",
                        "provider": "github",
                        "project": "owner/repoA",
                        "bundleIdentifier": "com.example.a",
                        "assetGlob": "*.ipa",
                    },
                    {
                        "id": "job-b",
                        "provider": "gitlab",
                        "project": "group/projB",
                        "bundleIdentifier": "com.example.b",
                        "assetGlob": "*.ipa",
                        "allowedDownloadHosts": ["gitlab.com"],
                        "createIfMissing": True,
                        "name": "B",
                        "developerName": "Dev",
                    },
                ],
            }
        )
    )
    state_path = tmp_path / "state.json"

    session = FakeSession()
    session.add_response(
        "https://api.github.com/repos/owner/repoA/releases",
        FakeResponse(500),
    )
    gl_project_encoded = quote("group/projB", safe="")
    session.add_response(
        f"https://gitlab.com/api/v4/projects/{gl_project_encoded}/releases",
        FakeResponse(
            200,
            json_data=[
                {
                    "id": 1,
                    "tag_name": "v1.0.0",
                    "released_at": "2026-01-01T00:00:00Z",
                    "assets": {
                        "links": [
                            {
                                "id": 5,
                                "name": "App.ipa",
                                "url": "https://gitlab.com/group/projB/-/releases/v1.0.0/downloads/App.ipa",
                            }
                        ]
                    },
                }
            ],
        ),
    )
    session.add_response(
        "http://feather.example/api/app/com.example.b",
        FakeResponse(404),
    )

    monkeypatch.setattr(ingest.requests, "Session", lambda: session)
    monkeypatch.setenv("FEATHER_BASE_URL", "http://feather.example")

    exit_code = ingest.main(["--config", str(cfg), "--state", str(state_path)])

    assert exit_code == 1


# ---------------------------------------------------------------------------
# 22. process_job -- Android candidate path (plan 083)
# ---------------------------------------------------------------------------


def _make_apk_job(**overrides):
    overrides.setdefault("bundle_identifier", None)
    overrides.setdefault("package", "org.example.app")
    overrides.setdefault("asset_glob", "*.apk")
    return make_job(**overrides)


def test_android_candidate_publishes_and_advances_state(tmp_path, monkeypatch):
    job = _make_apk_job()
    session = make_github_session(b"fake-apk-bytes", asset_name="App.apk")
    _patch_pyaxmlparser_apk(
        monkeypatch, package="org.example.app", version_code="7", version_name="1.7"
    )
    feather = FakeFeatherClient(add_apk_result=(True, "Added org.example.app version 7", True))
    state = ingest.load_state(str(tmp_path / "state.json"))
    summary = ingest.Summary()

    ok = ingest.process_job(job, session, NO_TOKENS, feather, state, True, 30, 1_000_000, summary)

    assert ok is True
    assert summary.published == 1
    assert summary.failed == 0
    assert len(feather.add_apk_calls) == 1
    _, package_sent, content = feather.add_apk_calls[0]
    assert package_sent == "org.example.app"
    assert content == b"fake-apk-bytes"
    # Android never reads the iOS catalog.
    assert feather.get_app_calls == []
    assert state["jobs"][job.id]["android"]["version"] == "1.7"
    assert state["jobs"][job.id]["android"]["versionCode"] == 7
    assert state["jobs"][job.id]["android"]["package"] == "org.example.app"
    assert "ios" not in state["jobs"][job.id]


def test_android_candidate_second_run_reports_skip_not_failure(tmp_path, monkeypatch):
    job = _make_apk_job()
    _patch_pyaxmlparser_apk(
        monkeypatch, package="org.example.app", version_code="7", version_name="1.7"
    )
    state = ingest.load_state(str(tmp_path / "state.json"))
    summary = ingest.Summary()
    feather = FakeFeatherClient(add_apk_result=(True, "Added org.example.app version 7", True))

    session1 = make_github_session(b"fake-apk-bytes", asset_name="App.apk")
    ok1 = ingest.process_job(job, session1, NO_TOKENS, feather, state, True, 30, 1_000_000, summary)
    assert ok1 is True
    assert summary.published == 1

    # Second run -- the endpoint's own idempotency reports added: false.
    # This must count as a skip, not a failure.
    feather.add_apk_result = (True, "Already present: org.example.app versionCode 7", False)
    session2 = make_github_session(b"fake-apk-bytes", asset_name="App.apk")
    ok2 = ingest.process_job(job, session2, NO_TOKENS, feather, state, True, 30, 1_000_000, summary)

    assert ok2 is True
    assert summary.skipped == 1
    assert summary.failed == 0
    assert len(feather.add_apk_calls) == 2


def test_job_publishing_both_platforms_from_one_release_records_both(tmp_path, monkeypatch):
    job = make_job(package="org.example.app", asset_glob="App.*")
    _patch_pyaxmlparser_apk(
        monkeypatch, package="org.example.app", version_code="9", version_name="2.0"
    )
    session = make_github_session_dual(
        build_ipa_bytes(bundle_id="com.example.app", version="2.0.0"), b"fake-apk-bytes"
    )
    feather = FakeFeatherClient(
        apps={"com.example.app": {"bundleIdentifier": "com.example.app", "versions": []}},
        add_apk_result=(True, "Added", True),
    )
    state = ingest.load_state(str(tmp_path / "state.json"))
    summary = ingest.Summary()

    ok = ingest.process_job(job, session, NO_TOKENS, feather, state, True, 30, 1_000_000, summary)

    assert ok is True
    assert len(feather.add_version_calls) == 1
    assert len(feather.add_apk_calls) == 1
    assert state["jobs"][job.id]["ios"]["version"] == "2.0.0"
    assert state["jobs"][job.id]["android"]["versionCode"] == 9
    assert state["jobs"][job.id]["android"]["package"] == "org.example.app"


def test_dry_run_reports_each_platform_separately_and_writes_no_state(tmp_path, monkeypatch):
    job = make_job(package="org.example.app", asset_glob="App.*")
    session = make_github_session_dual(build_ipa_bytes(), b"fake-apk-bytes")
    feather = FakeFeatherClient(
        apps={"com.example.app": {"bundleIdentifier": "com.example.app", "versions": []}},
    )
    state = ingest.load_state(str(tmp_path / "state.json"))
    summary = ingest.Summary()

    def boom(*a, **k):
        raise AssertionError("stream_download must not be called in dry-run")

    monkeypatch.setattr(ingest, "stream_download", boom)

    ok = ingest.process_job(job, session, NO_TOKENS, feather, state, False, 30, 1_000_000, summary)

    assert ok is True
    assert summary.would_publish == 2  # one per platform
    assert summary.downloaded == 0
    assert state["jobs"] == {}


def test_old_flat_state_migrates_when_android_platform_added(tmp_path, monkeypatch):
    """A pre-083 state file only ever recorded an iOS publish, flat at the
    job's top level. Adding an Android identity to an existing job must not
    require deleting or hand-editing that old entry -- it is tolerated and
    migrated into the nested shape in place, rather than discarded."""
    job = make_job(package="org.example.app", asset_glob="App.*")
    old_flat_record = {
        "provider": "github", "project": "owner/repo",
        "releaseId": str(DEFAULT_RELEASE_ID), "assetId": "OLD-ASSET-ID",
        "bundleIdentifier": "com.example.app", "version": "0.9.0",
        "sha256": "deadbeef", "publishedAt": "2026-01-01T00:00:00Z",
    }
    state = {"schemaVersion": 1, "jobs": {job.id: dict(old_flat_record)}}
    _patch_pyaxmlparser_apk(
        monkeypatch, package="org.example.app", version_code="3", version_name="1.0"
    )
    session = make_github_session_dual(build_ipa_bytes(version="1.0.0"), b"fake-apk-bytes")
    feather = FakeFeatherClient(
        apps={"com.example.app": {"bundleIdentifier": "com.example.app", "versions": []}},
        add_apk_result=(True, "Added", True),
    )
    summary = ingest.Summary()

    ok = ingest.process_job(job, session, NO_TOKENS, feather, state, True, 30, 1_000_000, summary)

    assert ok is True
    # Old iOS record migrated into the nested shape, then overwritten with
    # this run's fresh publish -- not discarded, not left in the flat shape.
    assert state["jobs"][job.id]["ios"]["version"] == "1.0.0"
    assert state["jobs"][job.id]["android"]["versionCode"] == 3
    # No stray top-level flat keys survive the migration -- the entry is
    # cleanly {"ios": ..., "android": ...}, not a hybrid of both shapes.
    assert set(state["jobs"][job.id].keys()) == {"ios", "android"}
