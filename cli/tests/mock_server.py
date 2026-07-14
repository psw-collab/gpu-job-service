"""
Minimal in-memory mock of gpu-job-service's API, matching the real
schemas.py / main.py contract exactly. Used only to test the CLI
end-to-end without needing the real Postgres-backed server running.
"""

import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
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
        "logs": None,
    }
    return JobSubmitResponse(job_id=job_id, status="PENDING")


@app.get("/v1/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return JobStatusResponse(**job)


@app.get("/v1/jobs/{job_id}/logs", response_class=PlainTextResponse)
def get_job_logs(job_id: str):
    """Mirror the real gateway's log semantics exactly (gateway.py):
    200 + text when logs exist, 404 for a terminal job with none, 409 while
    the job hasn't produced any yet."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    logs = job.get("logs")
    if logs is not None:
        return logs

    if job["status"] in ("SUCCEEDED", "FAILED"):
        raise HTTPException(status_code=404, detail="No logs were captured for this job.")

    raise HTTPException(
        status_code=409,
        detail=f"Logs are not available yet -- job is currently {job['status']}. "
               f"Try again once it starts running.",
    )