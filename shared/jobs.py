"""Shared configuration and Redis data model for API and workers.

Redis key layout (single Redis, no other database):
  queue:jobs               LIST   job ids waiting (the real work queue)
  processing:<worker>      LIST   job ids a worker has claimed (BLMOVE) - lets us recover from crashes
  jobs:processing          SET    ids currently PROCESSING
  jobs:index               ZSET   all job ids (score = numeric id)
  job:<id>                 HASH   job record
  batch:<id>               HASH   batch record + counters
  batches:index            ZSET   batch ids (score = creation time)
  stats                    HASH   submitted / completed / failed (lifetime of the demo run)
  workers:known            SET    worker (pod) names ever registered
  worker:<name>            HASH   heartbeat + live progress of one worker pod
  events                   LIST   recent activity events (JSON, newest first)
  timeline                 LIST   periodic samples used for the chart (JSON, newest first)
"""
from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
STORAGE = Path(os.getenv("STORAGE_PATH", "/storage"))
INPUT_DIR = STORAGE / "input"
OUTPUT_DIR = STORAGE / "output"
THUMB_DIR = STORAGE / "thumbnails"

QUEUE = "queue:jobs"
PROCESSING_SET = "jobs:processing"
JOB_INDEX = "jobs:index"
BATCH_INDEX = "batches:index"
EVENTS = "events"
TIMELINE = "timeline"

# A worker whose heartbeat is older than this is considered LOST (crashed / force-killed).
WORKER_LOST_AFTER = float(os.getenv("WORKER_LOST_AFTER", "6"))
HEARTBEAT_EVERY = float(os.getenv("HEARTBEAT_EVERY", "1"))

QUEUED, PROCESSING, COMPLETED, FAILED = "QUEUED", "PROCESSING", "COMPLETED", "FAILED"
STATUSES = (QUEUED, PROCESSING, COMPLETED, FAILED)


def get_redis() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def pod_name() -> str:
    return os.getenv("HOSTNAME") or socket.gethostname()


def now() -> float:
    return time.time()


def push_event(r, kind: str, message: str, **extra) -> None:
    ev = {"t": round(now(), 3), "kind": kind, "msg": message, **extra}
    pipe = r.pipeline()
    pipe.lpush(EVENTS, json.dumps(ev))
    pipe.ltrim(EVENTS, 0, 299)
    pipe.execute()


def new_job_ids(r, n: int) -> list[int]:
    last = r.incrby("job:seq", n)
    return list(range(last - n + 1, last + 1))


def create_batch(r, jobs: list[dict], meta: dict) -> str:
    """Persist a batch + its jobs and push every job id onto the Redis queue."""
    batch_id = f"b{r.incr('batch:seq')}"
    ids = new_job_ids(r, len(jobs))
    t = now()
    pipe = r.pipeline()
    pipe.hset(f"batch:{batch_id}", mapping={
        "id": batch_id, "created": t, "total": len(jobs), "queued": len(jobs),
        "processing": 0, "completed": 0, "failed": 0, "first": ids[0], "last": ids[-1],
        **{k: str(v) for k, v in meta.items()},
    })
    pipe.zadd(BATCH_INDEX, {batch_id: t})
    for jid, job in zip(ids, jobs):
        pipe.hset(f"job:{jid}", mapping={
            "id": jid, "batch": batch_id, "status": QUEUED, "created": t,
            "progress": 0, "stage": "queued", "worker": "", "error": "",
            "input_file": job["input_file"], "original_name": job["original_name"],
            "operation": job["operation"], "params": json.dumps(job.get("params", {})),
            "out_name": job["out_name"],
        })
        pipe.zadd(JOB_INDEX, {str(jid): jid})
    pipe.hincrby("stats", "submitted", len(jobs))
    pipe.rpush(QUEUE, *[str(i) for i in ids])
    pipe.execute()
    push_event(r, "batch", f"Batch {batch_id} submitted: {len(jobs)} jobs enqueued ({meta.get('label', '')})",
               batch=batch_id)
    return batch_id
