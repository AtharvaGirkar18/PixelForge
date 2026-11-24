"""Kubernetes worker autoscaling and live Deployment/Pod state for the dashboard.

Runs a small background thread. Outside a cluster (docker compose / local dev) it reports
available=False and the dashboard falls back to worker heartbeats only - nothing is faked.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("k8s")

DEPLOYMENT = os.getenv("WORKER_DEPLOYMENT", "image-worker")
POD_SELECTOR = os.getenv("WORKER_POD_SELECTOR", "app=image-worker")
NS_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
MIN_WORKERS = max(1, int(os.getenv("WORKER_MIN_REPLICAS", "2")))
MAX_WORKERS = max(MIN_WORKERS, int(os.getenv("WORKER_MAX_REPLICAS", "8")))
JOBS_PER_WORKER = max(1, int(os.getenv("JOBS_PER_WORKER", "8")))


def scaling_limits() -> dict:
    return {"min": MIN_WORKERS, "max": MAX_WORKERS, "current": None,
            "desired": None, "cpu_current": None, "cpu_target": None}


class K8sWatcher:
    def __init__(self, interval: float = 2.0):
        self.interval = interval
        self.snapshot: dict = {"available": False, "reason": "not started", "hpa": scaling_limits(),
                              "autoscaler": "queue-depth"}
        self._lock = threading.Lock()
        self._apps = self._core = None
        self._last_scale_target = None
        self.namespace = os.getenv("K8S_NAMESPACE") or (NS_FILE.read_text().strip() if NS_FILE.exists() else "default")

    def _connect(self) -> bool:
        try:
            from kubernetes import client, config
            config.load_incluster_config()
            self._apps, self._core = client.AppsV1Api(), client.CoreV1Api()
            return True
        except Exception as e:  # not in a cluster
            with self._lock:
                self.snapshot = {"available": False, "reason": f"not running inside Kubernetes ({type(e).__name__})",
                                 "hpa": scaling_limits(), "autoscaler": "queue-depth"}
            return False

    def _poll(self) -> dict:
        dep = self._apps.read_namespaced_deployment(DEPLOYMENT, self.namespace)
        snap = {
            "available": True, "namespace": self.namespace, "deployment": DEPLOYMENT,
            "desired": dep.spec.replicas or 0,
            "ready": dep.status.ready_replicas or 0,
            "current": dep.status.replicas or 0,
            "hpa": {
                "min": MIN_WORKERS,
                "max": MAX_WORKERS,
                "current": dep.status.replicas or 0,
                "desired": dep.spec.replicas or 0,
                "cpu_current": None,
                "cpu_target": None,
            },
            "autoscaler": "queue-depth", "pods": [],
        }
        pods = self._core.list_namespaced_pod(self.namespace, label_selector=POD_SELECTOR).items
        for p in pods:
            ready = any(c.type == "Ready" and c.status == "True" for c in (p.status.conditions or []))
            snap["pods"].append({
                "name": p.metadata.name, "phase": p.status.phase, "ready": ready,
                "terminating": p.metadata.deletion_timestamp is not None,
                "created": p.metadata.creation_timestamp.timestamp() if p.metadata.creation_timestamp else 0,
            })
        return snap

    def run(self, on_update=None):
        connected = False
        while True:
            try:
                if not connected:
                    connected = self._connect()
                if connected:
                    snap = self._poll()
                    with self._lock:
                        self.snapshot = snap
                    if on_update:
                        on_update(snap)
            except Exception as e:
                log.warning("k8s poll failed: %s", e)
                with self._lock:
                    self.snapshot = {"available": False, "reason": f"Kubernetes API error: {str(e)[:100]}",
                                     "hpa": scaling_limits(), "autoscaler": "queue-depth"}
            time.sleep(self.interval if connected else 10)

    def get(self) -> dict:
        with self._lock:
            return dict(self.snapshot)

    def scale_for_queue(self, queue_depth: int, processing: int = 0) -> None:
        """Scale workers from queued + active work, regardless of image operation.

        CPU-based HPA misses short or lightweight operations. Queue depth is the
        workload signal the service actually needs to drain, and is available for
        every operation and dataset source.
        """
        if not self._apps:
            return
        minimum, maximum, jobs_per_worker = MIN_WORKERS, MAX_WORKERS, JOBS_PER_WORKER
        desired = min(maximum, max(minimum, (max(0, queue_depth) + max(0, processing) + jobs_per_worker - 1) // jobs_per_worker))
        try:
            deployment = self._apps.read_namespaced_deployment(DEPLOYMENT, self.namespace)
            current = deployment.spec.replicas or minimum
            if desired != current and desired != self._last_scale_target:
                self._apps.patch_namespaced_deployment_scale(
                    DEPLOYMENT, self.namespace, {"spec": {"replicas": desired}})
                self._last_scale_target = desired
                log.info("queue autoscaler changed workers %s -> %s (queued=%s, processing=%s)",
                         current, desired, queue_depth, processing)
            elif desired == current:
                self._last_scale_target = None
        except Exception as e:
            log.warning("queue autoscaler failed: %s", e)
