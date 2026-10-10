"""Orchestrator script to run Drill 1 (baseline) and Drill 2 (with DR) cleanly on Windows/Linux."""
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUN_DIR = ROOT / "run"
REPORTS_DIR = ROOT / "reports"
CHAOS_DIR = ROOT / "chaos"
STATE_DIR = ROOT / "state"


def stop_process(proc: subprocess.Popen | None):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def cleanup():
    # Kill any processes recorded in run/*.pid
    for f in RUN_DIR.glob("*.pid"):
        try:
            pid = int(f.read_text(encoding="utf-8").strip())
            os.kill(pid, getattr(signal, "SIGTERM", 15))
        except Exception:
            pass
        f.unlink(missing_ok=True)


def wait_service(port: int, path: str, timeout: float = 10.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def start_service(name: str, port: int, env_extra: dict) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(env_extra)
    module = "edge.proxy:app" if name == "edge" else "serving.app:app"
    log_file = open(RUN_DIR / f"{name}.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    (RUN_DIR / f"{name}.pid").write_text(str(proc.pid), encoding="utf-8")
    return proc


def run_cmd(args: list[str], wait=True) -> subprocess.Popen | subprocess.CompletedProcess:
    if wait:
        return subprocess.run(args, cwd=str(ROOT), check=True)
    return subprocess.Popen(args, cwd=str(ROOT))


def main():
    print("=== Starting DR Lab Automation ===")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    cleanup()

    # Clear old drill reports and chaos events
    (CHAOS_DIR / "chaos-events.jsonl").unlink(missing_ok=True)
    (REPORTS_DIR / "drill-1-nodr.jsonl").unlink(missing_ok=True)
    (REPORTS_DIR / "drill-2-withdr.jsonl").unlink(missing_ok=True)
    (REPORTS_DIR / "health-events.jsonl").unlink(missing_ok=True)
    (REPORTS_DIR / "failover-events.jsonl").unlink(missing_ok=True)
    (REPORTS_DIR / "runbook-run.jsonl").unlink(missing_ok=True)

    # -------------------------------------------------------------
    # DRILL 1: Baseline (No DR)
    # -------------------------------------------------------------
    print("\n--- [Step 1/2] Running Drill 1: Baseline (No DR) ---")
    run_cmd([sys.executable, "state/seed_vectors.py", "--region", "a", "--docs", "200"])
    run_cmd([sys.executable, "state/seed_vectors.py", "--region", "b", "--docs", "0", "--weights-mb", "0"])
    pathlib.Path("edge/active_region").write_text("a", encoding="utf-8")

    proc_a = start_service("region-a", 8001, {"REGION": "a", "STATE_DIR": "state/region-a", "WARMUP_SECONDS": "6"})
    proc_b = start_service("region-b", 8002, {"REGION": "b", "STATE_DIR": "state/region-b", "WARMUP_SECONDS": "6"})
    proc_edge = start_service("edge", 8080, {"EDGE_TTL_SECONDS": "5"})

    assert wait_service(8001, "/healthz"), "Region A failed to start"
    assert wait_service(8002, "/healthz"), "Region B failed to start"
    assert wait_service(8080, "/edge/state"), "Edge proxy failed to start"
    print("Services up: A (8001), B (8002), Edge (8080)")

    # Run traffic generator for 30s at 2 rps
    traffic_out_1 = REPORTS_DIR / "drill-1-nodr.jsonl"
    print("Starting loadgen for Drill 1 (30s)...")
    p_traffic_1 = subprocess.Popen([
        sys.executable, "loadgen/traffic.py", "--duration", "30", "--rps", "2", "--out", str(traffic_out_1)
    ], cwd=str(ROOT))

    # Wait 6s, then kill Region A
    time.sleep(6)
    print("Attacking Region A (mock netblock)...")
    run_cmd([sys.executable, "chaos/kill_region.py", "--region", "a", "--mode", "netblock", "--mock"])

    # Wait for traffic generator to complete
    p_traffic_1.wait()
    print("Drill 1 loadgen finished.")

    # Cleanup services
    stop_process(proc_a)
    stop_process(proc_b)
    stop_process(proc_edge)
    cleanup()

    # Measure Drill 1
    from tools.measure_rto import measure
    m1 = measure(REPORTS_DIR / "drill-1-nodr.jsonl", CHAOS_DIR / "chaos-events.jsonl",
                 REPORTS_DIR / "health-events.jsonl", REPORTS_DIR / "failover-events.jsonl", 300.0)
    print(f"Drill 1 Result: requests_failed={m1['requests_failed']}, verdict={m1['rto_verdict']}")
    assert m1["requests_failed"] > 0, "Drill 1 had no failed requests!"
    assert m1["rto_verdict"] == "NO_RECOVERY", f"Drill 1 verdict was {m1['rto_verdict']}, expected NO_RECOVERY"
    (REPORTS_DIR / "measure-drill-1.json").write_text(json.dumps(m1, indent=2), encoding="utf-8")

    # -------------------------------------------------------------
    # DRILL 2: Failover with DR
    # -------------------------------------------------------------
    print("\n--- [Step 2/2] Running Drill 2: Failover with DR ---")
    import shutil
    shutil.rmtree(STATE_DIR / "_replica", ignore_errors=True)

    run_cmd([sys.executable, "state/seed_vectors.py", "--region", "a", "--docs", "200"])
    run_cmd([sys.executable, "state/seed_vectors.py", "--region", "b", "--docs", "0", "--weights-mb", "0"])
    pathlib.Path("edge/active_region").write_text("a", encoding="utf-8")

    proc_a = start_service("region-a", 8001, {"REGION": "a", "STATE_DIR": "state/region-a", "WARMUP_SECONDS": "6"})
    proc_b = start_service("region-b", 8002, {"REGION": "b", "STATE_DIR": "state/region-b", "WARMUP_SECONDS": "6"})
    proc_edge = start_service("edge", 8080, {"EDGE_TTL_SECONDS": "5"})

    assert wait_service(8001, "/healthz"), "Region A failed to start"
    assert wait_service(8002, "/healthz"), "Region B failed to start"
    assert wait_service(8080, "/edge/state"), "Edge proxy failed to start"
    print("Services up: A (8001), B (8002), Edge (8080)")

    # 1. Start continuous ingest and replication
    print("Starting ingest and replication...")
    p_ingest = subprocess.Popen([
        sys.executable, "state/ingest.py", "--region", "a", "--rate", "0.5", "--duration", "120"
    ], cwd=str(ROOT))
    p_replicate = subprocess.Popen([
        sys.executable, "state/replicate.py", "--every", "30", "--duration", "120", "--backend", "fs"
    ], cwd=str(ROOT))

    # Wait 5 seconds so first snapshot is put
    time.sleep(5)
    manifest = STATE_DIR / "_replica" / "dr-artifacts" / "MANIFEST.json"
    assert manifest.exists(), "Replication failed to produce first snapshot manifest"
    print("First snapshot verified.")

    # 2. Start traffic generator (duration 75s) and health checker (duration 75s)
    traffic_out_2 = REPORTS_DIR / "drill-2-withdr.jsonl"
    health_out_2 = REPORTS_DIR / "health-events.jsonl"
    print("Starting loadgen and health checker...")
    p_traffic_2 = subprocess.Popen([
        sys.executable, "loadgen/traffic.py", "--duration", "75", "--rps", "2", "--out", str(traffic_out_2)
    ], cwd=str(ROOT))
    p_health_2 = subprocess.Popen([
        sys.executable, "dr/health_checker.py", "--interval", "5", "--threshold", "3", "--duration", "75", "--out", str(health_out_2)
    ], cwd=str(ROOT))

    # Wait 12 seconds
    time.sleep(12)

    # 3. Kill Region A
    print("Attacking Region A (mock netblock)...")
    run_cmd([sys.executable, "chaos/kill_region.py", "--region", "a", "--mode", "netblock", "--mock"])

    # 4. Run automated runbook
    print("Executing DR runbook (--auto)...")
    run_cmd([sys.executable, "dr/runbook.py", "--primary", "a", "--target", "b", "--backend", "fs", "--auto"])

    # Wait for loadgen to complete
    p_traffic_2.wait()
    print("Drill 2 loadgen finished.")

    # Cleanup background workers
    stop_process(p_ingest)
    stop_process(p_replicate)
    stop_process(p_health_2)
    stop_process(proc_a)
    stop_process(proc_b)
    stop_process(proc_edge)
    cleanup()

    # Measure Drill 2
    m2 = measure(REPORTS_DIR / "drill-2-withdr.jsonl", CHAOS_DIR / "chaos-events.jsonl",
                 REPORTS_DIR / "health-events.jsonl", REPORTS_DIR / "failover-events.jsonl", 300.0)
    print("\n--- Drill 2 Measurement Results ---")
    print(json.dumps(m2, indent=2))
    assert m2["valid"], f"Drill 2 invalid: {m2['invalid_reasons']}"
    assert not m2["warnings"], f"Drill 2 warnings: {m2['warnings']}"
    assert m2["rto_verdict"] == "PASS", f"RTO verdict: {m2['rto_verdict']}"
    (REPORTS_DIR / "measure-drill-2.json").write_text(json.dumps(m2, indent=2), encoding="utf-8")
    print("\n=== DR DRILLS COMPLETED SUCCESSFULLY! ===")


if __name__ == "__main__":
    main()
