# Metric contract (khai báo bắt buộc — chốt ở phút 30, trước khi chạy eval)

> Các metric chuẩn đã được cài trong `src/metrics/tracks.py`. File này là nơi nhóm **cam kết** cách dùng chúng.
> Không đổi testset, threshold hay trọng số sau khi đã thấy kết quả eval.

| Mục | Khai báo của nhóm |
|---|---|
| Track | T08 · Retention & Delete Request Simulator |
| Primary metric (tên đúng như evaluator in ra) | `Deletion Completeness` |
| Tăng tốt hay giảm tốt? | higher |
| Đơn vị đánh giá (image / region / pair / release / batch / case …) | cặp (request_id, artefact_id) |
| Công thức + numerator / denominator | số artefact có `expected_action=delete` mà ta ghi `delete` / tổng artefact có `expected_action=delete` |
| Cách ghép prediction ↔ GT (IoU? id? tolerance?) | khớp chính xác theo `(request_id, artefact_id)`; dòng GT không có prediction = không xóa |
| Candidate pool cố định | `fixtures/t08_deletion`: 212 artefact, 10 request (sha256 trong `manifest.csv`) |
| Review budget B / K | `review_budget = 60` dòng (đủ cho toàn bộ 56 dòng cần duyệt trên fixture) |
| Seed | 42 |
| Threshold policy (chọn trên dev như thế nào) | Không có ngưỡng số. Thứ tự ưu tiên chốt trước: legal hold > danh tính > ACL > nguồn gốc > nhiều subject > untagged > delete (`src/t08_deletion.py`) |
| Eval split (không dùng để tune) | toàn bộ fixture (không có split); policy chốt trước khi giảng viên chạy eval với `gt/` |
| Chỉ số lỗi xấu (miss / false alarm / false reject) | **False-delete count** (mục tiêu 0, guardrail cứng) > miss (giữ bản sao / embedding định danh) |
| Slices sẽ báo cáo (≥2) | theo `expected_action` (delete / escalate / retain_legal_hold) và theo `type` artefact |
| Cách báo độ bất định khi N nhỏ (Wilson CI / bootstrap) | Wilson 95% CI cho Completeness và Exception accuracy (N exception ≈ 30) |
| Ngày giờ chốt + người chốt | 2026-11-14 · TODO: tên owner |

Nếu baseline = 0 thì ghi rõ "baseline = 0" thay vì phần trăm cải thiện.
