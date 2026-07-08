"""
Minimal in-memory mock of gpu-job-service's API, matching the real
schemas.py / main.py contract plus the PROPOSED list/cancel/logs additions.
Used to test the CLI end-to-end without Postgres or the real backend.

Endpoints under /_test/ are NOT part of the real contract -- they exist only
so tests (and manual demos) can drive a job through states the real executor
would otherwise produce (RUNNING, log output, SUCCEEDED/FAILED).
"""

import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI()

_jobs: dict = {}
_logs: dict = {}  # job_id -> list[str] of log lines

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


class JobSubmitRequest(BaseModel):
    entrypoint: str
    entrypoint_content: str
    requirements: Optional[str] = None
    python_version: str = "3.11"
    gpu_type: str = "A100"
    gpu_count: int = Field(default=1, ge=1, le=8)


class JobSubmitResponse(BaseModel):
    job_id: str
    status: str


class JobStatusResponse(BaseModel):
    id: str
    status: str
    status_message: Optional[str] = None
    entrypoint: str
    python_version: str
    gpu_type: str
    gpu_count: int
    failure_reason: Optional[str] = None
    submitted_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class JobSummary(BaseModel):
    id: str
    status: str
    gpu_type: str
    gpu_count: int
    submitted_at: datetime


class JobListResponse(BaseModel):
    jobs: list[JobSummary]


class JobLogsResponse(BaseModel):
    job_id: str
    status: str
    failure_reason: Optional[str] = None
    logs: str
    next_since: int


@app.post("/v1/jobs", response_model=JobSubmitResponse)
def submit_job(request: JobSubmitRequest):
    job_id = "job-" + secrets.token_hex(4)
    _jobs[job_id] = {
        "id": job_id,
        "status": "PENDING",
        "status_message": "Job received, waiting to be scheduled",
        "entrypoint": request.entrypoint,
        "python_version": request.python_version,
        "gpu_type": request.gpu_type,
        "gpu_count": request.gpu_count,
        "failure_reason": None,
        "submitted_at": datetime.now(timezone.utc),
        "started_at": None,
        "completed_at": None,
    }
    _logs[job_id] = []
    return JobSubmitResponse(job_id=job_id, status="PENDING")


@app.get("/v1/jobs", response_model=JobListResponse)
def list_jobs(status: Optional[str] = None):
    jobs = list(_jobs.values())
    if status:
        jobs = [j for j in jobs if j["status"] == status]
    return JobListResponse(jobs=[JobSummary(**j) for j in jobs])


@app.get("/v1/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return JobStatusResponse(**job)


@app.post("/v1/jobs/{job_id}/cancel", response_model=JobStatusResponse)
def cancel_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if job["status"] in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel a job that is already {job['status']}",
        )
    job["status"] = "CANCELLED"
    job["status_message"] = "Job cancelled by user"
    job["completed_at"] = datetime.now(timezone.utc)
    return JobStatusResponse(**job)


@app.get("/v1/jobs/{job_id}/logs", response_model=JobLogsResponse)
def get_job_logs(job_id: str, since: int = 0):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    lines = _logs.get(job_id, [])
    new_lines = lines[since:]
    text = "".join(line if line.endswith("\n") else line + "\n" for line in new_lines)
    return JobLogsResponse(
        job_id=job_id,
        status=job["status"],
        failure_reason=job.get("failure_reason"),
        logs=text,
        next_since=len(lines),
    )


# ---------------------------------------------------------------------------
# TEST-ONLY control hook (not part of the real contract).
# ---------------------------------------------------------------------------
class TestAdvance(BaseModel):
    status: Optional[str] = None
    failure_reason: Optional[str] = None
    append_log: Optional[str] = None


@app.post("/_test/jobs/{job_id}", response_model=JobStatusResponse)
def _test_advance(job_id: str, body: TestAdvance):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if body.append_log is not None:
        _logs.setdefault(job_id, []).append(body.append_log)
    if body.status is not None:
        job["status"] = body.status
        if body.status == "RUNNING" and job["started_at"] is None:
            job["started_at"] = datetime.now(timezone.utc)
        if body.status in TERMINAL_STATUSES:
            job["completed_at"] = datetime.now(timezone.utc)
    if body.failure_reason is not None:
        job["failure_reason"] = body.failure_reason
    return JobStatusResponse(**job)
