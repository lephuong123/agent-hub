/* Island Điều phối — điểm vào (`/manager?ui=next`). Nối store ↔ luồng SSE v2 ↔ giao diện ↔ điều hướng (pushState/popstate).
 *
 * - Ảnh chụp đầu tiên nhúng trong trang (#coord-boot) → vẽ ngay, mở MỘT luồng SSE với `since=<cursor>` (không hở, không trùng).
 * - Đổi tab luồng / hội thoại đã mở: chỉ đổi khung đang hiện (khung khác giữ trong DOM, ẩn) + pushState — không fetch.
 *   Đích chưa mở: khung tạm "Đang tải…" hiện ngay, tải `/api/manager/snapshot`, rồi `POST /api/manager/watch` để luồng SSE (vẫn là luồng
 *   cũ) gửi thêm message của hội thoại đó.
 * - `resync` (hở ring / web hoặc daemon khởi động lại): tải lại ảnh chụp khung đang hiện; khung khác đánh dấu cũ, tải lại khi mở.
 */
import { render } from "preact";
import { signal, batch } from "@preact/signals";
import { createStore } from "./store.js";
import { createFeed } from "./sse.js";
import { getJSON, postForm, setCsrf, snapshotUrl } from "./api.js";
import { LOBBY, parseNav, paneUrl } from "./util.js";
import { html, ui, App, ConvList } from "./views.js";

const root = document.getElementById("coord");
const bootEl = document.getElementById("coord-boot");
const boot = JSON.parse(bootEl.textContent);
setCsrf(boot.csrf);
ui.csrf = boot.csrf;
ui.icons = boot.icons || ui.icons;

let storage = null;
try { storage = window.sessionStorage; } catch (_) { storage = null; }
const store = createStore({ signal, batch }, { storage });
ui.store = store;
window.cfhCoord = store;  // gỡ lỗi / đo hiệu năng trong trình duyệt (không có dữ liệu bí mật: chỉ những gì trang đang hiện)

const TAB = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random())).replace(/[^a-z0-9]/gi, "").slice(0, 16);
const watched = { c: new Set(), w: new Set() };
let feed = null;  // luồng SSE (mở ở cuối, sau khi vẽ)

function emit(name, detail) { try { document.dispatchEvent(new CustomEvent("mgr:" + name, { detail })); } catch (_) {} }
function visible() { return !document.hidden; }

// ------------------------------------------------------------------ phạm vi luồng SSE
function wantOf(p) { return p.conv.value ? { c: p.conv.value } : { w: p.scope }; }
function watch(p) {
  const x = wantOf(p);
  if ((x.c && watched.c.has(x.c)) || (x.w && watched.w.has(x.w))) return;
  if (x.c) watched.c.add(x.c); else watched.w.add(x.w);
  if (!feed) return;
  postForm("/api/manager/watch", { tab: TAB, c: x.c ? [x.c] : [], w: x.w ? [x.w] : [] }).catch(() => {});
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
    getJSON("/api/manager/status").then((j) => { if (j.manager) store.manager.value = j.manager; }, () => {});
  }, ms == null ? 1200 : ms);
}

// ------------------------------------------------------------------ hiệu ứng từ store
let raf = 0;
function runFx(fx) {
  for (const f of fx || []) {
    if (f.fx === "flush") { if (!raf) raf = requestAnimationFrame(() => { raf = 0; store.flush(); }); }
    else if (f.fx === "status") statusSoon();
    else if (f.fx === "resync") resync(f.reason);
    else if (f.fx === "event") emit(f.name, f.detail);
    else if (f.fx === "reply") {
      const a = store.active.value;
      if (f.pane && f.pane === a && visible()) markRead(f.conv);
      else if (f.conv && !(a && a.conv.value === f.conv)) store.bumpUnread(f.conv);
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

async function load(nav, p, mine) {
  if (navCtl) navCtl.abort();
  const ctl = new AbortController();
  navCtl = ctl;
  try {
    const snap = await getJSON(snapshotUrl(nav), ctl.signal);
    if (navCtl === ctl) navCtl = null;
    if (snap.setup) { location.href = "/manager"; return null; }
    const q = store.applySnapshot(snap, p);
    watch(q);
    if (mine === navSeq) show(q, false);
    return q;
  } catch (e) {
    if (e && e.name === "AbortError") return null;
    if (navCtl === ctl) navCtl = null;
    if (p) { p.error.value = "Không tải được: " + ((e && e.message) || "lỗi mạng") + " — bấm lại tab để thử lại."; p.stale = true; }
    return null;
  }
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
  const tmp = store.makePane(scope, nav.c);
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
  u.searchParams.set("ui", "next");
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
    load(p.conv.value ? { c: p.conv.value } : { w: p.scope !== LOBBY ? p.scope : null, fresh: true }, p, navSeq);
    statusSoon(0);
  }, 50);
}

// ------------------------------------------------------------------ gửi / dừng / điền
async function send(p, text, files, voice) {
  const tok = store.beginSend(p, text, files);
  p.scroll = { top: 0, stick: true };
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
const x0 = wantOf(first);
if (x0.c) watched.c.add(x0.c); else watched.w.add(x0.w);

root.textContent = "";
root.removeAttribute("data-state");
render(html`<${App} />`, root);
const side = document.getElementById("coord-convs");
if (side) { side.textContent = ""; render(html`<${ConvList} />`, side); }
if (first.conv.value) markRead(first.conv.value);

feed = createFeed({
  EventSource: window.EventSource, tab: TAB, since: boot.snap.cursor || "",
  scope: () => ({ c: Array.from(watched.c).slice(-64), w: Array.from(watched.w).slice(-64) }),
  onMessage: (m) => runFx(store.apply(m)),
  onState: (s) => { store.live.value = s; },
});
window.addEventListener("pagehide", () => feed && feed.close());
