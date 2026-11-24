"""Auto-sync script: watches api/static/ and instantly syncs any saved file
into the running Kubernetes image-api pod using `kubectl cp`.
"""
import os
import subprocess
import time
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "api" / "static"
NAMESPACE = "imgproc"


def get_api_pod():
    try:
        res = subprocess.run(
            ["kubectl", "get", "pods", "-n", NAMESPACE, "-l", "app=image-api",
             "-o", "jsonpath={.items[0].metadata.name}"],
            capture_output=True, text=True, timeout=30
        )
        pod = res.stdout.strip()
        return pod if pod else None
    except Exception as e:
        print(f"[!] Error finding pod: {e}", flush=True)
        return None


def main():
    print(f"[*] Frontend live auto-sync starting...", flush=True)
    pod = get_api_pod()
    while not pod:
        print("[!] Waiting for image-api pod to become available...", flush=True)
        time.sleep(3)
        pod = get_api_pod()

    print(f"[OK] Connected to pod: {pod}", flush=True)
    print(f"[*] Watching for file edits in: {STATIC_DIR}", flush=True)

    # Initial mtimes
    mtimes = {}
    for f in STATIC_DIR.glob("*"):
        if f.is_file():
            try:
                mtimes[f.name] = os.path.getmtime(f)
            except OSError:
                pass

    while True:
        try:
            for file_path in STATIC_DIR.glob("*"):
                if not file_path.is_file():
                    continue
                try:
                    mtime = os.path.getmtime(file_path)
                except OSError:
                    continue

                if file_path.name in mtimes:
                    if mtime > mtimes[file_path.name]:
                        mtimes[file_path.name] = mtime
                        t0 = time.time()
                        rel_path = f"api/static/{file_path.name}"
                        cmd = [
                            "kubectl", "cp",
                            rel_path,
                            f"{NAMESPACE}/{pod}:/app/api/static/{file_path.name}"
                        ]
                        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                        if res.returncode == 0:
                            elapsed = int((time.time() - t0) * 1000)
                            print(f"[SYNC] {file_path.name} -> pod ({elapsed}ms)", flush=True)
                        else:
                            print(f"[!] Copy failed: {res.stderr.strip()}", flush=True)
                            # Re-verify pod in case it was restarted
                            new_pod = get_api_pod()
                            if new_pod:
                                pod = new_pod
                else:
                    mtimes[file_path.name] = mtime

            time.sleep(0.4)
        except Exception as e:
            time.sleep(1)


if __name__ == "__main__":
    main()
