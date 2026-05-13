import argparse
import os
import sys
import threading

from dotenv import load_dotenv

load_dotenv()
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError, NoCredentialsError

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

SKIP_NAMES = {".DS_Store", "Thumbs.db", "__pycache__"}
SKIP_EXTENSIONS = {".pyc", ".pyo"}

MB = 1024 * 1024
GB = 1024 * MB

# multipart: 16 MB parts, up to 8 concurrent parts per file
TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=16 * MB,
    multipart_chunksize=16 * MB,
    max_concurrency=8,
    use_threads=True,
)

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
    return download_dir.parent / key


def should_skip_file(path: Path, include_hidden: bool) -> bool:
    if path.name in SKIP_NAMES:
        return True
    if path.suffix in SKIP_EXTENSIONS:
        return True
    if not include_hidden and path.name.startswith("."):
        return True
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


def fmt_size(n: int) -> str:
    if n >= GB:
        return f"{n / GB:.1f} GB"
    if n >= MB:
        return f"{n / MB:.0f} MB"
    return f"{n / 1024:.0f} KB"


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
# progress callback
# ---------------------------------------------------------------------------

class Progress:
    def __init__(self, label: str, total: int, print_lock: threading.Lock):
        self._label = label
        self._total = total
        self._seen = 0
        self._lock = threading.Lock()
        self._print_lock = print_lock

    def __call__(self, bytes_transferred: int):
        with self._lock:
            self._seen += bytes_transferred
            pct = self._seen * 100 // self._total if self._total else 0
            done = fmt_size(self._seen)
            total = fmt_size(self._total)
        with self._print_lock:
            print(f"\r  {self._label}  {done}/{total}  ({pct}%)      ", end="", flush=True)
        if self._seen >= self._total:
            with self._print_lock:
                print()


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------

def _upload_one(client, bucket: str, local_path: Path, key: str, print_lock: threading.Lock):
    size = local_path.stat().st_size
    label = key.split("/")[-1]
    with print_lock:
        print(f"UPLOAD  {key}  ({fmt_size(size)})")
    cb = Progress(label, size, print_lock)
    client.upload_file(
        str(local_path), bucket, key,
        Config=TRANSFER_CONFIG,
        Callback=cb,
    )


def upload_directory(
    client, config: dict, directory: Path,
    overwrite: bool, dry_run: bool, include_hidden: bool, workers: int,
):
    bucket = config["bucket"]
    prefix = config["prefix"]
    print_lock = threading.Lock()

    # collect work: check existence first (fast head_object calls)
    to_upload: list[tuple[Path, str]] = []
    skipped = 0
    errors = 0

    all_files = list(iter_local_files(directory, include_hidden))
    print(f"Scanning {len(all_files)} file(s)...")

    for local_path in all_files:
        key = build_s3_key(local_path, directory, prefix)
        try:
            if not dry_run and not overwrite and object_exists(client, bucket, key):
                print(f"SKIP  {key}")
                skipped += 1
            else:
                to_upload.append((local_path, key))
        except Exception as e:
            print(f"ERROR  {key}  ({e})")
            errors += 1

    if dry_run:
        for _, key in to_upload:
            size = next(
                (p.stat().st_size for p, k in [(lp, k2) for lp, k2 in to_upload if k2 == key]),
                0,
            )
            print(f"UPLOAD (dry-run)  {key}")
        print(f"\nWould upload: {len(to_upload)}  Skipped: {skipped}  Errors: {errors}")
        return

    uploaded = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_upload_one, client, bucket, lp, key, print_lock): key
            for lp, key in to_upload
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                future.result()
                uploaded += 1
            except Exception as e:
                with print_lock:
                    print(f"ERROR  {key}  ({e})")
                errors += 1

    print(f"\nUploaded: {uploaded}  Skipped: {skipped}  Errors: {errors}")


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

def _download_one(client, bucket: str, key: str, local_path: Path, size: int, print_lock: threading.Lock):
    label = key.split("/")[-1]
    with print_lock:
        print(f"DOWNLOAD  {key}  ({fmt_size(size)})")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    cb = Progress(label, size, print_lock) if size > 0 else None
    client.download_file(
        bucket, key, str(local_path),
        Config=TRANSFER_CONFIG,
        Callback=cb,
    )


def download_directory(
    client, config: dict, directory: Path,
    overwrite: bool, dry_run: bool, include_hidden: bool, workers: int,
):
    bucket = config["bucket"]
    prefix = config["prefix"]
    print_lock = threading.Lock()

    dir_name = directory.name
    list_prefix = f"{prefix}/{dir_name}" if prefix else dir_name

    to_download: list[tuple[str, Path, int]] = []
    skipped = 0
    errors = 0

    paginator = client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=list_prefix)

    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            local_path = build_local_path(key, prefix, directory)
            size = obj.get("Size", 0)
            try:
                if not dry_run and not overwrite and local_path.exists():
                    print(f"SKIP  {local_path}")
                    skipped += 1
                else:
                    to_download.append((key, local_path, size))
            except Exception as e:
                print(f"ERROR  {key}  ({e})")
                errors += 1

    if dry_run:
        for key, _, size in to_download:
            print(f"DOWNLOAD (dry-run)  {key}  ({fmt_size(size)})")
        print(f"\nWould download: {len(to_download)}  Skipped: {skipped}  Errors: {errors}")
        return

    downloaded = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_download_one, client, bucket, key, lp, size, print_lock): key
            for key, lp, size in to_download
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                future.result()
                downloaded += 1
            except Exception as e:
                with print_lock:
                    print(f"ERROR  {key}  ({e})")
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
    parser.add_argument(
        "--workers", type=int, default=4, metavar="N",
        help="Concurrent file transfers (default: 4)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    config = load_config()
    client = get_s3_client(config)

    if args.upload:
        directory = Path(args.upload).resolve()
        if not directory.is_dir():
            sys.exit(f"ERROR: {args.upload} is not a directory.")
        upload_directory(client, config, directory, args.overwrite, args.dry_run, args.include_hidden, args.workers)
    else:
        directory = Path(args.download).resolve()
        download_directory(client, config, directory, args.overwrite, args.dry_run, args.include_hidden, args.workers)


if __name__ == "__main__":
    main()
