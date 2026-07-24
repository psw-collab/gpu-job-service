"""
ship_logs.py

Continuously ships a job's log file to Google Cloud Storage while the job
runs, so logs survive even if the pod dies ungracefully (OOM, node loss)
before the end-of-job capture happens. Meant to run as a sidecar container in
the job pod.

Mirrors upload_outputs.py: authenticates via Application Default Credentials,
which on GKE resolves through Workload Identity to the pod's service account,
so no static keys are needed.

The main container tees its output to LOG_FILE on a shared volume; this script
re-uploads that file to gs://<GCS_BUCKET>/logs/<JOB_ID>/stdout.log every
LOG_SHIP_INTERVAL seconds, and once more on shutdown (SIGTERM from the kubelet
when the main container exits) so the final lines aren't lost.

Environment variables:
    GCS_BUCKET         Bucket to upload into. Defaults to gpujob-outputs-shared.
    JOB_ID             Used as the object key prefix for this job's logs.
    LOG_FILE           Path of the log file to ship. Defaults to /var/log/job/stdout.log.
    LOG_SHIP_INTERVAL  Seconds between uploads. Defaults to 5.
"""

import os
import signal
import sys
import time
from pathlib import Path

from google.api_core.exceptions import GoogleAPIError
from google.cloud import storage

DEFAULT_GCS_BUCKET = "gpujob-outputs-shared"

BUCKET = os.environ.get("GCS_BUCKET", DEFAULT_GCS_BUCKET)
JOB_ID = os.environ.get("JOB_ID")
LOG_FILE = Path(os.environ.get("LOG_FILE", "/var/log/job/stdout.log"))
INTERVAL = int(os.environ.get("LOG_SHIP_INTERVAL", "5"))
OBJECT_KEY = f"logs/{JOB_ID}/stdout.log"

_stop = False


def _handle_stop(signum, frame):
    global _stop
    _stop = True


def _upload(blob) -> None:
    if not LOG_FILE.exists():
        return
    try:
        blob.upload_from_filename(str(LOG_FILE))
    except GoogleAPIError as e:
        print(f"log ship failed: {e}", file=sys.stderr)
    except OSError as e:
        print(f"log ship read error: {e}", file=sys.stderr)


def main() -> None:
    if not JOB_ID:
        print("ship_logs: JOB_ID not set, nothing to do", file=sys.stderr)
        sys.exit(1)

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    blob = storage.Client().bucket(BUCKET).blob(OBJECT_KEY)
    print(f"ship_logs: shipping {LOG_FILE} -> gs://{BUCKET}/{OBJECT_KEY} every {INTERVAL}s")

    while not _stop:
        _upload(blob)
        for _ in range(INTERVAL):
            if _stop:
                break
            time.sleep(1)

    _upload(blob)  # final flush after the main container exits
    print("ship_logs: exited after final upload")


if __name__ == "__main__":
    main()
