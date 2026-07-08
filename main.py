"""
Local single-process dev entrypoint: runs the gateway's HTTP routes and the
worker's background loops in the same process, against whatever cluster your
kubeconfig/VPN currently points at. The loops talk back to this same
process's own HTTP server over loopback, exactly like the real worker talks
to the real gateway over the internet -- just localhost instead of a public
URL. For the actual GCP deployment, gateway.py and worker.py run as two
separate services instead -- see k8s_ops.py.
"""
import asyncio
import os

os.environ.setdefault("GATEWAY_URL", "http://127.0.0.1:8000")
os.environ.setdefault("INTERNAL_TOKEN", "local-dev-token")
os.environ.setdefault("API_KEY", "local-dev-key")

import k8s_ops
from gateway import app

__all__ = ["app"]


@app.on_event("startup")
async def start_background_loops():
    asyncio.create_task(k8s_ops.reconcile_loop())
    asyncio.create_task(k8s_ops.gc_loop())
