"""
upload_outputs.py

Uploads a job's output files to MinIO/S3-compatible object storage. Meant to
run as the final step inside a job's pod, after the user's script finishes.

Walks OUTPUTS_DIR recursively and uploads each file found under the object
key "<JOB_ID>/<relative path from OUTPUTS_DIR>", preserving subdirectory
structure.

Environment variables:
    MINIO_ENDPOINT     S3-compatible endpoint URL, e.g. http://localhost:9000
    MINIO_ACCESS_KEY   Access key for the endpoint
    MINIO_SECRET_KEY   Secret key for the endpoint
    MINIO_BUCKET       Bucket to upload into, e.g. job-outputs
    JOB_ID             Used as the object key prefix for this job's files
    OUTPUTS_DIR        Optional. Directory to walk. Defaults to /outputs.

Exit codes:
    0   Success, including the case where there is nothing to upload
    1   Missing required configuration, or at least one file failed to upload
"""

import logging
import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("upload_outputs")

REQUIRED_ENV_VARS = (
    "MINIO_ENDPOINT",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
    "MINIO_BUCKET",
    "JOB_ID",
)


def load_config() -> dict:
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        logger.error("Missing required environment variable(s): %s", ", ".join(missing))
        sys.exit(1)

    return {
        "endpoint": os.environ["MINIO_ENDPOINT"],
        "access_key": os.environ["MINIO_ACCESS_KEY"],
        "secret_key": os.environ["MINIO_SECRET_KEY"],
        "bucket": os.environ["MINIO_BUCKET"],
        "job_id": os.environ["JOB_ID"],
        "outputs_dir": Path(os.environ.get("OUTPUTS_DIR", "/outputs")),
    }


def find_output_files(outputs_dir: Path) -> list:
    """Return a sorted list of all files under outputs_dir, recursively."""
    return sorted(p for p in outputs_dir.rglob("*") if p.is_file())


def build_object_key(job_id: str, outputs_dir: Path, file_path: Path) -> str:
    relative = file_path.relative_to(outputs_dir)
    return f"{job_id}/{relative.as_posix()}"


def upload_files(s3_client, bucket: str, job_id: str, outputs_dir: Path, files: list) -> tuple:
    uploaded = 0
    failed = 0
    for file_path in files:
        key = build_object_key(job_id, outputs_dir, file_path)
        try:
            s3_client.upload_file(str(file_path), bucket, key)
        except (BotoCoreError, ClientError) as e:
            logger.error("FAILED  %s -> s3://%s/%s: %s", file_path, bucket, key, e)
            failed += 1
            continue
        logger.info("uploaded %s -> s3://%s/%s", file_path, bucket, key)
        uploaded += 1
    return uploaded, failed


def main():
    config = load_config()
    outputs_dir = config["outputs_dir"]

    if not outputs_dir.is_dir():
        logger.info("OUTPUTS_DIR %s does not exist, nothing to upload.", outputs_dir)
        sys.exit(0)

    files = find_output_files(outputs_dir)
    if not files:
        logger.info("OUTPUTS_DIR %s is empty, nothing to upload.", outputs_dir)
        sys.exit(0)

    s3_client = boto3.client(
        "s3",
        endpoint_url=config["endpoint"],
        aws_access_key_id=config["access_key"],
        aws_secret_access_key=config["secret_key"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )

    logger.info(
        "Uploading %d file(s) from %s to s3://%s/%s/...",
        len(files), outputs_dir, config["bucket"], config["job_id"],
    )

    uploaded, failed = upload_files(
        s3_client, config["bucket"], config["job_id"], outputs_dir, files
    )

    logger.info("Done: %d uploaded, %d failed (of %d total).", uploaded, failed, len(files))

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()