import hashlib
import secrets

from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import DBJob
from schemas import JobSubmitRequest, JobSubmitResponse, JobStatusResponse

app = FastAPI(title="GPU Job-as-a-Service")


def generate_job_id(db: Session) -> str:
    for _ in range(10):
        job_id = "job-" + secrets.token_hex(4)
        if not db.query(DBJob).filter(DBJob.id == job_id).first():
            return job_id
    raise HTTPException(status_code=500, detail="Failed to generate unique job ID")


@app.post("/v1/jobs", response_model=JobSubmitResponse)
def submit_job(request: JobSubmitRequest, db: Session = Depends(get_db)):
    job_id = generate_job_id(db)

    req_hash = None
    if request.requirements:
        req_hash = hashlib.sha256(request.requirements.encode()).hexdigest()

    new_job = DBJob(
        id=job_id,
        entrypoint=request.entrypoint,
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


@app.get("/v1/jobs/{job_id}", response_model=JobStatusResponse)
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