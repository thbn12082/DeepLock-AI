# Giới hạn lock screen

## Đường hiển thị mặc định

Khi một chu kỳ học đủ điều kiện nhận `ACTION_SCREEN_ON`, DeepLock mặc định yêu cầu mở ngay `LearningCardActivity`. Đây là Activity toàn màn hình bình thường có `showWhenLocked`, không phải lock screen thay thế, Accessibility service, full-screen intent hay cửa sổ `TYPE_APPLICATION_OVERLAY`. App cũng không dùng `setTurnScreenOn`: màn hình chỉ sáng do thao tác của người dùng/hệ thống.

Android giới hạn việc một app ở nền tự mở Activity. Trên One UI, ngay cả foreground service cũng có thể bị chặn với `BAL_BLOCK`. Vì vậy người dùng phải cấp special access **Xuất hiện trên cùng** (`SYSTEM_ALERT_WINDOW`) để đường trực tiếp hoạt động. DeepLock kiểm tra quyền bằng API hệ thống và có nút mở đúng trang cài đặt của ứng dụng.

Trên Galaxy A56 5G (`SM-A566B`), Android 16 / One UI 8, đường này đã được xác nhận với special access ở trạng thái `allow`: màn hình chuyển `Dozing → Awake`, keyguard vẫn `showing=true`, `LearningCardActivity` là Activity top/resumed, và event `DIRECT_VISIBLE` có `latency_ms=145`, `notifyCount=0`.

## Khi thiếu quyền hoặc khởi chạy lỗi

- Thiếu **Xuất hiện trên cùng**: DeepLock chuyển sang trạng thái cần thiết lập/app-only, không đăng learning notification ID `2001` và không giả vờ rằng bảng đã hiện. Notification ID `1001` vẫn tồn tại vì đó là notification trạng thái bắt buộc của foreground service.
- Đã có quyền nhưng Activity ném lỗi, bị chặn ngoài dự kiến hoặc không xác nhận foreground trong 2 giây: app mới dùng learning notification ID `2001` làm fallback cho chính session đang chờ.
- Nếu người dùng chủ động tắt chế độ bảng trực tiếp để dùng notification-first, khả năng hiện notification trên lock screen còn phụ thuộc quyền notification, channel và cài đặt riêng của OEM.

## Riêng tư

Cài đặt **Hiện nội dung trên màn hình khóa** mặc định bật theo yêu cầu hiển thị bảng học ngay. Nếu người dùng tắt, Activity trực tiếp chỉ hiển thị thông báo chung “Thẻ học đã sẵn sàng” và nút mở khóa để học; nội dung bài/quiz không xuất hiện trên keyguard. Giá trị `false` đã lưu được tôn trọng qua nâng cấp. Learning notification fallback cũng dùng cùng cờ riêng tư.

## Tần suất học

DeepLock không áp dụng giới hạn thẻ theo ngày. Khi Lock Review đang bật và các điều kiện màn hình/quyền hợp lệ, mỗi chu kỳ tắt–bật có thể tạo một session mới. Số session trong ngày vẫn được giữ trong chẩn đoán để quan sát, không dùng để chặn bảng học.

## Nhịp kiến thức và quiz

DeepLock luôn giao lesson trước rồi mới giao quiz ở chu kỳ kế tiếp. Hai session phải có cùng `LearningPair` và atom. Quiz chưa trả lời vẫn được giữ nguyên; quiz sai chuyển về lesson của chính pair đó để học lại trước khi hỏi câu thay thế. Quiz đúng kết thúc pair và ưu tiên kiến thức mới đủ prerequisite ở chu kỳ sau. Trạng thái `QUIZ_RETRY` do phiên bản cũ lưu được tự chuyển về lesson khi nâng cấp, không xóa attempt, mistake hay progress.

## Phạm vi bảo đảm

Kết quả trên A56 xác nhận đường triển khai cho thiết bị mục tiêu, không bảo đảm mọi hãng hoặc mọi bản Android sẽ cho phép cùng hành vi. Chính sách background Activity start, quản lý pin và giao diện special access có thể khác theo OEM hoặc thay đổi sau cập nhật hệ thống. Luôn có đường vào Full Study từ launcher, Quick Settings Tile và widget.

Tham khảo tài liệu Android về [background activity starts](https://developer.android.com/guide/components/activities/secure-bal), [`SYSTEM_ALERT_WINDOW`](https://developer.android.com/reference/android/Manifest.permission#SYSTEM_ALERT_WINDOW) và [`setShowWhenLocked`](https://developer.android.com/reference/android/app/Activity#setShowWhenLocked(boolean)).
