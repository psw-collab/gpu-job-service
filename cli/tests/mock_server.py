"""
Minimal in-memory mock of gpu-job-service's API, matching the real
schemas.py / main.py contract exactly. Used only to test the CLI
end-to-end without needing the real Postgres-backed server running.
"""

import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI()

_jobs: dict = {}


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
    return JobSubmitResponse(job_id=job_id, status="PENDING")


@app.get("/v1/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return JobStatusResponse(**job)

# ---------------------------------------------------------------------------
# PROPOSED contract additions (list / cancel). Mirrors what Person 2's real
# API needs to implement. `CANCELLED` is a new status value for the contract.
# ---------------------------------------------------------------------------

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


class JobSummary(BaseModel):
    id: str
    status: str
    gpu_type: str
    gpu_count: int
    submitted_at: datetime


class JobListResponse(BaseModel):
    jobs: list[JobSummary]


@app.get("/v1/jobs", response_model=JobListResponse)
def list_jobs(status: Optional[str] = None):
    jobs = list(_jobs.values())
    if status:
        jobs = [j for j in jobs if j["status"] == status]
    return JobListResponse(jobs=[JobSummary(**j) for j in jobs])


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
