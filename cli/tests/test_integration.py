"""
Integration test: drives the real CLI through api_client against the live
mock server (tests/mock_server.py) using FastAPI's TestClient.

This is the test that actually proves the CLI<->API contract end to end,
including that the server accepts `entrypoint_content`. It skips cleanly if
fastapi isn't installed (run `pip install -e ".[test]"` to enable it).
"""

import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

pytest.importorskip("fastapi", reason="install test extras: pip install -e '.[test]'")

from fastapi.testclient import TestClient  # noqa: E402

from gpujob_cli import api_client  # noqa: E402
from gpujob_cli.cli import app  # noqa: E402
from tests import mock_server  # noqa: E402
from tests.mock_server import app as mock_app  # noqa: E402

runner = CliRunner()


@pytest.fixture
def route_cli_to_mock(monkeypatch):
    """Send the CLI's httpx calls into the in-process mock server."""
    client = TestClient(mock_app)

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        path = url.split("9002", 1)[1] if "9002" in url else url
        return client.post(path, json=json, headers=headers or {})

    def fake_get(url, headers=None, timeout=None, **kwargs):
        path = url.split("9002", 1)[1] if "9002" in url else url
        return client.get(path, headers=headers or {})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setenv("GPUJOB_API_URL", "http://localhost:9002")
    return client


def _make_job(tmp_path: Path) -> Path:
    (tmp_path / "train.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("numpy==1.26.0\n", encoding="utf-8")
    yaml_path = tmp_path / "job.yaml"
    yaml_path.write_text(
        "entrypoint: train.py\n"
        "requirements: requirements.txt\n"
        "gpu_type: A100\n"
        "gpu_count: 2\n",
        encoding="utf-8",
    )
    return yaml_path


def test_submit_then_status_end_to_end(tmp_path, route_cli_to_mock):
    submit = runner.invoke(app, ["submit", "-f", str(_make_job(tmp_path))])
    assert submit.exit_code == 0, submit.output
    assert "job-" in submit.stdout

    job_id = submit.stdout.split("job-", 1)[1].strip().split()[0]
    status = runner.invoke(app, ["status", f"job-{job_id}"])
    assert status.exit_code == 0, status.output
    assert "PENDING" in status.stdout
    assert "train.py" in status.stdout


def test_mock_requires_entrypoint_content(route_cli_to_mock):
    """The mock must reject a payload missing entrypoint_content (real contract)."""
    resp = route_cli_to_mock.post("/v1/jobs", json={"entrypoint": "train.py"})
    assert resp.status_code == 422


def test_status_unknown_job_is_404(route_cli_to_mock):
    result = runner.invoke(app, ["status", "job-nope"])
    assert result.exit_code == 1
    assert "No job found" in result.output


def _seed(job_id, status, logs=None):
    """Put a job straight into the mock's store for log-path tests."""
    from datetime import datetime, timezone
    mock_server._jobs[job_id] = {
        "id": job_id,
        "status": status,
        "status_message": status.title(),
        "entrypoint": "train.py",
        "python_version": "3.11",
        "gpu_type": "A100",
        "gpu_count": 1,
        "failure_reason": None,
        "submitted_at": datetime.now(timezone.utc),
        "started_at": None,
        "completed_at": None,
        "logs": logs,
    }


def test_logs_oneshot_end_to_end(route_cli_to_mock):
    _seed("job-done", "SUCCEEDED", logs="epoch 1 loss=0.5\nepoch 2 loss=0.3\n")
    result = runner.invoke(app, ["logs", "job-done"])
    assert result.exit_code == 0, result.output
    assert "epoch 1 loss=0.5" in result.stdout
    assert "epoch 2 loss=0.3" in result.stdout


def test_logs_oneshot_not_ready_is_409(route_cli_to_mock):
    _seed("job-pending", "PENDING", logs=None)
    result = runner.invoke(app, ["logs", "job-pending"])
    assert result.exit_code == 1
    assert "not available yet" in result.output.lower()


def test_logs_follow_end_to_end_terminal(route_cli_to_mock, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    _seed("job-fin", "SUCCEEDED", logs="all done\n")
    result = runner.invoke(app, ["logs", "-f", "job-fin"])
    assert result.exit_code == 0, result.output
    assert "all done" in result.stdout
