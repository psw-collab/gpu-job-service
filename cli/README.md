# gpujob CLI

CLI for submitting and tracking GPU jobs against the `gpu-job-service` API.

## Install

```bash
pip install -e .
```

This installs the `gpujob` command.

## Configure the API URL

By default the CLI talks to `http://localhost:8000`. Override with an env var
if the API is running elsewhere (e.g. the Cloud Run URL, or a WSL host):

```bash
export GPUJOB_API_URL=https://gpu-gateway-xxxxx-uc.a.run.app
```

## Commands

### submit

```bash
gpujob submit -f job.yaml
```

`job.yaml`:

```yaml
entrypoint: train.py
requirements: requirements.txt   # optional
python_version: "3.11"           # optional, default 3.11
gpu_type: A100                   # optional, default A100
gpu_count: 2                     # optional, default 1 (1-8)
```

`entrypoint`/`requirements` are paths relative to the yaml file; the CLI sends
their **contents**, not the paths.

### status

```bash
gpujob status job-a1b2c3d4
```

Prints status, timestamps, GPU info, and — if the job failed — the failure reason.

### list

```bash
gpujob list                       # table of all jobs
gpujob list --status RUNNING      # filter by status
gpujob list --output json         # machine-readable, for scripts/piping
```

### cancel

```bash
gpujob cancel job-a1b2c3d4
```

Cancels a job that hasn't finished. A job already in a terminal state
(SUCCEEDED / FAILED / CANCELLED) returns a clear error.

### logs

```bash
gpujob logs job-a1b2c3d4          # print current logs and exit
gpujob logs job-a1b2c3d4 --follow # stream new output until the job finishes
```

In `--follow` mode the CLI polls for new output until the job reaches a
terminal state, then prints a final status line (and the failure reason if it
failed). Ctrl-C stops following cleanly.

## Validation

All YAML/file validation happens client-side before any network call: missing
files, malformed YAML, unsupported `gpu_type`/`python_version`, and
out-of-range `gpu_count` are caught locally with a clear message and exit
code 1. The server independently re-validates everything.

## Testing without the real server

`tests/mock_server.py` is an in-memory FastAPI server implementing the full
contract (including the proposed list/cancel/logs endpoints). Run the suite:

```bash
pip install -e ".[test]"
pytest
```

Or drive it manually:

```bash
cd tests && uvicorn mock_server:app --port 8000
```

The `/_test/jobs/{id}` endpoint is a test-only hook to push a job through
states (RUNNING, log lines, SUCCEEDED/FAILED); it is not part of the real API.
