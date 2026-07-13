from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func

from database import Base


class DBJob(Base):
    __tablename__ = "jobs"

    id = Column(String(20), primary_key=True)
    status = Column(String(20), nullable=False, default="PENDING")
    status_message = Column(Text, nullable=True)
    entrypoint = Column(String(255), nullable=False)
    entrypoint_content = Column(Text, nullable=False)
    requirements = Column(Text, nullable=True)
    python_version = Column(String(10), nullable=False, default="3.11")
    gpu_type = Column(String(20), nullable=False)
    gpu_count = Column(Integer, nullable=False, default=1)
    image_tag = Column(String(500), nullable=True)
    requirements_hash = Column(String(64), nullable=True)
    failure_reason = Column(Text, nullable=True)
    logs = Column(Text, nullable=True)
    submitted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)