"""
Parsing and client-side validation for job.yaml config files.

Expected job.yaml shape (single-file, legacy mode):

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

Multi-file mode: add a `context` key pointing at a project directory
(resolved relative to the yaml file, like `entrypoint`/`requirements`):

    entrypoint: train.py
    requirements: requirements.txt
    context: .

When `context` is set, `entrypoint`/`requirements` are paths *relative to
that directory* rather than files read for their raw content -- the whole
directory gets packaged into a tar.gz archive and sent to the API instead,
which builds a container image from it (see the Kaniko design doc). Without
`context`, behavior is unchanged from the single-file mode above.
"""

import base64
import io
import tarfile
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


# Directories excluded when packaging a `context` directory into an archive.
# Not .gitignore-aware -- just a small, common-sense denylist.
_ARCHIVE_EXCLUDE_NAMES = {".git", "__pycache__", "venv", ".venv", "node_modules"}


@dataclass
class JobConfig:
    entrypoint_name: str
    entrypoint_content: Optional[str]
    requirements_content: Optional[str]
    python_version: str
    gpu_type: str
    gpu_count: int
    source_archive_b64: Optional[str] = None

    def to_request_payload(self) -> dict:
        """Build the JSON body expected by POST /v1/jobs."""
        payload = {
            "entrypoint": self.entrypoint_name,
            "python_version": self.python_version,
            "gpu_type": self.gpu_type,
            "gpu_count": self.gpu_count,
        }
        if self.source_archive_b64 is not None:
            payload["source_archive_b64"] = self.source_archive_b64
        else:
            payload["entrypoint_content"] = self.entrypoint_content
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


def _build_source_archive(context_path: Path) -> str:
    """Tar.gz `context_path` (excluding common noise dirs), base64-encoded for JSON transport."""

    def _filter(tarinfo: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
        if any(part in _ARCHIVE_EXCLUDE_NAMES for part in Path(tarinfo.name).parts):
            return None
        return tarinfo

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(context_path, arcname=".", filter=_filter)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def load_job_config(yaml_path: Path) -> JobConfig:
    """
    Read and validate a job.yaml file, resolving and checking referenced
    files (entrypoint script, requirements file, and optionally a project
    `context` directory) relative to the yaml file's own directory.

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

    # context: optional. If present, entrypoint/requirements are resolved
    # relative to it (and only checked for existence, not read for content) --
    # the whole directory is packaged into an archive instead.
    context_path = None
    if "context" in data and data["context"] is not None:
        context_str = _require_str(data, "context", yaml_path)
        context_path = (yaml_path.parent / context_str).resolve()
        if not context_path.exists():
            raise JobConfigError(f"{yaml_path}: context directory not found: {context_str}")
        if not context_path.is_dir():
            raise JobConfigError(f"{yaml_path}: context is not a directory: {context_str}")

    base_dir = context_path if context_path is not None else yaml_path.parent

    # entrypoint: required, must point to a real file (relative to base_dir)
    entrypoint_str = _require_str(data, "entrypoint", yaml_path)
    entrypoint_path = (base_dir / entrypoint_str).resolve()
    if not entrypoint_path.exists():
        raise JobConfigError(
            f"{yaml_path}: entrypoint file not found: {entrypoint_str}"
        )
    if not entrypoint_path.is_file():
        raise JobConfigError(
            f"{yaml_path}: entrypoint is not a file: {entrypoint_str}"
        )

    # requirements: optional, but if present must point to a real file (relative to base_dir)
    requirements_path = None
    requirements_str = None
    if "requirements" in data and data["requirements"] is not None:
        requirements_str = _require_str(data, "requirements", yaml_path)
        requirements_path = (base_dir / requirements_str).resolve()
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

    if context_path is not None:
        # Multi-file mode: entrypoint/requirements travel as relative paths
        # inside the archive, not as raw text.
        entrypoint_name = str(entrypoint_path.relative_to(context_path))
        requirements_content = (
            str(requirements_path.relative_to(context_path))
            if requirements_path is not None
            else None
        )
        entrypoint_content = None
        source_archive_b64 = _build_source_archive(context_path)
    else:
        entrypoint_name = entrypoint_path.name
        entrypoint_content = _read_text_file(entrypoint_path, yaml_path, "entrypoint")
        requirements_content = (
            _read_text_file(requirements_path, yaml_path, "requirements")
            if requirements_path is not None
            else None
        )
        source_archive_b64 = None

    return JobConfig(
        entrypoint_name=entrypoint_name,
        entrypoint_content=entrypoint_content,
        requirements_content=requirements_content,
        python_version=python_version,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        source_archive_b64=source_archive_b64,
    )
