"""Queue worker. Every Kubernetes worker pod runs this same image.

Loop:  BLMOVE queue -> processing:<pod>  ->  process with Pillow  ->  mark COMPLETED
A separate thread publishes a 1 s heartbeat (the live progress the dashboard shows).
A reaper re-queues jobs claimed by workers that died without finishing (kubectl delete --force, OOM...).
"""
import json
import logging
import signal
import threading
import time
from pathlib import Path

from shared import jobs as J
from shared.imageops import make_thumbnail, run_operation

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

NAME = J.pod_name()
stop = threading.Event()
r = J.get_redis()
state_lock = threading.Lock()
state = {"status": "IDLE", "job": "", "op": "", "progress": 0, "stage": "idle", "name": NAME}
counters = {"completed": 0, "failed": 0, "busy_ms": 0, "last_ms": 0}
started_at = J.now()


def publish(**fields):
    """Update local state and flush it to Redis immediately."""
    with state_lock:
        state.update(fields)
        snap = {**state, **counters, "last_seen": J.now()}
    r.hset(f"worker:{NAME}", mapping={k: str(v) for k, v in snap.items()})


def register():
    # Allocate the lowest free ordinal among workers with a live heartbeat.
    # Lua makes the selection and registration atomic when pods start together.
    ordinal = r.eval("""
        local members = redis.call('SMEMBERS', KEYS[1])
        local used = {}
        for _, name in ipairs(members) do
            if name ~= ARGV[1] then
                local key = 'worker:' .. name
                local status = redis.call('HGET', key, 'status')
                local seen = tonumber(redis.call('HGET', key, 'last_seen') or '0')
                if (status == 'IDLE' or status == 'PROCESSING') and tonumber(ARGV[2]) - seen <= tonumber(ARGV[3]) then
                    local n = tonumber(redis.call('HGET', key, 'ordinal'))
                    if n then used[n] = true end
                end
            end
        end
        local ordinal = 1
        while used[ordinal] do ordinal = ordinal + 1 end
        local key = 'worker:' .. ARGV[1]
        redis.call('HSET', key, 'ordinal', ordinal, 'started_at', ARGV[2], 'last_seen', ARGV[2], 'status', 'IDLE')
        redis.call('SADD', KEYS[1], ARGV[1])
        redis.call('HDEL', key, 'lost_reported')
        return ordinal
    """, 1, "workers:known", NAME, started_at, J.WORKER_LOST_AFTER)
    publish(status="IDLE")
    J.push_event(r, "worker_online", f"Worker-{ordinal} ({NAME}) came online", worker=f"Worker-{ordinal}")
    return ordinal


def heartbeat_loop():
    while not stop.wait(J.HEARTBEAT_EVERY):
        try:
            publish()
        except Exception as e:  # keep beating through transient Redis errors
            log.warning("heartbeat failed: %s", e)


def label() -> str:
    return f"Worker-{r.hget(f'worker:{NAME}', 'ordinal')}"


# ---------------------------------------------------------------- reaper (fault tolerance)
def reap_dead_workers():
    for key in r.scan_iter("processing:*", count=100):
        owner = key.split(":", 1)[1]
        if owner == NAME:
            continue
        w = r.hgetall(f"worker:{owner}")
        alive = w and w.get("status") != "TERMINATED" and J.now() - float(w.get("last_seen", 0)) < J.WORKER_LOST_AFTER
        if alive:
            continue
        if w and w.get("status") not in ("TERMINATED", "LOST") and r.hsetnx(f"worker:{owner}", "lost_reported", "1"):
            r.hset(f"worker:{owner}", "status", "LOST")
            J.push_event(r, "worker_lost", f"Worker-{w.get('ordinal', '?')} ({owner}) lost - heartbeat stopped",
                         worker=f"Worker-{w.get('ordinal', '?')}")
        for jid in r.lrange(key, 0, -1):
            if r.lrem(key, 1, jid) != 1:  # another reaper already took it
                continue
            batch = r.hget(f"job:{jid}", "batch")
            pipe = r.pipeline()
            pipe.hset(f"job:{jid}", mapping={"status": J.QUEUED, "worker": "", "progress": 0, "stage": "requeued"})
            pipe.srem(J.PROCESSING_SET, jid)
            if batch:
                pipe.hincrby(f"batch:{batch}", "processing", -1)
                pipe.hincrby(f"batch:{batch}", "queued", 1)
            pipe.lpush(J.QUEUE, jid)  # front of the queue: resume ASAP
            pipe.execute()
            J.push_event(r, "requeued", f"Job {jid} re-queued after {owner} failed", job=jid)
            log.warning("re-queued job %s from dead worker %s", jid, owner)


def reaper_loop():
    while not stop.wait(3):
        try:
            reap_dead_workers()
        except Exception as e:
            log.warning("reaper error: %s", e)


# ---------------------------------------------------------------- job execution
def process_job(jid: str):
    key = f"job:{jid}"
    job = r.hgetall(key)
    if not job:
        r.lrem(f"processing:{NAME}", 1, jid)
        return
    batch, wl = job["batch"], label()
    t0 = time.perf_counter()
    started = J.now()
    pipe = r.pipeline()
    pipe.hset(key, mapping={"status": J.PROCESSING, "worker": wl, "pod": NAME, "started": started,
                            "progress": 5, "stage": "starting"})
    pipe.sadd(J.PROCESSING_SET, jid)
    pipe.hincrby(f"batch:{batch}", "queued", -1)
    pipe.hincrby(f"batch:{batch}", "processing", 1)
    pipe.execute()
    publish(status="PROCESSING", job=jid, op=job["operation"], progress=5, stage="starting")
    J.push_event(r, "started", f"{wl} started job {jid} ({job['operation']} {job['original_name']})",
                 worker=wl, job=jid)

    def progress(pct: int, stage: str):
        r.hset(key, mapping={"progress": pct, "stage": stage})
        publish(progress=pct, stage=stage)

    try:
        params = json.loads(job.get("params") or "{}")
        src = J.INPUT_DIR / job["input_file"]
        dest_base = J.OUTPUT_DIR / batch / job["out_name"]
        out = run_operation(src, dest_base, job["operation"], params, progress)
        thumb = J.THUMB_DIR / f"{jid}.jpg"
        make_thumbnail(out, thumb)
        ms = int((time.perf_counter() - t0) * 1000)
        pipe = r.pipeline()
        pipe.hset(key, mapping={"status": J.COMPLETED, "progress": 100, "stage": "done", "finished": J.now(),
                                "duration_ms": ms, "output_file": str(out.relative_to(J.OUTPUT_DIR)),
                                "thumb": thumb.name, "out_bytes": out.stat().st_size})
        pipe.srem(J.PROCESSING_SET, jid)
        pipe.lrem(f"processing:{NAME}", 1, jid)
        pipe.hincrby(f"batch:{batch}", "processing", -1)
        pipe.hincrby(f"batch:{batch}", "completed", 1)
        pipe.hincrby("stats", "completed", 1)
        pipe.execute()
        counters["completed"] += 1
        counters["busy_ms"] += ms
        counters["last_ms"] = ms
        publish(status="IDLE", job="", op="", progress=100, stage="idle")
        J.push_event(r, "completed", f"{wl} completed job {jid} in {ms} ms", worker=wl, job=jid, ms=ms)
    except Exception as e:  # noqa: BLE001 - any failure marks the job FAILED, worker keeps running
        log.exception("job %s failed", jid)
        pipe = r.pipeline()
        pipe.hset(key, mapping={"status": J.FAILED, "stage": "failed", "error": str(e)[:300],
                                "finished": J.now()})
        pipe.srem(J.PROCESSING_SET, jid)
        pipe.lrem(f"processing:{NAME}", 1, jid)
        pipe.hincrby(f"batch:{batch}", "processing", -1)
        pipe.hincrby(f"batch:{batch}", "failed", 1)
        pipe.hincrby("stats", "failed", 1)
        pipe.execute()
        counters["failed"] += 1
        publish(status="IDLE", job="", op="", progress=0, stage="idle")
        J.push_event(r, "failed", f"{wl} FAILED job {jid}: {str(e)[:80]}", worker=wl, job=jid)


def main():
    for d in (J.INPUT_DIR, J.OUTPUT_DIR, J.THUMB_DIR):
        d.mkdir(parents=True, exist_ok=True)

    def on_term(signum, frame):
        log.info("SIGTERM received - finishing current job then exiting")
        stop.set()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    ordinal = register()
    log.info("worker %s (Worker-%s) ready", NAME, ordinal)
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=reaper_loop, daemon=True).start()

    while not stop.is_set():
        try:
            jid = r.blmove(J.QUEUE, f"processing:{NAME}", 2, src="LEFT", dest="RIGHT")
        except Exception as e:
            log.warning("redis error: %s", e)
            time.sleep(1)
            continue
        if jid:
            process_job(jid)

    publish(status="TERMINATED", job="", stage="terminated")
    J.push_event(r, "worker_stopped", f"Worker-{ordinal} ({NAME}) terminated", worker=f"Worker-{ordinal}")
    log.info("worker stopped")


if __name__ == "__main__":
    main()
