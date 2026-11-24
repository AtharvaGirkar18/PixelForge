import json, time, urllib.request

def call(path, body=None):
    req = urllib.request.Request("http://127.0.0.1:8000" + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    return json.load(urllib.request.urlopen(req, timeout=60))

print(call("/api/health"))
print(call("/api/demo", {"operation": "blur", "count": 40, "originals": 4, "size": 1200, "params": {"radius": 6}}))
for _ in range(25):
    d = call("/api/dashboard")
    t = d["totals"]
    print(t, [(w["name"], w["status"], w["progress"], w["completed"]) for w in d["workers"]])
    if t["completed"] + t["failed"] >= 40:
        break
    time.sleep(1)
print(call("/api/jobs?limit=2"))
print([e["msg"] for e in d["events"][:4]])
