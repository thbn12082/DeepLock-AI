# Kiến trúc DeepLock AI

```text
Tài liệu local -> Python builder -> 9router/Responses GPT-5.5
               -> validator -> GPT-5.5 reviewer độc lập
               -> immutable .dlpack -> APK assets -> Room -> UI/notification
```

Ranh giới mạng nằm trước `.dlpack`. Module Android không chứa SDK HTTP/OpenAI, không có quyền Internet/network-state và không tải nội dung lúc chạy. `StudyRepository` là nơi duy nhất ghi attempt, Sổ lỗi và mastery cho Full Study lẫn Lock Review; thao tác được bọc trong Room transaction.

`LearningPair(atom, lesson, question bank)` là invariant cứng. APK xác minh SHA-256 toàn bộ member và trạng thái `AI_APPROVED` trước khi decode. Lock cycle persist atom đang chờ trong Room; state machine thuần gồm `IDLE → ARMED → CARD_PENDING → STUDYING → COOLDOWN`.

Build hiện dùng compile/target SDK 36 (stable SDK cài được ngày 2026-08-23) và Compose BOM 2026.04.01. BOM 2026.08 yêu cầu SDK 37 nhưng package API 37 chưa xuất hiện trong repository stable/beta/canary của Android CLI trên máy build; không dùng preview SDK giả để vượt gate.
