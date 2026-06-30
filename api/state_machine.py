"""
Job state machine. Pure functions, no FastAPI, no HTTP — this module knows
nothing about requests, responses, or status codes. It takes plain Python
values in and returns plain Python values (or DBJob ORM objects) out, and
raises clear exceptions when something invalid is attempted.

This is deliberately separate from main.py (the API layer) and from
whatever module ends up watching Kubernetes (the "controller" piece) — both
of those should call into this module rather than touching the `jobs`
table directly, so the legal-transition rules live in exactly one place.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from models import DBJob, JobStatus, FailureCode


class IllegalTransitionError(Exception):
    """Raised when a transition is attempted that the state graph forbids."""
    pass


class JobNotFoundError(Exception):
    """Raised when a transition or read is attempted on a job ID that doesn't exist."""
    pass


# The legal transition graph. Keys are the current status; values are the
# set of statuses that status is allowed to move to. This is the single
# source of truth for "what's a legal move" — nothing else in this module
# (or anywhere else) should encode this logic separately.
_LEGAL_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.PENDING: {JobStatus.SCHEDULED, JobStatus.FAILED},
    JobStatus.SCHEDULED: {JobStatus.RUNNING, JobStatus.FAILED},
    JobStatus.RUNNING: {JobStatus.SUCCEEDED, JobStatus.FAILED},
    JobStatus.SUCCEEDED: set(),  # terminal
    JobStatus.FAILED: set(),     # terminal
}


def create_job(
    db: Session,
    job_id: str,
    customer_id: str,
    artifact_location: str,
    python_version: str,
    gpu_type: str,
    gpu_count: int,
    requirements_spec: Optional[str] = None,
) -> DBJob:
    """
    Create a new job row in PENDING status. Does not touch Kubernetes —
    that's the API layer's job, after this function returns successfully.
    """
    job = DBJob(
        id=job_id,
        customer_id=customer_id,
        status=JobStatus.PENDING,
        artifact_location=artifact_location,
        requirements_spec=requirements_spec,
        python_version=python_version,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_job(db: Session, job_id: str, customer_id: str) -> DBJob:
    """
    Fetch a job, scoped to the given customer_id. Raises JobNotFoundError
    if no matching row exists (either the ID is wrong, or it belongs to a
    different customer — both cases look the same from the caller's side,
    which is intentional: don't leak whether a job ID exists for someone
    else's customer_id).
    """
    job = (
        db.query(DBJob)
        .filter(DBJob.id == job_id, DBJob.customer_id == customer_id)
        .first()
    )
    if job is None:
        raise JobNotFoundError(f"No job '{job_id}' found for this customer")
    return job


def transition_job(
    db: Session,
    job_id: str,
    new_status: JobStatus,
    status_message: Optional[str] = None,
    failure_code: Optional[FailureCode] = None,
    failure_reason: Optional[str] = None,
) -> DBJob:
    """
    Move a job from its current status to new_status, enforcing the legal
    transition graph. Raises IllegalTransitionError if the move isn't
    allowed (e.g. SUCCEEDED -> RUNNING, or skipping SCHEDULED entirely).

    This does NOT scope by customer_id — it's meant to be called by trusted
    internal callers (the API right after job creation, or the K8s watcher
    module), not exposed directly to customer-facing requests. Customer-
    facing reads should go through get_job().

    Side effects beyond status: sets started_at when entering RUNNING, sets
    completed_at when entering SUCCEEDED or FAILED, and stamps failure_code
    / failure_reason only when transitioning into FAILED.
    """
    job = db.query(DBJob).filter(DBJob.id == job_id).first()
    if job is None:
        raise JobNotFoundError(f"No job '{job_id}' found")

    current_status = job.status
    allowed = _LEGAL_TRANSITIONS.get(current_status, set())

    if new_status not in allowed:
        raise IllegalTransitionError(
            f"Cannot transition job '{job_id}' from {current_status} to {new_status}"
        )

    job.status = new_status
    now = datetime.now(timezone.utc)

    if new_status == JobStatus.RUNNING and job.started_at is None:
        job.started_at = now

    if new_status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
        job.completed_at = now

    if new_status == JobStatus.FAILED:
        job.failure_code = failure_code
        job.failure_reason = failure_reason

    if status_message is not None:
        job.status_message = status_message

    job.updated_at = now

    db.commit()
    db.refresh(job)
    return job
