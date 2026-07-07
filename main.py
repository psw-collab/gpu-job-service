import asyncio
import hashlib
import secrets
from datetime import datetime, timezone
import os
NAMESPACE = os.getenv("K8S_NAMESPACE", "tsonar-space")
SCHEDULING_TIMEOUT_MINUTES = int(os.getenv("SCHEDULING_TIMEOUT_MINUTES", "30"))

from fastapi import FastAPI, Depends, HTTPException
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
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

config.load_kube_config()
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
    """Create a batch Job first, then a ConfigMap owned by that Job.
    Kubernetes auto-deletes the ConfigMap whenever the Job is deleted (TTL or manual)."""
    cm_name = f"{job_id}-files"

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
            config_map=client.V1ConfigMapVolumeSource(name=cm_name))],
    )

    # 1. Create the Job first so we get back its UID for the owner reference
    created_job = batch.create_namespaced_job(
        NAMESPACE,
        client.V1Job(
            metadata=client.V1ObjectMeta(name=job_id),
            spec=client.V1JobSpec(
                template=client.V1PodTemplateSpec(spec=pod_spec),
                backoff_limit=0,
                ttl_seconds_after_finished=300),
        ),
    )

    # 2. Create the ConfigMap owned by the Job — auto-deleted when the Job is deleted
    owner_ref = client.V1OwnerReference(
        api_version="batch/v1",
        kind="Job",
        name=created_job.metadata.name,
        uid=created_job.metadata.uid,
        block_owner_deletion=True,
        controller=True,
    )
    try:
        core.create_namespaced_config_map(
            NAMESPACE,
            client.V1ConfigMap(
                metadata=client.V1ObjectMeta(name=cm_name, owner_references=[owner_ref]),
                data={entrypoint: entrypoint_content, "requirements.txt": requirements or ""},
            ),
        )
    except Exception:
        # ConfigMap failed after the Job was created — clean up the Job so we
        # don't leave a Job with no script mounted (it would just error in the pod)
        try:
            batch.delete_namespaced_job(job_id, NAMESPACE, propagation_policy="Foreground")
        except Exception:
            pass
        raise

@app.post("/v1/jobs", response_model=JobSubmitResponse)
def submit_job(request: JobSubmitRequest, db: Session = Depends(get_db)):
    if request.gpu_type not in ALLOWED_GPU_TYPES:
        raise HTTPException(422, f"Unsupported gpu_type '{request.gpu_type}'. Allowed: {', '.join(sorted(ALLOWED_GPU_TYPES))}")
    if request.python_version not in ALLOWED_PYTHON_VERSIONS:
        raise HTTPException(422, f"Unsupported python_version '{request.python_version}'. Allowed: {', '.join(sorted(ALLOWED_PYTHON_VERSIONS))}")

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

    try:
        create_k8s_job(job_id, request.entrypoint, request.entrypoint_content,
                       request.requirements, request.python_version)
    except Exception as e:
        new_job.status = "FAILED"
        new_job.failure_reason = "platform_error: failed to schedule job on cluster"
        new_job.status_message = "We could not start your job due to a platform error. Please try again."
        new_job.completed_at = datetime.now(timezone.utc)
        db.commit()
        print(f"create_k8s_job failed for {job_id}: {e}")
        return JobSubmitResponse(job_id=new_job.id, status=new_job.status)

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


def k8s_status(job_id: str) -> str:
    s = batch.read_namespaced_job_status(job_id, NAMESPACE).status
    if s.succeeded:
        return "SUCCEEDED"
    if s.failed:
        return "FAILED"
    if s.active:
        return "RUNNING"
    return "PENDING"


async def reconcile_loop():
    while True:
        try:
            db = SessionLocal()
            try:
                jobs = db.query(DBJob).filter(DBJob.status.notin_(("SUCCEEDED", "FAILED"))).all()
                for job in jobs:
                    try:
                        # --- FIX #4: timeout check goes here, before status lookup ---
                        age_seconds = (datetime.now(timezone.utc) - job.submitted_at).total_seconds()
                        if age_seconds > SCHEDULING_TIMEOUT_MINUTES * 60:
                            job.status = "FAILED"
                            job.failure_reason = "scheduling_timeout"
                            job.status_message = (
                                f"No GPUs of the requested type were available within the "
                                f"{SCHEDULING_TIMEOUT_MINUTES}-minute timeout window. "
                                f"Please try again or contact support."
                            )
                            job.completed_at = datetime.now(timezone.utc)
                            db.commit()
                            print(f"[timeout] job {job.id} exceeded {SCHEDULING_TIMEOUT_MINUTES}min, marked FAILED")
                            continue

                        new_status = k8s_status(job.id)
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
                        db.commit()
                    except ApiException as e:
                        if e.status == 404:
                            job.status = "FAILED"
                            job.failure_reason = "ghost: no matching K8s Job found"
                            job.status_message = (
                                "This job has no matching workload on the cluster. "
                                "It was likely lost due to a scheduling failure."
                            )
                            job.completed_at = datetime.now(timezone.utc)
                            db.commit()
                            print(f"[ghost-fix] retired ghost job {job.id}")
                        else:
                            print(f"reconcile K8s error for {job.id}: {e}")
                            db.rollback()
                    except Exception as e:
                        print(f"reconcile error for {job.id}: {e}")
                        db.rollback()
            finally:
                db.close()
        except Exception as e:
            # Catches DB connection failures, query errors — anything at the outer level.
            # Without this, an exception here would kill the whole coroutine permanently.
            print(f"reconcile_loop outer error (will retry in 5s): {e}")

        await asyncio.sleep(5)


@app.on_event("startup")
async def start_reconciler():
    asyncio.create_task(reconcile_loop())

