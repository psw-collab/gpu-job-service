from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


ALLOWED_GPU_TYPES = {"A100", "H100"}
ALLOWED_PYTHON_VERSIONS = {"3.11", "3.12", "3.13"}


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