"""
Thin HTTP client for the gpu-job-service API.

Matches the contract defined in schemas.py / main.py:

    POST /v1/jobs                 JobSubmitRequest -> JobSubmitResponse
    GET  /v1/jobs                 -> JobListResponse         (list)
    GET  /v1/jobs/{job_id}        -> JobStatusResponse
    POST /v1/jobs/{job_id}/cancel -> JobStatusResponse       (cancel)
    GET  /v1/jobs/{job_id}/logs   -> JobLogsResponse         (logs)
"""

from typing import Optional

import httpx


class ApiError(Exception):
    """Raised when the API returns an error response or is unreachable."""


def _get(url: str, base_url: str, **kwargs):
    try:
        return httpx.get(url, timeout=30.0, **kwargs)
    except httpx.ConnectError as e:
        raise ApiError(
            f"Could not connect to the API at {base_url}. "
            f"Is the server running? ({e})"
        ) from e
    except httpx.TimeoutException as e:
        raise ApiError(f"Request to {url} timed out.") from e


def _post(url: str, base_url: str, **kwargs):
    try:
        return httpx.post(url, timeout=30.0, **kwargs)
    except httpx.ConnectError as e:
        raise ApiError(
            f"Could not connect to the API at {base_url}. "
            f"Is the server running? ({e})"
        ) from e
    except httpx.TimeoutException as e:
        raise ApiError(f"Request to {url} timed out.") from e


def submit_job(base_url: str, payload: dict) -> dict:
    """POST the job payload to /v1/jobs. Returns the parsed JSON body."""
    url = f"{base_url}/v1/jobs"
    response = _post(url, base_url, json=payload)
    if response.status_code >= 400:
        raise ApiError(_format_error(response))
    return response.json()


def get_job_status(base_url: str, job_id: str) -> dict:
    """GET /v1/jobs/{job_id}. Friendly message on 404."""
    url = f"{base_url}/v1/jobs/{job_id}"
    response = _get(url, base_url)
    if response.status_code == 404:
        raise ApiError(f"No job found with ID '{job_id}'.")
    if response.status_code >= 400:
        raise ApiError(_format_error(response))
    return response.json()


def list_jobs(base_url: str, status: Optional[str] = None) -> dict:
    """GET /v1/jobs, optionally filtered by status. Returns {"jobs": [...]}."""
    url = f"{base_url}/v1/jobs"
    params = {"status": status} if status else None
    response = _get(url, base_url, params=params)
    if response.status_code >= 400:
        raise ApiError(_format_error(response))
    return response.json()


def cancel_job(base_url: str, job_id: str) -> dict:
    """
    POST /v1/jobs/{job_id}/cancel. Returns the updated job dict.
    Clear messages for 404 (unknown) and 409 (already terminal).
    """
    url = f"{base_url}/v1/jobs/{job_id}/cancel"
    response = _post(url, base_url)
    if response.status_code == 404:
        raise ApiError(f"No job found with ID '{job_id}'.")
    if response.status_code >= 400:
        raise ApiError(_format_error(response))
    return response.json()


def get_job_logs(base_url: str, job_id: str, since: int = 0) -> dict:
    """
    GET /v1/jobs/{job_id}/logs?since=<cursor>.

    Returns {"job_id", "status", "failure_reason", "logs", "next_since"}.
    `since`/`next_since` form a cursor so callers can poll for only the
    new output since their last request (the basis for --follow).
    """
    url = f"{base_url}/v1/jobs/{job_id}/logs"
    response = _get(url, base_url, params={"since": since})
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
