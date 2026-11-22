/* cfharness web — đo hiệu năng phía trình duyệt (docs/dev-notes/perf-monitor.md). Nạp SAU htmx, TRƯỚC app.js (để bọc EventSource
 * trước khi app.js mở luồng SSE). Chỉ số ms + đường dẫn trang (server ánh xạ về route template) — không nội dung trang/tin nhắn.
 * Mẫu {m: metric, r: đường dẫn, d: ms} gom lô, gửi bằng navigator.sendBeacon tới POST /perf/beacon (thân text/plain chứa JSON kèm
 * csrf + khoá phiên `k` như mọi POST — sendBeacon không đặt được header). Chưa đăng nhập (không có csrf trên trang) → không làm gì.
 */
(function () {
  "use strict";
  var ENDPOINT = "/perf/beacon", MAX_Q = 200, FLUSH_MS = 15000, SW_KEY = "cfh_perf_sw", SW_MAX_AGE = 30000;
  var q = [];
  var perf = window.performance;
  function now() { return perf && perf.now ? perf.now() : Date.now(); }
  function epochNow() { return perf && perf.timeOrigin ? perf.timeOrigin + perf.now() : Date.now(); }
  function pathOf(u) {
    try { return new URL(u, location.href).pathname; } catch (_) { return "/"; }
  }
  function add(m, r, d) {
    if (typeof d !== "number" || !isFinite(d) || d < 0) return;
    if (q.length >= MAX_Q) q.shift();
    q.push({ m: m, r: r || location.pathname, d: Math.round(d * 10) / 10 });
  }
  function csrf() {
    var el = document.querySelector('input[name="csrf"]');
    return el ? el.value : "";
  }
  function key() { try { return localStorage.getItem("ahub_k") || ""; } catch (_) { return ""; } }
  function flush() {
    if (!q.length) return;
    var c = csrf(), k = key();
    if (!c || !k) { q = []; return; }
    var body = JSON.stringify({ csrf: c, k: k, s: q.splice(0, MAX_Q) });
    try {
      if (navigator.sendBeacon) { navigator.sendBeacon(ENDPOINT, new Blob([body], { type: "text/plain" })); return; }
    } catch (_) {}
    try { fetch(ENDPOINT, { method: "POST", body: body, keepalive: true, credentials: "same-origin", headers: { "Content-Type": "text/plain" } }); } catch (_) {}
  }
  setInterval(flush, FLUSH_MS);
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") flush(); });
  window.addEventListener("pagehide", flush);

  // ------------------------------------------------------------ tải trang (Navigation Timing) + chuyển tab / luồng
  function navTiming() {
    var n = perf && perf.getEntriesByType ? perf.getEntriesByType("navigation")[0] : null;
    if (!n) return;
    var r = location.pathname;
    add("nav.ttfb", n.responseStart - n.startTime, r);
    add("nav.dcl", n.domContentLoadedEventEnd - n.startTime, r);
    if (n.loadEventEnd > 0) add("nav.load", n.loadEventEnd - n.startTime, r);
    var sw = null;
    try { sw = JSON.parse(sessionStorage.getItem(SW_KEY) || "null"); sessionStorage.removeItem(SW_KEY); } catch (_) {}
    if (sw && sw.to === r && typeof sw.t === "number") {
      var end = perf.timeOrigin ? perf.timeOrigin + (n.loadEventEnd > 0 ? n.loadEventEnd : perf.now()) : Date.now();
      var d = end - sw.t;
      if (d >= 0 && d < SW_MAX_AGE) add(sw.k === "ws" ? "switch.ws" : "switch.tab", d, r);
    }
  }
  if (document.readyState === "complete") setTimeout(navTiming, 0);
  else window.addEventListener("load", function () { setTimeout(navTiming, 0); });

  function markSwitch(k, href) {
    try { sessionStorage.setItem(SW_KEY, JSON.stringify({ k: k, t: epochNow(), to: pathOf(href) })); } catch (_) {}
  }
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a || a.target === "_blank") return;
    if (a.matches(".ws-tab, [data-ws]")) markSwitch("ws", a.href);
    else if (a.closest("nav.nav, nav.tabbar")) markSwitch("tab", a.href);
  }, true);
  document.addEventListener("submit", function (e) {
    var f = e.target;  // form "Chuyển sang luồng" (GET /manager?workstream=…)
    if (f instanceof HTMLFormElement && f.querySelector('select[name="workstream"]') && (f.method || "get").toLowerCase() === "get") {
      markSwitch("ws", f.getAttribute("action") || location.pathname);
    }
  }, true);

  // ------------------------------------------------------------ htmx: beforeRequest → afterSettle (lỗi: afterRequest)
  var starts = typeof WeakMap === "function" ? new WeakMap() : null;
  function hxPath(e) {
    var c = e.detail && e.detail.requestConfig;
    return pathOf((c && c.path) || (e.detail && e.detail.pathInfo && e.detail.pathInfo.requestPath) || location.pathname);
  }
  if (starts && document.body) {
    document.body.addEventListener("htmx:beforeRequest", function (e) {
      if (e.detail && e.detail.xhr) starts.set(e.detail.xhr, now());
    });
    var done = function (e) {
      var x = e.detail && e.detail.xhr;
      if (!x || !starts.has(x)) return;
      var t0 = starts.get(x);
      starts.delete(x);
      add("htmx", now() - t0, hxPath(e));
    };
    document.body.addEventListener("htmx:afterSettle", done);
    document.body.addEventListener("htmx:afterRequest", function (e) {
      if (e.detail && (e.detail.failed || !e.detail.successful)) done(e);
      else if (e.detail && e.detail.xhr && (e.detail.xhr.status === 204 || e.detail.xhr.status === 286)) done(e);  // không swap → không settle
    });
  }

  // ------------------------------------------------------------ SSE: (nối lại) → event đầu; độ trễ ts event → nhận
  var Native = window.EventSource;
  if (typeof Native === "function") {
    var ETS = /"ets":\s*"([^"]+)"/;
    var watch = function (es, url) {
      var r = pathOf(url), t0 = now(), waiting = true, lastLag = 0;
      es.addEventListener("error", function () { if (!waiting) { waiting = true; t0 = now(); } });
      es.addEventListener("message", function (ev) {
        if (waiting) { waiting = false; add("sse.first", now() - t0, r); }
        var data = ev.data;
        if (typeof data !== "string" || data.indexOf('"ets"') < 0) return;
        var tnow = now();
        if (tnow - lastLag < 500) return;  // tối đa 2 mẫu/giây/luồng
        var m = ETS.exec(data);
        var ts = m ? Date.parse(m[1]) : NaN;
        if (isNaN(ts)) return;
        lastLag = tnow;
        var lag = Date.now() - ts;
        if (lag >= 0 && lag < 600000) add("sse.lag", lag, r);
      });
    };
    var Wrapped = function (url, cfg) {
      var es = cfg === undefined ? new Native(url) : new Native(url, cfg);
      try { watch(es, url); } catch (_) {}
      return es;
    };
    Wrapped.prototype = Native.prototype;
    Wrapped.CONNECTING = 0; Wrapped.OPEN = 1; Wrapped.CLOSED = 2;
    window.EventSource = Wrapped;
  }

  // ------------------------------------------------------------ long task (Chrome/Edge)
  try {
    if (window.PerformanceObserver && PerformanceObserver.supportedEntryTypes && PerformanceObserver.supportedEntryTypes.indexOf("longtask") >= 0) {
      new PerformanceObserver(function (list) {
        var es = list.getEntries();
        for (var i = 0; i < es.length; i++) add("longtask", es[i].duration, location.pathname);
      }).observe({ type: "longtask", buffered: true });
    }
  } catch (_) {}
})();
