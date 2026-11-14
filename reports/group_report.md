# Group Report — Day 06 Data Lab (Field-to-Release PoC)

> Mọi con số trong report lấy từ file do script sinh ra (`out/*/run_info.json`, `out/*/actions.csv`,
> `out/poc/decision.json`, `tools/show_t08.py`). Repo học viên không có `gt/` nên **chưa có metric chính thức**
> (`reports/metrics_*.json`, `table1_baseline_vs_poc.md`); các số dưới đây là **self-check không cần nhãn**, ghi rõ ở từng chỗ.

## 00 · Header

| Mục | Giá trị |
|---|---|
| Group ID | G00C300 |
| Challenge | T08 · Retention & Delete Request Simulator |
| Thành viên (5–6) + vai trò | 1. Vu Duy The (Lead/Integrator + Decision owner) · 2. ____ (Data & Lineage) · 3. ____ (Policy engine) · 4. ____ (Baseline & Evaluation) · 5. ____ (Human review & Audit) · 6. ____ (QA tests + Report & Demo) |
| Git URL | https://github.com/ethan4496/K5-Day6-L2-C300Team-DataGovernanceAndPrivacy |
| Tag `demo-180m` (commit) | ____ |
| Tag `final-24h` (commit) | ____ |
| Deadline (giờ VN) | ____ |

## 01 · Problem statement

- **Ai bị ảnh hưởng:** chủ thể dữ liệu (người trong video lái xe) gửi yêu cầu xóa. Nếu sót bản sao/embedding,
  dữ liệu định danh của họ vẫn tồn tại và tiếp tục đi vào training export. Ngược lại, nếu xóa nhầm dữ liệu đang bị
  **legal hold**, tổ chức vi phạm nghĩa vụ lưu giữ bằng chứng. Nếu xóa nhầm artefact chung, dữ liệu của **người khác** bị mất.
- **Điểm nghẽn:** một video gốc sinh ra cả chuỗi bản sao: backup, frame, face_crop, embedding, annotation, rồi gộp vào
  training_export của nhiều người. Xóa theo tên file chỉ chạm được video gốc.
- **Đầu vào → đầu ra:** `requests.json` (10 yêu cầu) + `data_graph.json` (212 artefact mock) →
  `actions.csv`: một dòng / cặp (request, artefact), `action ∈ {delete, retain_legal_hold, escalate}` + `audit_log.jsonl`.
- **Tiêu chí GO / HOLD / REWORK (chốt trước eval, `src/pipeline.py::decide`):**
  - REWORK nếu vi phạm bất biến: xóa artefact đang hold, xóa artefact của subject khác, trùng dòng, action lạ.
  - HOLD nếu còn bất kỳ dòng cần duyệt nào chưa có reviewer + quyết định (rỗng hoặc `skip`).
  - GO khi mọi dòng cần duyệt đã có người duyệt và không vi phạm. Chỉ khi GO **và** `dry_run: false` mới xóa mock.
- **Giả định cần DPO/legal xác nhận:**
  1. Legal hold chỉ được xét trên chính artefact có cờ `legal_hold`. Bản dẫn xuất của artefact đang hold **không** tự động bị hold
     mà được đưa vào hàng đợi duyệt (xem Failure case 1).
  2. Artefact nhiều subject (training_export, frame chung) không xóa mà escalate để redaction/retrain.
  3. Operator xóa có vai trò `data-platform`; artefact có ACL khác → escalate cho owner.

## 02 · Data & provenance

| Split | N | Nguồn / link | License | Synthetic? |
|---|---|---|---|---|
| dev = eval (không chia split) | 212 artefact, 10 request → 158 cặp in-scope | `fixtures/t08_deletion/` (generator ở repo giảng viên, seed 42) | CC0-1.0 | Có, mock hoàn toàn |
| eval / heldout | như trên. GT `gt/gt_actions.csv` chỉ giảng viên giữ | | | |

- **Schema:** artefact gồm `artefact_id, type, subject_ids, parent_ids, uri, owner, acl, legal_hold, origin_known, created_at, filename`.
  Có 7 loại `type`: raw_video, backup, frame, face_crop, embedding, annotation, training_export. Request gồm
  `request_id, subject_id, received_at, deadline, channel, identity_verified`.
- **Seed:** 42. Không có split vì policy là luật cố định, không học tham số.
- **`manifest.csv` verified (sha256): có.** `make verify` cho kết quả 14 manifest, 0 lỗi. Pipeline cũng kiểm hash ở bước load
  (`strict_provenance: true`): sai hash thì dừng.
- **Dữ liệu thật:** không dùng, theo đúng yêu cầu của track: không xóa dữ liệu thực trong lab.

## 03 · Research & design

- **References:** GDPR Art. 17 (quyền xóa và các ngoại lệ ở khoản 3), Luật 91/2025 + Nghị định 356/2025 (ngoại lệ quyền xóa),
  Data Cards Playbook (ghi lineage/provenance). Chi tiết ở mục 11.
  - **Vì sao BFS theo lineage:** dữ liệu định danh lan theo quan hệ cha → con. Duyệt theo chiều rộng từ mọi artefact có tag subject sẽ
    bắt được backup, frame, crop, embedding, annotation và export.
  - **Vì sao policy precedence cố định:** ngoại lệ pháp lý (legal hold) phải thắng quyền xóa. Thứ tự là luật chốt trước,
    không phải ngưỡng tune trên dữ liệu.
- **Data flow:**

```
requests.json + data_graph.json + manifest.csv
  → 01 load      kiểm sha256, ID trùng, parent treo; audit request_received
  → 02 process   BFS lineage/request + policy precedence → plan_dry_run.csv (158 dòng)
                 legal_hold > identity_not_verified > no_permission_acl > unknown_origin
                 > multi_subject > untagged_derived > delete
                 con chỉ thuộc subject khác → out_of_scope.csv (không đụng, không đi tiếp)
  → 03 rank      review_queue.csv + contact_sheet.html (56 dòng, budget 60)
  → 04 review    đọc review_queue_reviewed.csv; guardrail chặn override → delete trên hold/nhiều subject
  → 05 decide    GO/HOLD/REWORK + reason_code + owner; GO & dry_run=false → mock_delete (tombstone, idempotent)
  → actions.csv · request_summary.csv · decision.json · decision_log.csv · audit_log.jsonl
```

- **Thành phần offline:** không có model hay weights. Chỉ dùng pandas + thư viện chuẩn.
- **Trade-offs:**
  - **Completeness vs. false-delete:** ưu tiên false-delete = 0, nên mọi trường hợp mơ hồ (nhiều subject, không rõ nguồn,
    thiếu quyền) đều escalate thay vì xóa. Escalate làm tăng tải cho người duyệt nhưng an toàn hơn.
  - **Không đi xuyên qua artefact của người khác:** nếu một face_crop của subject khác nằm dưới frame chung, BFS dừng ở đó.
    Cách này tránh xóa nhầm, đổi lại có thể sót bản dẫn xuất sâu hơn nếu dữ liệu được gắn tag sai.
  - **Dry-run mặc định:** cần đủ 2 khóa (người duyệt cho GO + operator tắt `dry_run`) mới xóa mock.

## 04 · Baseline vs PoC

- **Baseline:** `src/examples/t08_baseline.py`, commit `5ad14de`. Với mỗi request, xóa video `drive_<subject>_*.mp4` tìm theo tên file.
  Không lần theo lineage, không kiểm legal hold hay quyền (đúng mô tả trong `docs/tracks/T08.md` §4).
- **PoC:** `src/pipeline.py` + `src/t08_deletion.py`, commit `5ad14de`, config `configs/lab.yaml`
  (`dry_run: true, operator_role: data-platform, review_budget: 60`).
- **Cùng input / seed:** xác nhận. Cùng `fixtures/t08_deletion`, seed 42, `--offline`.

**Bảng 1 · Baseline vs PoC.** Chưa có output của `src.compare` vì thiếu `gt/`. Lệnh sẽ chạy khi có overlay:
`make eval-baseline eval-poc compare TRACK=t08`. Bảng dưới là **self-check không cần nhãn** (`python -m tools.show_t08 --baseline out/baseline --poc out/poc`):

| Metric (unit) | Baseline | PoC | Δ & chú thích |
|---|---|---|---|
| Primary: Deletion Completeness (chính thức) | ____ (cần GT) | ____ (cần GT) | higher = tốt |
| Self-check: artefact có tag subject được xử lý / tổng | 10/158 = 6.3% | 158/158 = 100.0% | +93.7 điểm %. Proxy, không phải metric chính thức |
| Self-check: số dòng delete / escalate / retain | 10 / 0 / 0 | 128 / 28 / 2 | |
| Self-check: delete trên artefact đang legal hold (count, mục tiêu 0) | **2** (`a0043`, `a0099`) | **0** | Baseline vi phạm hard guardrail |
| Self-check: delete trên artefact nhiều subject / của người khác | 0 / 0 | 0 / 0 | |
| Review queue (B) | không có | 56 dòng (B = 60) | |
| Runtime (s / toàn bộ run) | 0.0023 s (10 dòng) | 0.0523 s (158 dòng) ≈ 33 ms / 100 dòng | Apple Silicon (arm64), macOS 26.4, Python 3.15.2 |

## 05 · Evaluation protocol

- **Primary metric:** `Deletion Completeness` = số cặp (request, artefact) có `expected_action = delete` mà ta ghi `delete`
  chia cho tổng số cặp có `expected_action = delete`. Đơn vị đánh giá là cặp (request_id, artefact_id), khớp chính xác theo id.
  Dòng GT không có prediction được tính là không xóa. Chi tiết trong `reports/metric_contract.md` và `src/metrics/tracks.py::eval_t08`.
- **Secondary:**
  - **False-delete count** (mục tiêu 0): số dòng exception bị ghi delete + số delete ngoài phạm vi GT.
  - **Exception handling accuracy:** retain/escalate đúng chia cho số dòng exception.
  - **Audit trace completeness:** số action có dòng audit tương ứng.
- **Threshold / budget:** không có ngưỡng số. Thứ tự policy và `review_budget = 60` được chốt ngày 2026-11-14 trước khi có eval.
- **Độ bất định:** sẽ báo Wilson 95% CI cho Completeness và Exception accuracy khi có GT (số dòng exception dự kiến khoảng 30).
- **Kiểm tra nội bộ thay GT:** các self-check ở Bảng 1, tính trực tiếp từ graph đầu vào. 13 unit test trên đồ thị mini
  tự xây có đáp án biết trước (`tests/test_t08_deletion.py`).
- **Lệnh evaluate chính xác:**
  `python -m src.evaluate --track t08 --pred out/poc/actions.csv --run-name poc --out reports/metrics_poc.json`

## 06 · Result slices

Slice theo `type` artefact (số dòng in-scope; số dòng delete; PoC chia thành delete / escalate / retain). Tất cả là self-check, chưa đối chiếu GT:

| Slice (type) | N in-scope | Baseline delete | PoC delete / esc / retain | Ghi chú |
|---|---|---|---|---|
| raw_video | 10 | 10 | 8 / 0 / 2 | 2 video đang hold được giữ lại |
| backup | 10 | 0 | 9 / 1 / 0 | `a0156` có ACL `vendor-x` → escalate |
| frame | 33 | 0 | 27 / 6 / 0 | 6 frame nhiều subject |
| face_crop | 30 | 0 | 30 / 0 / 0 | |
| embedding | 30 | 0 | 27 / 3 / 0 | 3 embedding `origin_known = false` (DR-004) |
| annotation | 33 | 0 | 27 / 6 / 0 | 6 annotation nhiều subject |
| training_export | 12 | 0 | 0 / 12 / 0 | 2 export (`a0211`, `a0212`) chứa 9–10 subject, cần redaction/retrain |

Slice theo `reason_code` của PoC: SUBJECT_DATA 128, MULTI_SUBJECT 24, UNKNOWN_ORIGIN 3, LEGAL_HOLD 2, NO_PERMISSION_ACL 1.
Có 3 artefact của người khác nằm dưới frame chung được cố ý bỏ qua: `a0012` (subj_00, khi xử lý DR-001), `a0068` (DR-004), `a0124` (DR-007).

- **Failure case 1: bản sao của dữ liệu đang hold bị lên kế hoạch xóa (rủi ro false-delete).**
  `a0043` (raw_video, subj_03) có `legal_hold = LH-2026-03` → PoC giữ lại. Nhưng con trực tiếp của nó là `a0044`
  (`type = backup`, `filename = drive_subj_03_003.mp4.bak`, `legal_hold = None`) cùng 12 frame/crop/embedding/annotation phía dưới
  vẫn ra `delete`. Tương tự ở DR-005: `a0099` đang hold `LH-2026-07`, còn `a0100` (backup) và 12 artefact con ra `delete`.
  Tổng cộng **26 dòng delete nằm dưới một artefact đang hold**.
  **Root cause:** policy chỉ đọc cờ `legal_hold` của chính artefact; hold không lan xuống bản dẫn xuất trong data graph.
  **Giảm thiểu hiện tại:** các dòng này có score 0.7 (`HELD_ANCESTOR`) nên luôn vào review queue và chặn GO cho tới khi có người duyệt.
  Tuy vậy, `actions.csv` được chấm vẫn ghi `delete`. **Đề xuất:** cho bản `backup` (bản sao y nguyên) kế thừa hold; với các dẫn xuất khác,
  để DPO quyết định phạm vi của lệnh hold.
- **Failure case 2: chỉ xóa video gốc, giữ bản sao và embedding (baseline).**
  Baseline xóa 10 video gốc: chỉ đạt 6.3% số artefact có tag và **không xóa** backup, face_crop hay embedding nào (bảng slice ở trên).
  Embedding là dữ liệu định danh, vẫn nằm trong `mock://vector-db/...`. Baseline còn xóa `a0043` và `a0099` đang legal hold.
  **Root cause:** tìm theo tên file, không có khái niệm lineage hay ngoại lệ.
- **Latency / compute:** PoC chạy 0.0523 s cho 158 dòng (≈ 0.33 ms/dòng) trên Apple Silicon arm64, macOS 26.4, Python 3.15.2, CPU, không GPU.

## 07 · Human & decision

- **Review queue:** `out/poc/review_queue.csv` và `contact_sheet.html` (offline, có nút Export CSV). B = 60 và có 56 dòng cần duyệt
  (30 exception + 26 delete dưới artefact đang hold), nên **không có dòng nào bị cắt khỏi queue**. Thứ tự ưu tiên:
  LEGAL_HOLD 1.0 > NO_PERMISSION_ACL 0.95 > UNKNOWN_ORIGIN / IDENTITY 0.9 > UNTAGGED_DERIVED 0.85 > MULTI_SUBJECT 0.8 > HELD_ANCESTOR 0.7;
  training_export được cộng 0.05.
  102 dòng delete thông thường (dữ liệu chỉ của subject, có quyền, rõ nguồn) không cần duyệt.
- **Override + audit:**
  - Reviewer chọn `approve / edit / reject / escalate / skip`, ghi vào `out/poc/review_queue_reviewed.csv`.
  - Dòng có quyết định nhưng thiếu `reviewer` → pipeline dừng với lỗi.
  - **Guardrail:** reviewer không thể đổi artefact đang hold hoặc nhiều subject thành `delete`. Lệnh override đó bị từ chối và
    ghi `override_refused` vào audit (đã có test). Trong lần chạy demo, reviewer thử ép xóa `a0043` và bị chặn với lý do
    "legal hold LH-2026-03 cannot be overridden by a reviewer".
  - Mọi bước (request_received, plan:*, review:*, delete_mock) được ghi append-only vào `audit_log.jsonl`.
- **Decision policy:** `decision.json` + `decision_log.csv` có `status`, `reason_code`, `owner = Vu Duy The`, kèm evidence.
  Trạng thái hiện tại: **HOLD / `EXCEPTIONS_PENDING_REVIEW`** (2/56 dòng đã duyệt, cả hai approve giữ legal hold).
  Sau khi duyệt đủ 56 dòng: GO / `DELETION_PLAN_APPROVED`. Kết quả: ____
- **Khi nào HOLD/REWORK thay vì tự động xóa:**
  - HOLD khi còn dòng chưa duyệt.
  - REWORK khi vi phạm bất biến.
  - Kể cả khi GO, mặc định `dry_run: true` chỉ dừng ở kế hoạch.
  - Xóa mock là ghi tombstone vào `mock_store_state.json`, idempotent: chạy lại cho `deleted_new = 0, already_deleted = 128`.

## 08 · Reproduce & tests

```bash
make setup          # hoặc: python -m venv .venv && pip install -r requirements.txt
python -m src.main --input fixtures/t08_deletion --config configs/lab.yaml --output out/ --offline --seed 42
python -m src.main --track t08 --output out/baseline --run-name baseline --pipeline src.examples.t08_baseline --offline
python -m src.main --track t08 --output out/poc --run-name poc --offline
python -m src.evaluate --track t08 --pred out/poc/actions.csv --out reports/metrics_poc.json   # cần gt/ của giảng viên
pytest -q
```

- **Output của `pytest -q`:** exit 0, 0 failed. Các test cần GT bị skip. 13 test của nhóm trong `tests/test_t08_deletion.py` đều pass:
  - lineage đầy đủ;
  - thứ tự policy;
  - legal hold thắng mọi luật khác;
  - không đụng dữ liệu của người khác;
  - danh tính chưa xác minh → escalate;
  - subject không có dữ liệu;
  - guardrail override;
  - invariant;
  - mock delete idempotent;
  - parent treo → lỗi rõ;
  - E2E chạy 2 lần cho cùng output;
  - review thiếu tên reviewer → lỗi;
  - baseline sót lineage và xóa nhầm hold.
- `make preflight`: 0 FAIL. `make verify`: 14 manifest OK.
- **Máy:** Apple Silicon arm64, macOS 26.4, Python 3.15.2. **git sha:** `5ad14de`.

## 09 · Limitations & ethics

- **Rủi ro pháp lý / PII / license:** toàn bộ là dữ liệu mock (CC0), không có PII thật và không xóa dữ liệu thật.
  Phạm vi của legal hold đối với bản dẫn xuất (Failure case 1) là câu hỏi pháp lý, cần DPO xác nhận.
- **Điều chưa biết:**
  - Chưa có metric chính thức (thiếu GT).
  - Hai luật `IDENTITY_NOT_VERIFIED` và `UNTAGGED_DERIVED` có test nhưng không xuất hiện trên fixture: mọi request đều đã xác minh,
    mọi artefact đều có tag.
  - Nếu tag subject trong graph sai, BFS sẽ sót hoặc dừng sớm.
- **Nếu scale 10×:** BFS tuyến tính theo số cạnh, nên chạy nhanh. Nút thắt là người duyệt: 56 dòng cho 10 request
  thành khoảng 560 dòng. Cần SLA dashboard (đã có `request_summary.csv` với `days_to_deadline`) và gom duyệt theo legal hold / export.
- **Không tuyên bố** "đạt GDPR / luật Việt Nam". Policy precedence là giả định kỹ thuật, cần DPO/legal xác nhận.
- **Kế hoạch cải tiến:**
  1. Lan legal hold xuống backup.
  2. Thêm luồng redaction cho training_export nhiều subject.
  3. Notification stub + resume sau khi job bị ngắt (mốc +24h).

## 10 · Team contribution

**Bảng 2 · Individual contribution evidence.** Đối chiếu `git shortlog -sne --all` và `reports/team_contributions.csv`.

| Học viên | Git author/email | Module / vai trò | Commit SHA / diff chứng minh | Trình bày? |
|---|---|---|---|---|
| Vu Duy The | ____ | Lead / Integrator + Decision owner: `configs/lab.yaml`, `src/pipeline.py` (run + decide) | `5ad14de` | ____ |
| ____ | ____ | Data & Lineage: `src/t08_deletion.py` (load_inputs, discover_scope) | ____ | ____ |
| ____ | ____ | Policy engine: `decide_action`, `check_invariants`, `override_allowed` | ____ | ____ |
| ____ | ____ | Baseline & Evaluation: `src/examples/t08_baseline.py`, `reports/metric_contract.md` | ____ | ____ |
| ____ | ____ | Human review & Audit: `src/pipeline.py` (rank + review), `mock_delete` | ____ | ____ |
| ____ | ____ | QA tests + Report & Demo: `tests/test_t08_deletion.py`, `tools/show_t08.py`, report | ____ | ____ |

## 11 · References

| URL | Ngày xem | 1–2 dòng ý nghĩa (không chép nguyên văn) |
|---|---|---|
| https://gdpr-info.eu/art-17-gdpr/ | ____ | Quyền được xóa. Khoản 3 liệt kê ngoại lệ, trong đó có nghĩa vụ pháp lý và khiếu kiện; đây là cơ sở để legal hold thắng yêu cầu xóa |
| Luật Bảo vệ dữ liệu cá nhân 91/2025 (link: ____) | ____ | Quyền xóa của chủ thể dữ liệu và các trường hợp không được xóa |
| Nghị định 356/2025 (link: ____) | ____ | Hướng dẫn thi hành, gồm thời hạn xử lý yêu cầu (dùng cho cột `days_to_deadline`) |
| Data Cards Playbook, Google PAIR (link: ____) | ____ | Ghi lineage/provenance cho từng artefact, là nền tảng để truy được bản dẫn xuất |
| California DROP (link: ____) | ____ | Một yêu cầu xóa áp dụng cho mọi data broker, để so sánh với cách lan yêu cầu xóa theo lineage |

---

**Bảng 3 · Checklist chốt khi nộp**

- [ ] Link repo và tag cuối cùng trong README; input mẫu không chứa PII thật.
- [x] Đúng 1 lệnh run, 1 lệnh evaluate, 1 lệnh tests; không cần download khi chạy.
- [ ] Có baseline/PoC metrics, ≥1 failure case, reviewer evidence, decision log. (Đã có failure case và decision log. Còn thiếu metrics chính thức (cần GT) và reviewer evidence đầy đủ 56 dòng.)
- [x] Có license/source provenance, data splits, seed, offline environment, GT/eval N.
- [ ] Mỗi thành viên có ≥1 commit thực chất và được nêu minh bạch trong bảng trên.
