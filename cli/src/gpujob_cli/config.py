"""
Configuration for the gpujob CLI.

The API base URL can be overridden via the GPUJOB_API_URL environment
variable, e.g.:

    export GPUJOB_API_URL=http://172.23.18.129:8000

If unset, defaults to http://localhost:8000 (matches `uvicorn main:app --reload`
running locally).

GPUJOB_API_KEY is required whenever the API enforces one (e.g. the hosted
demo deployment) -- ask whoever runs the platform for the current value.
"""

import os

DEFAULT_API_URL = "http://localhost:8000"


def get_api_url() -> str:
    """Return the configured API base URL, stripped of any trailing slash."""
    url = os.environ.get("GPUJOB_API_URL", DEFAULT_API_URL)
    return url.rstrip("/")


def get_api_key() -> str | None:
    """Return the configured API key, or None if unset (e.g. local dev)."""
    return os.environ.get("GPUJOB_API_KEY")
