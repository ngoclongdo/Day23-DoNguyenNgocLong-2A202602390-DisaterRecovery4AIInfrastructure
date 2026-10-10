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
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n: int, name: str, **kw) -> dict:
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = kw.pop("ts", time.time())
    iso = kw.pop("iso", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)))
    entry = {"ts": ts, "iso": iso, "step": n, "name": name, **kw}
    line = json.dumps(entry)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
    print(f"[{iso}] [Step {n}: {name}] {line}")
    return entry


def confirm(auto: bool, msg: str) -> bool:
    """Auto=True -> True; ngược lại hỏi y/N."""
    if auto:
        return True
    print(f"\n[XÁC NHẬN] {msg}")
    try:
        ans = input("Tiếp tục thực hiện? [y/N]: ").strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước runbook theo quy trình phản ứng sự cố."""
    t_start = time.time()

    # Bước 1: 1 xac_nhan_outage
    # Chờ health checker phát hiện outage của primary (tránh cutover trước khi detect)
    health_log = pathlib.Path("reports/health-events.jsonl")
    chaos_file = pathlib.Path("chaos/chaos-events.jsonl")
    t_outage = None
    if chaos_file.exists():
        lines = []
        for l in chaos_file.read_text(encoding="utf-8").splitlines():
            if l.strip():
                try:
                    lines.append(json.loads(l))
                except Exception:
                    pass
        kills = [e for e in lines if e.get("action") == "kill" and e.get("region") == primary]
        if kills:
            t_outage = kills[-1].get("ts")

    t_wait_start = time.time()
    while time.time() - t_wait_start < 45.0:
        if health_log.exists():
            hev = []
            for l in health_log.read_text(encoding="utf-8").splitlines():
                if l.strip():
                    try:
                        hev.append(json.loads(l))
                    except Exception:
                        pass
            if any(
                e.get("event") == "state_change"
                and e.get("to") == "UNHEALTHY"
                and e.get("region") == primary
                and (t_outage is None or e.get("ts", 0) >= t_outage)
                for e in hev
            ):
                break
        time.sleep(0.5)

    p_ready, p_reason = hc.probe(primary, timeout=2.0)
    t_ready, t_reason = hc.probe(target, timeout=2.0)
    step(
        1,
        "xac_nhan_outage",
        primary=primary,
        primary_ready=p_ready,
        primary_reason=p_reason,
        target=target,
        target_ready=t_ready,
        target_reason=t_reason,
    )
    if not confirm(auto, f"Phát hiện Region {primary} down ({p_reason}). Xác nhận outage để mở incident?"):
        return {"ok": False, "aborted_at": 1, "reason": "operator_rejected"}

    # Bước 2: 2 thong_bao_incident
    t_outage = None
    chaos_file = pathlib.Path("chaos/chaos-events.jsonl")
    if chaos_file.exists():
        lines = []
        for l in chaos_file.read_text(encoding="utf-8").splitlines():
            if l.strip():
                try:
                    lines.append(json.loads(l))
                except Exception:
                    pass
        kills = [e for e in lines if e.get("action") == "kill" and e.get("region") == primary]
        if kills:
            t_outage = kills[-1].get("ts")
    ts_notice = time.time()
    delay_s = round(ts_notice - t_outage, 2) if t_outage else 0.0
    step(2, "thong_bao_incident", t_outage=t_outage, ts_notice=ts_notice, delay_notice_s=delay_s)
    if not confirm(auto, f"Đã ghi nhận incident. Tiến hành failover sang Region {target}?"):
        return {"ok": False, "aborted_at": 2, "reason": "operator_rejected"}

    # Bước 3: 3 scale_gpu_pool (gọi failover() 1 lần duy nhất)
    fo_res = fo.failover(target=target, backend=backend, wait=60.0)
    step(
        3,
        "scale_gpu_pool",
        target=target,
        failover_ok=fo_res.get("ok"),
        waited_s=fo_res.get("waited_s"),
    )
    if not fo_res.get("ok"):
        return {"ok": False, "aborted_at": 3, "failover": fo_res}

    # Bước 4: 4 verify_state_replica (chỉ đọc kết quả từ fo_res)
    step(
        4,
        "verify_state_replica",
        target=target,
        rpo_seconds=fo_res.get("rpo_seconds"),
        docs_lost=fo_res.get("docs_lost"),
        embed_model_version=fo_res.get("embed_model_version"),
    )

    # Bước 5: 5 dns_cutover (kiểm tra file active_region)
    active_path = pathlib.Path("edge/active_region")
    active_reg = active_path.read_text(encoding="utf-8").strip() if active_path.exists() else None
    step(5, "dns_cutover", active_region=active_reg, cutover_ok=(active_reg == target))

    # Bước 6: 6 verify_golden_signals (10 request thật vào region phụ)
    latencies = []
    errors = 0
    for _ in range(10):
        t0 = time.time()
        try:
            r = httpx.get(f"{URL[target]}/v1/infer?q=golden_probe", timeout=2.0)
            lat = (time.time() - t0) * 1000
            latencies.append(lat)
            if r.status_code != 200 or not r.json().get("answer"):
                errors += 1
        except Exception:
            errors += 1
    latencies.sort()
    p95_ms = round(latencies[int(len(latencies) * 0.95)] if latencies else 0, 1)
    err_rate = round(errors / 10.0, 2)
    step(6, "verify_golden_signals", samples=10, p95_latency_ms=p95_ms, error_rate=err_rate)

    # Bước 7: 7 post_incident
    elapsed_s = round(time.time() - t_start, 2)
    measure_cmd = "python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl"
    step(7, "post_incident", elapsed_s=elapsed_s, measure_cmd=measure_cmd)

    return {
        "ok": True,
        "elapsed_s": elapsed_s,
        "failover": fo_res,
        "golden_signals": {"p95_ms": p95_ms, "error_rate": err_rate},
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
