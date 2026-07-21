"""
Public-facing API. Talks only to Postgres -- no Kubernetes access, no
network path to the cluster required. This is what gets deployed to Cloud
Run.

Actually creating/scheduling K8s Jobs, reconciling status, and capturing
logs is the worker's job (k8s_ops.py / worker.py). The worker has no direct
DB access either -- the colo cluster's network blocks arbitrary outbound
ports, so the worker talks to the /internal/* endpoints below over plain
HTTPS instead, authenticated with a shared token.
"""
import base64
import binascii
import hashlib
import os
import secrets
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.orm import Session

from database import get_db
from models import DBJob
from schemas import (
    ALLOWED_GPU_TYPES,
    ALLOWED_PYTHON_VERSIONS,
    JobSubmitRequest,
    JobSubmitResponse,
    JobStatusResponse,
    JobReport,
)

app = FastAPI(title="GPU Job-as-a-Service Gateway")

INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN")
if not INTERNAL_TOKEN:
    raise RuntimeError("INTERNAL_TOKEN environment variable is required")

API_KEY = os.getenv("API_KEY")
if not API_KEY:
    raise RuntimeError("API_KEY environment variable is required")

RETENTION_PERIOD = timedelta(days=int(os.getenv("RETENTION_DAYS", "7")))


def require_internal_token(x_internal_token: str = Header(...)):
    if x_internal_token != INTERNAL_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")


def require_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


def generate_job_id(db: Session) -> str:
    for _ in range(10):
        job_id = "job-" + secrets.token_hex(4)
        if not db.query(DBJob).filter(DBJob.id == job_id).first():
            return job_id
    raise HTTPException(status_code=500, detail="Failed to generate unique job ID")


@app.post("/v1/jobs", response_model=JobSubmitResponse, dependencies=[Depends(require_api_key)])
def submit_job(request: JobSubmitRequest, db: Session = Depends(get_db)):
    if request.gpu_type not in ALLOWED_GPU_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported gpu_type '{request.gpu_type}'. Allowed: {', '.join(sorted(ALLOWED_GPU_TYPES))}",
        )
    if request.python_version not in ALLOWED_PYTHON_VERSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported python_version '{request.python_version}'. Allowed: {', '.join(sorted(ALLOWED_PYTHON_VERSIONS))}",
        )

    if (request.entrypoint_content is None) == (request.source_archive_b64 is None):
        raise HTTPException(
            status_code=422,
            detail="Exactly one of entrypoint_content or source_archive_b64 must be provided.",
        )

    source_archive = None
    if request.source_archive_b64 is not None:
        try:
            source_archive = base64.b64decode(request.source_archive_b64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=422, detail="source_archive_b64 is not valid base64.")

    job_id = generate_job_id(db)

    req_hash = None
    if request.requirements:
        req_hash = hashlib.sha256(request.requirements.encode()).hexdigest()

    new_job = DBJob(
        id=job_id,
        entrypoint=request.entrypoint,
        entrypoint_content=request.entrypoint_content,
        source_archive=source_archive,
        requirements=request.requirements,
        python_version=request.python_version,
        gpu_type=request.gpu_type,
        gpu_count=request.gpu_count,
        requirements_hash=req_hash,
        status="PENDING",
        status_message="Job received, waiting to be scheduled",
    )
    db.add(new_job)
    db.commit()
    db.refresh(new_job)

    return JobSubmitResponse(job_id=new_job.id, status=new_job.status)


@app.get("/v1/jobs/{job_id}", response_model=JobStatusResponse, dependencies=[Depends(require_api_key)])
def get_job_status(job_id: str, db: Session = Depends(get_db)):
    job = db.query(DBJob).filter(DBJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return JobStatusResponse(
        id=job.id,
        status=job.status,
        status_message=job.status_message,
        entrypoint=job.entrypoint,
        python_version=job.python_version,
        gpu_type=job.gpu_type,
        gpu_count=job.gpu_count,
        failure_reason=job.failure_reason,
        submitted_at=job.submitted_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


@app.get("/v1/jobs/{job_id}/logs", response_class=PlainTextResponse, dependencies=[Depends(require_api_key)])
def get_job_logs(job_id: str, db: Session = Depends(get_db)):
    job = db.query(DBJob).filter(DBJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job.logs is not None:
        return job.logs

    if job.status in ("SUCCEEDED", "FAILED"):
        raise HTTPException(status_code=404, detail="No logs were captured for this job.")

    raise HTTPException(
        status_code=409,
        detail=f"Logs are not available yet -- job is currently {job.status}. Try again once it starts running.",
    )


# --- Internal endpoints, called only by the worker over HTTPS ---

@app.get("/internal/jobs", dependencies=[Depends(require_internal_token)])
def list_active_jobs(db: Session = Depends(get_db)):
    jobs = db.query(DBJob).filter(DBJob.status.notin_(("SUCCEEDED", "FAILED"))).all()
    return [
        {
            "id": j.id,
            "status": j.status,
            "entrypoint": j.entrypoint,
            "entrypoint_content": j.entrypoint_content,
            "has_archive": j.source_archive is not None,
            "requirements": j.requirements,
            "python_version": j.python_version,
            "gpu_type": j.gpu_type,
            "gpu_count": j.gpu_count,
            "submitted_at": j.submitted_at.isoformat(),
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "scheduled_at": j.scheduled_at.isoformat() if j.scheduled_at else None,
        }
        for j in jobs
    ]


@app.get("/internal/jobs/{job_id}/source", dependencies=[Depends(require_internal_token)])
def get_job_source(job_id: str, db: Session = Depends(get_db)):
    job = db.query(DBJob).filter(DBJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if job.source_archive is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} has no source archive")
    return Response(content=job.source_archive, media_type="application/gzip")


@app.post("/internal/jobs/{job_id}/report", dependencies=[Depends(require_internal_token)])
def report_job(job_id: str, report: JobReport, db: Session = Depends(get_db)):
    job = db.query(DBJob).filter(DBJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    job.status = report.status
    if report.status_message is not None:
        job.status_message = report.status_message
    if report.failure_reason is not None:
        job.failure_reason = report.failure_reason
    if report.logs is not None:
        job.logs = report.logs
    if report.image_tag is not None:
        job.image_tag = report.image_tag
    if report.started_at is not None:
        job.started_at = report.started_at
    if report.scheduled_at is not None:
        job.scheduled_at = report.scheduled_at
    if report.completed_at is not None:
        job.completed_at = report.completed_at
    db.commit()
    return {"ok": True}


@app.post("/internal/gc", dependencies=[Depends(require_internal_token)])
def run_gc(db: Session = Depends(get_db)):
    cutoff = datetime.now(timezone.utc) - RETENTION_PERIOD
    deleted = (
        db.query(DBJob)
        .filter(DBJob.status.in_(("SUCCEEDED", "FAILED")))
        .filter(DBJob.submitted_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"deleted": deleted}
