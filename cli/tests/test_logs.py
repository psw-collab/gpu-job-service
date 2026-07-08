"""Tests for the `logs` command: client, snapshot, --follow, and failure path."""

import httpx
import pytest
from typer.testing import CliRunner

import gpujob_cli.cli as cli_module
from gpujob_cli import api_client
from gpujob_cli.cli import app

runner = CliRunner()


class FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no json")
        return self._json_body


# --- api_client layer -------------------------------------------------------
def test_get_job_logs_success(monkeypatch):
    def fake_get(url, params=None, timeout=None, **kw):
        assert url.endswith("/v1/jobs/job-1/logs")
        assert params == {"since": 0}
        return FakeResponse(200, {
            "job_id": "job-1", "status": "RUNNING",
            "logs": "epoch 1\n", "next_since": 1,
        })

    monkeypatch.setattr(httpx, "get", fake_get)
    result = api_client.get_job_logs("http://x:9002", "job-1", since=0)
    assert result["logs"] == "epoch 1\n"
    assert result["next_since"] == 1


def test_get_job_logs_404(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, params=None, timeout=None, **kw: FakeResponse(404))
    with pytest.raises(api_client.ApiError, match="No job found with ID 'job-nope'"):
        api_client.get_job_logs("http://x:9002", "job-nope")


# --- CLI layer: snapshot ----------------------------------------------------
def test_logs_snapshot_prints_and_exits(monkeypatch):
    monkeypatch.setattr(
        api_client, "get_job_logs",
        lambda base_url, job_id, since=0: {
            "job_id": job_id, "status": "RUNNING",
            "logs": "line A\nline B\n", "next_since": 2,
        },
    )
    result = runner.invoke(app, ["logs", "job-1"])
    assert result.exit_code == 0
    assert "line A" in result.stdout
    assert "line B" in result.stdout


# --- CLI layer: follow polls until terminal ---------------------------------
def test_logs_follow_streams_until_terminal(monkeypatch):
    monkeypatch.setattr(cli_module, "LOGS_POLL_INTERVAL", 0)
    responses = [
        {"job_id": "job-1", "status": "RUNNING", "logs": "start\n", "next_since": 1},
        {"job_id": "job-1", "status": "RUNNING", "logs": "middle\n", "next_since": 2},
        {"job_id": "job-1", "status": "SUCCEEDED", "logs": "done\n", "next_since": 3},
    ]
    calls = {"n": 0}

    def fake_logs(base_url, job_id, since=0):
        r = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return r

    monkeypatch.setattr(api_client, "get_job_logs", fake_logs)
    result = runner.invoke(app, ["logs", "job-1", "--follow"])
    assert result.exit_code == 0
    for expected in ("start", "middle", "done", "SUCCEEDED"):
        assert expected in result.output


def test_logs_follow_shows_failure_reason(monkeypatch):
    monkeypatch.setattr(cli_module, "LOGS_POLL_INTERVAL", 0)
    monkeypatch.setattr(
        api_client, "get_job_logs",
        lambda base_url, job_id, since=0: {
            "job_id": job_id, "status": "FAILED",
            "failure_reason": "scheduling_timeout",
            "logs": "boom\n", "next_since": 1,
        },
    )
    result = runner.invoke(app, ["logs", "job-1", "--follow"])
    assert result.exit_code == 0
    assert "boom" in result.output
    assert "scheduling_timeout" in result.output


# --- end-to-end against the live mock, driven through states ----------------
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


def _submit(tmp_path):
    (tmp_path / "train.py").write_text("print('hi')\n", encoding="utf-8")
    yaml_path = tmp_path / "job.yaml"
    yaml_path.write_text("entrypoint: train.py\ngpu_type: A100\n", encoding="utf-8")
    out = runner.invoke(app, ["submit", "-f", str(yaml_path)])
    assert out.exit_code == 0, out.output
    return "job-" + out.stdout.split("job-", 1)[1].strip().split()[0]


def test_logs_end_to_end_failed_job(tmp_path, route_cli_to_mock):
    job_id = _submit(tmp_path)
    # Drive the job the way the real executor would: running -> logs -> FAILED.
    route_cli_to_mock.post(f"/_test/jobs/{job_id}", json={"status": "RUNNING", "append_log": "epoch 1 loss=0.9"})
    route_cli_to_mock.post(f"/_test/jobs/{job_id}", json={"append_log": "CUDA out of memory"})
    route_cli_to_mock.post(f"/_test/jobs/{job_id}", json={"status": "FAILED", "failure_reason": "oom_killed"})

    result = runner.invoke(app, ["logs", job_id])
    assert result.exit_code == 0
    assert "epoch 1 loss=0.9" in result.stdout
    assert "CUDA out of memory" in result.stdout

    # status command should surface the failure reason too
    st = runner.invoke(app, ["status", job_id])
    assert "FAILED" in st.stdout
    assert "oom_killed" in st.stdout
