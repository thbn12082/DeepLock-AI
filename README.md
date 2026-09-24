# DeepLock AI

Tiếp tục công việc Luna/9router: đọc [bàn giao ngày 2026-09-08](docs/luna-handoff.md) trước khi chạy hoặc báo tiến độ content builder.

DeepLock AI là ứng dụng Android native học Deep Learning theo hai bề mặt dùng chung tiến độ:

- **Full Study**: đọc bài, làm quiz active recall, xem nguồn, Sổ lỗi và Mini Lab hoàn toàn offline.
- **Lock Review**: mở thẳng bảng trên màn hình khóa theo nhịp cố định **kiến thức → quiz đúng từ kiến thức đó**; không thay lock screen hệ thống.

Android APK không có network layer, không có `INTERNET`/`ACCESS_NETWORK_STATE`. GPT-5.5 chỉ được gọi bởi content builder trên máy tính trước lúc build APK.

## Bản đã build

- APK cài được: `dist/DeepLock-AI-v1.0-debug.apk`
- APK release minified chưa ký: `dist/DeepLock-AI-v1.0-release-unsigned.apk`
- Gói nội dung đã duyệt: `dist/gradient-descent-v1.dlpack`
- Alias gói mặc định dùng khi build: `dist/default.dlpack`
- Nội dung mẫu thật: 4 atom, 20 câu hỏi, 3 Mini Lab; reviewer độc lập duyệt 32/32 mục.

APK debug đã được cài và smoke-test trên Galaxy A56 5G (`SM-A566B`), Android 16. Với quyền **Xuất hiện trên cùng** đã bật, `LearningCardActivity` được xác nhận ở trạng thái top/resumed trên keyguard sau khi màn hình chuyển `Dozing → Awake`. Bản release chưa ký chỉ cài hoặc phân phối được sau khi ký bằng keystore phát hành.

## Thiết lập Lock Review trực tiếp

1. Mở DeepLock, cho phép notification và bật Lock Review.
2. Trong phần thiết lập Lock Review, mở trang Android **Xuất hiện trên cùng** rồi cấp quyền cho DeepLock. Quyền đặc biệt `SYSTEM_ALERT_WINDOW` này được One UI dùng để cho phép khởi chạy Activity từ foreground service; DeepLock không tạo cửa sổ overlay tự do.
3. Khóa rồi bật lại màn hình. Đường mặc định khởi chạy ngay `LearningCardActivity` có `showWhenLocked`, nên bảng học nằm trên keyguard mà không cần chạm notification. App không tự đánh thức màn hình.

Nếu chưa cấp quyền **Xuất hiện trên cùng**, app báo cần thiết lập và không đăng learning notification `2001`; chỉ notification trạng thái `1001` của foreground service vẫn còn vì Android yêu cầu. Sau khi đã cấp quyền, learning notification chỉ là fallback khi Activity gặp lỗi hoặc không xác nhận hiển thị trong 2 giây. Hành vi khởi chạy nền vẫn phụ thuộc Android/OEM nên kết quả đã xác nhận trên A56 không phải bảo đảm cho mọi thiết bị.

Cài đặt **Hiện nội dung trên màn hình khóa** mặc định bật. Nếu tắt, bảng trực tiếp vẫn mở nhưng chỉ hiện thẻ an toàn chung và yêu cầu mở khóa để học; giá trị riêng tư đã lưu được giữ nguyên khi nâng cấp.

Lock Review không giới hạn số thẻ mỗi ngày. Mỗi chu kỳ tắt–bật màn hình hợp lệ đều có thể tạo một bảng học; mục cài đặt cũ `cards_per_day` được bỏ qua khi nâng cấp.

Chu kỳ đầu của một cặp luôn hiện **KIẾN THỨC CẦN NHỚ**. Chu kỳ kế tiếp mới hiện quiz thuộc đúng `LearningPair`/atom vừa học. Nếu trả lời sai, chu kỳ sau dạy lại kiến thức đó rồi chu kỳ tiếp theo mới hỏi một câu khác; nếu trả lời đúng, app ưu tiên kiến thức mới đủ prerequisite.

## Yêu cầu

- Python 3.12+
- JDK 17+
- Android SDK Platform 36, Build Tools 36.0.0+
- 9router đang chạy tại `http://127.0.0.1:20128/v1`
- Gradle wrapper 9.4.1

## Tạo content pack qua 9router

```powershell
Copy-Item .env.example .env
# Trên Windows, builder tự đọc API key đang active từ DB local của 9router.
# Có thể override bằng OPENAI_API_KEY; key không được in hoặc copy vào APK.
python -m deeplock_content.cli doctor
make content-generate INPUT=fixtures/gradient_descent.md MODEL=gpt-5.5
make content-validate
make content-ai-review REVIEW_MODEL=gpt-5.5
make content-package
make content-install
```

Builder ánh xạ model logic `gpt-5.5` sang route 9router `cx/gpt-5.5` và reviewer sang `cx/gpt-5.5-review`, dùng tối đa 10 request song song (hai request cho mỗi account trong cấu hình 5 account đã kiểm chứng), timeout 20 phút cho response lecture dài, connection reuse, retry/backoff, SQLite cache và resume. Reviewer dùng request mới, `store:false`, cache/prompt riêng và không nhận đáp án generator đã khai báo; yêu cầu `REPAIR` được sửa tối đa hai vòng rồi review lại từ đầu.

## Build offline

Sau khi `default.dlpack` đã được cài vào assets, có thể xóa API key và ngắt mạng:

```powershell
cd android
.\gradlew.bat verifyNoNetworkPermission verifyNoNetworkDependencies verifyBundledPack testDebugUnitTest assembleDebug assembleRelease
```

APK debug đã ký bằng debug key và cài trực tiếp được; release minified nằm ở `android/app/build/outputs/apk/release/app-release-unsigned.apk` để người phát hành ký bằng keystore riêng.

## Test

```powershell
cd content_builder
python -m pytest

cd ..\android
.\gradlew.bat test
```

Kết quả nghiệm thu hiện tại: `40/40` Python tests và `72/72` Android JVM tests pass; clean debug build, release/R8 và `lintVital` đều thành công. Chi tiết bằng chứng thiết bị và những kịch bản còn phải chạy vật lý nằm trong báo cáo nghiệm thu.

Xem [kiến trúc](docs/architecture.md), [content builder](docs/content-builder.md), [giới hạn lock screen](docs/android-lockscreen-limitations.md) và [ma trận nghiệm thu](docs/acceptance-report.md).
# DeepLock-AI
