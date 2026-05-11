import argparse
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

SKIP_NAMES = {".DS_Store", "Thumbs.db", "__pycache__"}
SKIP_EXTENSIONS = {".pyc", ".pyo"}


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    bucket = os.environ.get("S3_BUCKET", "").strip()
    if not bucket:
        sys.exit("ERROR: S3_BUCKET environment variable is not set.")
    return {
        "bucket": bucket,
        "prefix": os.environ.get("S3_PREFIX", "").strip().strip("/"),
        "region": os.environ.get("AWS_DEFAULT_REGION", "us-east-1").strip(),
        "endpoint_url": os.environ.get("S3_ENDPOINT_URL", "").strip() or None,
    }


def get_s3_client(config: dict):
    kwargs = {"region_name": config["region"]}
    if config["endpoint_url"]:
        kwargs["endpoint_url"] = config["endpoint_url"]
    try:
        client = boto3.client("s3", **kwargs)
        return client
    except NoCredentialsError:
        sys.exit("ERROR: AWS credentials not found. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.")


# ---------------------------------------------------------------------------
# path helpers
# ---------------------------------------------------------------------------

def build_s3_key(local_path: Path, base_dir: Path, prefix: str) -> str:
    relative = local_path.relative_to(base_dir.parent)
    key = str(relative).replace("\\", "/")
    if prefix:
        key = f"{prefix}/{key}"
    return key


def build_local_path(s3_key: str, prefix: str, download_dir: Path) -> Path:
    key = s3_key
    if prefix and key.startswith(prefix + "/"):
        key = key[len(prefix) + 1:]
    # key starts with the directory name, e.g. "comfymodels/checkpoints/a.pt"
    # download_dir is "./comfymodels" so we go one level up
    return download_dir.parent / key


def should_skip_file(path: Path, include_hidden: bool) -> bool:
    if path.name in SKIP_NAMES:
        return True
    if path.suffix in SKIP_EXTENSIONS:
        return True
    if not include_hidden and path.name.startswith("."):
        return True
    # skip hidden directories in the path
    if not include_hidden:
        for part in path.parts:
            if part.startswith(".") and part not in (".", ".."):
                return True
    return False


def iter_local_files(directory: Path, include_hidden: bool):
    for root, dirs, files in os.walk(directory):
        root_path = Path(root)
        if not include_hidden:
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_NAMES]
        for fname in files:
            fpath = root_path / fname
            if not should_skip_file(fpath, include_hidden):
                yield fpath


# ---------------------------------------------------------------------------
# s3 helpers
# ---------------------------------------------------------------------------

def object_exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------

def upload_directory(client, config: dict, directory: Path, overwrite: bool, dry_run: bool, include_hidden: bool):
    bucket = config["bucket"]
    prefix = config["prefix"]

    uploaded = 0
    skipped = 0
    errors = 0

    for local_path in iter_local_files(directory, include_hidden):
        key = build_s3_key(local_path, directory, prefix)
        try:
            if not overwrite and object_exists(client, bucket, key):
                print(f"SKIP already exists  {key}")
                skipped += 1
                continue
            if dry_run:
                print(f"UPLOAD (dry-run)      {key}")
                uploaded += 1
                continue
            print(f"UPLOAD               {key}")
            client.upload_file(str(local_path), bucket, key)
            uploaded += 1
        except Exception as e:
            print(f"ERROR                {key}  ({e})")
            errors += 1

    print(f"\nUploaded: {uploaded}  Skipped: {skipped}  Errors: {errors}")


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

def download_directory(client, config: dict, directory: Path, overwrite: bool, dry_run: bool, include_hidden: bool):
    bucket = config["bucket"]
    prefix = config["prefix"]

    # build the s3 prefix to list under
    dir_name = directory.name
    list_prefix = f"{prefix}/{dir_name}" if prefix else dir_name

    downloaded = 0
    skipped = 0
    errors = 0

    paginator = client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=list_prefix)

    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # skip "directory" placeholder objects
            if key.endswith("/"):
                continue
            local_path = build_local_path(key, prefix, directory)
            try:
                if not overwrite and local_path.exists():
                    print(f"SKIP already exists  {local_path}")
                    skipped += 1
                    continue
                if dry_run:
                    print(f"DOWNLOAD (dry-run)   {key}")
                    downloaded += 1
                    continue
                print(f"DOWNLOAD             {key}")
                local_path.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(bucket, key, str(local_path))
                downloaded += 1
            except Exception as e:
                print(f"ERROR                {key}  ({e})")
                errors += 1

    print(f"\nDownloaded: {downloaded}  Skipped: {skipped}  Errors: {errors}")


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generic S3 directory upload/download tool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables (required):
  AWS_ACCESS_KEY_ID       AWS access key
  AWS_SECRET_ACCESS_KEY   AWS secret key
  AWS_DEFAULT_REGION      AWS region (default: us-east-1)
  S3_BUCKET               Target S3 bucket name

Optional:
  S3_PREFIX               Prepended to all S3 keys (e.g. "private-assets")
  S3_ENDPOINT_URL         Custom endpoint for S3-compatible stores
        """,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--upload", metavar="DIR", help="Local directory to upload")
    group.add_argument("--download", metavar="DIR", help="Local directory to download into")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without doing it")
    parser.add_argument("--include-hidden", action="store_true", help="Include hidden files and directories")
    return parser.parse_args()


def main():
    args = parse_args()
    config = load_config()
    client = get_s3_client(config)

    if args.upload:
        directory = Path(args.upload).resolve()
        if not directory.is_dir():
            sys.exit(f"ERROR: {args.upload} is not a directory.")
        upload_directory(client, config, directory, args.overwrite, args.dry_run, args.include_hidden)
    else:
        directory = Path(args.download).resolve()
        download_directory(client, config, directory, args.overwrite, args.dry_run, args.include_hidden)


if __name__ == "__main__":
    main()
