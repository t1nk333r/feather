#!/usr/bin/env python3
"""Migrate data/ipas/*.ipa to the Garage S3 object store (Plan 011).

Dry-run by default -- prints exactly what would happen and writes nothing.
Pass --apply to actually upload. This script never deletes, moves, or
modifies anything under data/; its only write is to Garage.

Usage:
    .venv/bin/python scripts/migrate_ipas_to_garage.py            # dry run
    .venv/bin/python scripts/migrate_ipas_to_garage.py --apply    # upload

See plans/011-garage-s3-ipa-storage.md, Step 5, for the full design.
"""
import argparse
import json
import logging
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402 -- reuses config + secure_filename so
                           # this script's key layout can never drift from
                           # GarageIpaStorage's.
from werkzeug.utils import secure_filename  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def garage_key(bundle_id, version):
    """Same key layout as GarageIpaStorage._key in app.py -- pinned by
    tests/test_storage.py::test_garage_key_layout. Kept in lockstep so the
    migration writes to exactly the keys serve_ipa will look up.
    """
    prefix = app_module.GARAGE_KEY_PREFIX
    return f"{prefix}/{secure_filename(bundle_id)}/{secure_filename(version)}.ipa"


def require_garage_config():
    """Validated independently of STORAGE_BACKEND -- this script is meant
    to run *before* the app is switched over to Garage (Step 5 happens
    before Step 7), so it cannot rely on app._require_garage_config(),
    which only fires when STORAGE_BACKEND=garage. Only called for --apply;
    a dry run needs no Garage credentials at all, since it only inspects
    local files.
    """
    required = {
        "GARAGE_S3_ENDPOINT": app_module.GARAGE_S3_ENDPOINT,
        "GARAGE_S3_ACCESS_KEY_ID": app_module.GARAGE_S3_ACCESS_KEY_ID,
        "GARAGE_S3_SECRET_ACCESS_KEY": app_module.GARAGE_S3_SECRET_ACCESS_KEY,
        "GARAGE_BUCKET": app_module.GARAGE_BUCKET,
        "GARAGE_PUBLIC_BASE_URL": app_module.GARAGE_PUBLIC_BASE_URL,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        print(
            f"ERROR: --apply requires the following environment variable(s), "
            f"which are not set: {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(1)


def find_ipa_files(ipa_folder):
    """Walk data/ipas/<bundle_id>/<version>.ipa, returning a list of
    (bundle_id, version, path, size) for every *.ipa file found. Read-only
    -- never creates or modifies anything under ipa_folder.
    """
    results = []
    if not os.path.isdir(ipa_folder):
        return results
    for bundle_id in sorted(os.listdir(ipa_folder)):
        bundle_dir = os.path.join(ipa_folder, bundle_id)
        if not os.path.isdir(bundle_dir):
            continue
        for filename in sorted(os.listdir(bundle_dir)):
            if not filename.endswith(".ipa"):
                continue
            path = os.path.join(bundle_dir, filename)
            version = filename[: -len(".ipa")]
            try:
                size = os.path.getsize(path)
            except OSError:
                size = None
            results.append((bundle_id, version, path, size))
    return results


def load_catalog_download_urls(source_json_path):
    """Return the set of (bundle_id, version) pairs that data/source.json
    references via a downloadURL pointing at this app's own /ipas/ route.
    Used only to flag orphans -- files on disk with no catalog entry
    pointing at them. Read-only.
    """
    referenced = set()
    if not os.path.exists(source_json_path):
        return referenced
    with open(source_json_path, "r") as f:
        data = json.load(f)
    for app_entry in data.get("apps", []):
        bundle_id = app_entry.get("bundleIdentifier", "")
        for version_entry in app_entry.get("versions", []):
            url = version_entry.get("downloadURL", "") or ""
            if "/ipas/" in url:
                referenced.add((bundle_id, version_entry.get("version", "")))
    return referenced


def classify(files, referenced):
    """Split found files into (uploadable, skipped_corrupt, orphans).

    uploadable: real ZIP archives (an IPA is a ZIP; zipfile.is_zipfile()
        is the integrity gate -- this is what separates the three known
        gzip-HTML files from the real binaries with no guesswork).
    skipped_corrupt: files that fail the ZIP check. Never migrated.
    orphans: a subset of uploadable -- on disk but not referenced by any
        downloadURL in the catalog. Migrated, but reported separately.
    """
    uploadable = []
    skipped_corrupt = []
    orphans = []
    for bundle_id, version, path, size in files:
        if not zipfile.is_zipfile(path):
            skipped_corrupt.append((bundle_id, version, path, size))
            continue
        uploadable.append((bundle_id, version, path, size))
        if (bundle_id, version) not in referenced:
            orphans.append((bundle_id, version, path, size))
    return uploadable, skipped_corrupt, orphans


def print_plan(files, uploadable, skipped_corrupt, orphans, ipa_folder):
    total_bytes = sum(size or 0 for _, _, _, size in uploadable)
    print(f"Found {len(files)} .ipa file(s) under {ipa_folder}")
    print()
    if skipped_corrupt:
        print(f"Skipped as corrupt (not a valid ZIP) -- {len(skipped_corrupt)}:")
        for bundle_id, version, path, size in skipped_corrupt:
            print(f"  BAD     {size!s:>12} bytes  {path}")
        print()
    if orphans:
        print(f"Orphans (on disk, not referenced by data/source.json) -- {len(orphans)}:")
        for bundle_id, version, path, size in orphans:
            print(f"  ORPHAN  {size!s:>12} bytes  {path}")
        print()
    print(
        f"{len(uploadable)} file(s) to upload (~{total_bytes / 1_000_000_000:.2f} GB), "
        f"{len(skipped_corrupt)} skipped as corrupt, {len(orphans)} flagged as orphans"
    )


def apply_uploads(uploadable):
    """Upload every file in uploadable to Garage. Idempotent (skips a key
    that already exists with the same size) and verifies every upload
    with a post-upload head_object before counting it as a success. A
    ContentLength mismatch is a hard failure for that file only -- the
    rest of the batch keeps going.

    Returns (uploaded, uploaded_bytes, skipped_existing, failed).
    """
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client(
        "s3",
        endpoint_url=app_module.GARAGE_S3_ENDPOINT,
        region_name=app_module.GARAGE_S3_REGION,
        aws_access_key_id=app_module.GARAGE_S3_ACCESS_KEY_ID,
        aws_secret_access_key=app_module.GARAGE_S3_SECRET_ACCESS_KEY,
    )
    bucket = app_module.GARAGE_BUCKET

    uploaded = 0
    uploaded_bytes = 0
    skipped_existing = 0
    failed = 0

    for bundle_id, version, path, size in uploadable:
        key = garage_key(bundle_id, version)

        try:
            head = client.head_object(Bucket=bucket, Key=key)
            if head.get("ContentLength") == size:
                print(f"SKIP (already uploaded, same size)  {key}")
                skipped_existing += 1
                continue
        except ClientError:
            pass  # not there yet -- proceed to upload

        try:
            client.upload_file(
                path, bucket, key,
                ExtraArgs={"ContentType": "application/octet-stream"},
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "unknown")
            print(f"FAILED (upload)  {key}: {code}")
            failed += 1
            continue

        try:
            head = client.head_object(Bucket=bucket, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "unknown")
            print(f"FAILED (post-upload verify)  {key}: {code}")
            failed += 1
            continue

        remote_size = head.get("ContentLength")
        if remote_size != size:
            print(f"FAILED (size mismatch)  {key}: local={size} remote={remote_size}")
            failed += 1
            continue

        print(f"OK  {key}  ({size} bytes)")
        uploaded += 1
        uploaded_bytes += size or 0

    return uploaded, uploaded_bytes, skipped_existing, failed


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually upload to Garage. Without this flag, only prints what would happen and writes nothing.",
    )
    args = parser.parse_args()

    ipa_folder = app_module.IPA_FOLDER
    source_json_path = app_module.SOURCE_FILE

    files = find_ipa_files(ipa_folder)
    referenced = load_catalog_download_urls(source_json_path)
    uploadable, skipped_corrupt, orphans = classify(files, referenced)

    print_plan(files, uploadable, skipped_corrupt, orphans, ipa_folder)

    if not args.apply:
        print()
        print("Dry run only -- nothing written to Garage. Re-run with --apply to upload.")
        return

    require_garage_config()

    print()
    uploaded, uploaded_bytes, skipped_existing, failed = apply_uploads(uploadable)

    print()
    print(
        f"Summary: uploaded={uploaded} ({uploaded_bytes} bytes), "
        f"skipped_existing={skipped_existing}, "
        f"skipped_corrupt={len(skipped_corrupt)}, "
        f"orphans={len(orphans)}, "
        f"failed={failed}"
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
