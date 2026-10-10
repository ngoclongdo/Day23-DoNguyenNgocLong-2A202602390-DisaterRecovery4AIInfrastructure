"""Script to automate Drill 1 (No DR baseline) and Drill 2 (With DR) end-to-end."""
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402
from tools.measure_rto import measure  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUN = ROOT / "run"
REPORTS = ROOT / "reports"
RUN.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)


def stop_all_services():
    """Stop any running region/edge services."""
    for f in RUN.glob("*.pid"):
        try:
            pid = int(f.read_text(encoding="utf-8").strip())
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], capture_output=True)
                else:
                    os.kill(pid, getattr(signal, "SIGTERM", 15))
            except OSError:
                pass
        except Exception:
            pass
        f.unlink(missing_ok=True)
    time.sleep(1.0)


def start_services():
    """Start Region A (8001), Region B (8002), Edge (8080)."""
    stop_all_services()

    # Region A
    env_a = os.environ.copy()
    env_a["REGION"] = "a"
    env_a["STATE_DIR"] = "state/region-a"
    env_a["WARMUP_SECONDS"] = "6"
    p_a = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "serving.app:app", "--host", "127.0.0.1", "--port", "8001", "--log-level", "warning"],
        cwd=str(ROOT),
        env=env_a,
        stdout=open(RUN / "region-a.log", "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    (RUN / "region-a.pid").write_text(str(p_a.pid), encoding="utf-8")

    # Region B
    env_b = os.environ.copy()
    env_b["REGION"] = "b"
    env_b["STATE_DIR"] = "state/region-b"
    env_b["WARMUP_SECONDS"] = "6"
    p_b = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "serving.app:app", "--host", "127.0.0.1", "--port", "8002", "--log-level", "warning"],
        cwd=str(ROOT),
        env=env_b,
        stdout=open(RUN / "region-b.log", "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    (RUN / "region-b.pid").write_text(str(p_b.pid), encoding="utf-8")

    # Edge Proxy
    env_edge = os.environ.copy()
    env_edge["EDGE_TTL_SECONDS"] = "5"
    p_edge = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "edge.proxy:app", "--host", "127.0.0.1", "--port", "8080", "--log-level", "warning"],
        cwd=str(ROOT),
        env=env_edge,
        stdout=open(RUN / "edge.log", "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    (RUN / "edge.pid").write_text(str(p_edge.pid), encoding="utf-8")

    # Wait for services
    print("Waiting for services...")
    for name, port in [("region-a", 8001), ("region-b", 8002), ("edge", 8080)]:
        up = False
        for _ in range(25):
            try:
                path = "/edge/state" if port == 8080 else "/healthz"
                r = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=1.0)
                if r.status_code == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not up:
            raise RuntimeError(f"Service {name} on port {port} failed to start!")
        print(f"  {name} (port {port}): UP")
    return [p_a, p_b, p_edge]


def run_drill_1():
    print("\n" + "=" * 50)
    print("STARTING DRILL 1 (BASELINE: NO DR)")
    print("=" * 50)

    # 1. Reset state
    (ROOT / "chaos/chaos-events.jsonl").unlink(missing_ok=True)
    subprocess.run([sys.executable, "state/seed_vectors.py", "--region", "a", "--docs", "200"], check=True)
    subprocess.run([sys.executable, "state/seed_vectors.py", "--region", "b", "--docs", "0", "--weights-mb", "0"], check=True)
    pathlib.Path("edge/active_region").write_text("a", encoding="utf-8")

    # Start services
    procs = start_services()
    try:
        # Start traffic generator for 30s
        t_out = REPORTS / "drill-1-nodr.jsonl"
        t_out.unlink(missing_ok=True)
        p_traffic = subprocess.Popen([
            sys.executable, "loadgen/traffic.py", "--duration", "30", "--rps", "2", "--out", str(t_out)
        ], cwd=str(ROOT))

        print("Waiting 8s for traffic to stabilize...")
        time.sleep(8)

        print("Killing Region A (netblock)...")
        subprocess.run([
            sys.executable, "chaos/kill_region.py", "--region", "a", "--mode", "netblock", "--mock"
        ], check=True)

        print("Waiting for traffic generator to finish...")
        p_traffic.wait()

        # Measure RTO
        res = measure(t_out, ROOT / "chaos/chaos-events.jsonl",
                      REPORTS / "health-events.jsonl", REPORTS / "failover-events.jsonl", 300)
        (REPORTS / "measure-drill-1.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
        print("\nDRILL 1 RESULT:")
        print(f"  requests_failed: {res['requests_failed']}")
        print(f"  rto_verdict: {res['rto_verdict']}")
        assert res["requests_failed"] > 0, "No requests failed in drill 1!"
        assert res["rto_verdict"] == "NO_RECOVERY", "Drill 1 should have NO_RECOVERY!"
    finally:
        stop_all_services()


def run_drill_2():
    print("\n" + "=" * 50)
    print("STARTING DRILL 2 (WITH DR)")
    print("=" * 50)

    # 1. Reset state
    subprocess.run([sys.executable, "state/seed_vectors.py", "--region", "a", "--docs", "200"], check=True)
    subprocess.run([sys.executable, "state/seed_vectors.py", "--region", "b", "--docs", "0", "--weights-mb", "0"], check=True)
    pathlib.Path("edge/active_region").write_text("a", encoding="utf-8")

    # Clean old events for drill 2
    (REPORTS / "health-events.jsonl").unlink(missing_ok=True)
    (REPORTS / "failover-events.jsonl").unlink(missing_ok=True)
    (REPORTS / "runbook-run.jsonl").unlink(missing_ok=True)

    # Clean old replica artifacts for drill 2
    import shutil
    shutil.rmtree(ROOT / "state/_replica", ignore_errors=True)

    # Ensure at least 1 replica snapshot exists before drill starts
    snapshot.put("a", "fs")

    # Start services
    procs = start_services()
    bg_procs = []
    try:
        # 1. Ingest
        p_ingest = subprocess.Popen([
            sys.executable, "state/ingest.py", "--region", "a", "--rate", "0.5", "--duration", "100"
        ], cwd=str(ROOT))
        bg_procs.append(p_ingest)

        # 2. Replicate
        p_replicate = subprocess.Popen([
            sys.executable, "state/replicate.py", "--every", "30", "--duration", "100", "--backend", "fs"
        ], cwd=str(ROOT))
        bg_procs.append(p_replicate)

        print("Waiting 5s before starting traffic...")
        time.sleep(5)

        # 3. Traffic generator (80s)
        t_out = REPORTS / "drill-2-withdr.jsonl"
        t_out.unlink(missing_ok=True)
        p_traffic = subprocess.Popen([
            sys.executable, "loadgen/traffic.py", "--duration", "80", "--rps", "2", "--out", str(t_out)
        ], cwd=str(ROOT))
        bg_procs.append(p_traffic)

        # 4. Health checker (80s)
        p_health = subprocess.Popen([
            sys.executable, "dr/health_checker.py", "--interval", "5", "--threshold", "3", "--duration", "80",
            "--out", str(REPORTS / "health-events.jsonl")
        ], cwd=str(ROOT))
        bg_procs.append(p_health)

        print("Waiting 10.2s before chaos kill (align with health check poll)...")
        time.sleep(10.2)

        print("Triggering Chaos Kill on Region A...")
        subprocess.run([
            sys.executable, "chaos/kill_region.py", "--region", "a", "--mode", "netblock", "--mock"
        ], check=True)

        print("Running automated Runbook...")
        subprocess.run([
            sys.executable, "dr/runbook.py", "--primary", "a", "--target", "b", "--backend", "fs", "--auto"
        ], check=True)

        print("Waiting for traffic generator to finish...")
        p_traffic.wait()

        # Measure RTO
        res = measure(t_out, ROOT / "chaos/chaos-events.jsonl",
                      REPORTS / "health-events.jsonl", REPORTS / "failover-events.jsonl", 300)
        (REPORTS / "measure-drill-2.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
        print("\nDRILL 2 RESULT:")
        print(json.dumps(res, indent=2, ensure_ascii=False))
        assert res["valid"], f"Drill 2 invalid: {res['invalid_reasons']}"
        assert not res["warnings"], f"Drill 2 warnings: {res['warnings']}"
        assert res["rto_verdict"] == "PASS", f"Drill 2 verdict not PASS: {res['rto_verdict']}"
        assert res["recovered_by_region"] == "b", f"Recovered by {res['recovered_by_region']}"
    finally:
        for p in bg_procs:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/PID", str(p.pid), "/T"], capture_output=True)
                else:
                    p.terminate()
            except Exception:
                pass
        stop_all_services()


if __name__ == "__main__":
    run_drill_1()
    time.sleep(3)
    run_drill_2()
