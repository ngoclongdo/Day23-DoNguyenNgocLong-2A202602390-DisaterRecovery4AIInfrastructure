# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T03:37:33 | outage bắt đầu (Region A bị netblock) | `chaos/chaos-events.jsonl:5` |
| 2026-10-09T03:37:33 | user đầu tiên bị ảnh hưởng (request fail) | `reports/drill-2-withdr.jsonl:72` |
| 2026-10-09T03:37:50 | health check alert (đổi sang UNHEALTHY) | `reports/health-events.jsonl:4` |
| 2026-10-09T03:38:26 | operator confirm cutover (DNS trỏ sang Region B) | `reports/failover-events.jsonl:30` |
| 2026-10-09T03:38:28 | resolved (request đầu tiên OK từ region phụ) | `reports/drill-2-withdr.jsonl:99` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: 54.7s · gap: nhanh hơn mục tiêu 245.3s (PASS)
- RPO mục tiêu: 300s · đo được: 4.0s (2 doc bị mất) · gap: nhanh hơn mục tiêu 296.0s (PASS)
- **Bước tốn nhiều giây nhất:** Thời gian xác nhận và kích hoạt cứu hộ của operator (~36.4s) — vì đây là quy trình bán tự động (semi-automated) yêu cầu xác minh an toàn để tránh kích hoạt nhầm failover khi mạng chỉ chập chờn tạm thời.

## 3. Root cause (5 whys)

* **Why 1:** Tại sao người dùng không nhận được kết quả inference?  
  -> Vì Region A bị mất kết nối mạng và không phản hồi request.
* **Why 2:** Tại sao kết nối mạng tới Region A bị đứt?  
  -> Vì xảy ra phân vùng mạng (network partition / dropped packets mô phỏng bởi SIGSTOP netblock).
* **Why 3:** Tại sao hệ thống không chuyển traffic sang Region B ngay lập tức ở giây đầu tiên?  
  -> Vì Health Checker cần quan sát đủ detection floor (15 giây) để tránh báo động giả.
* **Why 4:** Tại sao không thể chuyển DNS ngay khi phát hiện Region A chết?  
  -> Vì Region B ban đầu rỗng, cần hoàn tất quy trình restore dữ liệu snapshot và nạp model weights vào GPU pool trước.
* **Why 5:** Tại sao phải thiết kế quy trình 5 bước nghiêm ngặt?  
  -> Để đảm bảo nguyên tắc: *Process sống khác với Region sẵn sàng phục vụ*, tránh tình trạng người dùng nhận lỗi 503 từ cả 2 region.

## 4. Action items (có owner + deadline)

| # | Action item | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Tối ưu hóa interval của health check xuống 3s (threshold 3) để rút ngắn detection floor | SRE Team | 2026-10-25 | Giảm RTO 6 giây |
| 2 | Chuyển đổi cơ chế snapshot định kỳ 30s sang streaming replication (CDC) cho Vector DB | Data Platform | 2026-11-05 | Giảm RPO từ 4.0s xuống < 1s (0 doc lost) |
| 3 | Tự động hóa thông báo sự cố qua Slack/PagerDuty webhook khi health checker phát hiện UNHEALTHY | Platform Team | 2026-10-30 | Giảm thời gian phản ứng của on-call 10-15 giây |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**  
   `interval = 5s`, `threshold = 3` $\implies 5\text{s} \times 3 = 15.0\text{s}$.  
   Khoản thời gian này chiếm khoảng **27.4%** tổng thời gian RTO đo được (15.0s / 54.7s).

2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**  
   Nếu hạ xuống 1s, detection floor chỉ còn $1\text{s} \times 3 = 3\text{s}$, giúp **giảm 12 giây** trong RTO.  
   Cái giá phải trả là **nguy cơ flapping cực cao**: Khi mạng chỉ bị trễ nhẹ hoặc nghẽn tạm thời trong 2-3 giây, hệ thống sẽ vội vã kết luận region chính sập và kích hoạt chuyển đổi toàn bộ cụm, gây lãng phí tài nguyên và làm gián đoạn request của khách hàng không đáng có.

3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**  
   Con số `docs_lost = 2` có nghĩa là toàn bộ dữ liệu của 2 văn bản / ticket mà khách hàng vừa gửi lên trong 4 giây cuối trước thảm họa đã biến mất vĩnh viễn. Để xử lý, hệ thống cần đối chiếu log từ lớp API Gateway để gửi thông báo yêu cầu người dùng gửi lại hoặc replay lại 2 request đó vào Region B.
