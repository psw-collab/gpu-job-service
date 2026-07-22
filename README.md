# GPU Job as a Service

A platform for submitting GPU compute jobs to a
Kubernetes cluster while mostly abstracting Kubernetes. Customers give the platform a Python
script, a dependency list, and a resource requirement (GPU type/count); the platform builds
the environment, schedules the job onto a GPU node, and reports status back. No
manifests, Dockerfiles, or cluster credentials required.

Full rationale and architecture are in
[`GPU Job as a Service — Design Document.pdf`](<./GPU Job as a Service _  Design Document.pdf>).

## How it works

- **REST API** (`main.py`, FastAPI) — accepts job submissions, validates `gpu_type` and
  `python_version`, writes a job record to Postgres, and creates a Kubernetes `Job` that runs
  the submitted script.
- **Postgres** — source of truth for job state (`PENDING` → `RUNNING` → `SUCCEEDED`/`FAILED`).
- **Reconciler** (background task in `main.py`) — polls the Kubernetes Job status every 5
  seconds and updates the Postgres record as the pod progresses.
- **CLI** (`cli/`) — the customer-facing interface for submitting jobs and checking status. See
  [`cli/README.md`](cli/README.md) for CLI-specific usage and options.


## Setup

1. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

2. **Start Postgres**

   ```bash
   docker-compose up -d
   ```

3. **Run database migrations**

   Migrations are plain SQL files under `migrations/`, written for
   [goose](https://github.com/pressly/goose):

   ```bash
   goose -dir migrations postgres "postgresql://postgres:secret@127.0.0.1:5432/postgres?sslmode=disable" up
   ```

4. **Configure and start the API**

   The API needs a Kubernetes namespace to create jobs in:

   ```bash
   export K8S_NAMESPACE=gpu-jobs
   export DATABASE_URL="postgresql://postgres:secret@127.0.0.1:5432/postgres?sslmode=disable"  
   uvicorn main:app --reload
   ```

5. **(Optional) Configure output uploads to MinIO/S3**

   After a job's entrypoint finishes, `upload_outputs.py` runs inside the pod and pushes
   anything written to `/outputs` up to S3-compatible object storage. This step is
   optional — if unset, it fails harmlessly (the job's own exit status is unaffected,
   see `k8s_ops.py`'s `create_k8s_job`) and logs `output upload failed`.

   To enable it against the local MinIO from `docker-compose.yml` (`job-outputs` bucket,
   already created by the `createbucket` service), set these in the **worker's own
   environment** before starting it — `k8s_ops.py` reads them and forwards them into
   each job's pod:

   ```bash
   export MINIO_ENDPOINT=http://localhost:9000
   export MINIO_ACCESS_KEY=minioadmin
   export MINIO_SECRET_KEY=minioadmin
   export MINIO_BUCKET=job-outputs
   ```

   A `.env.example` at the repo root has these documented too. There's no `.env` auto-loading
   in this codebase (no `python-dotenv`), so either `export` them directly or
   `set -a; source .env; set +a` before running `uvicorn`.

   **Note:** these values only work if the pod can actually reach that endpoint. If your
   Kubernetes Job runs on a real remote cluster (not a local kind/minikube), `localhost`
   inside the pod refers to the pod itself, not your laptop — you'd need to expose MinIO
   via a tunnel (e.g. `ngrok`) or run it inside the cluster instead.


## Submitting a job

Install the CLI and point it at the API (see [`cli/README.md`](cli/README.md) for full details):

```bash
cd cli
pip install -e .
export GPUJOB_API_URL=http://localhost:8000
```

Write a `job.yaml`:

```yaml
entrypoint: train.py
requirements: requirements.txt
python_version: "3.11"
gpu_type: A100
gpu_count: 2
```

Submit it and check on it:

```bash
gpujob submit -f job.yaml
# Job submitted: job-a1b2c3d4

gpujob status job-a1b2c3d4
```