"""
Everything that needs Kubernetes access: creating Jobs, polling their status,
classifying failures, and pulling pod logs. Used by the worker (deployed
inside the cluster) and, for local single-process dev, by main.py directly.

The worker has no direct database access -- the colo cluster's network
blocks outbound traffic on non-standard ports (confirmed: Cloud SQL on 5432
times out), so instead of connecting to Postgres, this module talks to the
gateway's /internal/* endpoints over plain HTTPS, authenticated with a
shared token. Port 443 egress is essentially always allowed.
"""
import asyncio
import os
from datetime import datetime, timezone, timedelta

import httpx
from kubernetes import client, config

try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

NAMESPACE = os.getenv("K8S_NAMESPACE")
if not NAMESPACE:
    raise RuntimeError("K8S_NAMESPACE environment variable is required")
core = client.CoreV1Api()
batch = client.BatchV1Api()

GATEWAY_URL = os.getenv("GATEWAY_URL")
if not GATEWAY_URL:
    raise RuntimeError("GATEWAY_URL environment variable is required")
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN")
if not INTERNAL_TOKEN:
    raise RuntimeError("INTERNAL_TOKEN environment variable is required")

_HEADERS = {"X-Internal-Token": INTERNAL_TOKEN}

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "job-outputs")

# Distinct from MINIO_ENDPOINT above: that one is whatever the outer shell
# has set (e.g. localhost:9000 for a developer's own convenience), which is
# meaningless inside a job pod -- "localhost" there is the pod itself, not
# this machine. Job pods always get IN_CLUSTER_MINIO_ENDPOINT instead, which
# defaults to the in-cluster Service DNS name so it works with no extra
# config as long as MinIO is deployed in the same namespace as the jobs.
IN_CLUSTER_MINIO_ENDPOINT = os.getenv(
    "IN_CLUSTER_MINIO_ENDPOINT", f"http://minio-service.{NAMESPACE}.svc.cluster.local:9000"
)

GPU_ENABLED = os.getenv("GPU_ENABLED", "false").lower() == "true"
GPU_ACCELERATOR_TYPE = os.getenv("GPU_ACCELERATOR_TYPE", "nvidia-l4")

FAILURE_REASON_OOM = "Your job ran out of memory. Try requesting more memory or reducing batch size."
FAILURE_REASON_NODE = "A hardware issue interrupted your job. This is not a problem with your code, please resubmit."
FAILURE_REASON_USER_CODE = "Your job exited with an error. Check the logs for the full traceback."
FAILURE_REASON_UNKNOWN = "Your job failed for an unknown reason. Contact support with your job ID."
FAILURE_REASON_SCHEDULING_TIMEOUT = (
    "No GPUs of the requested type were available within the timeout window. "
    "Please try again or contact support."
)
FAILURE_REASON_SCHEDULING_ERROR = "We could not schedule this job on the cluster. Please try again or contact support."

SCHEDULING_TIMEOUT = timedelta(minutes=int(os.getenv("SCHEDULING_TIMEOUT_MINUTES", "30")))
GC_INTERVAL_SECONDS = int(os.getenv("GC_INTERVAL_MINUTES", str(24 * 60))) * 60


def fetch_active_jobs() -> list[dict]:
    resp = httpx.get(f"{GATEWAY_URL}/internal/jobs", headers=_HEADERS, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def report_job(job_id: str, **fields) -> None:
    resp = httpx.post(f"{GATEWAY_URL}/internal/jobs/{job_id}/report", headers=_HEADERS, json=fields, timeout=30.0)
    resp.raise_for_status()


def trigger_gc() -> int:
    resp = httpx.post(f"{GATEWAY_URL}/internal/gc", headers=_HEADERS, timeout=30.0)
    resp.raise_for_status()
    return resp.json().get("deleted", 0)


def create_k8s_job(job_id: str, entrypoint: str, entrypoint_content: str, requirements: str, python_version: str,
                    gpu_count: int):
    uploader_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upload_outputs.py")
    with open(uploader_path) as f:
        uploader_content = f.read()

    core.create_namespaced_config_map(
        NAMESPACE,
        client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=f"{job_id}-files"),
            data={
                entrypoint: entrypoint_content,
                "requirements.txt": requirements or "",
                "upload_outputs.py": uploader_content,
            },
        ),
    )
    # Run the uploader after the entrypoint regardless of outcome, but the
    # container must still exit with the entrypoint's own exit code so job
    # success/failure classification (k8s_status / classify_pod_failure)
    # keeps reflecting the user's script, not the uploader.
    command = (
        "mkdir -p /outputs\n"
        "pip install -r /scripts/requirements.txt 2>/dev/null\n"
        "pip install boto3 2>/dev/null\n"
        f"python /scripts/{entrypoint}\n"
        "ENTRYPOINT_EXIT=$?\n"
        'python /scripts/upload_outputs.py || echo "output upload failed"\n'
        "exit $ENTRYPOINT_EXIT"
    )
    # GPU requesting is env-gated so we can run the full pipeline on a
    # CPU-only cluster today and switch to real GPUs later with no code
    # change. Autopilot requires the accelerator node_selector alongside the
    # nvidia.com/gpu limit, or it rejects the pod -- so both are added together.
    resources = None
    node_selector = None
    if GPU_ENABLED:
        resources = client.V1ResourceRequirements(limits={"nvidia.com/gpu": str(gpu_count)})
        node_selector = {"cloud.google.com/gke-accelerator": GPU_ACCELERATOR_TYPE}

    container = client.V1Container(
        name="runner",
        image=f"python:{python_version}-slim",
        command=["sh", "-c", command],
        volume_mounts=[
            client.V1VolumeMount(name="code", mount_path="/scripts"),
            client.V1VolumeMount(name="outputs", mount_path="/outputs"),
        ],
        env=[
            client.V1EnvVar(name="MINIO_ENDPOINT", value=IN_CLUSTER_MINIO_ENDPOINT),
            client.V1EnvVar(name="MINIO_ACCESS_KEY", value=MINIO_ACCESS_KEY),
            client.V1EnvVar(name="MINIO_SECRET_KEY", value=MINIO_SECRET_KEY),
            client.V1EnvVar(name="MINIO_BUCKET", value=MINIO_BUCKET),
            client.V1EnvVar(name="JOB_ID", value=job_id),
            client.V1EnvVar(name="OUTPUTS_DIR", value="/outputs"),
        ],
        resources=resources,
    )
    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        containers=[container],
        volumes=[
            client.V1Volume(
                name="code",
                config_map=client.V1ConfigMapVolumeSource(name=f"{job_id}-files")),
            client.V1Volume(
                name="outputs",
                empty_dir=client.V1EmptyDirVolumeSource()),
        ],
        node_selector=node_selector,
    )
    batch.create_namespaced_job(
        NAMESPACE,
        client.V1Job(
            metadata=client.V1ObjectMeta(name=job_id),
            spec=client.V1JobSpec(
                template=client.V1PodTemplateSpec(spec=pod_spec),
                backoff_limit=0,
                ttl_seconds_after_finished=300),
        ),
    )


def k8s_status(job_id: str) -> str | None:
    try:
        s = batch.read_namespaced_job_status(job_id, NAMESPACE).status
    except client.ApiException as e:
        if e.status == 404:
            return None
        raise
    if s.succeeded:
        return "SUCCEEDED"
    if s.failed:
        return "FAILED"
    if s.active:
        # Job.status.active only means "has a non-terminal pod" -- it's set as
        # soon as the pod object exists, even while still Pending (e.g. stuck
        # unschedulable). Check the pod's actual phase so we don't report
        # RUNNING before the container has actually started, which would also
        # let it skip past the SCHEDULED-only scheduling timeout.
        return "RUNNING" if _pod_is_running(job_id) else "SCHEDULED"
    return "SCHEDULED"


def _pod_is_running(job_id: str) -> bool:
    try:
        pods = core.list_namespaced_pod(NAMESPACE, label_selector=f"job-name={job_id}").items
    except client.ApiException:
        return False
    return bool(pods) and pods[0].status.phase == "Running"


def classify_pod_failure(job_id: str) -> str:
    try:
        pods = core.list_namespaced_pod(NAMESPACE, label_selector=f"job-name={job_id}").items
    except client.ApiException:
        return FAILURE_REASON_UNKNOWN

    if not pods:
        return FAILURE_REASON_UNKNOWN

    pod = pods[0]
    if pod.status.reason in ("Evicted", "NodeLost", "NodeAffinity"):
        return FAILURE_REASON_NODE

    for cs in pod.status.container_statuses or []:
        terminated = cs.state.terminated if cs.state else None
        if not terminated:
            continue
        if terminated.reason == "OOMKilled" or terminated.exit_code == 137:
            return FAILURE_REASON_OOM
        if terminated.exit_code:
            return FAILURE_REASON_USER_CODE

    return FAILURE_REASON_UNKNOWN


def fetch_pod_logs(job_id: str) -> str | None:
    try:
        pods = core.list_namespaced_pod(NAMESPACE, label_selector=f"job-name={job_id}").items
    except client.ApiException:
        return None
    if not pods:
        return None
    try:
        resp = core.read_namespaced_pod_log(pods[0].metadata.name, NAMESPACE, _preload_content=False)
    except client.ApiException:
        return None
    return resp.data.decode("utf-8", errors="replace")


async def reconcile_loop():
    while True:
        try:
            jobs = await asyncio.to_thread(fetch_active_jobs)
            for job in jobs:
                job_id = job["id"]
                try:
                    now = datetime.now(timezone.utc)
                    submitted_at = datetime.fromisoformat(job["submitted_at"])

                    if job["status"] == "PENDING":
                        try:
                            await asyncio.to_thread(
                                create_k8s_job, job_id, job["entrypoint"], job["entrypoint_content"],
                                job["requirements"], job["python_version"], job["gpu_count"],
                            )
                            await asyncio.to_thread(
                                report_job, job_id,
                                status="SCHEDULED",
                                status_message="Job scheduled, waiting for GPU capacity",
                            )
                        except Exception as e:
                            print(f"create_k8s_job failed for {job_id}: {e}")
                            await asyncio.to_thread(
                                report_job, job_id,
                                status="FAILED",
                                status_message="Job failed",
                                failure_reason=FAILURE_REASON_SCHEDULING_ERROR,
                                completed_at=now.isoformat(),
                            )
                        continue

                    if job["status"] == "SCHEDULED" and now - submitted_at > SCHEDULING_TIMEOUT:
                        await asyncio.to_thread(
                            report_job, job_id,
                            status="FAILED",
                            status_message="Job failed",
                            failure_reason=FAILURE_REASON_SCHEDULING_TIMEOUT,
                            completed_at=now.isoformat(),
                        )
                        continue

                    new_status = await asyncio.to_thread(k8s_status, job_id)
                    if new_status is None or new_status == job["status"]:
                        continue

                    fields = {"status": new_status}
                    if new_status == "RUNNING" and job["started_at"] is None:
                        fields["started_at"] = now.isoformat()
                        fields["status_message"] = "Job is running"
                    if new_status == "SUCCEEDED":
                        fields["completed_at"] = now.isoformat()
                        fields["status_message"] = "Job completed successfully"
                        fields["logs"] = await asyncio.to_thread(fetch_pod_logs, job_id)
                    if new_status == "FAILED":
                        fields["completed_at"] = now.isoformat()
                        fields["status_message"] = "Job failed"
                        fields["failure_reason"] = await asyncio.to_thread(classify_pod_failure, job_id)
                        fields["logs"] = await asyncio.to_thread(fetch_pod_logs, job_id)
                    await asyncio.to_thread(report_job, job_id, **fields)
                except Exception as e:
                    print(f"reconcile error for {job_id}: {e}")
        except Exception as e:
            print(f"reconcile loop error: {e}")
        await asyncio.sleep(5)


async def gc_loop():
    while True:
        try:
            deleted = await asyncio.to_thread(trigger_gc)
            if deleted:
                print(f"GC: deleted {deleted} job record(s)")
        except Exception as e:
            print(f"gc loop error: {e}")
        await asyncio.sleep(GC_INTERVAL_SECONDS)
