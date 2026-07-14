"""
Thin HTTP client for the gpu-job-service API.

Matches the contract defined in schemas.py / main.py:

    POST /v1/jobs                   JobSubmitRequest  -> JobSubmitResponse
    GET  /v1/jobs/{job_id}          ->                   JobStatusResponse
    GET  /v1/jobs/{job_id}/logs     ->                   text/plain
    GET  /v1/jobs/{job_id}/outputs  ->                   JobOutputsResponse

NOTE: the /outputs contract is agreed with the gateway owner but the real
endpoint isn't merged yet -- built against the mock server for now. Shape:
{job_id, status, outputs:[{path, size_bytes, url, expires_at}]}, 409 while the
job is still running, 404 for an unknown/not-owned job.
"""

from pathlib import Path
from typing import Optional

import httpx


class ApiError(Exception):
    """Raised when the API returns an error response or is unreachable.

    ``status_code`` is the HTTP status when the failure came from an API
    response (None for connection/timeout errors). ``logs --follow`` uses it
    to tell "logs not ready yet" (409) apart from real failures.
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _headers(api_key: Optional[str], identity_token: Optional[str] = None) -> dict:
    headers = {"X-Api-Key": api_key} if api_key else {}
    if identity_token:
        headers["Authorization"] = f"Bearer {identity_token}"
    return headers


def submit_job(base_url: str, payload: dict, api_key: Optional[str] = None,
                identity_token: Optional[str] = None) -> dict:
    """
    POST the job payload to /v1/jobs.

    Returns the parsed JSON body on success (job_id, status).
    Raises ApiError with a human-readable message on any failure.
    """
    url = f"{base_url}/v1/jobs"
    try:
        response = httpx.post(url, json=payload, headers=_headers(api_key, identity_token), timeout=30.0)
    except httpx.ConnectError as e:
        raise ApiError(
            f"Could not connect to the API at {base_url}. "
            f"Is the server running? ({e})"
        ) from e
    except httpx.TimeoutException as e:
        raise ApiError(f"Request to {url} timed out.") from e

    if response.status_code >= 400:
        raise ApiError(_format_error(response))

    return response.json()


def get_job_status(base_url: str, job_id: str, api_key: Optional[str] = None,
                    identity_token: Optional[str] = None) -> dict:
    """
    GET /v1/jobs/{job_id}.

    Returns the parsed JSON body on success.
    Raises ApiError with a human-readable message on any failure,
    including a clear message for a 404 (unknown job ID).
    """
    url = f"{base_url}/v1/jobs/{job_id}"
    try:
        response = httpx.get(url, headers=_headers(api_key, identity_token), timeout=30.0)
    except httpx.ConnectError as e:
        raise ApiError(
            f"Could not connect to the API at {base_url}. "
            f"Is the server running? ({e})"
        ) from e
    except httpx.TimeoutException as e:
        raise ApiError(f"Request to {url} timed out.") from e

    if response.status_code == 404:
        raise ApiError(f"No job found with ID '{job_id}'.", status_code=404)
    if response.status_code >= 400:
        raise ApiError(_format_error(response), status_code=response.status_code)

    return response.json()


def get_job_logs(base_url: str, job_id: str, api_key: Optional[str] = None,
                  identity_token: Optional[str] = None) -> str:
    """
    GET /v1/jobs/{job_id}/logs.

    Returns the raw log text on success.
    Raises ApiError with a human-readable message on any failure,
    including a clear message for a 404 (unknown job, or logs no longer available).
    """
    url = f"{base_url}/v1/jobs/{job_id}/logs"
    try:
        response = httpx.get(url, headers=_headers(api_key, identity_token), timeout=30.0)
    except httpx.ConnectError as e:
        raise ApiError(
            f"Could not connect to the API at {base_url}. "
            f"Is the server running? ({e})"
        ) from e
    except httpx.TimeoutException as e:
        raise ApiError(f"Request to {url} timed out.") from e

    if response.status_code >= 400:
        raise ApiError(_format_error(response), status_code=response.status_code)

    return response.text


def get_job_outputs(base_url: str, job_id: str, api_key: Optional[str] = None,
                     identity_token: Optional[str] = None) -> dict:
    """
    GET /v1/jobs/{job_id}/outputs.

    Returns the parsed JSON body, expected to look like:

        {
          "job_id": "job-abc",
          "status": "SUCCEEDED",
          "outputs": [
            {"path": "models/best.pt", "size_bytes": 12345,
             "url": "<presigned GET url>", "expires_at": "<iso8601>"}
          ]
        }

    A job that finished with no outputs returns an empty ``outputs`` list.
    Raises ApiError (with ``status_code`` set) on any failure: 404 for an
    unknown/not-owned job, 409 while the job is still running (outputs aren't
    available until it completes).
    """
    url = f"{base_url}/v1/jobs/{job_id}/outputs"
    try:
        response = httpx.get(url, headers=_headers(api_key, identity_token), timeout=30.0)
    except httpx.ConnectError as e:
        raise ApiError(
            f"Could not connect to the API at {base_url}. "
            f"Is the server running? ({e})"
        ) from e
    except httpx.TimeoutException as e:
        raise ApiError(f"Request to {url} timed out.") from e

    if response.status_code == 404:
        raise ApiError(f"No job found with ID '{job_id}'.", status_code=404)
    if response.status_code >= 400:
        raise ApiError(_format_error(response), status_code=response.status_code)

    return response.json()


def download_output(url: str, dest: Path) -> None:
    """
    Stream a presigned output URL to ``dest``, creating parent directories.

    Presigned URLs are self-authenticating, so no API key or identity token is
    attached -- the signature in the URL is the credential. Raises ApiError on
    any HTTP or connection failure; the partial file is removed on failure so a
    half-written download is never mistaken for a complete one.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.stream("GET", url, timeout=300.0, follow_redirects=True) as response:
            if response.status_code >= 400:
                response.read()
                raise ApiError(
                    f"Download failed ({response.status_code}) for {url}",
                    status_code=response.status_code,
                )
            try:
                with open(dest, "wb") as f:
                    for chunk in response.iter_bytes():
                        f.write(chunk)
            except OSError:
                dest.unlink(missing_ok=True)
                raise
    except httpx.ConnectError as e:
        raise ApiError(f"Could not download from {url}: connection failed ({e}).") from e
    except httpx.TimeoutException as e:
        raise ApiError(f"Download from {url} timed out.") from e


def _format_error(response: httpx.Response) -> str:
    """Try to pull FastAPI's {"detail": "..."} out of an error response."""
    try:
        body = response.json()
        detail: Optional[str] = body.get("detail") if isinstance(body, dict) else None
    except ValueError:
        detail = None

    if detail:
        return f"API error ({response.status_code}): {detail}"
    return f"API error ({response.status_code}): {response.text}"
