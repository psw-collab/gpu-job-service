"""
Minimal in-memory mock of gpu-job-service's API, matching the real
schemas.py / main.py contract exactly. Used only to test the CLI
end-to-end without needing the real Postgres-backed server running.
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

app = FastAPI()

_jobs: dict = {}

# job_id -> list of {"path": str, "content": bytes}. Seeded by tests via
# seed_outputs(); stands in for what upload_outputs.py would put in MinIO.
_outputs: dict = {}


def seed_outputs(job_id: str, files: list) -> None:
    """Test helper. ``files`` is a list of (path, content_bytes) tuples."""
    _outputs[job_id] = [{"path": path, "content": content} for path, content in files]


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


@app.get("/v1/jobs/{job_id}/outputs")
def get_job_outputs(job_id: str, request: Request):
    """Output-retrieval contract (agreed with the gateway owner):
    200 with {job_id, status, outputs:[{path,size_bytes,url,expires_at}]} for a
    completed job (empty outputs list if it produced none), 409 while the job is
    still running, 404 for an unknown/not-owned job. Presigned-style URLs point
    at this mock's own object route, since there's no real MinIO here."""
    job = _jobs.get(job_id)
    if job is None and job_id not in _outputs:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job is not None and job["status"] not in ("SUCCEEDED", "FAILED"):
        raise HTTPException(
            status_code=409,
            detail=f"Outputs are not available until the job completes -- "
                   f"job is currently {job['status']}.",
        )

    status = job["status"] if job is not None else "SUCCEEDED"
    entries = _outputs.get(job_id, [])
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    base = str(request.base_url).rstrip("/")
    outputs = [
        {
            "path": entry["path"],
            "size_bytes": len(entry["content"]),
            "url": f"{base}/_mock_object/{job_id}/{entry['path']}",
            "expires_at": expires_at,
        }
        for entry in entries
    ]
    return {"job_id": job_id, "status": status, "outputs": outputs}


@app.get("/_mock_object/{job_id}/{obj_path:path}")
def get_mock_object(job_id: str, obj_path: str):
    """Stand-in for the presigned object store. Serves seeded output bytes so
    the CLI's download path can be exercised end to end. Not part of the API
    contract -- the real URLs point at MinIO/S3."""
    for entry in _outputs.get(job_id, []):
        if entry["path"] == obj_path:
            return Response(content=entry["content"], media_type="application/octet-stream")
    raise HTTPException(status_code=404, detail="object not found")