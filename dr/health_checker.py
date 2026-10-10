"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Kiểm tra /readyz của một region.
    Timeout PHẢI có — netblock làm request treo mãi.
    Trả về (ready, reason):
    - ready=True nếu status_code == 200 và response chỉ ra ready
    - ready=False nếu timeout hoặc status != 200
    """
    url = f"{URL[region]}/readyz"
    t0 = time.time()
    try:
        r = httpx.get(url, timeout=timeout)
        if r.status_code == 200:
            return True, "ok"
        else:
            return False, f"status_{r.status_code}"
    except httpx.TimeoutException:
        return False, "Timeout"
    except Exception as e:
        elapsed = time.time() - t0
        if elapsed < timeout:
            time.sleep(timeout - elapsed)
        return False, type(e).__name__


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Vòng lặp poll + phát hiện transition + ghi JSONL."""
    out.parent.mkdir(parents=True, exist_ok=True)
    t_start = time.time()

    state = {
        r: {
            "status": "HEALTHY",
            "consecutive_fails": 0,
        }
        for r in ["a", "b"]
    }

    with out.open("a", encoding="utf-8") as f:
        while time.time() - t_start < duration:
            for region in ["a", "b"]:
                ready, reason = probe(region, timeout)
                s = state[region]

                if ready:
                    s["consecutive_fails"] = 0
                    if s["status"] != "HEALTHY":
                        s["status"] = "HEALTHY"
                        now = time.time()
                        entry = {
                            "ts": now,
                            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                            "event": "state_change",
                            "region": region,
                            "to": "HEALTHY",
                            "consecutive_fails": 0,
                            "reason": reason,
                            "interval_s": interval,
                            "threshold": threshold,
                        }
                        f.write(json.dumps(entry) + "\n")
                        f.flush()
                        print(f"[{entry['iso']}] {region} -> HEALTHY ({reason})")
                else:
                    s["consecutive_fails"] += 1
                    if s["consecutive_fails"] >= threshold and s["status"] != "UNHEALTHY":
                        s["status"] = "UNHEALTHY"
                        now = time.time()
                        entry = {
                            "ts": now,
                            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                            "event": "state_change",
                            "region": region,
                            "to": "UNHEALTHY",
                            "consecutive_fails": s["consecutive_fails"],
                            "reason": reason,
                            "interval_s": interval,
                            "threshold": threshold,
                        }
                        f.write(json.dumps(entry) + "\n")
                        f.flush()
                        print(f"[{entry['iso']}] {region} -> UNHEALTHY ({reason}, fails={s['consecutive_fails']})")

            time.sleep(interval)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
