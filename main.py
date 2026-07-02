import asyncio
import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, HTTPException
from kubernetes import client, config
from sqlalchemy.orm import Session

from database import get_db, SessionLocal
from models import DBJob
from schemas import (
    ALLOWED_GPU_TYPES,
    ALLOWED_PYTHON_VERSIONS,
    JobSubmitRequest,
    JobSubmitResponse,
    JobStatusResponse,
)

import os

try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

NAMESPACE = os.getenv("K8S_NAMESPACE")
if not NAMESPACE:
    raise RuntimeError("K8S_NAMESPACE environment variable is required")
core = client.CoreV1Api()
batch = client.BatchV1Api()

app = FastAPI(title="GPU Job-as-a-Service")


def generate_job_id(db: Session) -> str:
    for _ in range(10):
        job_id = "job-" + secrets.token_hex(4)
        if not db.query(DBJob).filter(DBJob.id == job_id).first():
            return job_id
    raise HTTPException(status_code=500, detail="Failed to generate unique job ID")


def create_k8s_job(job_id: str, entrypoint: str, entrypoint_content: str, requirements: str, python_version: str):
    core.create_namespaced_config_map(
        NAMESPACE,
        client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=f"{job_id}-files"),
            data={entrypoint: entrypoint_content, "requirements.txt": requirements or ""},
        ),
    )
    container = client.V1Container(
        name="runner",
        image=f"python:{python_version}-slim",
        command=["sh", "-c",
                 f"pip install -r /scripts/requirements.txt 2>/dev/null; python /scripts/{entrypoint}"],
        volume_mounts=[client.V1VolumeMount(name="code", mount_path="/scripts")],
    )
    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        containers=[container],
        volumes=[client.V1Volume(
            name="code",
            config_map=client.V1ConfigMapVolumeSource(name=f"{job_id}-files"))],
    )
    batch.create_namespaced_job(
        NAMESPACE,
        client.V1Job(
            metadata=client.V1ObjectMeta(name=job_id),
            spec=client.V1JobSpec(
                template=client.V1PodTemplateSpec(spec=pod_spec),
                backoff_limit=0,
                ttl_seconds_after_finished=300),
        ),
    )


@app.post("/v1/jobs", response_model=JobSubmitResponse)
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

    job_id = generate_job_id(db)

    req_hash = None
    if request.requirements:
        req_hash = hashlib.sha256(request.requirements.encode()).hexdigest()

    new_job = DBJob(
        id=job_id,
        entrypoint=request.entrypoint,
        entrypoint_content=request.entrypoint_content,
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

    create_k8s_job(job_id, request.entrypoint, request.entrypoint_content,
                   request.requirements, request.python_version)

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


def k8s_status(job_id: str) -> str | None:
    try:
        s = batch.read_namespaced_job_status(job_id, NAMESPACE).status
    except client.ApiException as e:
        if e.status == 404:
            return None
        raise
    if s.succeeded:
        return "SUCCEEDED"
    if s.failed:
        return "FAILED"
    if s.active:
        return "RUNNING"
    return "PENDING"


FAILURE_REASON_OOM = "Your job ran out of memory. Try requesting more memory or reducing batch size."
FAILURE_REASON_NODE = "A hardware issue interrupted your job. This is not a problem with your code, please resubmit."
FAILURE_REASON_USER_CODE = "Your job exited with an error. Check the logs for the full traceback."
FAILURE_REASON_UNKNOWN = "Your job failed for an unknown reason. Contact support with your job ID."


def classify_pod_failure(job_id: str) -> str:
    try:
        pods = core.list_namespaced_pod(NAMESPACE, label_selector=f"job-name={job_id}").items
    except client.ApiException:
        return FAILURE_REASON_UNKNOWN

    if not pods:
        return FAILURE_REASON_UNKNOWN

    pod = pods[0]
    if pod.status.reason in ("Evicted", "NodeLost", "NodeAffinity"):
        return FAILURE_REASON_NODE

    for cs in pod.status.container_statuses or []:
        terminated = cs.state.terminated if cs.state else None
        if not terminated:
            continue
        if terminated.reason == "OOMKilled" or terminated.exit_code == 137:
            return FAILURE_REASON_OOM
        if terminated.exit_code:
            return FAILURE_REASON_USER_CODE

    return FAILURE_REASON_UNKNOWN


async def reconcile_loop():
    while True:
        try:
            db = SessionLocal()
            try:
                jobs = db.query(DBJob).filter(DBJob.status.notin_(("SUCCEEDED", "FAILED"))).all()
                for job in jobs:
                    job_id = job.id
                    try:
                        new_status = k8s_status(job_id)
                        if new_status is None:
                            continue
                        if new_status != job.status:
                            job.status = new_status
                            now = datetime.now(timezone.utc)
                            if new_status == "RUNNING" and job.started_at is None:
                                job.started_at = now
                                job.status_message = "Job is running"
                            if new_status == "SUCCEEDED":
                                job.completed_at = now
                                job.status_message = "Job completed successfully"
                            if new_status == "FAILED":
                                job.completed_at = now
                                job.status_message = "Job failed"
                                job.failure_reason = classify_pod_failure(job_id)
                        db.commit()
                    except Exception as e:
                        print(f"reconcile error for {job_id}: {e}")
                        db.rollback()
            finally:
                db.close()
        except Exception as e:
            print(f"reconcile loop error: {e}")
        await asyncio.sleep(5)


@app.on_event("startup")
async def start_reconciler():
    asyncio.create_task(reconcile_loop())