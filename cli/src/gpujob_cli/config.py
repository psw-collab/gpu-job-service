"""
Configuration for the gpujob CLI.

The API base URL can be overridden via the GPUJOB_API_URL environment
variable, e.g.:

    export GPUJOB_API_URL=http://172.23.18.129:8000

If unset, defaults to http://localhost:8000 (matches `uvicorn main:app --reload`
running locally).

GPUJOB_API_KEY is required whenever the API enforces one (e.g. the hosted
demo deployment) -- ask whoever runs the platform for the current value.

Set GPUJOB_USE_GCLOUD_AUTH=1 if the deployment also requires a Google Cloud
identity token (e.g. while the hosted gateway is restricted to an IAM
allowlist rather than public access) -- the CLI will then shell out to
`gcloud auth print-identity-token` and attach it automatically. Requires
`gcloud` to be installed and logged in as an account on that allowlist.
"""

import os
import shutil
import subprocess

DEFAULT_API_URL = "http://localhost:8000"


def get_api_url() -> str:
    """Return the configured API base URL, stripped of any trailing slash."""
    url = os.environ.get("GPUJOB_API_URL", DEFAULT_API_URL)
    return url.rstrip("/")


def get_api_key() -> str | None:
    """Return the configured API key, or None if unset (e.g. local dev)."""
    return os.environ.get("GPUJOB_API_KEY")


def get_identity_token() -> str | None:
    """
    If GPUJOB_USE_GCLOUD_AUTH is set, fetch a fresh Google identity token via
    `gcloud auth print-identity-token`. Returns None if unset, or if gcloud
    is unavailable/fails (the caller will then get a clear 401/403 from the
    API instead of a confusing local error).
    """
    if not os.environ.get("GPUJOB_USE_GCLOUD_AUTH"):
        return None
    gcloud_path = shutil.which("gcloud")
    if not gcloud_path:
        return None
    try:
        result = subprocess.run(
            [gcloud_path, "auth", "print-identity-token"],
            capture_output=True, text=True, timeout=15, check=True,
        )
        return result.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        return None
