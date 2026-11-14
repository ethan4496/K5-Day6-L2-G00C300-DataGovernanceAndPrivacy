# T08 · Đề bài tự tạo, đáp án và cách chứng minh bằng hệ thống ảo

> Tài liệu này mô tả **giả định của nhóm**, không phải đáp án của giảng viên và không phải tư vấn pháp lý.
> Phần code và tài liệu này được viết với sự hỗ trợ của AI (Claude Code); nhóm cần đọc, hiểu và chịu trách nhiệm về các giả định.
> Mọi con số kết quả nằm trong `out/t08_selfcheck/selfcheck.md` do script sinh ra — không chép tay vào report.

## 1. Đề bài

Một hệ thống lưu dữ liệu video lái xe. Từ mỗi video gốc, hệ thống sinh ra backup, frame, face crop, embedding,
annotation và các gói training export. Mỗi artefact ghi: thuộc về ai (`subject_ids`), sinh ra từ đâu (`parent_ids`),
ai giữ quyền (`owner`, `acl`), có bị legal hold không, có rõ nguồn gốc không.

Có N người gửi yêu cầu xóa dữ liệu của mình. Với **mỗi cặp (yêu cầu, artefact liên quan)**, hãy chọn đúng một hành động:

| Hành động | Khi nào |
|---|---|
| `delete` | artefact chỉ thuộc người yêu cầu và không vướng ngoại lệ nào |
| `retain_legal_hold` | artefact đang bị giữ theo nghĩa vụ pháp lý |
| `escalate` | máy không được tự quyết, phải chuyển cho người duyệt |

Yêu cầu của lời giải: **không sót** (không còn dữ liệu nhận diện được người yêu cầu mà không có lý do),
**không xóa nhầm** (không đụng tới dữ liệu của người khác hoặc dữ liệu đang bị giữ), **chạy lại ra cùng kết quả**,
và **mọi hành động đều có dấu vết**.

## 2. Đáp án: chính sách của nhóm (`src/t08/policy.py`)

**Phạm vi của một yêu cầu xóa cho người S**

- *tagged*: mọi artefact có S trong `subject_ids`.
- *implicit*: artefact không gắn S nhưng vẫn chứa S, vì một artefact gắn S được cắt ra từ nó (đi ngược qua các
  liên kết một-cha), cộng với bản sao nguyên vẹn (`backup`) của những artefact đó.

**Thứ tự ưu tiên** (luật đầu tiên khớp sẽ thắng)

| # | Điều kiện | Hành động | `reason_code` |
|---|---|---|---|
| 1 | Có legal hold, hoặc **toàn bộ** cha đều đang bị hold | `retain_legal_hold` | `LEGAL_HOLD`, `LEGAL_HOLD_INHERITED` |
| 2 | Người yêu cầu chưa xác minh danh tính | `escalate` | `IDENTITY_NOT_VERIFIED` |
| 3 | `owner` không nằm trong `acl` | `escalate` | `NO_PERMISSION` |
| 4 | `origin_known = false` | `escalate` | `UNKNOWN_ORIGIN` |
| 5 | Artefact thuộc phạm vi *implicit* | `escalate` | `IMPLICIT_CONTAINER` |
| 6 | Có nhiều hơn một người trong `subject_ids` | `escalate` | `MULTI_SUBJECT` |
| 7 | Còn lại | `delete` | `DELETE_OK` |

**Các giả định nhóm phải tự bảo vệ trong report** (ground truth của giảng viên có thể khác):

1. Legal hold lan xuống bản dẫn xuất chỉ khi mọi cha đều bị hold. Gói export gom 12 crop mà chỉ 1 crop bị hold thì không bị coi là hold.
   Đổi bằng `legal_hold_propagation: self` trong `configs/lab.yaml` nếu nhóm quyết định hold chỉ áp cho đúng artefact mang cờ.
2. Artefact nhiều người không bị xóa tự động mà chuyển cho người duyệt (cần che/cắt, không phải xóa cả file).
3. Video của người khác có chứa người yêu cầu được đưa vào kế hoạch dưới dạng `escalate`. Tắt bằng `include_implicit_containers: false`.
4. Người duyệt **không** thể đổi một dòng legal hold thành `delete`; phải gỡ hold ở dữ liệu nguồn trước.

## 3. Chứng minh bằng hệ thống ảo

Chạy một lệnh (không cần ground truth, không cần mạng):

```bash
.venv/Scripts/python -m tools.t08_selfcheck
```

Kết quả ghi vào `out/t08_selfcheck/selfcheck.md` và `selfcheck.json`. Script đưa ra ba loại bằng chứng độc lập:

**A. Đề tự tạo có đáp án cài sẵn** (`src/t08/scenario.py`). Bộ sinh đề dựng một graph nhỏ và *cố ý cài* từng ca khó
(legal hold, backup của vendor, embedding không rõ nguồn gốc, frame chung hai người, frame chung nằm dưới video bị hold,
người yêu cầu chưa xác minh, gói export nhiều người, người ngoài cuộc). Đáp án của mỗi dòng được ghi ngay lúc tạo
artefact, dựa vào ca nào được cài ở đó, **không** tính bằng cách chạy planner. Sau đó planner được chấm bằng đúng
evaluator chính thức `eval_t08`.

**B. Fixture của lab chạy trên kho ảo** (`src/t08/virtual_system.py`). `VirtualStore` là bản sao trong bộ nhớ; kế hoạch
được thực thi trên đó, rồi verifier nhìn vào **trạng thái cuối** và kiểm tra:

| Kiểm tra | Ý nghĩa |
|---|---|
| C1 well-formed plan | không trùng cặp, id hợp lệ, hành động hợp lệ |
| C2 no false delete | mọi thứ đã xóa đều là dữ liệu một-người của người yêu cầu, không hold, đủ quyền, rõ nguồn gốc |
| C3 no residual data | không còn artefact nào gắn người yêu cầu mà thiếu lý do hợp lệ |
| C4 exceptions documented | mọi artefact được giữ lại đều có dòng với đúng hành động |
| C5 idempotent | thực thi lần hai xóa thêm 0 artefact, trạng thái không đổi |
| C6 audit complete | mọi hành động có dòng trong `audit_log.jsonl` (kiểm khi chạy pipeline) |

Verifier không dùng lại code của planner: nó tự suy ra legal hold bằng cách đi ngược lên cha (đệ quy), còn planner đi
từ trên xuống. Baseline "xóa theo tên file" cũng đi qua đúng các kiểm tra này để so sánh.

**C. Thử phá đáp án (mutation test).** Lấy kế hoạch đúng, cố ý làm sai từng kiểu (xóa file đang bị hold, quên một
embedding, xóa dữ liệu người không yêu cầu, giấu lệnh xóa sau một dòng trùng…). Verifier phải bắt được **tất cả**.
Nếu verifier không bao giờ báo lỗi thì nó không chứng minh được gì.

## 4. Giới hạn của chứng minh

- Chứng minh được: planner cài **đúng chính sách ở mục 2**, không sót và không xóa nhầm theo chính sách đó, chạy lại ra cùng kết quả.
- **Không** chứng minh được: chính sách ở mục 2 trùng với ground truth ẩn của giảng viên. Điểm chính thức chỉ có khi
  giảng viên chạy `src.evaluate` với overlay.
- Đáp án cài sẵn ở phần A do cùng một tác giả với planner viết, nên cả hai có thể hiểu sai đề theo cùng một cách.
  Nhóm nên tự gán nhãn tay một số ca trên fixture thật và ghi kết quả đối chiếu vào report.

## 5. Chạy pipeline

> Lưu ý: các lệnh trong mục này dùng `src/pipeline.py` và `configs/lab.yaml` bản của tác giả tài liệu. Hai file đó chưa được
> đẩy lên vì đang xung đột với bản của đồng đội. Bộ tự chấm ở mục 3 (`tools/t08_selfcheck.py`) chạy độc lập, không cần chúng.

```bash
.venv/Scripts/python -m src.main --track t08 --output out/poc --run-name poc --offline --seed 42
```

```bash
.venv/Scripts/python -m src.main --track t08 --pipeline src.baselines.t08_filename --output out/baseline --run-name baseline --offline --seed 42
```

Quyết định mặc định là `HOLD / EXCEPTIONS_PENDING_REVIEW` cho tới khi có người duyệt các ngoại lệ trong
`out/poc/review_queue.csv` (điền `reviewer`, `reviewer_decision`, lưu thành `out/poc/review_queue_reviewed.csv`, chạy lại).
