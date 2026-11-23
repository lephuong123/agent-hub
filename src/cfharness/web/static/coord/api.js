/* Island Điều phối — gọi server. Mọi POST mang `csrf` + khoá phiên `k` (localStorage `ahub_k`, WT-1 — không bao giờ nằm trong HTML). */

let CSRF = "";
export function setCsrf(v) { CSRF = v || ""; }
export function csrf() { return CSRF; }
export function key() { try { return localStorage.getItem("ahub_k") || ""; } catch (_) { return ""; } }

/** Hết phiên (401 / bị chuyển tới /login) → tải lại trang (server đưa về đăng nhập). */
function guard(r) {
  if (r.status === 401 || (r.redirected && new URL(r.url, location.href).pathname === "/login")) {
    location.reload();
    throw new Error("auth");
  }
  return r;
}

export async function getJSON(url, signal) {
  const r = guard(await fetch(url, { credentials: "same-origin", signal, headers: { Accept: "application/json" } }));
  let j = null;
  try { j = await r.json(); } catch (_) { j = null; }
  if (!r.ok || !j) throw Object.assign(new Error((j && j.error) || "Lỗi " + r.status), { status: r.status });
  return j;
}

/** POST form (FormData hoặc object) → JSON (lỗi HTTP vẫn trả JSON có `error` khi server gửi). */
export async function postForm(url, data, opts) {
  const fd = data instanceof FormData ? data : new FormData();
  if (!(data instanceof FormData)) for (const k in data || {}) {
    const v = data[k];
    if (Array.isArray(v)) for (const x of v) fd.append(k, x); else if (v != null) fd.append(k, v);
  }
  if (!fd.has("csrf")) fd.set("csrf", CSRF);
  fd.set("k", key());
  const r = guard(await fetch(url, { method: "POST", body: fd, credentials: "same-origin", signal: opts && opts.signal,
                                     headers: { "X-Ahub-Fetch": "1", Accept: "application/json" } }));
  let j;
  try { j = await r.json(); } catch (_) { j = { error: "Lỗi " + r.status }; }
  if (!r.ok && !j.error) j.error = "Lỗi " + r.status;
  return j;
}

export function snapshotUrl(nav) {
  const q = new URLSearchParams();
  if (nav.c) q.set("c", nav.c);
  if (nav.w) q.set("w", nav.w);
  if (nav.fresh) q.set("new", "1");
  if (nav.run) q.set("run", nav.run);
  return "/api/manager/snapshot?" + q.toString();
}
