"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG và in ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"RUNBOOK [{n}/7] {name}:", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N."""
    if auto:
        return True
    try:
        ans = input(f"{msg} [y/N]: ").strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước quy trình ứng cứu sự cố chuẩn."""
    t_start = time.time()

    # Bước 1: 1 xac_nhan_outage — probe cả 2 region
    p_down = True
    try:
        r = httpx.get(f"{URL[primary]}/readyz", timeout=1.0)
        p_down = (r.status_code != 200)
    except Exception:
        p_down = True

    t_alive = False
    try:
        t_alive = (httpx.get(f"{URL[target]}/healthz", timeout=1.0).status_code == 200)
    except Exception:
        t_alive = False

    step(1, "xac_nhan_outage",
         primary=primary, primary_down=p_down,
         target=target, target_alive=t_alive)

    # Bước 2: 2 thong_bao_incident — ghi nhận timestamp bắt đầu RTO
    chaos_events = pathlib.Path("chaos/chaos-events.jsonl")
    t_outage = None
    if chaos_events.exists():
        kills = [json.loads(l) for l in chaos_events.read_text().splitlines() if l.strip()]
        kills = [k for k in kills if k.get("action") == "kill"]
        if kills:
            t_outage = kills[-1].get("ts")

    t_operator = time.time()
    step(2, "thong_bao_incident",
         operator_noticed_ts=t_operator,
         t_outage=t_outage,
         delay_s=round(t_operator - t_outage, 2) if t_outage else None)

    if not confirm(auto, f"Xác nhận kích hoạt failover từ region-{primary} sang region-{target}?"):
        print("Huỷ bỏ failover bởi operator.")
        return {"ok": False, "reason": "operator_cancelled"}

    # Bước 3: 3 scale_gpu_pool — gọi HÀM failover.failover(...) MỘT LẦN DUY NHẤT
    fo_res = fo.failover(target=target, backend=backend, wait=60.0)
    step(3, "scale_gpu_pool", failover_result=fo_res)
    if not fo_res.get("ok"):
        return {"ok": False, "step": 3, "failover": fo_res}

    # Bước 4: 4 verify_state_replica — chỉ đọc kết quả từ dict bước 3 trả về
    t_state = fo_res.get("target_state") or {}
    v_info = t_state.get("vectors") if isinstance(t_state.get("vectors"), dict) else {}
    step(4, "verify_state_replica",
         target=target,
         vector_count=v_info.get("count", t_state.get("count")),
         weights=t_state.get("weights"),
         rpo_seconds=fo_res.get("rpo_seconds"),
         docs_lost=fo_res.get("docs_lost"))

    # Bước 5: 5 dns_cutover — kiểm tra cutover ok
    active_path = pathlib.Path("edge/active_region")
    active_val = active_path.read_text().strip() if active_path.exists() else None
    step(5, "dns_cutover", active_region=active_val, cutover_ok=(active_val == target))

    # Bước 6: 6 verify_golden_signals — 10 request thật đo p95 latency + error rate
    latencies = []
    errors = 0
    with httpx.Client(timeout=3.0) as client:
        for i in range(10):
            t0 = time.time()
            try:
                r = client.get("http://127.0.0.1:8080/v1/infer", params={"q": f"test {i}"})
                if r.status_code == 200:
                    latencies.append((time.time() - t0) * 1000)
                else:
                    errors += 1
            except Exception:
                errors += 1
            time.sleep(0.05)

    latencies.sort()
    p95 = round(latencies[int(len(latencies) * 0.95)] if latencies else 0.0, 1)
    step(6, "verify_golden_signals", requests=10, errors=errors,
         p95_latency_ms=p95, error_rate=round(errors / 10.0, 2))

    # Bước 7: 7 post_incident — elapsed_s + lệnh đo RTO
    elapsed = round(time.time() - t_start, 2)
    step(7, "post_incident",
         elapsed_s=elapsed,
         measure_command="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")

    return {
        "ok": True,
        "elapsed_s": elapsed,
        "primary": primary,
        "target": target,
        "failover": fo_res,
        "p95_latency_ms": p95,
        "errors": errors,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
