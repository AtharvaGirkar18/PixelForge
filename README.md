# PixelForge Cloud — Distributed Image Processing on Kubernetes

A containerised, Kubernetes-native image-processing service that demonstrates **horizontal pod autoscaling**, **self-healing**, **persistent shared storage**, **Redis-backed distributed queuing**, and a **real-time monitoring dashboard** — all wired to live Kubernetes state, never simulated.

```
Browser  ──→  FastAPI API Pod  ──→  Redis Queue  ──→  Worker Pods (queue autoscaler)  ──→  PersistentVolume
    ↑                  ↑                                      ↑
 Real-time         K8s RBAC                         Heartbeat / Reaper
  polling       (watches pods,                      (self-healing from
                scales workers                        crashed pods)
                from queue depth)
```

---

## Prerequisites

- Docker Desktop (with Kubernetes enabled) **or** Minikube >= 1.32
- `kubectl` configured against your cluster
- Python 3.10+ (for local dev / tests only)

Worker autoscaling uses Redis queue depth and does not require metrics-server.

---

## Quick Start (Kubernetes)

```bat
REM Windows (PowerShell or CMD)
build.bat
```

```bash
# Mac / Linux / WSL
./build.sh
```

After ~60 s, open the UI:

```bat
REM Minikube
minikube service image-api -n imgproc

REM Docker Desktop Kubernetes
start http://localhost:30080
REM Or: kubectl port-forward svc/image-api 30080:8000 -n imgproc -> http://localhost:30080
```

### Useful kubectl commands

```bash
kubectl get pods -n imgproc -w
kubectl top pods -n imgproc
kubectl get deployment/image-worker -n imgproc -w
kubectl scale deployment/image-worker --replicas=5 -n imgproc
kubectl delete pod <worker-pod-name> -n imgproc   # self-healing demo
```

---

## Quick Start (Local Dev)

No Kubernetes needed.

```bat
dev.bat        # Windows: opens 5 command windows (fakeredis + api + 3 workers)
dev.bat 5      # start with 5 workers
dev.bat stop   # kill everything
```

Then open http://127.0.0.1:8000. Local dev and Docker Compose use a fixed worker count; queue-based autoscaling requires the Kubernetes deployment above.

---

## Running Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

All 20 unit tests cover `imageops` operations and the Redis job state machine.

---

## Demo Guide (Viva)

### Feature 1 - Upload & process real images

1. Open the UI at `http://localhost:8000` (or `http://localhost:30080` if using Minikube or port-forward)
2. Click **Upload & Process**
3. Upload any JPEG/PNG, choose an operation, click Submit
4. Watch the pipeline cards update in real time: Queued -> Processing -> Completed
5. Click **Results**, find the job, click **Download**

### Feature 2 - Large batch workload (queue buildup)

```bash
python scripts/demo.py workload   # pushes 500 blur jobs via the API
```

Watch the **Queue depth** and **Active workers** counters rise immediately.

### Feature 3 - Queue-driven worker autoscaling

Worker replicas scale from queued and active jobs, regardless of which image operation is selected.

```bash
kubectl get deployment/image-worker -n imgproc -w  # watch scale-out happen live
python scripts/demo.py pods       # list worker pods with status
```

The dashboard **Kubernetes** section shows `desired`, `available`, and the pod list updating live.

### Feature 4 - Self-Healing

```bash
kubectl get pods -n imgproc
kubectl delete pod <pod-name> -n imgproc --grace-period=0 --force
```

Watch in the dashboard:
- The killed pod disappears from the pod list
- Within 30 s Kubernetes recreates it (ReplicaSet controller)
- The worker **Reaper** reclaims any jobs that were mid-processing

### Feature 5 - Persistent Storage

Even after deleting all pods, completed images remain on the PVC.
The **Results** tab downloads them from the same PVC path.

### Feature 6 - Manual scaling

```bash
python scripts/demo.py scale 5   # scale to 5 replicas
python scripts/demo.py scale 1   # scale back down
```

### Reset between demos

```bash
python scripts/demo.py reset     # clears queue, jobs, and output files
```

---

## Project Layout

```
ACC PROJECT/
├── api/
│   ├── main.py          FastAPI server (job submission, status, K8s reflection)
│   ├── k8s.py           Kubernetes queue autoscaler + Deployment / Pod watcher
│   ├── Dockerfile
│   └── static/
│       ├── index.html   Dashboard UI
│       ├── style.css    Dark-glass design system
│       └── app.js       Real-time polling + chart rendering
├── worker/
│   ├── worker.py        Queue consumer; heartbeat; reaper
│   └── Dockerfile
├── shared/
│   ├── imageops.py      Pillow/NumPy image operations
│   └── jobs.py          Redis schemas, job/batch/worker state machine
├── k8s/
│   ├── 00-base.yaml     Namespace, PV/PVC, ConfigMap, RBAC
│   ├── 10-redis.yaml    Redis Deployment + Service
│   ├── 20-api.yaml      API Deployment + NodePort + scaling RBAC
│   └── 30-worker.yaml   Worker Deployment
├── scripts/
│   ├── dev_redis.py     fakeredis server (local dev)
│   ├── demo.py          Demo-day CLI (scale/kill/workload/reset/autoscale)
│   └── smoke.py         End-to-end smoke test
├── tests/
│   └── test_imageops.py 20 unit tests
├── build.bat / build.sh Build + deploy scripts
├── dev.bat              Local dev launcher (Windows)
├── docker-compose.yml   Alternative docker-compose quick-start
└── requirements.txt
```

---

## Architecture

| Layer | Component | Description |
|---|---|---|
| Frontend | `api/static/` | Dark-glass dashboard; polls `/api/dashboard` every 1 s |
| API | `api/main.py` | FastAPI; validates uploads, enqueues jobs, reads live K8s state |
| K8s Watcher | `api/k8s.py` | Background thread; scales from queue depth and reads Deployment/Pod state |
| Queue | Redis | RPUSH (enqueue) / BLPOP (dequeue) |
| Workers | `worker/worker.py` | Multi-threaded; heartbeat every 5 s; reaper for crashed pods |
| Image ops | `shared/imageops.py` | Pillow + NumPy: blur, sharpen, resize, grayscale, edge-detect, augment |
| Storage | Kubernetes PV/PVC | Host-path PV mounted RW by API and worker pods |

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Ping |
| GET | `/api/dashboard` | Full system snapshot (totals, workers, K8s, events) |
| GET | `/api/timeline?n=150` | Time-series samples for the chart |
| POST | `/api/process` | Upload images + submit batch (multipart/form-data) |
| POST | `/api/demo` | Generate server-side demo workload (JSON body) |
| POST | `/api/reset` | Clear queue / jobs / files |
| GET | `/api/batches` | List all batches |
| GET | `/api/batches/{id}` | Batch detail |
| GET | `/api/batches/{id}/download` | ZIP of all completed results |
| GET | `/api/jobs` | List jobs (filter by batch / status) |
| GET | `/api/jobs/{id}` | Single job detail |
| GET | `/api/jobs/{id}/result` | Processed image |
| GET | `/api/jobs/{id}/input` | Original image |
| GET | `/api/jobs/{id}/thumbnail` | Thumbnail |
| GET | `/api/operations` | List supported operations |
