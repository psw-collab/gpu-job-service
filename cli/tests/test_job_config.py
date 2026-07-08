"""Tests for loading and validating job.yaml config files."""

from pathlib import Path

import pytest

from gpujob_cli.job_config import JobConfigError, load_job_config


def _write_job(
    tmp_path: Path,
    *,
    yaml_body: str | None = None,
    entrypoint: str | None = "train.py",
    entrypoint_bytes: bytes | None = None,
    requirements: str | None = "requirements.txt",
) -> Path:
    """Create a job.yaml (plus referenced files) in tmp_path and return its path."""
    if entrypoint is not None:
        if entrypoint_bytes is not None:
            (tmp_path / entrypoint).write_bytes(entrypoint_bytes)
        else:
            (tmp_path / entrypoint).write_text("print('hi')\n", encoding="utf-8")
    if requirements is not None:
        (tmp_path / requirements).write_text("numpy==1.26.0\n", encoding="utf-8")

    if yaml_body is None:
        yaml_body = (
            "entrypoint: train.py\n"
            "requirements: requirements.txt\n"
            'python_version: "3.11"\n'
            "gpu_type: A100\n"
            "gpu_count: 2\n"
        )
    yaml_path = tmp_path / "job.yaml"
    yaml_path.write_text(yaml_body, encoding="utf-8")
    return yaml_path


def test_valid_config_builds_expected_payload(tmp_path):
    cfg = load_job_config(_write_job(tmp_path))
    payload = cfg.to_request_payload()

    assert payload["entrypoint"] == "train.py"
    assert payload["entrypoint_content"] == "print('hi')\n"
    assert payload["requirements"] == "numpy==1.26.0\n"
    assert payload["python_version"] == "3.11"
    assert payload["gpu_type"] == "A100"
    assert payload["gpu_count"] == 2


def test_requirements_optional(tmp_path):
    yaml_body = "entrypoint: train.py\ngpu_type: A100\n"
    cfg = load_job_config(_write_job(tmp_path, yaml_body=yaml_body, requirements=None))
    payload = cfg.to_request_payload()

    assert "requirements" not in payload
    # defaults fill in
    assert payload["python_version"] == "3.11"
    assert payload["gpu_count"] == 1


def test_missing_yaml_file(tmp_path):
    with pytest.raises(JobConfigError, match="not found"):
        load_job_config(tmp_path / "does_not_exist.yaml")


def test_missing_entrypoint_field(tmp_path):
    with pytest.raises(JobConfigError, match="entrypoint"):
        load_job_config(
            _write_job(tmp_path, yaml_body="gpu_type: A100\n", entrypoint=None)
        )


def test_entrypoint_file_missing(tmp_path):
    yaml_body = "entrypoint: nope.py\ngpu_type: A100\n"
    with pytest.raises(JobConfigError, match="entrypoint file not found"):
        load_job_config(_write_job(tmp_path, yaml_body=yaml_body, entrypoint=None))


def test_bad_gpu_type(tmp_path):
    yaml_body = "entrypoint: train.py\ngpu_type: V100\n"
    with pytest.raises(JobConfigError, match="gpu_type"):
        load_job_config(_write_job(tmp_path, yaml_body=yaml_body))


def test_bad_python_version(tmp_path):
    yaml_body = 'entrypoint: train.py\npython_version: "2.7"\ngpu_type: A100\n'
    with pytest.raises(JobConfigError, match="python_version"):
        load_job_config(_write_job(tmp_path, yaml_body=yaml_body))


@pytest.mark.parametrize("count", ["0", "9", "-1"])
def test_gpu_count_out_of_range(tmp_path, count):
    yaml_body = f"entrypoint: train.py\ngpu_type: A100\ngpu_count: {count}\n"
    with pytest.raises(JobConfigError, match="gpu_count must be between"):
        load_job_config(_write_job(tmp_path, yaml_body=yaml_body))


def test_gpu_count_not_an_int(tmp_path):
    yaml_body = "entrypoint: train.py\ngpu_type: A100\ngpu_count: two\n"
    with pytest.raises(JobConfigError, match="gpu_count must be an integer"):
        load_job_config(_write_job(tmp_path, yaml_body=yaml_body))


def test_top_level_not_a_mapping(tmp_path):
    with pytest.raises(JobConfigError, match="mapping"):
        load_job_config(_write_job(tmp_path, yaml_body="- just\n- a\n- list\n"))


def test_utf16_entrypoint_gives_clean_error(tmp_path):
    """A UTF-16 file (PowerShell `>` artifact) must fail cleanly, not traceback."""
    utf16 = "print('hi')\n".encode("utf-16")  # includes BOM
    yaml_body = "entrypoint: train.py\ngpu_type: A100\n"
    with pytest.raises(JobConfigError, match="not valid UTF-8"):
        load_job_config(
            _write_job(tmp_path, yaml_body=yaml_body, entrypoint_bytes=utf16)
        )


def test_utf8_bom_entrypoint_is_accepted(tmp_path):
    """A UTF-8 file *with* a BOM should still load (utf-8-sig strips it)."""
    utf8_bom = "print('hi')\n".encode("utf-8-sig")
    yaml_body = "entrypoint: train.py\ngpu_type: A100\n"
    cfg = load_job_config(
        _write_job(tmp_path, yaml_body=yaml_body, entrypoint_bytes=utf8_bom)
    )
    assert cfg.to_request_payload()["entrypoint_content"] == "print('hi')\n"
