"""FastAPI server: validates uploads, creates jobs, enqueues them in Redis, reports status.

It NEVER processes images itself - the only pixel work here is the one-off synthetic
demo-image generator (a handful of source files); every job goes through Redis to a worker pod.
"""
from __future__ import annotations

import json
import logging
import random
import re
import shutil
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from shared import jobs as J
from shared.imageops import ALLOWED_EXT, AUGMENT_OPS, OPERATIONS, synthetic_image

from .k8s import K8sWatcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("api")
r = J.get_redis()
k8s = K8sWatcher()
MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # 50 MB per file
MAX_BODY_BYTES   = 2 * 1024 * 1024 * 1024  # 2 GiB total upload (large datasets)
MAX_FILES_PER_UPLOAD = 5000
MAX_JOBS_PER_BATCH = 5000
STATIC_DIR = Path(__file__).parent / "static"


# ------------------------------------------------------------------ background threads
def on_k8s_update(snap: dict):
    """Record real replica changes (queue autoscaling / manual scaling / self-healing)."""
    desired = snap["desired"]
    prev = r.get("scale:last")
    r.set("scale:last", desired)
    if prev is not None and int(prev) != desired:
        J.push_event(r, "scale", f"Kubernetes scaled workers {prev} → {desired}",
                     frm=int(prev), to=desired)
        r.lpush("scale:history", json.dumps({"t": J.now(), "from": int(prev), "to": desired}))
        r.ltrim("scale:history", 0, 49)


def sampler_loop():
    """Every 2 s store one timeline sample for the chart."""
    while True:
        try:
            queued = r.llen(J.QUEUE)
            processing = r.scard(J.PROCESSING_SET)
            k8s.scale_for_queue(queued, processing)
            snap = k8s.get()
            workers = collect_workers()
            sample = {
                "t": round(J.now(), 1), "queue": queued, "processing": processing,
                "completed": int(r.hget("stats", "completed") or 0),
                "replicas": snap["desired"] if snap.get("available") else len([w for w in workers if w["online"]]),
                "online": len([w for w in workers if w["online"]]),
                "cpu": None,
            }
            pipe = r.pipeline()
            pipe.lpush(J.TIMELINE, json.dumps(sample))
            pipe.ltrim(J.TIMELINE, 0, 599)
            pipe.execute()
        except Exception as e:
            log.warning("sampler: %s", e)
        time.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    for d in (J.INPUT_DIR, J.OUTPUT_DIR, J.THUMB_DIR):
        d.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=lambda: k8s.run(on_k8s_update), daemon=True).start()
    threading.Thread(target=sampler_loop, daemon=True).start()
    yield


app = FastAPI(title="Scalable Image Processing Service", lifespan=lifespan)

# ------------------------------------------------------------------ helpers
def safe_rel(path: str) -> PurePosixPath:
    """Sanitise a client-supplied (possibly folder-relative) path."""
    parts = [re.sub(r"[^A-Za-z0-9._ -]", "_", p) for p in PurePosixPath(path.replace("\\", "/")).parts
             if p not in ("", ".", "..", "/")]
    if not parts:
        raise HTTPException(400, "invalid file name")
    return PurePosixPath(*parts)


def parse_params(raw: str) -> dict:
    try:
        p = json.loads(raw or "{}")
        return p if isinstance(p, dict) else {}
    except json.JSONDecodeError:
        raise HTTPException(400, "params must be valid JSON")


def build_jobs(inputs: list[tuple[str, str]], operation: str, params: dict, total: Optional[int] = None) -> list[dict]:
    """inputs: (relative input file under INPUT_DIR, original relative name).

    - augment: every original yields `per_image` jobs  (100 images x 5 = 500 jobs)
    - other ops: one job per file, or `total` jobs cycling over the inputs (demo workload)
    """
    jobs: list[dict] = []
    seq = 0

    def add(inp: str, orig: str, p: dict, suffix: str):
        nonlocal seq
        seq += 1
        rel = PurePosixPath(orig)
        out = str(rel.parent / f"{rel.stem}_{suffix}_{seq:04d}") if str(rel.parent) != "." \
            else f"{rel.stem}_{suffix}_{seq:04d}"
        jobs.append({"input_file": inp, "original_name": orig, "operation": operation, "params": p,
                     "out_name": out})

    if operation == "augment":
        per = max(1, min(int(params.get("per_image", 5)), 200))
        ops = [o for o in params.get("augment_ops", AUGMENT_OPS) if o in AUGMENT_OPS] or AUGMENT_OPS
        for inp, orig in inputs:
            for k in range(per):
                add(inp, orig, {**params, "augment_ops": ops, "seed": random.randint(1, 10**9)}, f"aug{k + 1}")
    else:
        n = total or len(inputs)
        for i in range(n):
            inp, orig = inputs[i % len(inputs)]
            add(inp, orig, params, operation)
    if len(jobs) > MAX_JOBS_PER_BATCH:
        raise HTTPException(400, f"too many jobs ({len(jobs)}); max {MAX_JOBS_PER_BATCH}")
    return jobs


def fmt_batch(b: dict) -> dict:
    if not b:
        return {}
    out = dict(b)
    for k in ("total", "queued", "processing", "completed", "failed", "first", "last"):
        out[k] = int(float(out.get(k, 0)))
    out["created"] = float(out.get("created", 0))
    done = out["completed"] + out["failed"]
    out["percent"] = round(100 * done / out["total"], 1) if out["total"] else 0
    out["done"] = out["total"] > 0 and done >= out["total"]
    return out


def fmt_job(j: dict) -> dict:
    out = {k: j.get(k, "") for k in ("id", "batch", "status", "operation", "original_name", "worker", "stage",
                                     "error", "output_file")}
    out["id"] = int(j["id"])
    out["progress"] = int(float(j.get("progress", 0)))
    out["duration_ms"] = int(float(j["duration_ms"])) if j.get("duration_ms") else None
    out["created"] = float(j.get("created", 0))
    out["started"] = float(j["started"]) if j.get("started") else None
    out["finished"] = float(j["finished"]) if j.get("finished") else None
    out["params"] = json.loads(j.get("params") or "{}")
    return out


def collect_workers() -> list[dict]:
    names = list(r.smembers("workers:known"))
    if not names:
        return []
    pipe = r.pipeline()
    for n in names:
        pipe.hgetall(f"worker:{n}")
    t = J.now()
    workers = []
    for n, h in zip(names, pipe.execute()):
        if not h:
            r.srem("workers:known", n)
            continue
        age = t - float(h.get("last_seen", 0))
        status = h.get("status", "IDLE")
        if status == "TERMINATED":
            r.srem("workers:known", n)
            r.delete(f"worker:{n}")
            continue
        if status in ("IDLE", "PROCESSING") and age > J.WORKER_LOST_AFTER:
            status = "LOST"
        if status in ("TERMINATED", "LOST") and age > 120:  # prune old dead workers from the view
            r.srem("workers:known", n)
            r.delete(f"worker:{n}")
            continue
        done = int(float(h.get("completed", 0)))
        workers.append({
            "pod": n, "name": f"Worker-{h.get('ordinal', '?')}", "ordinal": int(h.get("ordinal", 0)),
            "status": status, "online": status in ("IDLE", "PROCESSING"),
            "job": h.get("job", ""), "op": h.get("op", ""), "progress": int(float(h.get("progress", 0))),
            "stage": h.get("stage", ""), "completed": done, "failed": int(float(h.get("failed", 0))),
            "avg_ms": round(float(h.get("busy_ms", 0)) / done) if done else None,
            "last_ms": int(float(h.get("last_ms", 0))) or None,
            "uptime": round(t - float(h.get("started_at", t))), "age": round(age, 1),
        })
    workers.sort(key=lambda w: w["ordinal"])
    return workers


# ------------------------------------------------------------------ job submission
@app.post("/api/process")
async def submit(request: Request):
    """Upload one or many images (optionally with folder-relative paths as a JSON list)."""
    # Explicit parser limits avoid Starlette's default 1000-file cap and let larger
    # datasets be uploaded in a single request. UploadFile streams to disk after
    # its spool threshold, so this does not buffer all image bytes in memory.
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_BYTES:
        raise HTTPException(413, "dataset upload exceeds 2 GiB limit")
    form = await request.form(max_files=MAX_FILES_PER_UPLOAD, max_fields=20)
    operation = str(form.get("operation", ""))
    params = str(form.get("params", "{}"))
    files = form.getlist("files")
    paths_raw = form.get("paths")
    if not files:
        raise HTTPException(400, "at least one image is required")
    if operation not in OPERATIONS:
        raise HTTPException(400, f"unknown operation '{operation}'")
    p = parse_params(params)
    try:
        rel_paths = json.loads(str(paths_raw)) if paths_raw else []
    except json.JSONDecodeError:
        raise HTTPException(400, "paths must be valid JSON")
    if not isinstance(rel_paths, list):
        raise HTTPException(400, "paths must be a JSON list")
    batch_dir = f"u{int(J.now())}_{random.randint(1000, 9999)}"
    inputs = []
    for i, f in enumerate(files):
        rel = safe_rel(rel_paths[i] if i < len(rel_paths) and rel_paths[i] else (f.filename or "image"))
        if rel.suffix.lower() not in ALLOWED_EXT:
            raise HTTPException(400, f"{rel.name}: unsupported type (JPG, PNG, WebP only)")
        data = f.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"{rel.name}: larger than 50 MB")
        dest = J.INPUT_DIR / batch_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        try:  # cheap validation of the header only
            with Image.open(dest) as im:
                im.verify()
        except Exception:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, f"{rel.name}: not a valid image")
        inputs.append((f"{batch_dir}/{rel}", str(rel)))
    jobs = build_jobs(inputs, operation, p)
    label = f"{operation} on {len(inputs)} image(s)" + (f" x{p.get('per_image', 5)} augmentations" if operation == "augment" else "")
    bid = J.create_batch(r, jobs, {"operation": operation, "label": label, "source": "upload"})
    return {"batch_id": bid, "jobs": len(jobs), "first_job": r.hget(f"batch:{bid}", "first")}


@app.post("/api/demo")
def demo_workload(body: dict):
    """Generate a large demo workload server-side.

    body: {operation, count, originals, params}
      - augment: originals x params.per_image jobs
      - other:   `count` jobs cycling over `originals` generated source images
    Jobs go through the real Redis queue and are processed by the real worker pods.
    """
    operation = body.get("operation", "resize")
    if operation not in OPERATIONS:
        raise HTTPException(400, "unknown operation")
    params = body.get("params") or {}
    originals = max(1, min(int(body.get("originals", 20)), 100))
    size = int(body.get("size", 1600))
    batch_dir = f"demo{int(J.now())}_{random.randint(1000, 9999)}"
    inputs = []
    for i in range(originals):
        cls = ("cat", "dog", "bird", "car")[i % 4]
        rel = f"{cls}/{cls}{i + 1:03d}.jpg"
        dest = J.INPUT_DIR / batch_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        synthetic_image(random.randint(1, 10**6), size, int(size * 0.75)).save(dest, "JPEG", quality=92)
        inputs.append((f"{batch_dir}/{rel}", rel))
    total = None if operation == "augment" else max(1, int(body.get("count", 100)))
    jobs = build_jobs(inputs, operation, params, total)
    label = (f"DEMO: {originals} originals × {params.get('per_image', 5)} augmentations"
             if operation == "augment" else f"DEMO: {operation} × {len(jobs)} images")
    bid = J.create_batch(r, jobs, {"operation": operation, "label": label, "source": "demo"})
    return {"batch_id": bid, "jobs": len(jobs)}


# ------------------------------------------------------------------ status endpoints
@app.get("/api/dashboard")
def dashboard():
    stats = r.hgetall("stats")
    queue_len = r.llen(J.QUEUE)
    proc_ids = sorted(r.smembers(J.PROCESSING_SET), key=int)
    queue_ids = r.lrange(J.QUEUE, 0, 7)
    pipe = r.pipeline()
    for jid in proc_ids[:12] + queue_ids:
        pipe.hgetall(f"job:{jid}")
    rows = [fmt_job(j) for j in pipe.execute() if j]
    queue_view = sorted([x for x in rows if x["status"] == J.PROCESSING], key=lambda x: x["id"]) + \
                 [x for x in rows if x["status"] == J.QUEUED]
    workers = collect_workers()
    batch_ids = r.zrevrange(J.BATCH_INDEX, 0, 7)
    batches = [fmt_batch(r.hgetall(f"batch:{b}")) for b in batch_ids]
    active = next((b for b in batches if not b["done"]), batches[0] if batches else None)
    snap = k8s.get()
    snap["pods"] = sorted(snap.get("pods", []), key=lambda p: p["created"])
    return {
        "now": J.now(),
        "totals": {
            "submitted": int(stats.get("submitted", 0)), "queued": queue_len,
            "processing": len(proc_ids), "completed": int(stats.get("completed", 0)),
            "failed": int(stats.get("failed", 0)),
            "active_workers": len([w for w in workers if w["online"]]),
        },
        "queue": {"length": queue_len, "view": queue_view},
        "workers": workers,
        "batch": active, "batches": batches,
        "kubernetes": snap,
        "scale_history": [json.loads(x) for x in r.lrange("scale:history", 0, 9)],
        "events": [json.loads(x) for x in r.lrange(J.EVENTS, 0, 39)],
    }


@app.get("/api/timeline")
def timeline(n: int = 150):
    return [json.loads(x) for x in reversed(r.lrange(J.TIMELINE, 0, max(1, min(n, 600)) - 1))]


@app.get("/api/jobs")
def list_jobs(batch: Optional[str] = None, status: Optional[str] = None, limit: int = 100, offset: int = 0):
    limit = max(1, min(limit, 500))
    if batch:
        b = r.hgetall(f"batch:{batch}")
        if not b:
            raise HTTPException(404, "batch not found")
        ids = list(range(int(b["last"]), int(b["first"]) - 1, -1))
    else:
        ids = [int(x) for x in r.zrevrange(J.JOB_INDEX, 0, 4999)]
    out, total = [], 0
    for chunk_start in range(0, len(ids), 500):
        chunk = ids[chunk_start:chunk_start + 500]
        pipe = r.pipeline()
        for jid in chunk:
            pipe.hgetall(f"job:{jid}")
        for j in pipe.execute():
            if not j or (status and j.get("status") != status.upper()):
                continue
            if total >= offset and len(out) < limit:
                out.append(fmt_job(j))
            total += 1
    return {"total": total, "jobs": out}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    j = r.hgetall(f"job:{job_id}")
    if not j:
        raise HTTPException(404, "job not found")
    return fmt_job(j)


@app.get("/api/batches")
def list_batches():
    return [fmt_batch(r.hgetall(f"batch:{b}")) for b in r.zrevrange(J.BATCH_INDEX, 0, 29)]


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str):
    b = r.hgetall(f"batch:{batch_id}")
    if not b:
        raise HTTPException(404, "batch not found")
    return fmt_batch(b)


def _file(base: Path, rel: str) -> FileResponse:
    p = (base / rel).resolve()
    if base.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(p)


@app.get("/api/jobs/{job_id}/result")
def job_result(job_id: int, download: bool = False):
    f = r.hget(f"job:{job_id}", "output_file")
    if not f:
        raise HTTPException(404, "result not ready")
    resp = _file(J.OUTPUT_DIR, f)
    if download:
        resp.headers["Content-Disposition"] = f'attachment; filename="{Path(f).name}"'
    return resp


@app.get("/api/jobs/{job_id}/input")
def job_input(job_id: int):
    f = r.hget(f"job:{job_id}", "input_file")
    if not f:
        raise HTTPException(404, "job not found")
    return _file(J.INPUT_DIR, f)


@app.get("/api/jobs/{job_id}/thumbnail")
def job_thumb(job_id: int):
    f = r.hget(f"job:{job_id}", "thumb")
    if not f:
        raise HTTPException(404, "thumbnail not ready")
    return _file(J.THUMB_DIR, f)


@app.get("/api/batches/{batch_id}/download")
def download_batch(batch_id: str):
    """Zip of all completed outputs, preserving the dataset/class folder structure."""
    b = r.hgetall(f"batch:{batch_id}")
    if not b:
        raise HTTPException(404, "batch not found")
    root = J.OUTPUT_DIR / batch_id
    if not root.exists():
        raise HTTPException(404, "no results yet")
    tmp = tempfile.TemporaryFile()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(root).as_posix())
    tmp.seek(0)
    return StreamingResponse(iter(lambda: tmp.read(1 << 20), b""), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{batch_id}_results.zip"'})


@app.get("/api/operations")
def operations():
    return {"operations": OPERATIONS, "augment_ops": AUGMENT_OPS}


@app.get("/api/health")
def health():
    r.ping()
    return {"ok": True, "queue": r.llen(J.QUEUE)}


@app.post("/api/reset")
def reset():
    """Clear all jobs/batches/events/outputs between demo runs (workers stay registered)."""
    for pattern in ("job:*", "batch:*", "processing:*", "scale:*"):
        for k in r.scan_iter(pattern, count=500):
            if k in ("job:seq", "batch:seq"):
                continue
            r.delete(k)
    r.delete(J.QUEUE, J.PROCESSING_SET, J.JOB_INDEX, J.BATCH_INDEX, J.EVENTS, J.TIMELINE, "stats", "scale:history")
    for d in (J.INPUT_DIR, J.OUTPUT_DIR, J.THUMB_DIR):
        for child in d.iterdir():
            shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
    J.push_event(r, "reset", "System reset - queue, jobs and files cleared")
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
