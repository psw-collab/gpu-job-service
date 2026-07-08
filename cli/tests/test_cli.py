"""
Tests for the CLI commands themselves: argument wiring, output, and exit codes.

api_client is monkeypatched so these run offline and don't need a live API.
"""

from pathlib import Path

from typer.testing import CliRunner

from gpujob_cli import api_client
from gpujob_cli.cli import app

runner = CliRunner()


def _make_job(tmp_path: Path) -> Path:
    (tmp_path / "train.py").write_text("print('hi')\n", encoding="utf-8")
    yaml_path = tmp_path / "job.yaml"
    yaml_path.write_text(
        "entrypoint: train.py\ngpu_type: A100\ngpu_count: 1\n", encoding="utf-8"
    )
    return yaml_path


def test_submit_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        api_client, "submit_job",
        lambda base_url, payload: {"job_id": "job-abc123", "status": "PENDING"},
    )
    result = runner.invoke(app, ["submit", "-f", str(_make_job(tmp_path))])
    assert result.exit_code == 0
    assert "job-abc123" in result.stdout


def test_submit_bad_config_exits_1(tmp_path):
    # job.yaml references a file that doesn't exist -> clean error, exit 1
    yaml_path = tmp_path / "job.yaml"
    yaml_path.write_text("entrypoint: missing.py\ngpu_type: A100\n", encoding="utf-8")
    result = runner.invoke(app, ["submit", "-f", str(yaml_path)])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_submit_api_error_exits_1(tmp_path, monkeypatch):
    def boom(base_url, payload):
        raise api_client.ApiError("Could not connect to the API")

    monkeypatch.setattr(api_client, "submit_job", boom)
    result = runner.invoke(app, ["submit", "-f", str(_make_job(tmp_path))])
    assert result.exit_code == 1
    assert "Could not connect" in result.output


def test_status_success(monkeypatch):
    monkeypatch.setattr(
        api_client, "get_job_status",
        lambda base_url, job_id: {
            "id": job_id,
            "status": "SUCCEEDED",
            "status_message": "done",
            "entrypoint": "train.py",
            "python_version": "3.11",
            "gpu_type": "A100",
            "gpu_count": 1,
            "submitted_at": "2026-07-02T10:00:00Z",
        },
    )
    result = runner.invoke(app, ["status", "job-abc123"])
    assert result.exit_code == 0
    assert "SUCCEEDED" in result.stdout
    assert "job-abc123" in result.stdout


def test_status_not_found_exits_1(monkeypatch):
    def missing(base_url, job_id):
        raise api_client.ApiError("No job found with ID 'job-nope'.")

    monkeypatch.setattr(api_client, "get_job_status", missing)
    result = runner.invoke(app, ["status", "job-nope"])
    assert result.exit_code == 1
    assert "No job found" in result.output
