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

GCS_BUCKET = os.getenv("GCS_BUCKET", "gpujob-outputs-shared")
POD_SERVICE_ACCOUNT = os.getenv("POD_SERVICE_ACCOUNT", "gpu-job-sa")

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
FAILURE_REASON_BUILD_TIMEOUT = (
    "Building your environment took too long and timed out. "
    "Please try again or contact support."
)
FAILURE_REASON_BUILD_FAILED = "We could not build an environment from your code. Check the logs for the full traceback."

SCHEDULING_TIMEOUT = timedelta(minutes=int(os.getenv("SCHEDULING_TIMEOUT_MINUTES", "30")))
BUILD_TIMEOUT = timedelta(minutes=int(os.getenv("BUILD_TIMEOUT_MINUTES", "20")))
GC_INTERVAL_SECONDS = int(os.getenv("GC_INTERVAL_MINUTES", str(24 * 60))) * 60

# Registry for images Kaniko builds from user code (distinct from the
# worker's own image). See kaniko.md for the full design.
JOB_IMAGE_REGISTRY = os.getenv("JOB_IMAGE_REGISTRY", "ghcr.io/shizuka730/gpu-job-artifacts")
GHCR_PULL_SECRET_NAME = "ghcr-pull-secret"  # read-only, reused from the worker's own pull secret
GHCR_BUILD_SECRET_NAME = "ghcr-build-secret"  # write-scoped, Kaniko push only
WORKER_INTERNAL_TOKEN_SECRET_NAME = "worker-internal-token"

# GKE Autopilot rejects any pod requesting nvidia.com/gpu unless it also sets
# this node selector -- see the "autogke-gpu-limitation" Warden admission
# webhook. Maps our public gpu_type values (schemas.ALLOWED_GPU_TYPES) to the
# accelerator type string Autopilot expects.
GPU_ACCELERATOR_NODE_SELECTOR = "cloud.google.com/gke-accelerator"
GPU_TYPE_TO_ACCELERATOR = {
    "A100": "nvidia-tesla-a100",
    "H100": "nvidia-h100-80gb",
    "T4": "nvidia-tesla-t4",
}


def _gpu_node_selector(gpu_type: str) -> dict[str, str]:
    accelerator = GPU_TYPE_TO_ACCELERATOR.get(gpu_type)
    if not accelerator:
        raise ValueError(f"No GKE accelerator mapping for gpu_type '{gpu_type}'")
    return {GPU_ACCELERATOR_NODE_SELECTOR: accelerator}


def _job_image_tag(job_id: str) -> str:
    return f"{JOB_IMAGE_REGISTRY}:{job_id}"


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


def _owner_reference_for(created_job) -> "client.V1OwnerReference":
    """So a ConfigMap tied to a Job gets auto-deleted by K8s' own GC controller
    when the Job goes away (its TTL, or manual deletion) -- no extra RBAC
    (`delete` on configmaps) needed for the worker's own role."""
    return client.V1OwnerReference(
        api_version="batch/v1",
        kind="Job",
        name=created_job.metadata.name,
        uid=created_job.metadata.uid,
        block_owner_deletion=True,
        controller=True,
    )


def create_k8s_job(job_id: str, entrypoint: str, entrypoint_content: str, requirements: str, python_version: str,
                    gpu_type: str, gpu_count: int):
    """Legacy single-file path: run straight off python:{python_version}-slim,
    with the script/requirements mounted via ConfigMap and pip install at pod
    startup. Used when the job wasn't submitted with a `context` archive."""
    uploader_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upload_outputs.py")
    with open(uploader_path) as f:
        uploader_content = f.read()

    # Run the uploader after the entrypoint regardless of outcome, but the
    # container must still exit with the entrypoint's own exit code so job
    # success/failure classification (k8s_status / classify_pod_failure)
    # keeps reflecting the user's script, not the uploader.
    command = (
        "mkdir -p /outputs\n"
        "pip install -r /scripts/requirements.txt 2>/dev/null\n"
        "pip install google-cloud-storage 2>/dev/null\n"
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
            client.V1EnvVar(name="GCS_BUCKET", value=GCS_BUCKET),
            client.V1EnvVar(name="JOB_ID", value=job_id),
            client.V1EnvVar(name="OUTPUTS_DIR", value="/outputs"),
        ],
        resources=resources,
    )
    cm_name = f"{job_id}-files"
    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        service_account_name=POD_SERVICE_ACCOUNT,
        containers=[container],
        node_selector=_gpu_node_selector(gpu_type),
        volumes=[
            client.V1Volume(
                name="code",
                config_map=client.V1ConfigMapVolumeSource(name=cm_name)),
            client.V1Volume(
                name="outputs",
                empty_dir=client.V1EmptyDirVolumeSource()),
        ],
        node_selector=node_selector,
    )
    created_job = batch.create_namespaced_job(
        NAMESPACE,
        client.V1Job(
            metadata=client.V1ObjectMeta(name=job_id),
            spec=client.V1JobSpec(
                template=client.V1PodTemplateSpec(spec=pod_spec),
                backoff_limit=0,
                ttl_seconds_after_finished=300),
        ),
    )
    core.create_namespaced_config_map(
        NAMESPACE,
        client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=cm_name, owner_references=[_owner_reference_for(created_job)]),
            data={
                entrypoint: entrypoint_content,
                "requirements.txt": requirements or "",
                "upload_outputs.py": uploader_content,
            },
        ),
    )


def create_training_job_from_image(job_id: str, image_tag: str, entrypoint: str, gpu_type: str, gpu_count: int):
    """Multi-file path: the image already has code + dependencies baked in
    (built by Kaniko in create_build_job), so no ConfigMap/pip install here --
    just run the entrypoint directly. The image is private on GHCR, so this
    reuses the same read-only pull secret the worker's own Deployment uses."""
    container = client.V1Container(
        name="runner",
        image=image_tag,
        command=["python", f"/workspace/{entrypoint}"],
        resources=client.V1ResourceRequirements(
            limits={"nvidia.com/gpu": str(gpu_count)},
        ),
    )
    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        containers=[container],
        node_selector=_gpu_node_selector(gpu_type),
        image_pull_secrets=[client.V1LocalObjectReference(name=GHCR_PULL_SECRET_NAME)],
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


def _generate_dockerfile(python_version: str, requirements: str | None) -> str:
    lines = [f"FROM python:{python_version}-slim"]
    if requirements:
        lines.append(f"COPY {requirements} requirements.txt")
        lines.append("RUN pip install --no-cache-dir -r requirements.txt")
    lines.append("COPY . /workspace/")
    return "\n".join(lines) + "\n"


def create_build_job(job_id: str, python_version: str, entrypoint: str, requirements: str | None):
    """Multi-file path, step 1: build an image from the job's source archive
    (fetched from the gateway) via Kaniko -- no privileged/root access needed,
    matching the multi-tenant isolation model (see kaniko.md). An init
    container fetches+extracts the archive and drops in a generated
    Dockerfile; Kaniko then builds+pushes from that directory."""
    build_job_name = f"{job_id}-build"
    dockerfile_cm_name = f"{job_id}-dockerfile"

    init_container = client.V1Container(
        name="fetch-context",
        image="alpine:3.20",  # NOT busybox: its wget often lacks working HTTPS support
        command=["sh", "-c",
                 f"wget --header=\"X-Internal-Token: $INTERNAL_TOKEN\" "
                 f"-O /workspace/context.tar.gz \"{GATEWAY_URL}/internal/jobs/{job_id}/source\" && "
                 f"mkdir -p /workspace/build && "
                 f"tar -xzf /workspace/context.tar.gz -C /workspace/build && "
                 f"cp /dockerfile-config/Dockerfile /workspace/build/Dockerfile"],
        env=[client.V1EnvVar(
            name="INTERNAL_TOKEN",
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name=WORKER_INTERNAL_TOKEN_SECRET_NAME, key="INTERNAL_TOKEN"),
            ),
        )],
        volume_mounts=[
            client.V1VolumeMount(name="workspace", mount_path="/workspace"),
            client.V1VolumeMount(name="dockerfile", mount_path="/dockerfile-config"),
        ],
    )
    kaniko_container = client.V1Container(
        name="kaniko",
        image="gcr.io/kaniko-project/executor:v1.23.2",
        args=[
            "--context=dir:///workspace/build",
            "--dockerfile=/workspace/build/Dockerfile",
            f"--destination={_job_image_tag(job_id)}",
        ],
        volume_mounts=[
            client.V1VolumeMount(name="workspace", mount_path="/workspace"),
            # A dockerconfigjson Secret's data key is literally ".dockerconfigjson",
            # not "config.json" -- this subPath remap is required, not cosmetic.
            client.V1VolumeMount(name="ghcr-build-secret", mount_path="/kaniko/.docker/config.json",
                                  sub_path=".dockerconfigjson"),
        ],
    )
    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        init_containers=[init_container],
        containers=[kaniko_container],
        volumes=[
            client.V1Volume(name="workspace", empty_dir=client.V1EmptyDirVolumeSource()),
            client.V1Volume(name="dockerfile", config_map=client.V1ConfigMapVolumeSource(name=dockerfile_cm_name)),
            client.V1Volume(name="ghcr-build-secret",
                             secret=client.V1SecretVolumeSource(secret_name=GHCR_BUILD_SECRET_NAME)),
        ],
    )
    created_job = batch.create_namespaced_job(
        NAMESPACE,
        client.V1Job(
            metadata=client.V1ObjectMeta(name=build_job_name),
            spec=client.V1JobSpec(
                template=client.V1PodTemplateSpec(spec=pod_spec),
                backoff_limit=0,
                ttl_seconds_after_finished=300),
        ),
    )
    core.create_namespaced_config_map(
        NAMESPACE,
        client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=dockerfile_cm_name, owner_references=[_owner_reference_for(created_job)]),
            data={"Dockerfile": _generate_dockerfile(python_version, requirements)},
        ),
    )


def _delete_build_job_safely(job_id: str) -> None:
    try:
        batch.delete_namespaced_job(f"{job_id}-build", NAMESPACE, propagation_policy="Foreground")
    except client.ApiException:
        pass


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
                            if job.get("has_archive"):
                                await asyncio.to_thread(
                                    create_build_job, job_id, job["python_version"],
                                    job["entrypoint"], job["requirements"],
                                )
                                await asyncio.to_thread(
                                    report_job, job_id,
                                    status="BUILDING",
                                    status_message="Building your environment, this may take a few minutes",
                                )
                            else:
                                await asyncio.to_thread(
                                    create_k8s_job, job_id, job["entrypoint"], job["entrypoint_content"],
                                    job["requirements"], job["python_version"], job["gpu_type"], job["gpu_count"],
                                )
                                await asyncio.to_thread(
                                    report_job, job_id,
                                    status="SCHEDULED",
                                    status_message="Job scheduled, waiting for GPU capacity",
                                    scheduled_at=now.isoformat(),
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

                    if job["status"] == "BUILDING":
                        if now - submitted_at > BUILD_TIMEOUT:
                            await asyncio.to_thread(
                                report_job, job_id,
                                status="FAILED",
                                status_message="Job failed",
                                failure_reason=FAILURE_REASON_BUILD_TIMEOUT,
                                completed_at=now.isoformat(),
                            )
                            await asyncio.to_thread(_delete_build_job_safely, job_id)
                            continue

                        build_status = await asyncio.to_thread(k8s_status, f"{job_id}-build")
                        if build_status in (None, "SCHEDULED", "RUNNING"):
                            continue  # still building (or not observed yet); rechecked next tick

                        if build_status == "SUCCEEDED":
                            image_tag = _job_image_tag(job_id)
                            try:
                                await asyncio.to_thread(
                                    create_training_job_from_image, job_id, image_tag,
                                    job["entrypoint"], job["gpu_type"], job["gpu_count"],
                                )
                                await asyncio.to_thread(
                                    report_job, job_id,
                                    status="SCHEDULED",
                                    status_message="Job scheduled, waiting for GPU capacity",
                                    image_tag=image_tag,
                                    scheduled_at=now.isoformat(),
                                )
                            except Exception as e:
                                print(f"create_training_job_from_image failed for {job_id}: {e}")
                                await asyncio.to_thread(
                                    report_job, job_id,
                                    status="FAILED",
                                    status_message="Job failed",
                                    failure_reason=FAILURE_REASON_SCHEDULING_ERROR,
                                    completed_at=now.isoformat(),
                                )
                        else:  # build_status == "FAILED"
                            await asyncio.to_thread(
                                report_job, job_id,
                                status="FAILED",
                                status_message="Job failed",
                                failure_reason=FAILURE_REASON_BUILD_FAILED,
                                completed_at=now.isoformat(),
                            )
                        continue

                    if job["status"] == "SCHEDULED":
                        scheduled_at_str = job.get("scheduled_at")
                        anchor = datetime.fromisoformat(scheduled_at_str) if scheduled_at_str else submitted_at
                        if now - anchor > SCHEDULING_TIMEOUT:
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
