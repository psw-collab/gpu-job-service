"""
Parsing and client-side validation for job.yaml config files.

Expected job.yaml shape:

    entrypoint: train.py
    requirements: requirements.txt
    python_version: "3.11"
    gpu_type: A100
    gpu_count: 2

`entrypoint` is a path to the user's Python script. The CLI sends the
filename (e.g. "train.py") to the API, not the file contents.

`requirements` is a path to a requirements.txt file. The CLI reads its
contents and sends the raw text to the API.

`requirements` is optional -- a job with no dependencies beyond the base
image is valid.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .constants import (
    ALLOWED_GPU_TYPES,
    ALLOWED_PYTHON_VERSIONS,
    MAX_GPU_COUNT,
    MIN_GPU_COUNT,
)


class JobConfigError(Exception):
    """Raised for any problem reading or validating a job.yaml file."""


@dataclass
class JobConfig:
    entrypoint_name: str
    entrypoint_content: str
    requirements_content: Optional[str]
    python_version: str
    gpu_type: str
    gpu_count: int

    def to_request_payload(self) -> dict:
        """Build the JSON body expected by POST /v1/jobs."""
        payload = {
            "entrypoint": self.entrypoint_name,
            "entrypoint_content": self.entrypoint_content,
            "python_version": self.python_version,
            "gpu_type": self.gpu_type,
            "gpu_count": self.gpu_count,
        }
        if self.requirements_content is not None:
            payload["requirements"] = self.requirements_content
        return payload


def _read_text_file(path: Path, yaml_path: Path, label: str) -> str:
    """
    Read a referenced file as UTF-8, turning encoding/OS errors into a clear
    JobConfigError instead of an unhandled traceback. utf-8-sig transparently
    strips a UTF-8 BOM; a UTF-16 file (e.g. produced by PowerShell's `>`) will
    fail with an actionable message.
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise JobConfigError(
            f"{yaml_path}: {label} file '{path.name}' is not valid UTF-8. "
            f"Re-save it as UTF-8 (in PowerShell: "
            f"Get-Content '{path.name}' | Out-File -Encoding utf8 '{path.name}')."
        ) from e
    except OSError as e:
        raise JobConfigError(
            f"{yaml_path}: could not read {label} file '{path}': {e}"
        ) from e


def _require_str(data: dict, field: str, yaml_path: Path) -> str:
    value = data.get(field)
    if value is None:
        raise JobConfigError(f"{yaml_path}: missing required field '{field}'")
    if not isinstance(value, str):
        raise JobConfigError(
            f"{yaml_path}: field '{field}' must be a string, got {type(value).__name__}"
        )
    return value


def load_job_config(yaml_path: Path) -> JobConfig:
    """
    Read and validate a job.yaml file, resolving and checking referenced
    files (entrypoint script, requirements file) relative to the yaml
    file's own directory.

    Raises JobConfigError on any validation failure, with a message that
    points at exactly what's wrong.
    """
    if not yaml_path.exists():
        raise JobConfigError(f"Config file not found: {yaml_path}")
    if not yaml_path.is_file():
        raise JobConfigError(f"Not a file: {yaml_path}")

    try:
        raw = yaml_path.read_text()
    except OSError as e:
        raise JobConfigError(f"Could not read {yaml_path}: {e}") from e

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise JobConfigError(f"{yaml_path}: invalid YAML\n{e}") from e

    if not isinstance(data, dict):
        raise JobConfigError(
            f"{yaml_path}: top-level YAML content must be a mapping of fields"
        )

    # entrypoint: required, must point to a real file
    entrypoint_str = _require_str(data, "entrypoint", yaml_path)
    entrypoint_path = (yaml_path.parent / entrypoint_str).resolve()
    if not entrypoint_path.exists():
        raise JobConfigError(
            f"{yaml_path}: entrypoint file not found: {entrypoint_str}"
        )
    if not entrypoint_path.is_file():
        raise JobConfigError(
            f"{yaml_path}: entrypoint is not a file: {entrypoint_str}"
        )

    # requirements: optional, but if present must point to a real file
    requirements_path = None
    if "requirements" in data and data["requirements"] is not None:
        requirements_str = _require_str(data, "requirements", yaml_path)
        requirements_path = (yaml_path.parent / requirements_str).resolve()
        if not requirements_path.exists():
            raise JobConfigError(
                f"{yaml_path}: requirements file not found: {requirements_str}"
            )
        if not requirements_path.is_file():
            raise JobConfigError(
                f"{yaml_path}: requirements is not a file: {requirements_str}"
            )

    # python_version: optional, defaults to schema's default of 3.11
    python_version = str(data.get("python_version", "3.11"))
    if python_version not in ALLOWED_PYTHON_VERSIONS:
        raise JobConfigError(
            f"{yaml_path}: unsupported python_version '{python_version}'. "
            f"Supported: {', '.join(sorted(ALLOWED_PYTHON_VERSIONS))}"
        )

    # gpu_type: optional, defaults to schema's default of A100
    gpu_type = str(data.get("gpu_type", "A100"))
    if gpu_type not in ALLOWED_GPU_TYPES:
        raise JobConfigError(
            f"{yaml_path}: unsupported gpu_type '{gpu_type}'. "
            f"Supported: {', '.join(sorted(ALLOWED_GPU_TYPES))}"
        )

    # gpu_count: optional, defaults to schema's default of 1
    gpu_count_raw = data.get("gpu_count", 1)
    try:
        gpu_count = int(gpu_count_raw)
    except (TypeError, ValueError):
        raise JobConfigError(
            f"{yaml_path}: gpu_count must be an integer, got {gpu_count_raw!r}"
        ) from None
    if not (MIN_GPU_COUNT <= gpu_count <= MAX_GPU_COUNT):
        raise JobConfigError(
            f"{yaml_path}: gpu_count must be between {MIN_GPU_COUNT} and "
            f"{MAX_GPU_COUNT}, got {gpu_count}"
        )

    entrypoint_content = _read_text_file(entrypoint_path, yaml_path, "entrypoint")
    requirements_content = None
    if requirements_path is not None:
        requirements_content = _read_text_file(
            requirements_path, yaml_path, "requirements"
        )

    return JobConfig(
        entrypoint_name=entrypoint_path.name,
        entrypoint_content=entrypoint_content,
        requirements_content=requirements_content,
        python_version=python_version,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
    )