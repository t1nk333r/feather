#!/usr/bin/env python3
"""Feather MCP server: lets an AI agent publish IPAs/APKs to a Feather
instance and read its catalogue.

Stdio transport, newline-delimited JSON-RPC 2.0, standard library only, so
it runs anywhere Python 3.9+ does -- no pip install on the agent's machine.

    FEATHER_URL=https://feather.example.com \\
    FEATHER_TOKEN=ftr_... \\
    python3 scripts/feather_mcp.py

Create the token in the Feather admin page (Source tab -> API tokens). A
token can publish and read; it cannot delete apps or manage tokens.

Claude Code:
    claude mcp add feather --env FEATHER_URL=https://feather.example.com \\
        --env FEATHER_TOKEN=ftr_... -- python3 /path/to/feather_mcp.py
"""

import http.client
import json
import mimetypes
import os
import shutil
import ssl
import sys
import tempfile
import urllib.request
import uuid
from urllib.parse import quote, urlparse

SERVER_NAME = "feather"
SERVER_VERSION = "1.3.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
TIMEOUT = float(os.environ.get("FEATHER_TIMEOUT", "600"))

TOOLS = [
    {
        "name": "publish_app",
        "description": (
            "Publish an iOS .ipa or Android .apk to Feather. Give either a local file `path` "
            "or a public `url` Feather should download. Platform, bundle ID / package and "
            "version are read from the file. New apps are created unless create_if_missing "
            "is false; the app details (name, summary, description, licence, links, "
            "categories) apply only when the app is created -- use update_app afterwards. "
            "whats_new is this version's release notes. Publishing a version that already "
            "exists is a no-op (added=false). Check `warnings` in the result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Local path to an .ipa or .apk"},
                "url": {"type": "string", "description": "http(s) URL of an .ipa or .apk"},
                "name": {"type": "string", "description": "Display name (max 50)"},
                "developer_name": {"type": "string", "description": "Developer / author (max 100)"},
                "summary": {"type": "string", "description": "One-line summary (max 80); iOS subtitle"},
                "description": {"type": "string", "description": "Full description (max 4000)"},
                "license": {"type": "string", "description": "SPDX licence, e.g. GPL-3.0-only (Android only)"},
                "website": {"type": "string", "description": "http(s) URL (Android only)"},
                "source_code": {"type": "string", "description": "http(s) URL of the source repo (Android only)"},
                "categories": {"type": "array", "items": {"type": "string"}, "description": "Android only"},
                "whats_new": {"type": "string", "description": "Release notes for this version"},
                "create_if_missing": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "update_app",
        "description": ("Change an existing app's details by bundle ID or package. Only the fields given change. "
                        "To add release notes to an already-published version, pass whats_new with version "
                        "(iOS version string or Android versionCode)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Bundle ID (iOS) or package name (Android)"},
                "whats_new": {"type": "string", "description": "Release notes for `version`"},
                "version": {"type": "string", "description": "Version the notes belong to (iOS version / Android versionCode)"},
                "name": {"type": "string", "description": "Display name (max 50)"},
                "developer_name": {"type": "string", "description": "Developer / author (max 100)"},
                "summary": {"type": "string", "description": "One-line summary (max 80); iOS subtitle"},
                "description": {"type": "string", "description": "Full description (max 4000)"},
                "license": {"type": "string", "description": "SPDX licence, e.g. GPL-3.0-only (Android only)"},
                "website": {"type": "string", "description": "http(s) URL (Android only)"},
                "source_code": {"type": "string", "description": "http(s) URL of the source repo (Android only)"},
                "categories": {"type": "array", "items": {"type": "string"}, "description": "Android only"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "list_apps",
        "description": "List published apps with their versions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["ios", "android", "all"], "default": "all"},
            },
        },
    },
    {
        "name": "get_app",
        "description": "Get one app by iOS bundle identifier or Android package name.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "repo_status",
        "description": ("Server build (server_version = git commit) and Android F-Droid repo status: subscribe URL, "
                        "fingerprint, last index build and rejected APKs."),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class FeatherError(Exception):
    pass


class Feather:
    def __init__(self, base_url, token):
        if not base_url:
            raise FeatherError("FEATHER_URL is not set")
        parsed = urlparse(base_url.rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise FeatherError(f"FEATHER_URL must be an http(s) URL, got {base_url!r}")
        self.parsed = parsed
        self.prefix = parsed.path.rstrip("/")
        self.token = token

    def _conn(self):
        if self.parsed.scheme == "https":
            return http.client.HTTPSConnection(self.parsed.hostname, self.parsed.port,
                                               timeout=TIMEOUT, context=ssl.create_default_context())
        return http.client.HTTPConnection(self.parsed.hostname, self.parsed.port, timeout=TIMEOUT)

    def request(self, method, path, body=None, headers=None, auth=True):
        headers = dict(headers or {})
        if auth:
            if not self.token:
                raise FeatherError("FEATHER_TOKEN is not set (create one in the admin page)")
            headers["Authorization"] = f"Bearer {self.token}"
        conn = self._conn()
        try:
            if callable(body):
                conn.putrequest(method, self.prefix + path)
                for k, v in headers.items():
                    conn.putheader(k, v)
                conn.endheaders()
                body(conn)
            else:
                conn.request(method, self.prefix + path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except OSError as e:
            raise FeatherError(f"Could not reach Feather at {self.parsed.geturl()}: {e}")
        finally:
            conn.close()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {"error": raw[:300].decode("utf-8", "replace")}
        if resp.status >= 400:
            msg = data.get("error") if isinstance(data, dict) else None
            raise FeatherError(f"HTTP {resp.status}: {msg or resp.reason}")
        return data

    def publish_file(self, path, fields):
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                         .encode() + str(value).encode("utf-8") + b"\r\n")
        filename = os.path.basename(path).replace('"', "_")
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                     f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode())
        head = b"".join(parts)
        tail = f"\r\n--{boundary}--\r\n".encode()
        size = os.path.getsize(path)

        def send(conn):
            conn.send(head)
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    conn.send(chunk)
            conn.send(tail)

        return self.request("POST", "/api/publish", body=send, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(head) + size + len(tail)),
        })


_DETAIL_ARGS = (("name", "name"), ("developer_name", "developerName"), ("summary", "summary"),
                ("description", "description"), ("license", "license"), ("website", "website"),
                ("source_code", "sourceCode"))


def _detail_fields(args):
    fields = {field: args[arg] for arg, field in _DETAIL_ARGS if args.get(arg)}
    if args.get("categories"):
        cats = args["categories"]
        fields["categories"] = ",".join(cats) if isinstance(cats, list) else str(cats)
    return fields


def _update(feather, args):
    app_id = (args.get("id") or "").strip()
    if not app_id:
        raise FeatherError("`id` is required")
    body = {"id": app_id, **_detail_fields(args)}
    if args.get("whats_new"):
        body["whatsNew"] = args["whats_new"]
        body["version"] = str(args.get("version") or "")
    return feather.request("POST", "/api/app-details", body=json.dumps(body),
                           headers={"Content-Type": "application/json"})


MAX_DOWNLOAD_BYTES = int(os.environ.get("FEATHER_MAX_DOWNLOAD_BYTES", str(2 * 1024 ** 3)))


def _download(url):
    """Stream `url` to a private temp dir; return the file path. The name
    keeps the URL's file name so the server's extension fallback still works."""
    if urlparse(url).scheme not in ("http", "https"):
        raise FeatherError("url must be an http(s) URL")
    workdir = tempfile.mkdtemp(prefix="feather-mcp-")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"feather-mcp/{SERVER_VERSION}"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            name = os.path.basename(urlparse(resp.geturl()).path) or "download.bin"
            path = os.path.join(workdir, name.replace(os.sep, "_"))
            total = 0
            with open(path, "wb") as out:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise FeatherError(f"Download is larger than {MAX_DOWNLOAD_BYTES} bytes")
                    out.write(chunk)
        return path
    except FeatherError:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except (OSError, ValueError) as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise FeatherError(f"Could not download {url}: {e}")


def _publish(feather, args):
    path, url = args.get("path"), args.get("url")
    if bool(path) == bool(url):
        raise FeatherError("Give exactly one of `path` or `url`")
    fields = _detail_fields(args)
    if args.get("whats_new"):
        fields["whatsNew"] = args["whats_new"]
    if args.get("create_if_missing") is not None:
        flag = args["create_if_missing"]
        if isinstance(flag, str):
            flag = flag.strip().lower() not in ("false", "0", "no", "off", "")
        fields["createIfMissing"] = "true" if flag else "false"
    if url:
        # The server refuses `url` from API tokens (it would let a token make
        # the server fetch internal hosts), so download here and upload.
        tmp = _download(url)
        try:
            return feather.publish_file(tmp, fields)
        finally:
            shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise FeatherError(f"No such file: {path}")
    return feather.publish_file(path, fields)


def _summarise_ios(apps):
    return [{"platform": "ios", "id": a.get("bundleIdentifier"), "name": a.get("name"),
             "developer": a.get("developerName"),
             "versions": [v.get("version") for v in a.get("versions", []) if isinstance(v, dict)]}
            for a in apps if isinstance(a, dict)]


def _list(feather, args):
    platform = args.get("platform") or "all"
    out = []
    if platform in ("ios", "all"):
        out += _summarise_ios(feather.request("GET", "/api/apps", auth=False))
    if platform in ("android", "all"):
        for a in feather.request("GET", "/api/android/apps"):
            out.append({"platform": "android", "id": a.get("package"), "name": a.get("name"),
                        "versions": [{k: v.get(k) for k in ("versionCode", "versionName", "published")}
                                     for v in a.get("versions") or []]})
    return out


def _get(feather, args):
    app_id = (args.get("id") or "").strip()
    if not app_id:
        raise FeatherError("`id` is required")
    try:
        return {"platform": "ios", **feather.request("GET", "/api/app/" + quote(app_id, safe=""), auth=False)}
    except FeatherError as e:
        if not str(e).startswith("HTTP 404"):
            raise
    for a in feather.request("GET", "/api/android/apps"):
        if a.get("package") == app_id:
            return {"platform": "android", **a}
    raise FeatherError(f"No app with id {app_id!r}")


HANDLERS = {
    "publish_app": _publish,
    "update_app": _update,
    "list_apps": _list,
    "get_app": _get,
    "repo_status": lambda feather, _args: {
        "server_version": feather.request("GET", "/api/version", auth=False).get("version"),
        **feather.request("GET", "/api/android/status")},
}


def _call_tool(params):
    name = params.get("name")
    handler = HANDLERS.get(name)
    if handler is None:
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
    try:
        feather = Feather(os.environ.get("FEATHER_URL", ""), os.environ.get("FEATHER_TOKEN", ""))
        result = handler(feather, params.get("arguments") or {})
        is_error = isinstance(result, dict) and result.get("success") is False
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}], "isError": is_error}
    except FeatherError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}


def handle(msg):
    """Return the response dict for one JSON-RPC message, or None for a notification."""
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        result = {
            "protocolVersion": requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "Publish iOS .ipa / Android .apk files to a Feather app source with publish_app.",
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = _call_tool(params)
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            if not isinstance(msg, dict):
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
            else:
                response = handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
