"""
SQLAlchemy model for the `jobs` table.

This MUST stay in sync with migrations/00001_create_jobs.sql. If that
migration changes (new column, renamed column, new enum value), this file
needs a matching update in the same PR — per the Day 0 contract-freeze
agreement, schema changes go through a PR all three of us see.

Column types here are written to match the migration exactly:
  - id is TEXT, not UUID (matches existing migration)
  - artifact_location is the MinIO object key (matches existing migration,
    NOT script_path — that name was deliberately changed at Day 0)
  - status and failure_code are real Postgres ENUM types, not plain strings
"""

import enum
from datetime import datetime

from sqlalchemy import String, Integer, Text, DateTime, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from db import Base


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class FailureCode(str, enum.Enum):
    OOM = "oom"
    NODE_FAILURE = "node_failure"
    NONZERO_EXIT = "nonzero_exit"
    SCHEDULING_TIMEOUT = "scheduling_timeout"
    BUILD_FAILURE = "build_failure"


class DBJob(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String, nullable=False)

    status: Mapped[JobStatus] = mapped_column(
        SAEnum(
            JobStatus,
            name="job_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=JobStatus.PENDING,
    )
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # submission inputs
    artifact_location: Mapped[str] = mapped_column(Text, nullable=False)
    requirements_spec: Mapped[str | None] = mapped_column(Text, nullable=True)
    python_version: Mapped[str] = mapped_column(String, nullable=False)
    gpu_type: Mapped[str] = mapped_column(String, nullable=False)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # failure detail
    failure_code: Mapped[FailureCode | None] = mapped_column(
        SAEnum(
            FailureCode,
            name="failure_code",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=True,
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # retention
    retention_period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)

    # timestamps
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
