# Báo cáo nghiệm thu AC-01..AC-20

Ngày kiểm tra gần nhất: 2026-08-24. Thiết bị chính: Samsung Galaxy A56 5G (`SM-A566B`), Android 16 / One UI 8.

- `PASS`: đã có đủ bằng chứng cho tiêu chí được nêu.
- `PARTIAL`: tính năng đã có, nhưng còn thiếu một phần của đúng kịch bản nghiệm thu.
- `IMPLEMENTED`: đã build/kiểm logic, chưa chạy kịch bản nghiệm thu tương ứng.
- `NOT RUN`: chưa có bằng chứng hợp lệ.

| AC | Bằng chứng | Trạng thái |
|---|---|---|
| 01 | Pipeline thật qua 9router: inspect → 4 atom/20 quiz/3 lab → deterministic validation → 32/32 `AI_APPROVED` → pack có hash khóa | PASS |
| 02 | A56 vật lý: hai chu kỳ liên tiếp tạo `LESSON → QUIZ`, cùng `pair_f0b7566b5fde3a6c`/atom `a3`; lesson “Đọc một bước tối ưu” xuất hiện trước, quiz sau mới hỏi chuỗi bước gradient descent | PASS |
| 03 | Merged manifest debug/release không có `INTERNET`/`ACCESS_NETWORK_STATE`; dependency gate và clean build `--offline` pass. Chưa ghi đủ một lượt UI toàn phần trong airplane mode và quan sát DNS/socket | PARTIAL |
| 04 | Kill cưỡng bức đổi PID `15400 → 30190`; foreground service tự dựng lại sau khoảng 6 giây. Room vẫn giữ đúng pending pair/question và đủ 20 session | PASS |
| 05 | Fault-injection validators chặn hai đáp án đúng, source giả, orphan item, stale hash và coverage lỗi; active pack không bị thay | PASS |
| 06 | Scan source/resources/pack/APK không tìm thấy API key; Android clean build offline không đọc `OPENAI_API_KEY` | PASS |
| 07 | Bảng trực tiếp là mặc định: `ACTION_SCREEN_ON` khởi chạy ngay `LearningCardActivity` có `showWhenLocked`. A56 với **Xuất hiện trên cùng**=`allow` đã ghi `DIRECT_VISIBLE`, `latency_ms=145`, `notifyCount=0`; Activity top/resumed khi keyguard vẫn showing. Không dùng full-screen intent, cửa sổ overlay tự do hay `setTurnScreenOn`; fallback notification chỉ chạy khi lỗi/timeout 2 giây | PASS |
| 08 | Graph đã render trên A56, có pan/zoom/select; validator chặn prerequisite cycle và outline fallback dùng đủ node | PASS |
| 09 | Service kiểm notification trạng thái và special access **Xuất hiện trên cùng**. Thiếu special access chuyển `APP_ONLY`/cần thiết lập, hủy learning notification `2001` nhưng giữ status FGS `1001`; UI có trạng thái và nút mở trang cấp quyền. Chưa chủ động thu hồi quyền giữa một chu kỳ đang chạy trên A56 | PARTIAL |
| 10 | Candidate/reviewer/pack thật đã chạy qua 9router với cache/resume; sau đó clean Android build `--offline`, pack gate, 72 JVM tests, debug và release đều pass | PASS |
| 11 | Đã smoke Home, lesson, quiz và progress persistence trên A56. Chưa ghi đủ một lượt airplane mode + Lock Review disabled + search + Topic Practice 5 | PARTIAL |
| 12 | A56 vật lý: với SAW=`allow`, một chu kỳ `Dozing → Awake` mở bảng trực tiếp khi keyguard showing, ghi `DIRECT_VISIBLE`, `latency_ms=145`, `notifyCount=0`; notification hiện tại chỉ có status FGS `1001`, không có learning `2001`. Kiểm tra hồi quy bắt đầu với 26 session trong ngày (đã vượt quota cũ 20) vẫn tạo thêm hai session trực tiếp, tăng 26 → 28 mà tổng `notifyCount` giữ nguyên 26; không có event giới hạn mới. AOD 603 giây không sinh session; chưa đủ mẫu direct/fast biometric để kết luận p95 | PARTIAL |
| 13 | Validator kiểm pair integrity; 50 seed xác nhận thứ tự lesson → quiz cùng pair. Transition production được test: đúng → lesson mới; sai → học lại lesson cùng pair trước khi hỏi câu thay thế. A56 xác nhận cả `LESSON → QUIZ` cùng pair và chuỗi vật lý `LESSON → QUIZ sai → LESSON → QUIZ mới` không rời pair | PASS |
| 14 | Fresh reviewer, đáp án generator bị che, schema/prompt/config/source/candidate hash được khóa; package exit 8 cho missing/stale/reject/refusal/incomplete/disagreement | PASS |
| 15 | `PRE_RECALL` persist trong Room, A–D chỉ hiện sau timer/nút, attempt UID ổn định; quiz đã smoke trên thiết bị. Chưa có test riêng cho process recreation và setting 0 giây | PARTIAL |
| 16 | Sáu `LabMath` và năm decoder tests pass cho đủ năm renderer, invalid lab có textual fallback, không WebView/script. Chưa đo p95 slider trên A56 ở airplane mode | PARTIAL |
| 17 | Source/excerpt hash và offset được validator khóa; runtime có full-excerpt fallback nếu offset lỗi. Chưa ghi đủ thao tác vật lý từ cả lesson và quiz | PARTIAL |
| 18 | Answer/mistake/progress dùng một Room transaction; DB A56 gần nhất có 22 attempt và 8 mistake, ghi chú/lịch sử không bị xóa. Chưa chạy trọn replacement-answer → `IMPROVED` sau restart | PARTIAL |
| 19 | Tám JVM tests pass cho `ACTIVE ↔ PAUSED ↔ SETUP_REQUIRED`, state dựa trên permission/channel/content/heartbeat. Tile chưa được thêm và bấm vật lý trên A56 | PARTIAL |
| 20 | Bảy JVM tests pass cho due/weak/continue, privacy, stale ID và deep link; widget cập nhật từ snapshot local. Widget chưa được pin và bấm vật lý trên A56 | PARTIAL |

## Kết quả tự động

- Python content builder: `40/40` pass, không fail/error/skip.
- Android JVM: `72/72` pass, không fail/error/skip; bao gồm sequence production, 50-seed lesson/quiz, ưu tiên pair, pack/renderer, direct-delivery timing, special-access/setup, fallback, Tile và widget.
- Clean Gradle `--offline`: `verifyNoNetworkPermission`, `verifyNoNetworkDependencies`, `verifyBundledPack`, `testDebugUnitTest`, `assembleDebug`, `lintVitalRelease`, `assembleRelease` đều pass.
- Release/R8/minify pass; APK release vẫn chưa ký.
- Room migration lên schema v2 chạy thành công trên dữ liệu A56 cũ; app và foreground service khởi động không có crash.

## Bằng chứng thiết bị chính

- Package: `com.example.deeplock`; bản debug cuối đã cài đè thành công, cold launch `1362 ms`.
- Direct lock board: trên `SM-A566B` với `SYSTEM_ALERT_WINDOW`=`allow`, màn hình chuyển `Dozing → Awake`; keyguard vẫn `showing=true` trong khi `LearningCardActivity` là top/resumed. Event ghi `latency_ms=145;delivery=direct_activity;direct_launch_visible=true;fallback_used=false`, outcome `DIRECT_VISIBLE`, `notifyCount=0`.
- Notification tại thời điểm kiểm tra direct: chỉ có status foreground service ID `1001`; không có learning notification ID `2001`.
- Thứ tự lesson/quiz ngày 2026-08-24: session `9a89a7d9` là `LESSON`, session kế `679dce59` là `QUIZ`; cả hai cùng `pair_f0b7566b5fde3a6c`/atom `a3`, `DIRECT_VISIBLE`, `notifyCount=0`, latency lần lượt `108 ms` và `107 ms`. Ảnh vật lý cho thấy bảng “KIẾN THỨC CẦN NHỚ” trước, câu “Chuỗi đúng để đọc một bước gradient descent là gì?” sau.
- Nhánh trả lời sai cũng được A56 xác nhận: `LESSON cb45f5c2 → QUIZ e3db9bff (WRONG) → LESSON e1c20cbe → QUIZ fd192c6d`; cả bốn session giữ `pair_392d2395c01da589`/atom `a4`, câu thay thế đổi từ `q_2c81cc3c3ff34634` sang `q_960d8002247ffa31`. Event sai ghi `next_phase=READY_LESSON;relearn_same_pair=true`.
- Sau kiểm tra sequence, dữ liệu người dùng được hoàn nguyên theo hash về 29 session, 22 attempt, 8 mistake và trạng thái `READY_LESSON`.
- Không giới hạn theo ngày: bắt đầu với 26 session hôm nay, các event `screen_off_received → screen_on_received → quiz_direct_activity_visible` tiếp tục được ghi với latency `163 ms` và `276 ms`; session/distinct UUID tăng `26 → 28` trong khi tổng `notifyCount` vẫn là 26. Không phát sinh `screen_off_ignored_daily_limit` mới. Sau kiểm tra, dữ liệu người dùng đã được khôi phục về đúng 26 session, 21 attempt và 8 mistake.
- Lock cycle: `(session, distinct UUID, notifyCount) = (20, 20, 20)`.
- Process recovery: PID đổi `15400 → 30190`; service `isForeground=true`, `startRequested=true`; pending `pair_id`/`question_id` không đổi.
- AOD: 12 lần đo trong 603 giây đều `mWakefulness=Dozing`, `aod_show_state=1`; DB trước/sau vẫn `(20, 20, 20)`. Cài đặt AOD đã được hoàn nguyên sau test. Bản hiện tại không dùng số session trong ngày làm giới hạn phát bảng học.
- Fast fingerprint: một session `POST_UNLOCK_HEADS_UP`, `USER_PRESENT → notify = 159 ms`, đúng pending question.
- Privacy: cờ **Hiện nội dung trên màn hình khóa** được chuyển vào cả Activity trực tiếp và notification fallback. Khi tắt, bảng chỉ hiện thẻ chung an toàn cùng nút mở khóa; learning notification vẫn `VISIBILITY_PRIVATE` và public version không lộ nội dung.

## Artifact và SHA-256

- `dist/DeepLock-AI-v1.0-debug.apk`: `41934678FD28488E898DDA1F06DBBD93D72536E3F16166FDDC87E8FF93FC15F8`
- `dist/DeepLock-AI-v1.0-release-unsigned.apk`: `F0E7DFFB1D71E1DCC7F710301D311B78290CD36C10628738C686D5F737399703`
- `dist/gradient-descent-v1.dlpack` và `dist/default.dlpack`: `E649D7A524BE8020DB769F9EDEF9BCFE55A503690E152589E0DA9367307CF1F9`

## Phần còn cần kiểm thử vật lý để đạt toàn bộ Definition of Done

- Thu đủ mẫu bảng trực tiếp và fast biometric để tính p95 thay vì suy từ một lần direct 145 ms và một lần biometric 159 ms.
- Chạy trọn ma trận airplane mode/Lock Review disabled, permission denied, PRE_RECALL process recreation và source viewer lesson + quiz.
- Thêm/bấm Quick Settings Tile và pin/bấm widget thật trên A56.
- Đo Mini Lab slider p95 trên A56 và chạy replacement-answer → `IMPROVED` qua restart.
