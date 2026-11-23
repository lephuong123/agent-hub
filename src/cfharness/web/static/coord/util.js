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
