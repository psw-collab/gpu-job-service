"""
Runs inside the cluster that actually has GPU nodes (in-cluster credentials
via config.load_incluster_config(), see k8s_ops.py). Talks to Postgres over
the network -- no inbound connectivity from the gateway is required.

Deployed as a long-running Pod, not hit directly by end users. The /healthz
endpoint exists only for the Deployment's liveness/readiness probes.
"""
import asyncio

from fastapi import FastAPI

import k8s_ops

app = FastAPI(title="GPU Job-as-a-Service Worker")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.on_event("startup")
async def start_loops():
    asyncio.create_task(k8s_ops.reconcile_loop())
    asyncio.create_task(k8s_ops.gc_loop())
