/* Island Điều phối — cuộn "dính đáy" của một khung (THUẦN: không DOM → test Node). Views gọi các hàm này rồi tự đặt scrollTop.
 *
 * - Ở đáy (cách đáy ≤ NEAR px) → dính: nội dung lớn thêm (chữ chạy, ảnh, mục mới) thì ghim lại đáy.
 * - BẤT KỲ lần cuộn LÊN nào của người (lăn chuột, kéo thanh cuộn, phím, vuốt) → bỏ dính ngay; nội dung mới tới lúc đó → `unseen` (nút
 *   "Tin mới nhất"). Quay về đáy (tự cuộn hoặc bấm nút) → dính lại.
 * - Mỗi khung giữ trạng thái riêng (`p.scroll`) → đổi tab rồi quay lại đúng chỗ cũ (hoặc đáy nếu đang dính).
 */
export const NEAR = 48;

export function initScroll() { return { stick: true, top: 0, unseen: false }; }

/** Sự kiện scroll: m = {top, height, client}; `user` = người vừa thao tác cuộn (lăn chuột, vuốt, phím, kéo thanh cuộn — views tự đo).
 *  Cuộn LÊN do người → bỏ dính. Cuộn lên KHÔNG do người (trình duyệt kẹp scrollTop khi khung cao thêm vì ô nhập co lại, nội dung co…)
 *  → vẫn dính (ResizeObserver ghim lại đáy). */
export function onScroll(s, m, user) {
  if (!m.client) return s;  // khung đang ẩn (display:none) — không phải người cuộn
  const dist = m.height - m.top - m.client;
  if (dist <= 2 || (dist <= NEAR && m.top >= s.top)) return { stick: true, top: m.top, unseen: false };
  // không do người nhưng đã xa đáy hơn một màn hình (script / công cụ hỗ trợ / thanh cuộn ngoài ta không đo được) → cũng bỏ dính: trình
  // duyệt kẹp scrollTop chỉ vài trăm px, không bao giờ cả màn hình
  if (m.top < s.top - 1 && (user || !s.stick || dist > m.client)) return { ...s, stick: false, top: m.top };
  return { ...s, top: m.top };
}

/** Người chủ động đi LÊN (wheel deltaY < 0, PageUp/↑/Home, vuốt xuống) — bỏ dính trước khi sự kiện scroll tới. */
export function onUserUp(s) { return s.stick ? { ...s, stick: false } : s; }

/** Nội dung đổi cỡ → {s, pin}: dính thì ghim đáy; không dính mà nội dung LỚN thêm → có tin chưa thấy. */
export function onGrow(s, grew) {
  if (s.stick) return { s, pin: true };
  return { s: grew && !s.unseen ? { ...s, unseen: true } : s, pin: false };
}

/** Bấm "Tin mới nhất" / gửi tin → dính lại. */
export function onJump(s) { return { stick: true, top: s.top, unseen: false }; }
