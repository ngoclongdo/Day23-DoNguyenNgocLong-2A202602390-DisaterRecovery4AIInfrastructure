"""Python runner to start services on Windows or Linux."""
import os
import pathlib
import subprocess
import sys
import time

import httpx

root = pathlib.Path(__file__).resolve().parent.parent
run_dir = root / "run"
run_dir.mkdir(parents=True, exist_ok=True)
reports_dir = root / "reports"
reports_dir.mkdir(parents=True, exist_ok=True)

flags = 0
if sys.platform == "win32":
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

# 1. Start Region A
env_a = os.environ.copy()
env_a["REGION"] = "a"
env_a["STATE_DIR"] = "state/region-a"
env_a["WARMUP_SECONDS"] = "6"
p_a = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "serving.app:app", "--host", "127.0.0.1", "--port", "8001", "--log-level", "warning"],
    cwd=str(root),
    env=env_a,
    stdout=open(run_dir / "region-a.log", "w", encoding="utf-8"),
    stderr=subprocess.STDOUT,
    creationflags=flags,
)
(run_dir / "region-a.pid").write_text(str(p_a.pid), encoding="utf-8")
print(f"region-a pid={p_a.pid} port=8001")

# 2. Start Region B
env_b = os.environ.copy()
env_b["REGION"] = "b"
env_b["STATE_DIR"] = "state/region-b"
env_b["WARMUP_SECONDS"] = "6"
p_b = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "serving.app:app", "--host", "127.0.0.1", "--port", "8002", "--log-level", "warning"],
    cwd=str(root),
    env=env_b,
    stdout=open(run_dir / "region-b.log", "w", encoding="utf-8"),
    stderr=subprocess.STDOUT,
    creationflags=flags,
)
(run_dir / "region-b.pid").write_text(str(p_b.pid), encoding="utf-8")
print(f"region-b pid={p_b.pid} port=8002")

# 3. Start Edge Proxy
env_edge = os.environ.copy()
env_edge["EDGE_TTL_SECONDS"] = "5"
p_edge = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "edge.proxy:app", "--host", "127.0.0.1", "--port", "8080", "--log-level", "warning"],
    cwd=str(root),
    env=env_edge,
    stdout=open(run_dir / "edge.log", "w", encoding="utf-8"),
    stderr=subprocess.STDOUT,
    creationflags=flags,
)
(run_dir / "edge.pid").write_text(str(p_edge.pid), encoding="utf-8")
print(f"edge pid={p_edge.pid} port=8080")

# 4. Verify readiness
print("Cho service len...")
all_up = True
for name, port in [("region-a", 8001), ("region-b", 8002), ("edge", 8080)]:
    up = False
    for _ in range(20):
        try:
            path = "/edge/state" if port == 8080 else "/healthz"
            r = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=1.0)
            if r.status_code == 200:
                up = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    if up:
        print(f"  {name} (port {port}): UP")
    else:
        print(f"  {name} (port {port}): FAILED")
        all_up = False

if not all_up:
    print("MOT SO SERVICE CHUA LEN")
    sys.exit(1)
print("All services up and ready!")
