#!/usr/bin/env python3
"""Migrate local app icons to Garage.

The default is a read-only dry run.  Pass ``--apply`` to upload objects; local
icons are never removed or changed.
"""
import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def find_icon_files(icon_folder):
    """Return ``(bundle, ext, path, size)`` for allowed icon files."""
    results = []
    invalid = []
    if not os.path.isdir(icon_folder):
        return results, invalid
    for bundle in sorted(os.listdir(icon_folder)):
        bundle_dir = os.path.join(icon_folder, bundle)
        if not os.path.isdir(bundle_dir):
            invalid.append(os.path.join(icon_folder, bundle))
            continue
        for filename in sorted(os.listdir(bundle_dir)):
            path = os.path.join(bundle_dir, filename)
            if not os.path.isfile(path) or not filename.startswith("icon."):
                invalid.append(path)
                continue
            ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
            if filename != f"icon.{ext}" or ext not in app_module.ALLOWED_ICON_EXTENSIONS:
                invalid.append(path)
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                size = None
            results.append((bundle, ext, path, size))
    return results, invalid


def garage_client():
    app_module._require_garage_config()
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=app_module.GARAGE_S3_ENDPOINT,
        region_name=app_module.GARAGE_S3_REGION,
        aws_access_key_id=app_module.GARAGE_S3_ACCESS_KEY_ID,
        aws_secret_access_key=app_module.GARAGE_S3_SECRET_ACCESS_KEY,
    )


def catalog_icon_refs(source_path):
    refs = []
    if not os.path.exists(source_path):
        return refs
    with open(source_path, "r") as f:
        source = json.load(f)
    for entry in source.get("apps", []):
        bundle = entry.get("bundleIdentifier", "")
        url = entry.get("iconURL", "") or ""
        marker = "/icons/"
        if marker not in url:
            continue
        suffix = url.split(marker, 1)[1]
        parts = suffix.split("/")
        if len(parts) != 2 or not parts[1].startswith("icon."):
            refs.append((bundle, "", url))
            continue
        refs.append((bundle, parts[1][len("icon."):], url))
    return refs


def migrate(apply=False):
    files, invalid = find_icon_files(app_module.ICON_FOLDER)
    client = garage_client() if apply else None
    uploaded = skipped_existing = skipped_invalid = failed = 0
    uploaded_bytes = skipped_bytes = failed_bytes = 0

    for path in invalid:
        print(f"skipped-invalid: {path}")
    skipped_invalid = len(invalid)
    skipped_invalid_bytes = sum(
        os.path.getsize(path) for path in invalid if os.path.isfile(path)
    )

    available = {(bundle, ext): (path, size) for bundle, ext, path, size in files}
    for bundle, ext, url in catalog_icon_refs(app_module.SOURCE_FILE):
        if (bundle, ext) in available:
            continue
        if not apply:
            print(f"failed: catalog icon has no local file (or verified Garage object): {url}")
            failed += 1
            continue
        key = app_module.garage_icon_key(bundle, ext)
        try:
            client.head_object(Bucket=app_module.GARAGE_BUCKET, Key=key)
        except Exception:
            print(f"failed: catalog icon has no local or Garage object: {url}")
            failed += 1

    for bundle, ext, path, size in files:
        if size is None:
            print(f"failed: could not stat {path}")
            failed += 1
            continue
        key = app_module.garage_icon_key(bundle, ext)
        if not apply:
            print(f"would-upload: {path} -> {key} ({size} bytes)")
            continue
        try:
            try:
                head = client.head_object(Bucket=app_module.GARAGE_BUCKET, Key=key)
            except Exception:
                head = None
            if head is not None and head.get("ContentLength") == size:
                print(f"skipped-existing: {key} ({size} bytes)")
                skipped_existing += 1
                skipped_bytes += size
                continue
            client.upload_file(
                path,
                app_module.GARAGE_BUCKET,
                key,
                ExtraArgs={"ContentType": app_module.ICON_MIME_TYPES[ext]},
            )
            verified = client.head_object(Bucket=app_module.GARAGE_BUCKET, Key=key)
            if verified.get("ContentLength") != size:
                print(f"failed: size mismatch for {key}")
                failed += 1
                failed_bytes += size
                continue
            print(f"uploaded: {key} ({size} bytes)")
            uploaded += 1
            uploaded_bytes += size
        except Exception as e:
            logging.error("Failed to migrate %s: %s", key, e)
            failed += 1
            failed_bytes += size

    print(
        f"summary: uploaded={uploaded} ({uploaded_bytes} bytes), "
        f"skipped-existing={skipped_existing} ({skipped_bytes} bytes), "
        f"skipped-invalid={skipped_invalid} ({skipped_invalid_bytes} bytes), "
        f"failed={failed} ({failed_bytes} bytes)"
    )
    if not apply:
        print("dry-run: nothing written")
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="upload icons to Garage")
    args = parser.parse_args()
    raise SystemExit(migrate(apply=args.apply))


if __name__ == "__main__":
    main()
