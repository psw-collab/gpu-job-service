"""
Integration test: drives the real CLI through api_client against the live
mock server (tests/mock_server.py) using FastAPI's TestClient.

This is the test that actually proves the CLI<->API contract end to end,
including that the server accepts `entrypoint_content`. It skips cleanly if
fastapi isn't installed (run `pip install -e ".[test]"` to enable it).
"""

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

pytest.importorskip("fastapi", reason="install test extras: pip install -e '.[test]'")

from fastapi.testclient import TestClient  # noqa: E402

from gpujob_cli import api_client  # noqa: E402
from gpujob_cli.cli import app  # noqa: E402
from tests.mock_server import app as mock_app  # noqa: E402

runner = CliRunner()


@pytest.fixture
def route_cli_to_mock(monkeypatch):
    """Send the CLI's httpx calls into the in-process mock server."""
    client = TestClient(mock_app)

    def fake_post(url, json, timeout):
        path = url.split("9002", 1)[1] if "9002" in url else url
        return client.post(path, json=json)

    def fake_get(url, timeout):
        path = url.split("9002", 1)[1] if "9002" in url else url
        return client.get(path)

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
