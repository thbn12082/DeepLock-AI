# Bàn giao Luna / 9router

## Tối ưu ngày 2026-09-09 04:20 (Asia/Bangkok)

- Đã chuyển giao: dừng riêng hai process loop cũ `23800/18044`; giữ batch con `4256/9648` chạy nốt để lưu response/cache. Loop mới `24176/24420` chờ process batch kết thúc bằng Windows process handles rồi tự bắt đầu. Kiểm tra PID live trước mọi thao tác.
- Log mới: `run_logs/luna_optimized_20260909_041846.out.log` và `.err.log`. Trạng thái khi ghi: `handover-wait`.
- Lệnh mới giữ cấu hình cũ và thêm `--all-retained --elastic-requests`; `--wait-pid 4256 --wait-pid 9648` chỉ phục vụ bàn giao lần này.
- Queue tự tính từ catalog 544 trừ 126 drop_missing và 40 packed-low-value = 378 bài giữ lại; bỏ pack đã có và giữ backlog cũ trước. Đã xác nhận thêm đúng 88 bài ngoài backlog. Không sửa danh sách 179 ID lịch sử.
- Mốc kiểm tra: Luna 154/179, tổng giữ lại 265/378, thiếu 113. Đây là số file pack, chưa phải chứng nhận đủ outline.
- `run_course_v3_batch.py` và loop cấu hình stdout/stderr UTF-8. Lỗi charmap chiếm 16/25 thất bại vòng 2; batch vòng 3 đã nạp code cũ nên chưa hưởng sửa đổi này.
- `generation/api.py`: semaphore chung giới hạn số HTTP request đang chạy; nhả slot trước backoff. `catalog_build.py`: chế độ elastic cho mỗi bài tối đa max(phần chia cũ, min(32, tổng budget)) thread, toàn bộ HTTP vẫn tối đa 256. Áp dụng cho Responses API/Luna, không bật cho Claude. Log `request-budget` xác nhận cấu hình thực tế.
- `builder.py` + `editorial_rewrite.py`: correction giữ JSON sai để sửa nhưng được bổ sung module ID, thứ tự atom và toàn bộ projection trường builder-owned chuẩn. Không đổi cache key của output hợp lệ, không sửa ID/nguồn một cách mù quáng, validator vẫn kiểm tra.
- `luna-retry-state.json`: ValueError cố định lặp hai vòng được đánh dấu blocked; frozen input được tách ngay. RuntimeError/API/quota vẫn retry. Khi chỉ còn blocked, loop báo `remaining lectures require repair`, không báo hoàn tất. Dùng `--retry-blocked` sau khi đã sửa nguyên nhân để thử lại.
- Đã xác nhận frozen `lec_0f692295199fb4701181`: hai ảnh giữ nguyên bytes/illustration_id nhưng source_ref_id đã thay đổi do chunk nguồn khác (ví dụ anchor_end 561 → 644). Không thể xem là đổi tên ID thuần túy. Đã ghi blocked, giữ nguyên snapshot/cache; cần xử lý phiên bản nguồn riêng.
- Kiểm tra: **58/58 pass** (generation API, cache durability, catalog build, editorial rewrite, Luna queue, subbatch, vision audit); smoke process handover và UTF-8 redirected output pass. Sửa fixture test editorial cũ theo mục tiêu 360–460 từ hiện có và dùng nội dung quá dài cho cả hai atom (URL đã được normalizer loại bỏ). Chưa chạy toàn bộ suite hoặc đo throughput thực sau tối ưu.

Các snapshot bên dưới là lịch sử.

## Tiếp tục ngày 2026-09-09 03:21 (Asia/Bangkok)

- Đã kiểm tra tiến trình: không còn loop/runner cũ trước khi khởi động.
- Quota live trước khi chạy: session 100% trên cả hai account; weekly lần lượt 68% và 52%.
- Đếm lại lúc bắt đầu: backlog 140/179 pack, còn 39; toàn bộ bài giữ lại 251/378 pack, còn 127 (88 ngoài backlog).
- Đã chạy lại đúng một loop, PID ban đầu `23800`, giữ nguyên lệnh continuous / 80 rounds / 16 worker / concurrency 256 / editorial group 3 / min-session 0 / min-weekly 0.01.
- Log loop: `run_logs/luna_resume_20260909_032041.out.log`; log worker: `run_logs/luna_loop_r1_20260909_032041.out.log`. Đã xác nhận chuyển từ vision sang build/editorial.
- Chưa sửa nguyên nhân lỗi cũ: 14 atom generation, 11 quota/API, 6 builder-owned fields, 3 invalid candidate, 2 module ID, 1 atom ID, 1 frozen illustration identity, 1 vision incomplete. Cần đọc kết quả vòng mới để phân biệt lỗi còn lặp với lỗi đã qua sau khi hồi quota.
- Số pack chỉ xác nhận file tồn tại, chưa xác nhận đầy đủ outline. Chưa cài pack mới vào Android.

Phần dưới là snapshot phiên trước.

Cập nhật: **2026-09-08 17:22, Asia/Bangkok**. Đây là snapshot, cần đếm lại khi bắt đầu phiên mới.

## Mục tiêu và yêu cầu của người dùng

- Tạo pack cho các bài có giá trị đã giữ lại, dùng `gpt-5.6-luna` qua 9router local.
- Ưu tiên tốc độ, người dùng yêu cầu mức song song tối đa hiện có: **256**.
- Dùng hết quota còn lại. Không tự đặt ngưỡng giữ 35% hay chờ hồi 60%. Chỉ chờ khi hết quota/giới hạn thực tế; tuân thủ reset/cooldown upstream.
- Luồng liên tục: mỗi bài kiểm tra ảnh → sinh nội dung → editorial → đóng pack; bài xong thì bài mới vào. Không chờ cả nhóm kiểm tra ảnh hay cả nhóm 16 bài hoàn tất.
- Không bỏ validator để lấy tốc độ. Phân biệt “có pack” với “đầy đủ toàn bộ nội dung dự kiến”.

## Số liệu đã xác nhận

| Phạm vi | Tổng | Có pack | Chưa có pack |
|---|---:|---:|---:|
| Backlog Luna | 179 | 140 | 39 |
| Bài giữ lại toàn hệ thống | 378 | 251 | 127 |

- Danh sách nguồn thô có 544 bài. Đã loại 166: 126 `drop_missing` và 40 `packed-low-value`. Không dùng 544 làm mẫu số tiến độ nữa.
- 127 bài thiếu = 39 trong backlog Luna + 88 ngoài backlog này. Runner hiện tại **không tự xử lý 88 bài kia**.
- Sau lần hồi quota lúc khoảng 16:08, backlog tăng từ 93 lên 140 pack: **thêm 47 pack trên hai account**, có tận dụng cache. Không gọi đây là năng suất một account hay một lần build từ đầu.
- Quota kiểm tra lúc 17:17: session 0% trên cả hai account, weekly 68% trên cả hai.
- Lượt hàng đợi cuối có 76 bài, kết thúc 37 ready / 39 failed. `catalog-build-report.status=DONE` chỉ có nghĩa đã xử lý hết futures, không có nghĩa tất cả thành công. Runner trả **75** vì có lỗi quota.

## Tiến trình hiện tại

Lúc 17:22, loop vẫn sống (PID 10580 và Python con 18876), batch đã kết thúc. Log ghi `cooldown`, 1830 giây theo lỗi upstream `reset after 30m`. PID có thể thay đổi: kiểm tra lại, không kill theo snapshot.

Lệnh hiện dùng:

```powershell
& .\.venv\Scripts\python.exe tmp/luna_backlog_loop.py --continuous --rounds 80 --batch-size 16 --concurrency 256 --editorial-group-size 3 --min-session 0 --min-weekly 0.01
```

`--batch-size 16` trong chế độ continuous là số worker bài học, không phải nhóm phải đợi nhau. 16 worker × 16 request mỗi worker = trần 256; không đảm bảo luôn có 256 request hoạt động.

Không khởi động thêm loop khi loop cũ còn sống. Có khóa runner toàn course-v3. Cache/pack hiện hữu cần giữ nguyên; tránh restart nhiều lần vì response upstream đang chạy có thể không được lưu.

## File cần đọc

- `.build/course-v3/luna-greenfield.txt`: 179 ID backlog Luna; không giao với danh sách loại ở lần kiểm tra trước.
- `.build/all-lectures/catalog/catalog-plan.json`: danh sách nguồn thô 544 bài.
- `.build/course-v3/pruned-selection/pruned-selection.json`: `packed_clean` là số lịch sử, `keep_missing` và `drop_missing` là các danh sách lịch sử.
- `.build/course-v3/pruned-selection/packed-low-value.json`: 40 bài loại đã có pack.
- `.build/course-v3/pruned-selection/keep-lecture-ids.txt`: danh sách giữ còn thiếu tại thời điểm lọc, không phải toàn bộ mẫu số hiện tại.
- `.build/course-v3/packs/*.dlpack`: pack đầu ra.
- `.build/course-v3/work/<lecture_id>/`: cache.sqlite3, candidate, curriculum, báo cáo editorial/vision.
- `.build/course-v3/batches/ids-0076-3099f4ad0587/catalog-build-report.json`: kết quả và lỗi chi tiết lượt cuối.
- `.build/course-v3/batches/ids-0076-3099f4ad0587/course-v3-progress.json`: trạng thái tổng của lượt.
- `run_logs/luna_pipeline_20260908_164352.out.log`: loop/cooldown.
- `run_logs/luna_loop_r1_20260908_164352.out.log`: log worker, `lecture-vision`, `lecture-build` và lỗi chất lượng.

## Những thay đổi đã thực hiện

- `.env`: model generator/review/editorial đều Luna; effort đều `none`; `OPENAI_SERVICE_TIER=priority`; `NINEROUTER_CONCURRENCY=256`.
- `content_builder/deeplock_content/settings.py`: đọc service tier tùy chọn.
- `content_builder/deeplock_content/generation/api.py`: chuyển tiếp service tier. Chưa xác nhận upstream thực sự cấp priority; probe trả tier null.
- `tmp/run_course_v3_batch.py`: chấp nhận effort `none`; khi `NINEROUTER_VISION_PER_LECTURE=1`, hoãn vision tổng và gọi builder với `vision_per_lecture=True`.
- `tmp/luna_backlog_loop.py`: `--continuous` đưa mọi bài còn thiếu vào cùng hàng đợi; đặt `NINEROUTER_LECTURE_WORKERS` và `NINEROUTER_VISION_PER_LECTURE=1`. Ngưỡng quota chủ động mặc định 0; lúc thực sự hết quota chờ có quota trở lại (0.01), không còn mặc định 60%.
- `content_builder/deeplock_content/catalog_build.py`: giới hạn worker bài học qua env; mỗi worker vision riêng rồi build. Mỗi bài có plan/report vision riêng tại `<batch>/lecture-vision/<lecture_id>/`, tránh ghi đè report giữa workers.
- Materialization vẫn diễn ra trước toàn danh sách; đã thấy nhanh khi resume. Vision không còn là hàng rào toàn danh sách. Lỗi từng bài được thu thập; bài khác tiếp tục.
- Test `content_builder/tests/test_catalog_build.py`: **12 pass**, gồm kiểm tra queue tự lấp chỗ và bài nhanh build trong khi vision bài chậm còn chờ. py_compile runner/loop pass.
- Trước đó test API/effort pass. Test runner đầy đủ có lỗi đã tồn tại: expected curriculum thiếu `max_page_groups_per_atom=8`; chưa sửa test đó. Không tuyên bố toàn bộ suite pass.

## Việc cần làm ở phiên sau

1. Kiểm tra loop và log mới nhất, quota live; nếu loop còn chờ reset thực tế thì để tự tiếp tục. Không tự giữ lại quota. Nếu đã hồi nhưng loop không hoạt động, xác định nguyên nhân rồi khởi động đúng một loop.
2. Đếm lại file pack giao với ID backlog và ID giữ lại. Báo số có pack, không suy ra độ đầy đủ nội dung chỉ từ file tồn tại.
3. Đọc 39 lỗi trong báo cáo; sửa nguyên nhân để không lặp cùng lỗi qua 80 vòng. Các lỗi đã thấy: editorial đổi module ID / atom ID; frozen editorial input có identity ảnh cũ; generate atom thất bại; validator nội dung; quota 429 bọc trong 503. Không xóa frozen input/cache hoặc bỏ validator một cách mù quáng.
4. Sau khi ổn định backlog, đối chiếu **88 bài giữ lại ngoài backlog Luna** để đưa vào kế hoạch xử lý. Chưa thêm chúng vào runner trong phiên này. Kiểm tra tránh bài đã loại/pack hiện hữu.
5. Kiểm tra độ đầy đủ pack so với outline khi báo “hoàn thiện”: đã có lịch sử pack salvage thiếu atom; `tmp/repair_loop.py` có `_cache_ids`, `_targets`, cùng `pack-truncation-report.json` và `repair-queue.json` (báo cáo có thể cũ).
6. Chưa cài các pack mới lên Android hay build APK trong phiên này. Không báo rằng các pack mới đã có trên điện thoại.

## Đọc quota an toàn

```powershell
$env:PYTHONPATH=(Resolve-Path 'tmp').Path
& .\.venv\Scripts\python.exe -c "import repair_loop; print(repair_loop._quota())"
```

Dùng Python trong `.venv`; lệnh `python` trần từng lỗi trên máy. Helper trên dùng `x-9r-cli-token` đúng. `tmp/check_codex_live_quota.py` cũ dùng Authorization sai nên trả 401; không dùng nó để kết luận account lỗi. Không in API key/token hay nội dung DB credential.

## Các kết luận không được lặp lại

- Quota còn ít không chứng minh bị bóp tốc độ. Ghi chú cũ về 112/192 không phải bằng chứng nhân quả.
- “Nhanh hơn 15%” từ hai probe ngắn mỗi cấu hình không đủ tin cậy. Không dùng để quảng cáo throughput.
- 256 là trần cấu hình, không phải số request đang thực sự chạy hoặc tốc độ tối ưu đã chứng minh.
- Không cộng số pack Luna vào tổng lần nữa: đã nằm trong tổng pack giữ lại.
- Worktree có rất nhiều sửa đổi sẵn của người dùng, nhiều script nằm ở tmp chưa tracked. Không reset/clean/ghi đè thay đổi khác.
