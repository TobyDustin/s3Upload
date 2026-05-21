import argparse
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

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
        return boto3.client("s3", **kwargs)
    except NoCredentialsError:
        sys.exit("ERROR: AWS credentials not found. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.")


# ---------------------------------------------------------------------------
# path helpers
# ---------------------------------------------------------------------------

def local_to_s3_key(local_path: Path, base_dir: Path, prefix: str) -> str:
    relative = local_path.relative_to(base_dir.parent)
    key = str(relative).replace("\\", "/")
    return f"{prefix}/{key}" if prefix else key


def s3_to_local_path(s3_key: str, prefix: str, download_dir: Path) -> Path:
    key = s3_key
    if prefix and key.startswith(prefix + "/"):
        key = key[len(prefix) + 1:]
    return download_dir.parent / key


def should_skip_file(path: Path, include_hidden: bool) -> bool:
    if path.name in SKIP_NAMES or path.suffix in SKIP_EXTENSIONS:
        return True
    if not include_hidden:
        return path.name.startswith(".") or any(
            part.startswith(".") and part not in (".", "..")
            for part in path.parts
        )
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

def _is_not_found_error(e: ClientError) -> bool:
    return e.response["Error"]["Code"] in ("404", "NoSuchKey")


def object_exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if _is_not_found_error(e):
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
# transfer primitives
# ---------------------------------------------------------------------------

def _transfer_upload(client, bucket: str, local_path: Path, key: str, print_lock: threading.Lock):
    size = local_path.stat().st_size
    with print_lock:
        print(f"UPLOAD  {key}  ({fmt_size(size)})")
    progress = Progress(key.split("/")[-1], size, print_lock)
    client.upload_file(str(local_path), bucket, key, Config=TRANSFER_CONFIG, Callback=progress)


def _transfer_download(client, bucket: str, key: str, local_path: Path, size: int, print_lock: threading.Lock):
    with print_lock:
        print(f"DOWNLOAD  {key}  ({fmt_size(size)})")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    progress = Progress(key.split("/")[-1], size, print_lock) if size > 0 else None
    client.download_file(bucket, key, str(local_path), Config=TRANSFER_CONFIG, Callback=progress)


def _run_transfers(futures: dict, print_lock: threading.Lock) -> tuple[int, int]:
    success, errors = 0, 0
    for future in as_completed(futures):
        key = futures[future]
        try:
            future.result()
            success += 1
        except ClientError as e:
            with print_lock:
                print(f"ERROR  {key}  ({e})")
            errors += 1
    return success, errors


# ---------------------------------------------------------------------------
# dry-run reporting
# ---------------------------------------------------------------------------

def _print_dry_run(items: list[tuple[str, int]], action: str, skipped: int, errors: int):
    for key, size in items:
        print(f"{action} (dry-run)  {key}  ({fmt_size(size)})")
    print(f"\nWould {action.lower()}: {len(items)}  Skipped: {skipped}  Errors: {errors}")


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------

def _collect_uploads(
    client, config: dict, directory: Path, overwrite: bool, include_hidden: bool,
) -> tuple[list[tuple[Path, str, int]], int, int]:
    bucket = config["bucket"]
    prefix = config["prefix"]
    pending, skipped, errors = [], 0, 0

    all_files = list(iter_local_files(directory, include_hidden))
    print(f"Scanning {len(all_files)} file(s)...")

    for local_path in all_files:
        key = local_to_s3_key(local_path, directory, prefix)
        try:
            if not overwrite and object_exists(client, bucket, key):
                print(f"SKIP  {key}")
                skipped += 1
            else:
                pending.append((local_path, key, local_path.stat().st_size))
        except ClientError as e:
            print(f"ERROR  {key}  ({e})")
            errors += 1

    return pending, skipped, errors


def upload_file(client, config: dict, file_path: Path, overwrite: bool, dry_run: bool):
    bucket = config["bucket"]
    prefix = config["prefix"]
    key = f"{prefix}/{file_path.name}" if prefix else file_path.name

    if not dry_run and not overwrite and object_exists(client, bucket, key):
        print(f"SKIP  {key}")
        return

    if dry_run:
        print(f"UPLOAD (dry-run)  {key}  ({fmt_size(file_path.stat().st_size)})")
        return

    _transfer_upload(client, bucket, file_path, key, threading.Lock())


def upload_directory(
    client, config: dict, directory: Path,
    overwrite: bool, dry_run: bool, include_hidden: bool, workers: int,
):
    pending, skipped, errors = _collect_uploads(client, config, directory, overwrite, include_hidden)

    if dry_run:
        _print_dry_run([(key, size) for _, key, size in pending], "UPLOAD", skipped, errors)
        return

    print_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_transfer_upload, client, config["bucket"], local_path, key, print_lock): key
            for local_path, key, _ in pending
        }
        uploaded, transfer_errors = _run_transfers(futures, print_lock)

    print(f"\nUploaded: {uploaded}  Skipped: {skipped}  Errors: {errors + transfer_errors}")


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

def _collect_downloads(
    client, config: dict, directory: Path, overwrite: bool,
) -> tuple[list[tuple[str, Path, int]], int, int]:
    bucket = config["bucket"]
    prefix = config["prefix"]
    list_prefix = f"{prefix}/{directory.name}" if prefix else directory.name
    pending, skipped, errors = [], 0, 0

    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            local_path = s3_to_local_path(key, prefix, directory)
            size = obj.get("Size", 0)
            if not overwrite and local_path.exists():
                print(f"SKIP  {local_path}")
                skipped += 1
            else:
                pending.append((key, local_path, size))

    return pending, skipped, errors


def download_file(
    client, config: dict, s3_key: str,
    output: Path | None, overwrite: bool, dry_run: bool,
):
    bucket = config["bucket"]
    prefix = config["prefix"]
    full_key = f"{prefix}/{s3_key}" if prefix and not s3_key.startswith(prefix + "/") else s3_key
    local_path = output if output else Path.cwd() / full_key.split("/")[-1]

    if not dry_run and not overwrite and local_path.exists():
        print(f"SKIP  {local_path}")
        return

    try:
        size = client.head_object(Bucket=bucket, Key=full_key)["ContentLength"]
    except ClientError as e:
        sys.exit(f"ERROR: {full_key}  ({e})")

    if dry_run:
        print(f"DOWNLOAD (dry-run)  {full_key}  ({fmt_size(size)})  ->  {local_path}")
        return

    _transfer_download(client, bucket, full_key, local_path, size, threading.Lock())


def download_directory(
    client, config: dict, directory: Path,
    overwrite: bool, dry_run: bool, include_hidden: bool, workers: int,
):
    pending, skipped, errors = _collect_downloads(client, config, directory, overwrite)

    if dry_run:
        _print_dry_run([(key, size) for key, _, size in pending], "DOWNLOAD", skipped, errors)
        return

    print_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_transfer_download, client, config["bucket"], key, local_path, size, print_lock): key
            for key, local_path, size in pending
        }
        downloaded, transfer_errors = _run_transfers(futures, print_lock)

    print(f"\nDownloaded: {downloaded}  Skipped: {skipped}  Errors: {errors + transfer_errors}")


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
    group.add_argument("--upload", metavar="PATH", help="Local file or directory to upload")
    group.add_argument("--download", metavar="DIR", help="Local directory to download into")
    group.add_argument("--download-file", metavar="S3_KEY", help="Single S3 object key to download")
    parser.add_argument("--output", metavar="PATH", help="Local destination for --download-file (default: cwd/filename)")
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
        path = Path(args.upload).resolve()
        if path.is_file():
            upload_file(client, config, path, args.overwrite, args.dry_run)
        elif path.is_dir():
            upload_directory(client, config, path, args.overwrite, args.dry_run, args.include_hidden, args.workers)
        else:
            sys.exit(f"ERROR: {args.upload} is not a file or directory.")
    elif args.download_file:
        output = Path(args.output).resolve() if args.output else None
        download_file(client, config, args.download_file, output, args.overwrite, args.dry_run)
    else:
        directory = Path(args.download).resolve()
        download_directory(client, config, directory, args.overwrite, args.dry_run, args.include_hidden, args.workers)


if __name__ == "__main__":
    main()
