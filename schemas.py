from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


ALLOWED_GPU_TYPES = {"A100", "H100"}
ALLOWED_PYTHON_VERSIONS = {"3.11", "3.12", "3.13"}


ENTRYPOINT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.py$"


class JobSubmitRequest(BaseModel):
    entrypoint: str = Field(pattern=ENTRYPOINT_PATTERN)
    # Exactly one of these two must be set: entrypoint_content for the
    # legacy single-file mode (raw script text), source_archive_b64 for the
    # multi-file mode (base64-encoded tar.gz of a whole project directory,
    # built into an image via Kaniko -- see kaniko.md). Validated in gateway.py.
    entrypoint_content: Optional[str] = None
    source_archive_b64: Optional[str] = None
    requirements: Optional[str] = None
    python_version: str = "3.11"
    gpu_type: str = "A100"
    gpu_count: int = Field(default=1, ge=1, le=8)


class JobSubmitResponse(BaseModel):
    job_id: str
    status: str


class JobReport(BaseModel):
    """Body for POST /internal/jobs/{job_id}/report -- how the worker tells the
    gateway about a status transition, without ever touching Postgres itself."""
    status: str
    status_message: Optional[str] = None
    failure_reason: Optional[str] = None
    logs: Optional[str] = None
    image_tag: Optional[str] = None
    started_at: Optional[datetime] = None
    scheduled_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


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