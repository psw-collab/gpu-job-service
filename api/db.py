"""
Database connection setup: SQLAlchemy engine, session factory, and the
FastAPI dependency used to inject a DB session into route handlers.

Connection string is read from the DATABASE_URL environment variable so
each teammate can point at their own local Postgres without editing code.
Defaults to Prathamesh's local docker-compose setup if unset.
"""

import os
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://myapp_user:myapp_password@172.23.18.129:5433/gpu_job_service_db",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models in this project."""
    pass


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency. Yields a DB session and guarantees it's closed
    after the request, even if an exception is raised mid-request.

    Usage in a route:
        @app.get("/jobs/{id}")
        def read_job(id: str, db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
