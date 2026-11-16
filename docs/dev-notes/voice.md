# Giọng nói trên web — ra lệnh cho đội agent bằng tiếng Việt

Ngày: 2026-11-14. Không migration, không RPC mới (chỉ thêm tham số `voice?` cho `manager.chat`). Test: `tests/test_voice.py`.

## Hai cách dùng (giống luồng của Claude)

1. **Đọc chép** — nút micro cạnh ô nhập. Nói → chữ hiện dần vào ô nhập → người xem/sửa rồi TỰ bấm gửi.
   - Bấm = bật/tắt; giữ nút = nói-thả (push-to-talk). Ô nhập Điều phối: giữ phím Space khi ô trống (như Claude Code).
   - Có ở: ô nhập Điều phối, gửi nhanh ở Tổng quan, giao việc (Tổng quan, trang agent), chỉ đạo run (steer), nói tiếp run,
     bình luận task, mô tả task, trả lời câu hỏi bằng chữ. Thêm ô mới: gắn thuộc tính `data-voice` cho `<textarea>`.
2. **Hội thoại giọng nói** — nút sóng âm ở ô nhập Điều phối. Bảng nổi có quả cầu:
   nghe → im ~1,3 s thì tự gửi (`voice=conversation`) → "đang suy nghĩ / đang dùng <tool>" → đọc to câu trả lời → nghe tiếp.
   - Chạm quả cầu: đang nghe → tạm dừng; đang đọc → ngắt lời và nghe; đang chờ → nói thêm (= steer lượt đang chạy).
   - Nói "dừng lại" → dừng lượt đang chạy (cùng đường nút Dừng, `POST /manager/stop`); "kết thúc"/"thoát" hoặc Esc → thoát.
   - Đổi VI/EN, bật/tắt đọc to — lưu theo trình duyệt (`localStorage cfh_voice`).
   - 60 s không nói → tự tạm dừng nghe.

## Kỹ thuật

- `web/static/voice.js`: Web Speech API của trình duyệt (`SpeechRecognition` `vi-VN`, `speechSynthesis` chọn giọng `vi` tốt nhất —
  ưu tiên giọng Natural/Online, Google, HoaiMy, Linh). Không thư viện, không CDN, CSP không đổi.
- `app.js` (`initManager`) phát sự kiện DOM `mgr:sent|send_error|tool|reply|run_end` và nghe `mgr:stop` — voice.js chỉ dùng chúng,
  không có đường dữ liệu mới. Câu đọc to lấy từ `<template class="mm-src">` của câu trả lời, bỏ khối mã/link/bảng (`speakable`),
  cắt ~1.400 ký tự + "Phần còn lại xem trên màn hình". Chữ agent chỉ qua `textContent` và `speechSynthesis` (test: không `innerHTML`).
- Header `permissions-policy: microphone=(self)` (trước là `microphone=()` — chặn cả micro của chính trang).
- Cần secure context: `cfharness.localhost` (loopback) và web public https đều đạt. Firefox chưa có `SpeechRecognition` → nút ẩn.

## An toàn

- Tin giọng nói mang `voice=dictation|conversation` (`/manager/send` → `manager.chat{voice}`; giá trị khác bị bỏ qua).
- `ManagerService._owner_shortcut(voice=True)` → `_voice_shortcut`: **không bao giờ** duyệt approval (`duyệt`, `approve ap_…`) hay chốt
  câu hỏi xin phép (`action`) từ tin giọng nói — nhận dạng có thể nghe nhầm "ok"/"duyệt". Tin giống lệnh duyệt → ghi chú `[hub]` bảo điều
  phối viên hỏi lại bằng nút. Câu hỏi thường (không `action`) vẫn trả lời được bằng giọng.
- Điều phối viên nhận ghi chú `prompts.voice_note`: tin có thể nghe nhầm; chế độ hội thoại → trả lời ngắn, nói được, không bao giờ xin duyệt bằng giọng.
- Riêng tư: Chrome/Edge gửi âm thanh tới dịch vụ nhận dạng của hãng (Google/Microsoft); Safari dùng Siri (có thể trên máy). Bảng giọng
  nói ghi rõ điều này. Hub không lưu âm thanh — chỉ chữ đã nhận dạng (như tin gõ tay, có audit `manager.chat` với `voice`).

## Việc dở

- Nhận dạng tại máy chạy hub (Whisper/PhoWhisper, `MediaRecorder` → upload) cho Firefox và cho người không muốn gửi âm thanh ra ngoài.
- Đọc theo luồng (stream TTS khi câu trả lời đang chạy) — hiện đọc khi có câu trả lời cuối.
- Cảnh 3D `/office` và Telegram/Slack (tin thoại) chưa có giọng nói.
