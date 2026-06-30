# GPU Job as a Service — API Contract

This is the shared contract all three workstreams build against. If this drifts
from what's actually implemented, integration breaks — so changes to this file
should be agreed on by whoever owns the API (Prathamesh) before anyone codes
against an updated version. Per the Day 0 agreement: changes to this file go
through a PR that all three of us see before merging, not a direct push.

Source of truth for status values, failure reasons, and field names: the
design doc (REQ-1.1, REQ-1.2, REQ-4.1, REQ-4.2, REQ-4.3).

---

## Conventions

- **Auth header:** every endpoint requires an `X-Customer-Id` header. There's
  no real auth yet — this is a placeholder string, trusted as-is, scoping all
  reads/writes to that customer. The CLI must send it on every request, not
  just on job creation.
- **Timestamps:** all timestamps are UTC, ISO 8601, with a `Z` suffix (e.g.
  `2026-06-15T10:30:00Z`). Stored as `timestamptz` in Postgres.
- **Script upload:** scripts are uploaded directly to MinIO via a presigned
  PUT URL obtained from `POST /jobs/presign`, *before* calling `POST /jobs`.
  The API never receives script bytes directly. See the presign endpoint
  below for the full two-step flow.

---

## Status enum

```
PENDING → SCHEDULED → RUNNING → SUCCEEDED
   │           │           │
   └─► FAILED  └─► FAILED  └─► FAILED
```

| Status      | Meaning                                                         |
|-------------|------------------------------------------------------------------|
| PENDING     | Job record created; image build in progress or waiting for capacity |
| SCHEDULED   | Job admitted; K8s Job object created, pod not yet running       |
| RUNNING     | Pod is executing                                                |
| SUCCEEDED   | Pod exited with code 0                                          |
| FAILED      | Pod exited non-zero, timed out, or hit a platform error         |

Note: script validation (syntax check etc.) happens synchronously inside
`POST /jobs`, before a job row is ever created. A script that fails
validation never reaches PENDING — it returns a 422 immediately (see below).
There is no separate "validating" status.

## Failure codes → customer-facing messages

The controller writes `failure_code` (machine-readable); the API renders
`failure_reason` (human-readable) from this table. Both ship in the response
so future clients can branch on the code without parsing English text.

These codes only apply to jobs that were successfully created and later
failed. Pre-submission validation failures (e.g. a script that doesn't
parse) are a different error family — see `POST /jobs`'s 422 responses.

| failure_code          | failure_reason (exact string)                                                                       |
|------------------------|-------------------------------------------------------------------------------------------------------|
| `oom`                  | "Your job ran out of memory. Try requesting more memory or reducing batch size."                     |
| `node_failure`         | "A hardware issue interrupted your job. This is not a problem with your code — please resubmit."     |
| `nonzero_exit`         | "Your job exited with an error. Check the logs for the full traceback."                              |
| `scheduling_timeout`   | "No GPUs of the requested type were available within the timeout window. Please try again or contact support." |
| `build_failure`        | "We could not install your dependencies. Check that your requirements file is valid and all packages exist." |

---

## `POST /jobs/presign`

Request a presigned URL to upload a job script to MinIO. This is always the
first call in the submission flow — the CLI calls this, uploads the script
directly to the returned URL, then calls `POST /jobs` referencing the
resulting object key.

**Depends on:** Tanishka's MinIO bucket setup. Until that's live, this
endpoint can return a stubbed/local response for development purposes.

**Headers:** `X-Customer-Id: <customer_id>`

**Request**
```json
{
  "filename": "train.py"
}
```

**Response — 200 OK**
```json
{
  "upload_url": "https://minio.internal/scripts/<customer_id>/<upload_id>/train.py?presigned=...",
  "artifact_location": "scripts/<customer_id>/<upload_id>/train.py",
  "expires_at": "2026-06-15T10:40:00Z"
}
```

The CLI performs an HTTP PUT of the raw file bytes to `upload_url`, then
passes `artifact_location` as-is into the `POST /jobs` request body below.

---

## `POST /jobs`

Submit a new job. Requires the script to have already been uploaded via the
presign flow above.

The server synchronously validates the uploaded script (e.g. checks it
parses without syntax errors) before creating the job row. If validation
fails, no row is created and the endpoint returns 422 immediately.

**Headers:** `X-Customer-Id: <customer_id>`

**Request**
```json
{
  "artifact_location": "scripts/<customer_id>/<upload_id>/train.py",
  "requirements_spec": "torch==2.4.0\nnumpy==1.26.0",
  "python_version": "3.13",
  "gpu_type": "A100",
  "gpu_count": 2
}
```

**Response — 201 Created**
```json
{
  "id": "job-8f3a2c1d",
  "status": "PENDING",
  "submitted_at": "2026-06-15T10:30:00Z"
}
```

**Response — 422 Unprocessable Entity** (server-side validation failure)
```json
{
  "error": "unsupported_python_version",
  "message": "Python 3.10 is not supported. Available versions: 3.13, 3.14."
}
```
```json
{
  "error": "unknown_gpu_type",
  "message": "GPU type 'V100' is not available. Available types: A100, H100."
}
```
```json
{
  "error": "script_syntax_error",
  "message": "Your script could not be parsed. Please fix the syntax error and resubmit."
}
```

Validation errors this endpoint can return (server re-validates regardless of
what the CLI already checked):
- `unsupported_python_version`
- `unknown_gpu_type`
- `invalid_gpu_count` (must be > 0)
- `missing_requirements_spec`
- `script_syntax_error` (script at `artifact_location` failed to parse —
  exact validation logic TBD, this is a placeholder error code so the CLI
  can build against it now)
- `artifact_not_found` (no object exists at the given `artifact_location` —
  likely means the presigned upload never completed)

---

## `GET /jobs/{id}`

Get job status and metadata. Served entirely from Postgres — never queries
the cluster directly.

**Headers:** `X-Customer-Id: <customer_id>`

**Response — 200 OK**
```json
{
  "id": "job-8f3a2c1d",
  "status": "RUNNING",
  "status_message": "Your job is running on 2 x A100 GPUs.",
  "submitted_at": "2026-06-15T10:30:00Z",
  "started_at": "2026-06-15T10:32:10Z",
  "completed_at": null,
  "failure_code": null,
  "failure_reason": null,
  "gpu_type": "A100",
  "gpu_count": 2
}
```

**Response — 404 Not Found**
```json
{ "error": "job_not_found", "message": "No job with id 'job-xxxx' was found." }
```

**Example FAILED response**
```json
{
  "id": "job-8f3a2c1d",
  "status": "FAILED",
  "status_message": null,
  "submitted_at": "2026-06-15T10:30:00Z",
  "started_at": "2026-06-15T10:32:10Z",
  "completed_at": "2026-06-15T10:41:00Z",
  "failure_code": "oom",
  "failure_reason": "Your job ran out of memory. Try requesting more memory or reducing batch size.",
  "gpu_type": "A100",
  "gpu_count": 2
}
```

---

## `GET /jobs/{id}/logs`

Retrieve logs. Polling-based for the prototype — the CLI calls this on an
interval rather than holding an open stream. This is a deliberate choice, not
an oversight: the response shape below doesn't need to change if this is
upgraded to chunked/streaming transfer later.

- If job is RUNNING: proxies the most recent N lines (default 1000) from the
  pod, near-real-time.
- If job is completed (SUCCEEDED/FAILED): returns a presigned URL to the
  archived log file in object storage.

**Headers:** `X-Customer-Id: <customer_id>`

**Response — 200 OK (running job)**
```json
{
  "mode": "live",
  "lines": [
    "Epoch 1/10 — loss: 0.482",
    "Epoch 2/10 — loss: 0.401"
  ]
}
```

**Response — 200 OK (completed job)**
```json
{
  "mode": "archived",
  "url": "https://minio.internal/logs/job-8f3a2c1d/stdout.log?presigned=...",
  "expires_at": "2026-06-15T11:30:00Z"
}
```

---

## `GET /jobs/{id}/outputs`

List output artifacts with presigned URLs. Only meaningful for
SUCCEEDED/FAILED jobs (outputs may be partial on FAILED).

**Headers:** `X-Customer-Id: <customer_id>`

**Response — 200 OK**
```json
{
  "outputs": [
    {
      "path": "model_checkpoint.pt",
      "size_bytes": 48213123,
      "url": "https://minio.internal/outputs/job-8f3a2c1d/model_checkpoint.pt?presigned=...",
      "expires_at": "2026-06-15T11:30:00Z"
    },
    {
      "path": "metrics.json",
      "size_bytes": 1024,
      "url": "https://minio.internal/outputs/job-8f3a2c1d/metrics.json?presigned=...",
      "expires_at": "2026-06-15T11:30:00Z"
    }
  ]
}
```

**Response — 200 OK (no outputs yet)**
```json
{ "outputs": [] }
```

---

## Resolved Day 0 decisions (for reference)

These were the open questions in the original draft. All four are now
settled; recorded here so the reasoning isn't lost.

1. **`customer_id`** — CLI sends a placeholder `X-Customer-Id` header,
   trusted as-is. No real auth yet. Applies to every endpoint, not just job
   creation.
2. **Script upload mechanism** — presigned PUT to MinIO via
   `POST /jobs/presign`, followed by `POST /jobs` referencing the returned
   `artifact_location`. Pre-submission script validation (syntax etc.)
   happens synchronously inside `POST /jobs`, server-side, before the job
   row is created — no new status was added to support this.
3. **Polling vs streaming for live logs** — polling, for the prototype.
   Contract shape doesn't need to change if this is upgraded later.
4. **Timestamps** — UTC, ISO 8601, `Z` suffix. Confirmed, not unix epoch.
