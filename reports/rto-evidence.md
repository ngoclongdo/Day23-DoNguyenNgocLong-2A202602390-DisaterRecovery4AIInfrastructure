# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | 2026-10-09T02:58:43 | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.5s | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:18` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0s | `action:kill` | `chaos/chaos-events.jsonl:5` |
| User thấy lỗi đầu tiên | +0.4s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:72` |
| Health check phát hiện | +16.8s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:4` |
| Snapshot restore xong | +53.2s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:27` |
| Region phụ ready | +53.2s | `step:4_wait_ready` | `reports/failover-events.jsonl:29` |
| DNS cutover | +53.2s | `step:5_dns_cutover` | `reports/failover-events.jsonl:30` |
| **RTO đo được** | 54.7s | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:99` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | 54.7s | 300s (5 phút) | PASS |
| RPO — Vector DB | 4.0s / 2 doc | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 15.0s | `interval_s × threshold` trong `reports/health-events.jsonl:4` | Giảm interval hoặc threshold (nhưng tăng nguy cơ flapping) |
| Snapshot restore | 0.01s | 2_restore → 3_scale trong `reports/failover-events.jsonl:27` | Dùng snapshot incremental / CDC streaming |
| GPU pool warm-up | 0.01s | `waited_s` ở `4_wait_ready` trong `reports/failover-events.jsonl:29` | Pre-warm GPU pool trước ở region phụ |
| DNS/LB TTL cache | 1.5s | t_recovered − t_cutover (54.7s - 53.2s) | Hạ TTL ở Route53/Edge proxy xuống 1-2s |
