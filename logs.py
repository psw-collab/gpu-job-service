import ast

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from kubernetes import client
import os
from kubernetes.client.exceptions import ApiException
from database import get_db
from models import DBJob

router = APIRouter()
core = client.CoreV1Api()


def _namespace():
    ns = os.getenv("K8S_NAMESPACE")
    if not ns:
        raise RuntimeError("K8S_NAMESPACE environment variable is required")
    return ns


def _decode_log_text(raw) -> str:
    """
    kubernetes-client (observed on 36.0.2) sometimes returns pod log bytes
    already stringified via str(bytes) instead of decoded -- e.g. "b'hi\\n'"
    instead of "hi\n". Undo that so callers get real text.
    """
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    if isinstance(raw, str) and raw.startswith(("b'", 'b"')):
        try:
            return ast.literal_eval(raw).decode("utf-8", errors="replace")
        except (ValueError, SyntaxError):
            return raw
    return raw


@router.get("/v1/jobs/{job_id}/logs", response_class=PlainTextResponse)
def get_job_logs(job_id: str, tail_lines: int = 1000, db: Session = Depends(get_db)):
    job = db.query(DBJob).filter(DBJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    namespace = _namespace()

    try:
        pods = core.list_namespaced_pod(
            namespace, label_selector=f"job-name={job_id}"
        ).items
    except ApiException as e:
        raise HTTPException(status_code=502, detail=f"Could not reach cluster: {e.reason}")

    if not pods:
        return PlainTextResponse(
            f"No logs available yet for {job_id} (status: {job.status}).",
            status_code=200,
        )

    pod_name = pods[0].metadata.name

    try:
        raw_logs = core.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines,
        )
    except ApiException as e:
        if e.status == 400:
            return PlainTextResponse(
                f"Logs not ready yet for {job_id} (status: {job.status}).",
                status_code=200,
            )
        raise HTTPException(status_code=502, detail=f"Could not fetch logs: {e.reason}")

    logs = _decode_log_text(raw_logs)
    return PlainTextResponse(logs or "(no output)")