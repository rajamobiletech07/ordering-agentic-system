"""Block until every service's /healthz answers (used by CI and demo scripts)."""

import sys
import time
import urllib.request

URLS = ["http://localhost:8000/healthz", "http://localhost:8010/healthz", "http://localhost:8020/healthz",
        "http://localhost:9001/healthz", "http://localhost:9002/healthz", "http://localhost:9003/healthz",
        "http://localhost:9004/healthz"]
deadline = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 180)
pending = list(URLS)
while pending and time.time() < deadline:
    for url in list(pending):
        try:
            urllib.request.urlopen(url, timeout=2)
            pending.remove(url)
            print(f"ready {url}")
        except Exception:
            pass
    time.sleep(1)
if pending:
    sys.exit(f"not ready: {pending}")
