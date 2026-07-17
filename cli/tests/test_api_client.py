"""Tests for the api_client HTTP layer, using a fake httpx transport."""

import httpx
import pytest

from gpujob_cli import api_client


class FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no json")
        return self._json_body


def test_submit_job_success(monkeypatch):
    def fake_post(url, json, timeout, headers=None, **kwargs):
        assert url.endswith("/v1/jobs")
        assert json["entrypoint"] == "train.py"
        return FakeResponse(200, {"job_id": "job-abc123", "status": "PENDING"})

    monkeypatch.setattr(httpx, "post", fake_post)
    result = api_client.submit_job("http://x:9002", {"entrypoint": "train.py"})
    assert result["job_id"] == "job-abc123"


def test_submit_job_sends_api_key_header(monkeypatch):
    seen = {}

    def fake_post(url, json, timeout, headers=None, **kwargs):
        seen["headers"] = headers or {}
        return FakeResponse(200, {"job_id": "job-abc123", "status": "PENDING"})

    monkeypatch.setattr(httpx, "post", fake_post)
    api_client.submit_job("http://x:9002", {}, api_key="secret-key")
    assert seen["headers"].get("X-Api-Key") == "secret-key"


def test_submit_job_connect_error(monkeypatch):
    def fake_post(url, json, timeout, headers=None, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(api_client.ApiError, match="Could not connect"):
        api_client.submit_job("http://x:9002", {})


def test_submit_job_http_error_includes_detail(monkeypatch):
    def fake_post(url, json, timeout, headers=None, **kwargs):
        return FakeResponse(422, {"detail": "Unsupported gpu_type 'V100'"})

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(api_client.ApiError, match="Unsupported gpu_type"):
        api_client.submit_job("http://x:9002", {})


def test_get_status_success(monkeypatch):
    def fake_get(url, timeout, headers=None, **kwargs):
        assert url.endswith("/v1/jobs/job-abc123")
        return FakeResponse(200, {"id": "job-abc123", "status": "RUNNING"})

    monkeypatch.setattr(httpx, "get", fake_get)
    result = api_client.get_job_status("http://x:9002", "job-abc123")
    assert result["status"] == "RUNNING"


def test_get_status_404_is_friendly(monkeypatch):
    def fake_get(url, timeout, headers=None, **kwargs):
        return FakeResponse(404, {"detail": "Job job-nope not found"})

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(api_client.ApiError, match="No job found with ID 'job-nope'") as exc:
        api_client.get_job_status("http://x:9002", "job-nope")
    assert exc.value.status_code == 404


def test_get_status_timeout(monkeypatch):
    def fake_get(url, timeout, headers=None, **kwargs):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(api_client.ApiError, match="timed out"):
        api_client.get_job_status("http://x:9002", "job-abc123")


def test_get_logs_success_returns_text(monkeypatch):
    def fake_get(url, timeout, headers=None, **kwargs):
        assert url.endswith("/v1/jobs/job-abc123/logs")
        return FakeResponse(200, text="epoch 1\nepoch 2\n")

    monkeypatch.setattr(httpx, "get", fake_get)
    text = api_client.get_job_logs("http://x:9002", "job-abc123")
    assert text == "epoch 1\nepoch 2\n"


def test_get_logs_409_carries_status_code(monkeypatch):
    """follow-mode relies on being able to see the 409 to keep polling."""
    def fake_get(url, timeout, headers=None, **kwargs):
        return FakeResponse(409, {"detail": "Logs are not available yet"})

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(api_client.ApiError) as exc:
        api_client.get_job_logs("http://x:9002", "job-abc123")
    assert exc.value.status_code == 409


def test_get_logs_404_carries_status_code(monkeypatch):
    def fake_get(url, timeout, headers=None, **kwargs):
        return FakeResponse(404, {"detail": "No logs were captured for this job."})

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(api_client.ApiError) as exc:
        api_client.get_job_logs("http://x:9002", "job-abc123")
    assert exc.value.status_code == 404
