# Content builder

Pipeline production gồm `inspect -> freeze-contract (base) -> generate -> freeze-contract (candidate) -> validate -> ai-review -> package -> install-android`. Mỗi bước ghi SQLite cache bằng source/prompt/model/schema hash. Generator và reviewer dùng Responses API Structured Outputs qua 9router local.

Biến quan trọng:

- `OPENAI_BASE_URL=http://127.0.0.1:20128/v1`
- model logic: `gpt-5.5`
- route transport mặc định: `cx/gpt-5.5`
- concurrency mặc định: `5`
- `store:false`
- credential: ưu tiên `OPENAI_API_KEY`/`NINEROUTER_API_KEY`, nếu trống tự đọc key active từ `%APPDATA%/9router/db/data.sqlite` ở chế độ read-only

Reviewer luôn là request fresh-context, dùng prompt/schema/cache riêng. Quiz payload gửi reviewer bị bỏ `correct_option_id`; builder chỉ so đáp án độc lập sau khi response đã hoàn tất. `REPAIR` tạo revision mới nhưng giữ stable ID, chạy lại validator rồi reviewer mới; tối đa hai vòng, còn lỗi sẽ fail-closed.

Lượt build mẫu ngày 2026-08-23 đã đi qua 9router: 4 atom được sinh song song, 20 câu hỏi, 3 Mini Lab; deterministic validator pass và reviewer phê duyệt 32/32 item. Manifest `.dlpack` ghi đúng model logic `gpt-5.5`.

## Lecture core đầy đủ

`content_sources/lecture_core.json` là manifest có kiểm toán cho tám PDF lõi lấy từ
`Lecture/Full DL .zip`. Builder không gom một PDF thành một bài: mỗi phần liên tục
được AI chia tại chuyển tiếp chủ đề thành các bài một-khái-niệm, còn contract vẫn
buộc mọi source chunk kỹ thuật được gán đúng một lần.

Bốn trang MLP dạng raster-only (30, 33, 35, 37) có bản chép trực quan trong
manifest và ảnh PNG đã render. Sáu trang raster-only còn lại phải xuất hiện trong
`excluded_image_only_pages` với lý do divider/end-slide. Preflight fail-closed nếu
có trang thiếu text không thuộc đúng một trong hai nhóm này.

Sau khi stream-extract tám entry PDF theo trường `entry`/`output` vào
`.build/lecture-core/sources` và render media đã duyệt vào
`.build/lecture-core/media`, chạy:

```powershell
cd content_builder
& ..\.venv\Scripts\python.exe -m deeplock_content.cli inspect `
  --input ..\.build\lecture-core\sources `
  --workdir ..\.build\lecture-core
& ..\.venv\Scripts\python.exe -m deeplock_content.cli freeze-contract `
  --workdir ..\.build\lecture-core `
  --curriculum ..\content_sources\lecture_core.json
& ..\.venv\Scripts\python.exe -m deeplock_content.cli generate `
  --workdir ..\.build\lecture-core `
  --curriculum ..\content_sources\lecture_core.json `
  --model gpt-5.5 --prompt-version course-v2 --resume
& ..\.venv\Scripts\python.exe -m deeplock_content.cli freeze-contract `
  --workdir ..\.build\lecture-core `
  --curriculum ..\content_sources\lecture_core.json
```

`generate` kiểm exact source coverage trước khi cache output. Mỗi output sai
coverage/schema được sửa riêng tối đa theo `OPENAI_MAX_ATTEMPTS`; resume chỉ dùng
entry đã vượt semantic validator. Pack chứa `illustrations.json` và binary
PNG/WebP tên phẳng; Python và Android đều kiểm SHA-256, byte size, kích thước,
MIME và page/source link trước khi hiển thị offline.

Lệnh `freeze-contract` đầu tiên chỉ dùng `source.json`, curriculum và media đã duyệt để
khóa cardinality, toàn bộ SourceRef/SourceExcerpt, ID graph và SHA-256 ảnh. Khi
`candidate.json` đã tồn tại, cùng lệnh tự động khóa thêm toàn bộ outline/assignment.
`validate`, `ai-review` và `package` tự nạp `workdir/pack-contract.json`, mặc định từ
chối contract thiếu/chưa khóa/sai hash. Chỉ fixture cũ không dùng curriculum mới được
chạy qua cờ tường minh `--allow-legacy-no-contract`. Contract đã khóa được đóng gói vào
`.dlpack`; verifier offline kiểm lại contract, review hash và các JSON member.
