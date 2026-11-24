/* Island Điều phối — điểm vào (`/manager`). Nối store ↔ luồng SSE v2 ↔ giao diện ↔ điều hướng (pushState/popstate).
 *
 * - Ảnh chụp đầu tiên nhúng trong trang (#coord-boot) → vẽ ngay, mở MỘT luồng SSE với `since=<cursor>` (không hở, không trùng).
 * - Đổi tab luồng / hội thoại đã mở: chỉ đổi khung đang hiện (khung khác giữ trong DOM, ẩn) + pushState — không fetch.
 *   Đích chưa mở: khung tạm "Đang tải…" hiện ngay, tải `/api/manager/snapshot?tab=` — server đăng ký phạm vi của tab TRƯỚC khi lấy
 *   cursor (luồng SSE cũ gửi thêm message của đích đó, không hở). Message của khung tới trong lúc tải được store GIỮ, phát lại phần có
 *   id > cursor sau khi áp ảnh chụp (không mất, không bị ảnh chụp ghi đè). Tải bị huỷ / lỗi → khung đánh dấu cũ, mở lại là tải lại.
 * - `resync` (hở ring / web hoặc daemon khởi động lại): tải lại ảnh chụp khung đang hiện; khung khác đánh dấu cũ, tải lại khi mở.
 */
import { render } from "preact";
import { signal, batch, computed } from "@preact/signals";
import { createStore } from "./store.js";
import { createFeed } from "./sse.js";
import { getJSON, postForm, setCsrf, snapshotUrl } from "./api.js";
import { LOBBY, parseNav, paneUrl, rafBatch, newIdem } from "./util.js";
import { html, ui, App, ConvList, jumpBottom } from "./views.js";

try { performance.mark("coord:start"); } catch (_) {}  // đo khởi động island (DevTools → Performance / performance.measure)
const root = document.getElementById("coord");
const bootEl = document.getElementById("coord-boot");
const boot = JSON.parse(bootEl.textContent);
setCsrf(boot.csrf);
ui.csrf = boot.csrf;
ui.icons = boot.icons || ui.icons;

let storage = null;
try { storage = window.sessionStorage; } catch (_) { storage = null; }
const store = createStore({ signal, batch, computed }, { storage });
store.hub.value = boot.hub || {};
ui.store = store;
window.cfhCoord = store;  // gỡ lỗi / đo hiệu năng trong trình duyệt (không có dữ liệu bí mật: chỉ những gì trang đang hiện)

const TAB = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random())).replace(/[^a-z0-9]/gi, "").slice(0, 16);
const watched = { c: new Set(), w: new Set() };
let feed = null;  // luồng SSE (mở ở cuối, sau khi vẽ)

function emit(name, detail) { try { document.dispatchEvent(new CustomEvent("mgr:" + name, { detail })); } catch (_) {} }
function visible() { return !document.hidden; }

// ------------------------------------------------------------------ phạm vi luồng SSE
function wantOf(p) { return p.conv.value ? { c: p.conv.value } : { w: p.scope }; }
/** Ghi nhận phạm vi đã đăng ký ở server (ảnh chụp `?tab=` tự đăng ký) — dùng cho URL khi luồng SSE phải mở lại từ đầu. */
function noteWatched(p) {
  const x = wantOf(p);
  if (x.c) watched.c.add(x.c); else watched.w.add(x.w);
}
/** Đăng ký thêm phạm vi (hội thoại vừa tạo khi gửi tin đầu). Chỉ ghi nhận khi server nhận; lỗi → thử lại một lần. */
async function watch(p, again) {
  const x = wantOf(p);
  if ((x.c && watched.c.has(x.c)) || (x.w && watched.w.has(x.w))) return;
  let j;
  try { j = await postForm("/api/manager/watch", { tab: TAB, c: x.c ? [x.c] : [], w: x.w ? [x.w] : [] }); } catch (_) { j = { error: "mạng" }; }
  if (j && j.ok) { noteWatched(p); return; }
  if (!again) setTimeout(() => watch(p, true), 1500);
}

// ------------------------------------------------------------------ đã đọc
const readDirty = new Set();
let readT = 0;
function markRead(conv) {
  if (!conv) return;
  store.markSeen(conv);
  readDirty.add(conv);
  clearTimeout(readT);
  readT = setTimeout(() => {
    if (!visible()) return;
    const cs = Array.from(readDirty);
    readDirty.clear();
    for (const c of cs) postForm("/manager/read", { c }).catch(() => {});
  }, 300);
}

// ------------------------------------------------------------------ trạng thái điều phối viên (tick, run, luồng đổi)
let statusT = 0;
function statusSoon(ms) {
  clearTimeout(statusT);
  statusT = setTimeout(() => {
    if (!visible()) return;
    getJSON("/api/manager/status").then((j) => { if (j.manager) store.manager.value = j.manager; if (j.hub) store.hub.value = j.hub; }, () => {});
  }, ms == null ? 1200 : ms);
}

// ------------------------------------------------------------------ hiệu ứng từ store
// chữ đang chạy: store gom delta vào bộ đệm, đổ vào signal MỘT lần mỗi khung hình (~100 token/s không thành 100 lần vẽ/s)
const flushSoon = rafBatch((f) => requestAnimationFrame(f), () => store.flush());
function announce(t) {
  const el = document.getElementById("mgr-status-live");
  if (!el) return;
  el.textContent = "";
  setTimeout(() => { el.textContent = t; }, 60);
}
function runFx(fx) {
  for (const f of fx || []) {
    if (f.fx === "flush") flushSoon();
    else if (f.fx === "status") statusSoon();
    else if (f.fx === "resync") resync(f.reason);
    else if (f.fx === "refresh") refresh(f.pane);
    else if (f.fx === "event") emit(f.name, f.detail);
    else if (f.fx === "reply") {
      const a = store.active.value;
      if (f.pane && f.pane === a && visible()) markRead(f.conv);
      else if (f.conv && !(a && a.conv.value === f.conv)) store.bumpUnread(f.conv);
      if (f.pane && f.pane === a) announce(f.item && f.item.notify ? "Điều phối viên gửi thông báo mới" : "Điều phối viên đã trả lời");
      if (f.item) {
        const node = document.createElement("div");
        node.textContent = f.item.text || "";
        emit("reply", { run: f.item.run_id || "", node, conv: f.conv });
      }
      statusSoon();
    }
  }
}

// ------------------------------------------------------------------ điều hướng
let navCtl = null, navSeq = 0;
function canonical(p) { return paneUrl(p.conv.value, p.scope); }
function show(p, push, url) {
  store.activate(p);
  const want = url || canonical(p);
  try {
    if (push) history.pushState({ mgr: 2 }, "", want); else history.replaceState({ mgr: 2 }, "", want);
  } catch (_) {}
  closeMenus();
  document.title = (p.title.value || "Điều phối") + " · cfharness";
  if (p.conv.value && p.loaded.value) markRead(p.conv.value);
  focusComposer();
}
function closeMenus() {
  for (const d of document.querySelectorAll("#coord details[open]")) if (!d.closest(".mgr-side")) d.removeAttribute("open");
  document.documentElement.classList.remove("mgr-info-open");
}
function focusComposer() {
  if (!(window.matchMedia && matchMedia("(pointer: fine)").matches)) return;
  requestAnimationFrame(() => { const t = document.getElementById("mgr-text"); if (t) t.focus({ preventScroll: true }); });
}

const noRefresh = (fx) => fx.filter((f) => f.fx !== "refresh");
/** Tải ảnh chụp cho khung `p`. MỘT lượt tải tại một thời điểm (`navCtl`: điều hướng, resync, tải lại nền đều qua đây — lượt mới huỷ lượt
 *  cũ, không có hai ảnh chụp cùng ghi vào một khung theo thứ tự tuỳ mạng). `mine` = lượt điều hướng (null: tải nền, không đổi khung). */
async function load(nav, p, mine, bg) {
  if (navCtl) navCtl.abort();
  const ctl = new AbortController();
  navCtl = ctl;
  store.beginLoad(p);
  try {
    const snap = await getJSON(snapshotUrl(nav, TAB), ctl.signal);
    if (navCtl === ctl) navCtl = null;
    if (snap.setup) { store.endLoad(p); location.href = "/manager"; return null; }
    const fx = [];
    const q = store.applySnapshot(snap, p, fx);
    q.retryN = 0;
    noteWatched(q);
    if (mine != null && mine === navSeq) show(q, false);
    runFx(bg ? noRefresh(fx) : fx);
    return q;
  } catch (e) {
    if (navCtl === ctl) navCtl = null;
    const abort = e && e.name === "AbortError";
    // huỷ (đổi tab nhanh / resync) / lỗi: khung đánh dấu cũ → mở lại là tải lại, không kẹt "Đang tải…"; khung đã có dòng thời gian thì
    // phần SSE giữ lại trong lúc tải được phát lại lên bản đang hiện (store.endLoad)
    runFx(noRefresh(store.endLoad(p, abort ? "" : "Không tải được: " + ((e && e.message) || "lỗi mạng") + " — bấm lại tab để thử lại.")));
    if (!abort && p.loaded.value) retryLater(p);
    return null;
  }
}
/** Đích tải lại của một khung: hội thoại nếu có; khung tạm chưa tải xong (vd `?w=` = hội thoại gần nhất của luồng) → ĐÚNG đích ban đầu,
 *  không biến thành "hội thoại mới"; còn lại = phạm vi (Sảnh / luồng) chưa có hội thoại. */
function navOf(p) {
  return p.conv.value ? { c: p.conv.value } : !p.loaded.value && p.nav ? p.nav : { w: p.scope !== LOBBY ? p.scope : null, fresh: true };
}
/** Tải lại nền lỗi (thường ngay sau khi mạng vừa nối lại): thử lại khi khung vẫn đang hiện, giãn cách, tối đa 5 lần. */
function retryLater(p) {
  const n = (p.retryN = (p.retryN || 0) + 1);
  if (n > 5) return;
  setTimeout(() => {
    if (store.active.value === p && p.stale && !p.loading && visible()) load(navOf(p), p, null, true);
  }, Math.min(1000 * 2 ** (n - 1), 15000));
}

/** Tải lại NỀN ảnh chụp của một khung (không đổi khung đang hiện). Khung không hiện → chỉ đánh dấu cũ (tải khi mở lại). */
function refresh(p) {
  if (!p || p.loading || !p.conv.value) return;
  if (store.active.value !== p || !visible()) { p.stale = true; return; }
  load({ c: p.conv.value }, p, null, true);
}

/** Đi tới URL trang Điều phối (`?c=` / `?w=` / `?new=1`). Đã mở → chỉ đổi khung; chưa → khung tạm + tải ảnh chụp. */
function go(url, push) {
  const u = new URL(url, location.href);
  const nav = parseNav(u.search);
  const mine = ++navSeq;
  const cur = store.active.value;
  const have = store.paneFor(nav);
  if (have && !have.stale) { show(have, push && have !== cur); return; }
  if (have) {  // đã mở nhưng cũ (sau resync): hiện ngay bản đang có, tải lại nền
    show(have, push);
    load(nav.c ? { c: have.conv.value } : nav, have, mine);
    return;
  }
  const scope = nav.c ? (cur ? cur.scope : LOBBY) : nav.w || LOBBY;
  const tmp = store.makePane(scope, nav.c, nav);
  store.activate(tmp);
  try { if (push) history.pushState({ mgr: 2 }, "", u.pathname + u.search); } catch (_) {}
  closeMenus();
  load(nav, tmp, mine);
}
ui.go = go;

window.addEventListener("popstate", () => { if (location.pathname === "/manager") go(location.href, false); });

// link /manager khác trong trang (thanh bên "Cuộc trò chuyện mới", nút trên màn hẹp, link trong câu trả lời) → điều hướng trong island
document.addEventListener("click", (e) => {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const a = e.target.closest && e.target.closest("a[href]");
  if (!a || a.target || a.hasAttribute("download")) return;
  let u;
  try { u = new URL(a.getAttribute("href"), location.href); } catch (_) { return; }
  if (u.origin !== location.origin || u.pathname !== "/manager") return;
  e.preventDefault();
  u.searchParams.delete("ui");  // link cũ còn mang ?ui=… (lối thoát trang cũ đã gỡ) — bỏ khỏi URL
  go(u.pathname + u.search, true);
});

// ------------------------------------------------------------------ resync
let resyncT = 0;
function resync(reason) {
  if (reason === "manager") { location.reload(); return; }  // đổi điều phối viên: mọi thứ khác đi — tải lại cả trang
  clearTimeout(resyncT);
  resyncT = setTimeout(() => {
    const p = store.active.value;
    if (!p) return;
    load(navOf(p), p, navSeq);
    statusSoon(0);
  }, 50);
}

// ------------------------------------------------------------------ gửi / dừng / điền
async function send(p, text, files, voice) {
  if (!p.loaded.value || p.loading) {  // ảnh chụp chưa tới: gửi lúc này thì ảnh chụp sẽ xoá tin lạc quan → giữ nguyên ô nhập
    p.sendErr.value = "Đang tải hội thoại — gửi lại sau giây lát.";
    return;
  }
  const tok = store.beginSend(p, text, files);
  jumpBottom(p, false);
  const fd = new FormData();
  fd.set("text", text);
  if (p.conv.value) fd.set("c", p.conv.value); else if (p.scope !== LOBBY) fd.set("w", p.scope);
  if (voice) fd.set("voice", voice);
  for (const f of files || []) fd.append("attach", f, f.name);
  let j;
  try { j = await postForm("/manager/send", fd); } catch (_) { j = { error: "Không gửi được — kiểm tra kết nối rồi thử lại." }; }
  if (j.error) {
    store.sendFail(p, tok, j.error);
    emit("send_error", { error: j.error });
    return;
  }
  const wasNew = !p.conv.value;
  runFx(store.sendOk(p, tok, j));
  if (wasNew && p.conv.value) {
    watch(p);
    if (store.active.value === p) { try { history.replaceState({ mgr: 2 }, "", canonical(p)); } catch (_) {} }
  }
  emit("sent", { run: j.run_id || "", steered: !!j.steered, conv: p.conv.value || "" });
}
ui.send = send;

async function stop(p) {
  const pd = p && p.pending.value;
  if (!pd || !pd.run) return;
  p.pending.value = { ...pd, stopping: true };
  let j;
  try { j = await postForm("/manager/stop", { run: pd.run, c: p.conv.value || "" }); } catch (_) { j = { error: "Không dừng được — kiểm tra kết nối rồi thử lại." }; }
  if (j.error) p.sendErr.value = j.error;
}
ui.stop = stop;
document.addEventListener("mgr:stop", () => stop(store.active.value));

// ------------------------------------------------------------------ trả lời câu hỏi / duyệt thao tác phá huỷ (lạc quan + idem)
/** POST /api/manager/act/<name> với idem → {ok} | {error, retry (mạng lỗi: bấm lại dùng cùng idem)} | {need2}. */
async function act(name, fields) {
  let j;
  try { j = await postForm("/api/manager/act/" + name, fields); } catch (e) {
    return { error: "Không gửi được — kiểm tra kết nối rồi bấm lại (an toàn: không bị làm hai lần).", retry: true };
  }
  if (j.need === "confirm2") return { error: "Thao tác này cần xác nhận bước 2 (thao tác nặng / theo luật).", need2: true };
  if (j.need) return { error: j.message || "Cần xác nhận thêm." };
  if (j.error || j.ok === false) return { error: j.error || "Không thực hiện được." };
  return { ok: true };
}

ui.answer = async (p, q, form) => {
  const keys = Array.from(form.querySelectorAll('input[name="keys"]:checked')).map((x) => x.value);
  const ta = form.querySelector('textarea[name="text"]'), text = ta ? ta.value.trim() : "";
  if (!keys.length && !text) { store.patchQ(p, q.id, (x) => ({ ...x, err: "Chọn một phương án hoặc nhập câu trả lời." })); return; }
  const labels = keys.map((k) => ((q.options || []).find((o) => o.key === k) || {}).label || "");
  const idem = q.retryIdem || newIdem();
  const tok = store.actBegin(p, "answer", q.id, { idem, answer: { keys, labels, text: text || null } });
  const r = await act("answer", { id: q.id, keys, text, confirm: "yes", idem });
  if (r.ok) store.actOk(tok);
  else { store.actFail(tok, r.error, { retry: r.retry, draft: { keys, text } }); emit("send_error", { error: r.error }); }
};

ui.decide = async (p, a, kind, note) => {
  const idem = a.retryIdem || newIdem();
  const tok = store.actBegin(p, kind, a.id, { idem });
  const r = await act(kind, { id: a.id, confirm: "yes", idem, note: note || "", preview: a.text || "" });
  if (r.ok) store.actOk(tok);
  else store.actFail(tok, r.error, { retry: r.retry });
  if (r.need2) store.patchAp(p, a.id, (x) => ({ ...x, need2: true }));
};

ui.fill = (p, text, doSend) => {
  store.setDraft(p, text);
  const t = document.getElementById("mgr-text");
  if (t) {
    t.value = text;
    t.focus();
    try { t.setSelectionRange(text.length, text.length); } catch (_) {}
  }
  if (doSend) { const f = document.getElementById("mgr-form"); if (f && f.requestSubmit) f.requestSubmit(); }
};
ui.retry = (p) => {
  if (p.pending.value) return;
  const items = p.items.value;
  for (let i = items.length - 1; i >= 0; i--) if (items[i].kind === "user") { ui.fill(p, items[i].text, true); return; }
};

document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  const p = store.active.value;
  if (p && p.conv.value && p.loaded.value) markRead(p.conv.value);
  statusSoon(0);
});

// ------------------------------------------------------------------ khởi động
const first = store.applySnapshot(boot.snap, null);
store.activate(first);
try { history.replaceState({ mgr: 2 }, "", location.pathname + location.search); } catch (_) {}
noteWatched(first);

root.textContent = "";
root.removeAttribute("data-state");
ui.late = signal(false);
render(html`<${App} />`, root);
try { performance.measure("coord:boot", "coord:start"); } catch (_) {}
// phần không thấy ngay (panel phải, danh sách hội thoại bên trái) vẽ ở tác vụ sau khung hình đầu — tách long task lúc mở trang
requestAnimationFrame(() => setTimeout(() => {
  ui.late.value = true;
  const side = document.getElementById("coord-convs");
  if (side) { side.textContent = ""; render(html`<${ConvList} />`, side); }
}, 0));
if (first.conv.value) markRead(first.conv.value);

feed = createFeed({
  EventSource: window.EventSource, tab: TAB, since: boot.snap.cursor || "",
  scope: () => ({ c: Array.from(watched.c).slice(-64), w: Array.from(watched.w).slice(-64) }),
  onMessage: (m, id) => runFx(store.apply(m, id)),
  onState: (s) => { store.live.value = s; },
});
window.addEventListener("pagehide", () => feed && feed.close());
