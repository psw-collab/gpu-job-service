# gpujob CLI

CLI for submitting and tracking GPU jobs against the `gpu-job-service` API.

## Install

```bash
pip install -e .
```

This installs the `gpujob` command.

## Configure the API URL

By default the CLI talks to `http://localhost:8000`. Override with an env var
if the API is running elsewhere (e.g. behind a different host/port):

```bash
export GPUJOB_API_URL=http://172.23.18.129:8000
```

## Usage

### Submit a job

Write a `job.yaml`:

```yaml
entrypoint: train.py
requirements: requirements.txt
python_version: "3.11"
gpu_type: A100
gpu_count: 2
```

- `entrypoint` (required): path to your Python script, relative to the
  yaml file's location. The CLI reads its contents and sends them to the API.
- `requirements` (optional): path to a requirements.txt, relative to the
  yaml file's location. Also sent as file contents, not a path.
- `python_version` (optional, default `"3.11"`): must be one of the
  supported versions (see `src/gpujob_cli/constants.py`).
- `gpu_type` (optional, default `"A100"`): must be one of the supported
  types (see `src/gpujob_cli/constants.py`).
- `gpu_count` (optional, default `1`): integer between 1 and 8.

Then:

```bash
gpujob submit -f job.yaml
# Job submitted: job-a1b2c3d4
```

### Check status

```bash
gpujob status job-a1b2c3d4
```

Prints status, timestamps, GPU info, and (if failed) the failure reason.

## Validation

All YAML/file validation happens client-side before any network call:
missing files, malformed YAML, unsupported `gpu_type`/`python_version`,
and out-of-range `gpu_count` are all caught locally with a clear error
message and exit code 1. The server independently re-validates everything
it receives, so these two layers don't need to stay in lockstep, but the
allowed-value lists in `constants.py` should be updated if the server's
supported values change.

## API contract assumption

This CLI assumes the server's `JobSubmitRequest` schema (`entrypoint`,
`requirements`) expects **file contents as strings**, not file paths.
That's what the CLI sends. If the actual API expects something else
(e.g. paths, or a multipart file upload), `to_request_payload()` in
`src/gpujob_cli/job_config.py` is the only place that needs to change.

## Testing without the real server

`tests/mock_server.py` is a minimal in-memory FastAPI server matching the
documented contract, useful for testing the CLI without Postgres or the
real backend running:

```bash
pip install fastapi uvicorn
cd tests && uvicorn mock_server:app --port 8000
```

Then run `gpujob submit` / `gpujob status` against it in another terminal.
