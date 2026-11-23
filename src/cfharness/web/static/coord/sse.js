/* Island Điều phối — MỘT luồng SSE v2 mỗi trang (`/sse/manager/v2`), không phụ thuộc DOM (EventSource tiêm vào → test Node được).
 *
 * - Mở lần đầu với `?since=<cursor của snapshot>` (không hở giữa ảnh chụp và luồng) + phạm vi ban đầu `c`/`w` + `tab` (khoá ngẫu nhiên
 *   của tab trình duyệt — server nhớ phạm vi theo (phiên, tab) nên nối lại không mất phạm vi đã `watch`).
 * - Nối lại: EventSource tự gửi `Last-Event-ID`. Lỗi cứng (readyState CLOSED, vd 503) → tự mở lại với `since=<id cuối đã nhận>`, giãn cách.
 * - Bỏ trùng theo id `<boot>.<n>`: cùng boot và n ≤ n cuối → bỏ. Boot khác → server đã gửi `resync` (client tải lại ảnh chụp).
 */
import { parseId } from "./util.js";

export function createFeed(o) {
  const ES = o.EventSource;
  let es = null, last = o.since ? parseId(o.since) : null, lastRaw = o.since || "", closed = false, timer = 0, backoff = 1000, my = 0;
  const T = o.timers || { set: (f, ms) => setTimeout(f, ms), clear: (t) => clearTimeout(t) };

  function url() {
    const q = new URLSearchParams();
    q.set("tab", o.tab);
    for (const c of (o.scope && o.scope().c) || []) q.append("c", c);
    for (const w of (o.scope && o.scope().w) || []) q.append("w", w);
    if (lastRaw) q.set("since", lastRaw);
    return (o.base || "/sse/manager/v2") + "?" + q.toString();
  }
  /** id mới hơn cái đã xử lý? (và ghi nhận) — false = trùng, bỏ. */
  function fresh(raw) {
    if (!raw) return true;  // hello (không id)
    const id = parseId(raw);
    if (!id) return true;
    if (last && id.boot === last.boot && id.n <= last.n) return false;
    last = id; lastRaw = raw;
    return true;
  }
  function onmsg(ev) {
    if (!fresh(ev.lastEventId)) return;
    let m;
    try { m = JSON.parse(ev.data); } catch (_) { return; }
    if (m && m.t === "hello" && m.boot && last && last.boot !== m.boot) {
      // web khởi động lại: id cũ vô nghĩa — server sẽ gửi `resync` ngay sau; nhận id của boot mới từ đầu
      last = null;
    }
    o.onMessage(m, ev.lastEventId || "");
  }
  function open() {
    if (closed) return;
    const n = ++my;
    o.onState && o.onState(last ? "reconnecting" : "connecting");
    es = new ES(url(), { withCredentials: true });
    es.onopen = () => { if (n !== my) return; backoff = 1000; o.onState && o.onState("live"); };
    es.onmessage = (ev) => { if (n === my) onmsg(ev); };
    es.onerror = () => {
      if (n !== my) return;
      o.onState && o.onState("reconnecting");
      if (es.readyState === 2) {  // CLOSED: trình duyệt bỏ cuộc (HTTP lỗi) → tự mở lại với since = id cuối
        es.close();
        T.clear(timer);
        timer = T.set(open, backoff);
        backoff = Math.min(backoff * 2, 15000);
      }
    };
  }
  open();
  return {
    get last() { return lastRaw; },
    /** Mở lại ngay (vd đổi phạm vi ban đầu khi chưa có watch) — giữ vị trí. */
    reopen() { if (es) es.close(); T.clear(timer); open(); },
    close() { closed = true; T.clear(timer); if (es) es.close(); },
    _fresh: fresh,
  };
}
