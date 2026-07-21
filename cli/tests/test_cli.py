"""
Tests for the CLI commands themselves: argument wiring, output, and exit codes.

api_client is monkeypatched so these run offline and don't need a live API.
"""

import time
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
        lambda base_url, payload, **kwargs: {"job_id": "job-abc123", "status": "PENDING"},
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
    def boom(base_url, payload, **kwargs):
        raise api_client.ApiError("Could not connect to the API")

    monkeypatch.setattr(api_client, "submit_job", boom)
    result = runner.invoke(app, ["submit", "-f", str(_make_job(tmp_path))])
    assert result.exit_code == 1
    assert "Could not connect" in result.output


def test_status_success(monkeypatch):
    monkeypatch.setattr(
        api_client, "get_job_status",
        lambda base_url, job_id, **kwargs: {
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
    def missing(base_url, job_id, **kwargs):
        raise api_client.ApiError("No job found with ID 'job-nope'.", status_code=404)

    monkeypatch.setattr(api_client, "get_job_status", missing)
    result = runner.invoke(app, ["status", "job-nope"])
    assert result.exit_code == 1
    assert "No job found" in result.output


# --- logs (one-shot) ---

def test_logs_oneshot_prints_text(monkeypatch):
    monkeypatch.setattr(
        api_client, "get_job_logs",
        lambda base_url, job_id, **kwargs: "hello from the pod\n",
    )
    result = runner.invoke(app, ["logs", "job-abc123"])
    assert result.exit_code == 0
    assert "hello from the pod" in result.stdout


def test_logs_oneshot_error_exits_1(monkeypatch):
    def boom(base_url, job_id, **kwargs):
        raise api_client.ApiError("No job found with ID 'job-nope'.", status_code=404)

    monkeypatch.setattr(api_client, "get_job_logs", boom)
    result = runner.invoke(app, ["logs", "job-nope"])
    assert result.exit_code == 1
    assert "No job found" in result.output


# --- logs --follow ---

def _scripted(values):
    """Return a callable that yields successive values, repeating the last."""
    box = {"i": 0}

    def _next(*args, **kwargs):
        i = box["i"]
        val = values[min(i, len(values) - 1)]
        box["i"] = i + 1
        if isinstance(val, Exception):
            raise val
        return val

    return _next


def test_logs_follow_streams_deltas_until_terminal(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    # status: RUNNING, RUNNING, then SUCCEEDED (stop)
    monkeypatch.setattr(
        api_client, "get_job_status",
        _scripted([
            {"status": "RUNNING"},
            {"status": "RUNNING"},
            {"status": "SUCCEEDED"},
        ]),
    )
    # logs grow on each poll; only the newly-appended tail should print
    monkeypatch.setattr(
        api_client, "get_job_logs",
        _scripted([
            "line1\n",
            "line1\nline2\n",
            "line1\nline2\nline3\n",
        ]),
    )

    result = runner.invoke(app, ["logs", "-f", "job-abc123"])
    assert result.exit_code == 0
    out = result.stdout
    assert "line1" in out and "line2" in out and "line3" in out
    # delta printing: each line appears exactly once, no re-printing of the whole buffer
    assert out.count("line1") == 1
    assert out.count("line2") == 1
    assert out.count("line3") == 1


def test_logs_follow_waits_through_409_then_no_logs(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    monkeypatch.setattr(
        api_client, "get_job_status",
        _scripted([{"status": "PENDING"}, {"status": "SUCCEEDED"}]),
    )
    monkeypatch.setattr(
        api_client, "get_job_logs",
        _scripted([
            api_client.ApiError("not ready", status_code=409),
            api_client.ApiError("none captured", status_code=404),
        ]),
    )

    result = runner.invoke(app, ["logs", "-f", "job-abc123"])
    assert result.exit_code == 0
    assert "no logs were captured" in result.output.lower()


def test_logs_follow_status_error_exits_1(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    def boom(*args, **kwargs):
        raise api_client.ApiError("No job found with ID 'job-nope'.", status_code=404)

    monkeypatch.setattr(api_client, "get_job_status", boom)
    result = runner.invoke(app, ["logs", "-f", "job-nope"])
    assert result.exit_code == 1
    assert "No job found" in result.output


# --- outputs command ---


def _outputs_stub(files):
    def _stub(base_url, job_id, api_key=None, identity_token=None):
        return {"job_id": job_id, "outputs": files}
    return _stub


def test_outputs_list(monkeypatch):
    monkeypatch.setattr(api_client, "get_job_outputs", _outputs_stub(
        [{"path": "models/best.pt", "size_bytes": 2048, "url": "http://o/1", "expires_at": "z"}]
    ))
    result = runner.invoke(app, ["outputs", "job-abc"])
    assert result.exit_code == 0, result.output
    assert "models/best.pt" in result.stdout
    assert "2.0 KB" in result.stdout


def test_outputs_empty(monkeypatch):
    monkeypatch.setattr(api_client, "get_job_outputs", _outputs_stub([]))
    result = runner.invoke(app, ["outputs", "job-abc"])
    assert result.exit_code == 0
    assert "no output files" in result.stdout.lower()


def test_outputs_not_found_exits_1(monkeypatch):
    def boom(base_url, job_id, api_key=None, identity_token=None):
        raise api_client.ApiError("No job found with ID 'job-nope'.", status_code=404)

    monkeypatch.setattr(api_client, "get_job_outputs", boom)
    result = runner.invoke(app, ["outputs", "job-nope"])
    assert result.exit_code == 1
    assert "No job found" in result.output


def test_outputs_download(monkeypatch, tmp_path):
    monkeypatch.setattr(api_client, "get_job_outputs", _outputs_stub(
        [{"path": "models/best.pt", "size_bytes": 3, "url": "http://o/1", "expires_at": "z"}]
    ))

    def fake_dl(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"abc")

    monkeypatch.setattr(api_client, "download_output", fake_dl)
    result = runner.invoke(app, ["outputs", "job-abc", "--download", "--dest", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "models" / "best.pt").read_bytes() == b"abc"
    assert "Downloaded" in result.stdout


def test_outputs_download_rejects_path_traversal(monkeypatch, tmp_path):
    monkeypatch.setattr(api_client, "get_job_outputs", _outputs_stub(
        [{"path": "../evil.txt", "size_bytes": 3, "url": "http://o/1", "expires_at": "z"}]
    ))
    called = []
    monkeypatch.setattr(api_client, "download_output", lambda url, dest: called.append(dest))
    result = runner.invoke(app, ["outputs", "job-abc", "--download", "--dest", str(tmp_path)])
    assert result.exit_code == 0
    assert called == []  # traversal entry skipped, never downloaded
    assert "suspicious path" in result.output.lower()


def test_outputs_still_running_409(monkeypatch):
    def boom(base_url, job_id, api_key=None, identity_token=None):
        raise api_client.ApiError("Outputs are not available until the job completes.",
                                  status_code=409)

    monkeypatch.setattr(api_client, "get_job_outputs", boom)
    result = runner.invoke(app, ["outputs", "job-running"])
    assert result.exit_code == 1
    assert "still running" in result.output.lower()


def test_outputs_download_default_dest_is_job_id(monkeypatch, tmp_path):
    monkeypatch.setattr(api_client, "get_job_outputs", _outputs_stub(
        [{"path": "models/best.pt", "size_bytes": 3, "url": "http://o/1", "expires_at": "z"}]
    ))
    seen = []
    monkeypatch.setattr(api_client, "download_output", lambda url, dest: seen.append(dest))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["outputs", "job-abc", "--download"])
    assert result.exit_code == 0, result.output
    # No --dest given: files should land under ./job-abc/
    assert seen and (tmp_path / "job-abc").resolve() in seen[0].parents
