"""Tests for the `list` and `cancel` commands: client, CLI, and end-to-end."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from gpujob_cli import api_client
from gpujob_cli.cli import app

runner = CliRunner()


# --------------------------------------------------------------------------
# api_client layer (fake httpx)
# --------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no json")
        return self._json_body


def test_list_jobs_success(monkeypatch):
    def fake_get(url, params=None, timeout=None, **kw):
        assert url.endswith("/v1/jobs")
        return FakeResponse(200, {"jobs": [{"id": "job-1", "status": "RUNNING"}]})

    monkeypatch.setattr(httpx, "get", fake_get)
    result = api_client.list_jobs("http://x:9002")
    assert result["jobs"][0]["id"] == "job-1"


def test_list_jobs_passes_status_filter(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None, **kw):
        captured["params"] = params
        return FakeResponse(200, {"jobs": []})

    monkeypatch.setattr(httpx, "get", fake_get)
    api_client.list_jobs("http://x:9002", status="RUNNING")
    assert captured["params"] == {"status": "RUNNING"}


def test_cancel_job_success(monkeypatch):
    def fake_post(url, timeout=None, **kw):
        assert url.endswith("/v1/jobs/job-1/cancel")
        return FakeResponse(200, {"id": "job-1", "status": "CANCELLED"})

    monkeypatch.setattr(httpx, "post", fake_post)
    result = api_client.cancel_job("http://x:9002", "job-1")
    assert result["status"] == "CANCELLED"


def test_cancel_job_404(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, timeout=None, **kw: FakeResponse(404))
    with pytest.raises(api_client.ApiError, match="No job found with ID 'job-nope'"):
        api_client.cancel_job("http://x:9002", "job-nope")


def test_cancel_job_409_is_surfaced(monkeypatch):
    def fake_post(url, timeout=None, **kw):
        return FakeResponse(409, {"detail": "Cannot cancel a job that is already SUCCEEDED"})

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(api_client.ApiError, match="already SUCCEEDED"):
        api_client.cancel_job("http://x:9002", "job-1")


# --------------------------------------------------------------------------
# CLI layer (fake api_client)
# --------------------------------------------------------------------------
def test_list_table_output(monkeypatch):
    monkeypatch.setattr(
        api_client, "list_jobs",
        lambda base_url, status=None: {
            "jobs": [
                {"id": "job-1", "status": "RUNNING", "gpu_type": "A100", "gpu_count": 2,
                 "submitted_at": "2026-07-02T10:00:00Z"},
            ]
        },
    )
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "job-1" in result.stdout
    assert "RUNNING" in result.stdout


def test_list_json_output_is_parseable(monkeypatch):
    payload = {"jobs": [{"id": "job-1", "status": "PENDING", "gpu_type": "A100",
                         "gpu_count": 1, "submitted_at": "2026-07-02T10:00:00Z"}]}
    monkeypatch.setattr(api_client, "list_jobs", lambda base_url, status=None: payload)
    result = runner.invoke(app, ["list", "--output", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["jobs"][0]["id"] == "job-1"


def test_list_empty(monkeypatch):
    monkeypatch.setattr(api_client, "list_jobs", lambda base_url, status=None: {"jobs": []})
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No jobs found" in result.stdout


def test_list_bad_output_flag(monkeypatch):
    monkeypatch.setattr(api_client, "list_jobs", lambda base_url, status=None: {"jobs": []})
    result = runner.invoke(app, ["list", "--output", "xml"])
    assert result.exit_code == 2
    assert "unknown --output" in result.output


def test_cancel_success(monkeypatch):
    monkeypatch.setattr(
        api_client, "cancel_job",
        lambda base_url, job_id: {"id": job_id, "status": "CANCELLED"},
    )
    result = runner.invoke(app, ["cancel", "job-1"])
    assert result.exit_code == 0
    assert "CANCELLED" in result.stdout
    assert "job-1" in result.stdout


def test_cancel_unknown_exits_1(monkeypatch):
    def boom(base_url, job_id):
        raise api_client.ApiError("No job found with ID 'job-nope'.")

    monkeypatch.setattr(api_client, "cancel_job", boom)
    result = runner.invoke(app, ["cancel", "job-nope"])
    assert result.exit_code == 1
    assert "No job found" in result.output


# --------------------------------------------------------------------------
# end-to-end against the live mock server
# --------------------------------------------------------------------------
pytest.importorskip("fastapi", reason="install test extras: pip install -e '.[test]'")

from fastapi.testclient import TestClient  # noqa: E402
from tests.mock_server import app as mock_app  # noqa: E402


@pytest.fixture
def route_cli_to_mock(monkeypatch):
    client = TestClient(mock_app)

    def _path(url):
        return url.split("9002", 1)[1] if "9002" in url else url

    monkeypatch.setattr(httpx, "post",
                        lambda url, json=None, timeout=None, **kw: client.post(_path(url), json=json))
    monkeypatch.setattr(httpx, "get",
                        lambda url, params=None, timeout=None, **kw: client.get(_path(url), params=params))
    monkeypatch.setenv("GPUJOB_API_URL", "http://localhost:9002")
    return client


def _submit_a_job(tmp_path, route_cli_to_mock):
    (tmp_path / "train.py").write_text("print('hi')\n", encoding="utf-8")
    yaml_path = tmp_path / "job.yaml"
    yaml_path.write_text("entrypoint: train.py\ngpu_type: A100\n", encoding="utf-8")
    out = runner.invoke(app, ["submit", "-f", str(yaml_path)])
    assert out.exit_code == 0, out.output
    return "job-" + out.stdout.split("job-", 1)[1].strip().split()[0]


def test_submit_list_cancel_end_to_end(tmp_path, route_cli_to_mock):
    job_id = _submit_a_job(tmp_path, route_cli_to_mock)

    listed = runner.invoke(app, ["list", "--output", "json"])
    assert listed.exit_code == 0
    ids = [j["id"] for j in json.loads(listed.stdout)["jobs"]]
    assert job_id in ids

    cancelled = runner.invoke(app, ["cancel", job_id])
    assert cancelled.exit_code == 0
    assert "CANCELLED" in cancelled.stdout

    after = runner.invoke(app, ["list", "--output", "json"])
    states = {j["id"]: j["status"] for j in json.loads(after.stdout)["jobs"]}
    assert states[job_id] == "CANCELLED"


def test_cancel_twice_is_409(tmp_path, route_cli_to_mock):
    job_id = _submit_a_job(tmp_path, route_cli_to_mock)
    assert runner.invoke(app, ["cancel", job_id]).exit_code == 0
    # second cancel: job is already terminal -> 409 -> clean error, exit 1
    second = runner.invoke(app, ["cancel", job_id])
    assert second.exit_code == 1
    assert "already CANCELLED" in second.output
