"""
Unit tests for state_machine.py, run against a real local Postgres (not
mocks) — per the build plan, this validates actual DB transaction behavior,
not just in-memory logic.

Requires the local docker-compose Postgres to be running and migrated
before running these tests:
    docker compose up -d
    goose -dir migrations postgres "<DATABASE_URL>" up
    pytest api/tests/test_state_machine.py

Each test creates its own job with a unique ID and doesn't rely on test
ordering or shared state, so it's safe to run in any order or in parallel.
"""

import uuid

import pytest
from sqlalchemy.orm import Session

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import SessionLocal
from models import JobStatus, FailureCode
from state_machine import (
    create_job,
    get_job,
    transition_job,
    IllegalTransitionError,
    JobNotFoundError,
)


@pytest.fixture
def db() -> Session:
    session = SessionLocal()
    yield session
    session.close()


def _new_job_id() -> str:
    return f"job-test-{uuid.uuid4().hex[:8]}"


def _make_job(db: Session, customer_id: str = "test-customer"):
    job_id = _new_job_id()
    return create_job(
        db,
        job_id=job_id,
        customer_id=customer_id,
        artifact_location="scripts/test-customer/abc/train.py",
        python_version="3.13",
        gpu_type="A100",
        gpu_count=1,
    )


def test_create_job_starts_pending(db):
    job = _make_job(db)
    assert job.status == JobStatus.PENDING
    assert job.failure_code is None
    assert job.completed_at is None


def test_get_job_scoped_by_customer(db):
    job = _make_job(db, customer_id="customer-a")
    fetched = get_job(db, job.id, customer_id="customer-a")
    assert fetched.id == job.id


def test_get_job_wrong_customer_raises_not_found(db):
    job = _make_job(db, customer_id="customer-a")
    with pytest.raises(JobNotFoundError):
        get_job(db, job.id, customer_id="customer-b")


def test_get_job_missing_id_raises_not_found(db):
    with pytest.raises(JobNotFoundError):
        get_job(db, "job-does-not-exist", customer_id="test-customer")


def test_legal_transition_pending_to_scheduled(db):
    job = _make_job(db)
    updated = transition_job(db, job.id, JobStatus.SCHEDULED)
    assert updated.status == JobStatus.SCHEDULED


def test_legal_full_success_path_sets_timestamps(db):
    job = _make_job(db)
    transition_job(db, job.id, JobStatus.SCHEDULED)
    running = transition_job(db, job.id, JobStatus.RUNNING)
    assert running.started_at is not None
    assert running.completed_at is None

    succeeded = transition_job(db, job.id, JobStatus.SUCCEEDED)
    assert succeeded.completed_at is not None


def test_legal_failure_path_sets_failure_fields(db):
    job = _make_job(db)
    transition_job(db, job.id, JobStatus.SCHEDULED)
    transition_job(db, job.id, JobStatus.RUNNING)
    failed = transition_job(
        db,
        job.id,
        JobStatus.FAILED,
        failure_code=FailureCode.OOM,
        failure_reason="Your job ran out of memory. Try requesting more memory or reducing batch size.",
    )
    assert failed.status == JobStatus.FAILED
    assert failed.failure_code == FailureCode.OOM
    assert failed.completed_at is not None


def test_illegal_transition_skip_scheduled(db):
    """PENDING -> RUNNING directly should be rejected; SCHEDULED can't be skipped."""
    job = _make_job(db)
    with pytest.raises(IllegalTransitionError):
        transition_job(db, job.id, JobStatus.RUNNING)


def test_illegal_transition_from_terminal_succeeded(db):
    """SUCCEEDED is terminal — nothing can transition out of it."""
    job = _make_job(db)
    transition_job(db, job.id, JobStatus.SCHEDULED)
    transition_job(db, job.id, JobStatus.RUNNING)
    transition_job(db, job.id, JobStatus.SUCCEEDED)
    with pytest.raises(IllegalTransitionError):
        transition_job(db, job.id, JobStatus.RUNNING)


def test_illegal_transition_from_terminal_failed(db):
    """FAILED is terminal — nothing can transition out of it."""
    job = _make_job(db)
    transition_job(db, job.id, JobStatus.SCHEDULED)
    transition_job(db, job.id, JobStatus.FAILED, failure_code=FailureCode.NODE_FAILURE)
    with pytest.raises(IllegalTransitionError):
        transition_job(db, job.id, JobStatus.RUNNING)


def test_transition_nonexistent_job_raises_not_found(db):
    with pytest.raises(JobNotFoundError):
        transition_job(db, "job-does-not-exist", JobStatus.SCHEDULED)


def test_every_status_can_reach_failed_except_terminal_states(db):
    """FAILED should be reachable from PENDING, SCHEDULED, and RUNNING."""
    for intermediate_steps in ([], [JobStatus.SCHEDULED], [JobStatus.SCHEDULED, JobStatus.RUNNING]):
        job = _make_job(db)
        for step in intermediate_steps:
            transition_job(db, job.id, step)
        failed = transition_job(db, job.id, JobStatus.FAILED, failure_code=FailureCode.NONZERO_EXIT)
        assert failed.status == JobStatus.FAILED
