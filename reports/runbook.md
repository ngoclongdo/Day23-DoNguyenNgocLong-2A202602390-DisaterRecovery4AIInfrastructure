# Runbook 1 trang — Region chính down

Runbook được thiết kế để kỹ sư on-call có thể thực thi chính xác lúc 3h sáng mà không cần suy đoán.

| # | Bước | Lệnh copy-paste | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `curl -s localhost:8001/readyz` hoặc `python3 dr/health_checker.py` | HTTP status 503 / timeout 3 lần liên tiếp; Region A trả về `UNHEALTHY` | On-call Engineer |
| 2 | Mở incident + bấm giờ RTO | `python3 dr/runbook.py --primary a --target b --backend fs --auto` | Dòng `thong_bao_incident` được ghi vào `reports/runbook-run.jsonl` | Incident Commander |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py get --region b --backend fs` | Terminal in ra `restored_at` và `embed_model_version`, vector DB & weights có trên đĩa | On-call Engineer |
| 4 | Scale pool warm→full | `echo full > state/region-b/pool_state && curl localhost:8002/readyz` | `/readyz` của Region B trả về HTTP 200 `ready: true` | On-call Engineer |
| 5 | DNS/LB cutover | `printf b > edge/active_region && curl localhost:8080/edge/state` | Trả về `{"active_region": "b"}` | On-call Engineer |
| 6 | Verify golden signals | `for i in {1..10}; do curl -s "localhost:8080/v1/infer?q=ping"; done` | p95 latency < 50ms, error rate = 0% trên 10 requests | On-call Engineer |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Terminal in ra `rto_verdict: PASS`, RTO <= 300s | Incident Commander |

---

### Quy định Rollback (Failback về Region A)
* **Điều kiện tiên quyết:**
  1. Hạ tầng mạng và máy chủ của Region A đã được khôi phục và ổn định liên tục ít nhất **30 phút** (không còn packet drop hay CPU/Memory spike).
  2. Endpoint `curl localhost:8001/readyz` phải trả về `HTTP 200 OK` liên tục trong 10 lần probe thăm dò.
  3. Dữ liệu mới phát sinh tại Region B trong thời gian sự cố đã được đồng bộ ngược (Reverse Replication) về Region A thành công, bảo đảm RPO = 0.
* **Người có thẩm quyền quyết định:** **Incident Commander / Lead SRE** là người duy nhất có quyền phê duyệt rollback, kỹ sư on-call không được tự ý chuyển DNS về A để tránh gây ra hiện tượng flapping giữa 2 region.
