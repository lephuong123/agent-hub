/* Island Điều phối — MỘT luồng SSE v2 mỗi trang (`/sse/manager/v2`), không phụ thuộc DOM (EventSource tiêm vào → test Node được).
 *
 * - Mở lần đầu với `?since=<cursor của snapshot>` (không hở giữa ảnh chụp và luồng) + phạm vi `c`/`w` + `tab` (khoá ngẫu nhiên của tab
 *   trình duyệt — server nhớ phạm vi theo (phiên, tab)).
 * - Nối lại LUÔN do client tự mở lại (không để EventSource tự nối): EventSource tự nối dùng lại URL BAN ĐẦU (chỉ phạm vi lúc mở trang) —
 *   ngắt lâu hơn TTL của server (ngủ máy) thì server tạo phạm vi mới chỉ gồm phạm vi ban đầu → mất message của mọi khung mở sau đó. Tự mở
 *   lại = URL mới mang ĐỦ phạm vi hiện tại (`scope()`) + `since=<id cuối đã nhận>`, giãn cách.
 * - Canh im lặng: server gửi `ping` mỗi ~15 s khi rảnh → quá QUIET ms không có gì (luồng chết lặng: TCP nửa mở, proxy cắt — EventSource
 *   không báo lỗi) thì mở lại.
 * - Bỏ trùng theo id `<boot>.<n>`: cùng boot và n ≤ n cuối → bỏ. Boot khác → server đã gửi `resync` (client tải lại ảnh chụp).
 */
import { parseId } from "./util.js";

export const QUIET = 40000;  // > 2 nhịp ping (HEARTBEAT_S = 15 s)

export function createFeed(o) {
  const ES = o.EventSource;
  let es = null, last = o.since ? parseId(o.since) : null, lastRaw = o.since || "", closed = false, timer = 0, dog = 0, backoff = 1000, my = 0, at = 0;
  const T = o.timers || { set: (f, ms) => setTimeout(f, ms), clear: (t) => clearTimeout(t) };
  const now = o.now || Date.now;
  const quiet = o.quiet || QUIET;

  function url() {
    const q = new URLSearchParams(), sc = (o.scope && o.scope()) || {};
    q.set("tab", o.tab);
    for (const c of sc.c || []) q.append("c", c);
    for (const w of sc.w || []) q.append("w", w);
    if (lastRaw) q.set("since", lastRaw);
    return (o.base || "/sse/manager/v2") + "?" + q.toString();
  }
  /** id mới hơn cái đã xử lý? (và ghi nhận) — false = trùng, bỏ. */
  function fresh(raw) {
    if (!raw) return true;  // hello / ping (không id)
    const id = parseId(raw);
    if (!id) return true;
    if (last && id.boot === last.boot && id.n <= last.n) return false;
    last = id; lastRaw = raw;
    return true;
  }
  function onmsg(ev) {
    at = now();
    if (!fresh(ev.lastEventId)) return;
    let m;
    try { m = JSON.parse(ev.data); } catch (_) { return; }
    if (!m || m.t === "ping") return;
    if (m.t === "hello" && m.boot && last && last.boot !== m.boot) {
      // web khởi động lại: id cũ vô nghĩa — server sẽ gửi `resync` ngay sau; nhận id của boot mới từ đầu
      last = null;
    }
    o.onMessage(m, ev.lastEventId || "");
  }
  function watchdog() {
    T.clear(dog);
    dog = T.set(() => {
      dog = 0;
      if (closed) return;
      if (es && now() - at >= quiet) { o.onState && o.onState("reconnecting"); reopen(); return; }
      watchdog();
    }, quiet / 4);
  }
  function open() {
    if (closed) return;
    const n = ++my;
    at = now();
    o.onState && o.onState(last ? "reconnecting" : "connecting");
    es = new ES(url(), { withCredentials: true });
    es.onopen = () => { if (n !== my) return; at = now(); backoff = 1000; o.onState && o.onState("live"); };
    es.onmessage = (ev) => { if (n === my) onmsg(ev); };
    es.onerror = () => {
      if (n !== my) return;
      o.onState && o.onState("reconnecting");
      // mọi lỗi (kể cả CONNECTING — trình duyệt định tự nối bằng URL cũ): đóng, tự mở lại với phạm vi hiện tại + since = id cuối
      es.close();
      es = null;  // canh im lặng không mở lại chen ngang lúc đang giãn cách
      my++;
      T.clear(timer);
      timer = T.set(open, backoff);
      backoff = Math.min(backoff * 2, 15000);
    };
    watchdog();
  }
  function reopen() { if (es) es.close(); T.clear(timer); open(); }
  open();
  return {
    get last() { return lastRaw; },
    /** Mở lại ngay (giữ vị trí `since`, URL mang phạm vi hiện tại). */
    reopen,
    close() { closed = true; T.clear(timer); T.clear(dog); if (es) es.close(); },
    _fresh: fresh,
  };
}
