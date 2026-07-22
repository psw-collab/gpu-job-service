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

5. **Output uploads (GCS via Workload Identity)**

   After a job's entrypoint finishes, `upload_outputs.py` runs inside the pod and pushes
   anything written to `/outputs` to a Google Cloud Storage bucket. Authentication uses
   **Workload Identity** — the pod runs under a Kubernetes service account (`gpu-job-sa`)
   bound to a GCP service account (`gpu-worker-sa`) that has write access to the bucket.
   No static keys or credentials are needed (the project's org policy blocks
   service-account key creation).

   Configure the bucket via the worker's environment (forwarded into each job's pod):

```bash
   export GCS_BUCKET=gpujob-outputs-shared
```

   **One-time Workload Identity setup** (per cluster):

```bash
   # 1. Grant the GCP service account write access to the bucket
   gcloud storage buckets add-iam-policy-binding gs://gpujob-outputs-shared \
     --member="serviceAccount:gpu-worker-sa@intern-501105.iam.gserviceaccount.com" \
     --role="roles/storage.objectAdmin"

   # 2. Create + annotate the Kubernetes service account
   kubectl create serviceaccount gpu-job-sa -n gpu-jobs
   kubectl annotate serviceaccount gpu-job-sa -n gpu-jobs \
     iam.gke.io/gcp-service-account=gpu-worker-sa@intern-501105.iam.gserviceaccount.com

   # 3. Bind the K8s SA to the GCP SA (needs Service Account Admin; may require a project admin)
   gcloud iam service-accounts add-iam-policy-binding \
     gpu-worker-sa@intern-501105.iam.gserviceaccount.com \
     --role roles/iam.workloadIdentityUser \
     --member "serviceAccount:intern-501105.svc.id.goog[gpu-jobs/gpu-job-sa]"
```

   The cluster must have Workload Identity enabled
   (`--workload-pool=intern-501105.svc.id.goog` at creation).

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