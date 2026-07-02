"""
Thin HTTP client for the gpu-job-service API.

Matches the contract defined in schemas.py / main.py:

    POST /v1/jobs           JobSubmitRequest  -> JobSubmitResponse
    GET  /v1/jobs/{job_id}  ->                   JobStatusResponse
"""

from typing import Optional

import httpx


class ApiError(Exception):
    """Raised when the API returns an error response or is unreachable."""


def submit_job(base_url: str, payload: dict) -> dict:
    """
    POST the job payload to /v1/jobs.

    Returns the parsed JSON body on success (job_id, status).
    Raises ApiError with a human-readable message on any failure.
    """
    url = f"{base_url}/v1/jobs"
    try:
        response = httpx.post(url, json=payload, timeout=30.0)
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


def get_job_status(base_url: str, job_id: str) -> dict:
    """
    GET /v1/jobs/{job_id}.

    Returns the parsed JSON body on success.
    Raises ApiError with a human-readable message on any failure,
    including a clear message for a 404 (unknown job ID).
    """
    url = f"{base_url}/v1/jobs/{job_id}"
    try:
        response = httpx.get(url, timeout=30.0)
    except httpx.ConnectError as e:
        raise ApiError(
            f"Could not connect to the API at {base_url}. "
            f"Is the server running? ({e})"
        ) from e
    except httpx.TimeoutException as e:
        raise ApiError(f"Request to {url} timed out.") from e

    if response.status_code == 404:
        raise ApiError(f"No job found with ID '{job_id}'.")
    if response.status_code >= 400:
        raise ApiError(_format_error(response))

    return response.json()


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


def list_jobs(base_url: str, status: Optional[str] = None) -> dict:
    """
    GET /v1/jobs, optionally filtered by status.

    Returns the parsed JSON body on success ({"jobs": [...]}).
    Raises ApiError with a human-readable message on any failure.
    """
    url = f"{base_url}/v1/jobs"
    params = {"status": status} if status else None
    try:
        response = httpx.get(url, params=params, timeout=30.0)
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


def cancel_job(base_url: str, job_id: str) -> dict:
    """
    POST /v1/jobs/{job_id}/cancel.

    Returns the updated job dict on success. Raises ApiError with a clear
    message for a 404 (unknown job) or 409 (job already in a terminal state).
    """
    url = f"{base_url}/v1/jobs/{job_id}/cancel"
    try:
        response = httpx.post(url, timeout=30.0)
    except httpx.ConnectError as e:
        raise ApiError(
            f"Could not connect to the API at {base_url}. "
            f"Is the server running? ({e})"
        ) from e
    except httpx.TimeoutException as e:
        raise ApiError(f"Request to {url} timed out.") from e

    if response.status_code == 404:
        raise ApiError(f"No job found with ID '{job_id}'.")
    if response.status_code >= 400:
        raise ApiError(_format_error(response))

    return response.json()
