"""Demo-day helper scripts (run from the project root with Python).

Usage:
  python scripts/demo.py scale <n>     -- scale the worker deployment to n replicas
  python scripts/demo.py kill <pod>    -- delete a worker pod (self-healing demo)
  python scripts/demo.py pods          -- list worker pods with status
  python scripts/demo.py workload      -- push a 500-job blur workload via the API
  python scripts/demo.py reset         -- clear queue, jobs, files via API
  python scripts/demo.py autoscale     -- watch queue-driven worker scaling

All kubectl commands target the 'imgproc' namespace.
"""
import json
import subprocess
import sys
import time
import urllib.request

NS = "imgproc"
API = "http://localhost:30080"  # NodePort in Kubernetes; change to http://127.0.0.1:8000 for dev


def kubectl(*args, check=True):
    cmd = ["kubectl", "-n", NS, *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print("kubectl error:", r.stderr.strip())
    return r


def api_post(path, body=None):
    data = json.dumps(body).encode() if body else b""
    req = urllib.request.Request(API + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=30))


def cmd_pods():
    r = kubectl("get", "pods", "-l", "app=image-worker", "-o", "wide")
    print(r.stdout or r.stderr)


def cmd_scale(n):
    n = int(n)
    kubectl("scale", "deployment/image-worker", f"--replicas={n}")
    print(f"Scaled image-worker to {n} replicas. Watching...")
    kubectl("rollout", "status", "deployment/image-worker", "--timeout=60s", check=False)
    cmd_pods()


def cmd_kill(pod):
    r = kubectl("delete", "pod", pod, "--grace-period=0", "--force")
    print(r.stdout or r.stderr)
    print(f"Pod {pod} deleted. Kubernetes will recreate it automatically.")
    time.sleep(1)
    cmd_pods()


def cmd_workload():
    print("Pushing 500-job Heavy blur workload to the API...")
    r = api_post("/api/demo", {
        "operation": "blur", "count": 500, "originals": 20, "size": 1600,
        "params": {"radius": 6, "passes": 20}
    })
    print(f"Batch {r['batch_id']}: {r['jobs']} jobs pushed into Redis queue.")


def cmd_reset():
    api_post("/api/reset")
    print("System reset: queue, jobs, events and files cleared.")


def cmd_autoscale():
    print("Queue-driven worker replicas (Ctrl-C to stop):")
    while True:
        r = kubectl("get", "deployment", "image-worker", "--no-headers")
        print(r.stdout.strip())
        time.sleep(3)


CMDS = {"pods": cmd_pods, "scale": cmd_scale, "kill": cmd_kill,
        "workload": cmd_workload, "reset": cmd_reset,
        "autoscale": cmd_autoscale, "hpa": cmd_autoscale}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print(__doc__)
        sys.exit(0)
    CMDS[sys.argv[1]](*sys.argv[2:])
