# Contract proposal: list / cancel / logs endpoints

These three endpoints back the `gpujob list`, `cancel`, and `logs` commands.
The CLI side is built and tested against the mock in `cli/tests/mock_server.py`;
these are the server-side routes the real API (`main.py`) needs to add.

## 1. GET /v1/jobs  — list

Query params: `status` (optional) — exact-match filter, e.g. `RUNNING`.

Response `200`:
```json
{ "jobs": [
  { "id": "job-abc123", "status": "RUNNING", "gpu_type": "A100",
    "gpu_count": 2, "submitted_at": "2026-07-08T10:00:00Z" }
] }
```
(Scope note: once `X-Customer-Id` tenancy lands, this must be filtered to the
calling customer's jobs.)

## 2. POST /v1/jobs/{job_id}/cancel  — cancel

No body. Transitions a non-terminal job to `CANCELLED`.

- `200` -> full `JobStatusResponse` with `status: "CANCELLED"`.
- `404` -> unknown job id.
- `409` -> job already terminal (`SUCCEEDED`/`FAILED`/`CANCELLED`);
  `detail` should read e.g. `"Cannot cancel a job that is already SUCCEEDED"`.

Requires `CANCELLED` as a valid status value (migration + state machine).

## 3. GET /v1/jobs/{job_id}/logs  — logs

Query params: `since` (int, default 0) — line cursor for incremental polling.

Response `200`:
```json
{ "job_id": "job-abc123", "status": "RUNNING",
  "failure_reason": null, "logs": "epoch 1 ...\n", "next_since": 12 }
```
- `logs` contains only lines from `since` onward.
- `next_since` is the caller's next cursor (total lines seen).
- `404` -> unknown job id.

`--follow` polls with the returned `next_since` until `status` is terminal.

## Prerequisite (separate bug, blocks everything)

`POST /v1/jobs` on `main` is currently broken: the DB column
`entrypoint_content` is `NOT NULL`, but `JobSubmitRequest` in `schemas.py`
doesn't accept it and `submit_job` never sets it, so every submit 500s. The
CLI already sends `entrypoint_content`; the real schema + insert need to
accept and persist it before any of the above matters end-to-end.
