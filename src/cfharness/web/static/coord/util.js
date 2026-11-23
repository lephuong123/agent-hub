/* Island Điều phối — hàm THUẦN (không DOM, không thư viện): id SSE, URL ↔ đích điều hướng, khoá nháp, thời gian.
 * Dùng chung cho trình duyệt và test Node (tests/coord_store_check.mjs). */

export const LOBBY = "_";

/** `<boot>.<n>` → {boot, n}; sai dạng → null. */
export function parseId(raw) {
  if (!raw) return null;
  const s = String(raw).trim(), i = s.lastIndexOf(".");
  if (i <= 0) return null;
  const n = s.slice(i + 1);
  if (!/^\d+$/.test(n)) return null;
  return { boot: s.slice(0, i), n: Number(n) };
}

/** Đích của một URL trang Điều phối: {c, w, fresh, run}. `w` giữ nguyên chữ người gõ (id hoặc mã luồng, vd LH-55166). */
export function parseNav(search) {
  const q = new URLSearchParams(search || "");
  return { c: q.get("c") || null, w: q.get("w") || null, fresh: q.get("new") === "1", run: q.get("run") || null };
}

/** URL của một khung (pane): hội thoại → `?c=`; chưa có hội thoại → `?w=<luồng>&new=1` (Sảnh: `?new=1`). Luôn kèm `ui=next`. */
export function paneUrl(conv, scope) {
  if (conv) return "/manager?ui=next&c=" + encodeURIComponent(conv);
  if (scope && scope !== LOBBY) return "/manager?ui=next&w=" + encodeURIComponent(scope) + "&new=1";
  return "/manager?ui=next&new=1";
}

/** Khoá bản nháp — CÙNG dạng với trang cũ (`cfh_mgr_draft:c:<hội thoại>` / `cfh_mgr_draft:w:<luồng|rỗng>`) → nháp giữ khi đổi giao diện. */
export function draftKey(conv, scope) {
  return conv ? "c:" + conv : "w:" + (scope && scope !== LOBBY ? scope : "");
}

export function relText(iso, now) {
  if (!iso) return "";
  const d = new Date(iso);
  let s = Math.round(((now == null ? Date.now() : now) - d.getTime()) / 1000);
  if (isNaN(s)) return "";
  if (s < 0) s = 0;
  if (s < 10) return "vừa xong";
  if (s < 60) return s + " giây trước";
  const m = Math.floor(s / 60);
  if (m < 60) return m + " phút trước";
  const h = Math.floor(m / 60);
  if (h < 24) return h + " giờ trước";
  const dd = Math.floor(h / 24);
  if (dd < 7) return dd + " ngày trước";
  return d.toLocaleDateString("vi-VN");
}

export function fmtTs(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  const p = (x) => String(x).padStart(2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes()) + " " + p(d.getDate()) + "/" + p(d.getMonth() + 1);
}

export function humanSize(n) {
  return n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB";
}

/* Đính kèm — giới hạn = của daemon (server kiểm lại theo nội dung). */
export const IMG_TYPES = { "image/png": 1, "image/jpeg": 1, "image/gif": 1, "image/webp": 1 };
export const MAX_IMG = 4, MAX_DOC = 4, IMG_BYTES = 5 * 1024 * 1024, DOC_BYTES = 10 * 1024 * 1024;
export const MAX_TEXT = 8000;

export function isImg(f) { return !!IMG_TYPES[String((f && f.type) || "").toLowerCase()]; }

/** Thêm tệp vào danh sách đã chọn theo giới hạn → {files (mảng MỚI), errors}. Thuần (f chỉ cần {name, size, type}). */
export function addFiles(picked, list) {
  const out = picked.slice(), errs = [];
  for (const f of list || []) {
    if (!f || !f.size) { if (f) errs.push(f.name + ": tệp rỗng"); continue; }
    const img = isImg(f);
    const n = out.filter((p) => isImg(p) === img).length;
    if (img && f.size > IMG_BYTES) { errs.push(f.name + ": ảnh quá lớn (tối đa 5 MB)"); continue; }
    if (!img && f.size > DOC_BYTES) { errs.push(f.name + ": tệp quá lớn (tối đa 10 MB)"); continue; }
    if (n >= (img ? MAX_IMG : MAX_DOC)) { errs.push("tối đa " + (img ? MAX_IMG + " ảnh" : MAX_DOC + " tệp") + " mỗi tin — bỏ " + f.name); continue; }
    out.push(f);
  }
  return { files: out, errors: errs };
}

/* ---------------------------------------------------------------- GĐ2 phần 2 */
const WEEKDAY = ["Chủ nhật", "Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy"];
/** Khoá ngày "YYYY-MM-DD" (giờ máy) của một thời điểm ISO; sai → null. */
export function dayKey(iso) {
  const d = iso ? new Date(iso) : new Date();
  if (isNaN(d.getTime())) return null;
  const p = (x) => String(x).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
}
/** Nhãn vạch ngày — CÙNG dạng `present.day_label`: "Hôm nay" / "Hôm qua" / "Thứ Hai, 10/11/2026". */
export function dayLabel(key, now) {
  const m = /^(\d{4})-(\d\d)-(\d\d)$/.exec(key || "");
  if (!m) return key || "";
  const d = new Date(+m[1], +m[2] - 1, +m[3]), t = now ? new Date(now) : new Date();
  const days = Math.round((new Date(t.getFullYear(), t.getMonth(), t.getDate()) - d) / 864e5);
  if (days === 0) return "Hôm nay";
  if (days === 1) return "Hôm qua";
  return WEEKDAY[d.getDay()] + ", " + m[3] + "/" + m[2] + "/" + m[1];
}

/** URL an toàn cho link/ảnh trong chữ đang chạy (markdown của agent = dữ liệu): http(s), mailto, đường dẫn tương đối / #neo. Khác → null. */
export function safeUrl(v) {
  const s = String(v == null ? "" : v).trim();
  if (!s || /[\u0000-\u001f\u007f]/.test(s)) return null;
  if (/^[\\/]{2}/.test(s)) return null;  // //host, \\host, /\host = URL tuyệt đối khác gốc
  if (/^(\/|\.{1,2}\/|#|\?)/.test(s)) return s;
  const m = /^([a-z][a-z0-9+.-]*):/i.exec(s);
  if (!m) return /^[^:]*$/.test(s) ? s : null;  // "docs/a.md"
  return /^(https?|mailto)$/i.test(m[1]) ? s : null;
}

/** Xếp thẻ chờ duyệt (approval) vào dòng thời gian theo giờ tạo — trước mục ĐẦU TIÊN mới hơn nó (vạch ngày đi cùng mục sau nó). */
export function mergeTimeline(items, extra) {
  if (!extra || !extra.length) return items;
  const out = items.slice();
  for (const a of extra.slice().sort((x, y) => String(x.ts || "").localeCompare(String(y.ts || "")))) {
    let at = out.length;
    if (a.ts) for (let i = 0; i < out.length; i++) {
      if (out[i].ts && out[i].ts > a.ts) {
        at = i;
        const k = dayKey(a.ts);  // vạch ngày đứng ngay trước thuộc về mục sau nó — chỉ lùi qua nó khi thẻ thuộc ngày KHÁC
        while (at > 0 && out[at - 1].kind === "day" && out[at - 1].key !== k) at--;
        break;
      }
    }
    out.splice(at, 0, a);
  }
  return out;
}

/** Gom nhiều lần gọi thành MỘT lần `fn` mỗi khung hình (`raf` tiêm vào → test Node). */
export function rafBatch(raf, fn) {
  let on = false;
  return () => { if (on) return; on = true; raf(() => { on = false; fn(); }); };
}

let idemN = 0;
/** Khoá idempotency cho MỘT lần bấm (gửi lại cùng khoá khi mạng lỗi → server trả đúng kết quả cũ, không chạy lần hai). */
export function newIdem() {
  const r = typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36);
  return "i" + (++idemN) + "-" + r.replace(/[^a-z0-9]/gi, "").slice(0, 20);
}
