/* cfharness web — JS nhỏ, không phụ thuộc gì ngoài htmx (vendored). Tuân CSP script-src 'self' (không inline/eval).
 * 1. Hộp xác nhận cho form [data-confirm] (thao tác nhạy cảm). Không có JS → server tự hiện trang xác nhận.
 * 2. Luồng live của run (SSE /sse/run/<id>) và "hoạt động gần đây" (SSE /sse/firehose).
 * 3. UX v2: đóng toast, thời gian tương đối (<time data-rel>), đóng menu/sheet khi bấm ra ngoài, ô nhập tự giãn.
 * Không bao giờ dựng HTML từ chữ của agent: chỉ chèn HTML do server vẽ (autoescape) hoặc gán textContent.
 */
(function () {
  "use strict";

  // ------------------------------------------------------------ toast
  document.addEventListener("click", function (e) {
    var x = e.target.closest && e.target.closest(".toast-x");
    if (x && x.parentNode) x.parentNode.remove();
  });
  setTimeout(function () {
    var ok = document.querySelectorAll("#toasts .flash.ok");
    for (var i = 0; i < ok.length; i++) ok[i].remove();
  }, 7000);

  // ------------------------------------------------------------ thời gian tương đối
  function relText(d) {
    var s = Math.round((Date.now() - d.getTime()) / 1000);
    if (isNaN(s)) return null;
    if (s < 0) s = 0;
    if (s < 10) return "vừa xong";
    if (s < 60) return s + " giây trước";
    var m = Math.floor(s / 60);
    if (m < 60) return m + " phút trước";
    var h = Math.floor(m / 60);
    if (h < 24) return h + " giờ trước";
    var dd = Math.floor(h / 24);
    if (dd < 7) return dd + " ngày trước";
    return d.toLocaleDateString("vi-VN");
  }
  function refreshTimes(root) {
    var ts = (root || document).querySelectorAll("time[data-rel]");
    for (var i = 0; i < ts.length; i++) {
      var v = ts[i].getAttribute("datetime");
      if (!v) continue;
      var t = relText(new Date(v));
      if (t) ts[i].textContent = t;
    }
  }
  refreshTimes();
  setInterval(refreshTimes, 30000);
  document.body.addEventListener("htmx:afterSwap", function (e) { refreshTimes(e.target); });

  // ------------------------------------------------------------ menu "⋯" + sheet "Thêm": đóng khi bấm ra ngoài / Esc
  document.addEventListener("click", function (e) {
    var open = document.querySelectorAll("details.menu[open], details.tab-more[open]");
    for (var i = 0; i < open.length; i++) if (!open[i].contains(e.target)) open[i].removeAttribute("open");
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    document.documentElement.classList.remove("mgr-info-open");
    var open = document.querySelectorAll("details.menu[open], details.tab-more[open]");
    for (var i = 0; i < open.length; i++) open[i].removeAttribute("open");
  });

  // ------------------------------------------------------------ thanh bên thu gọn (kiểu claude.ai) — nhớ theo người xem (localStorage)
  var root = document.documentElement;
  var sideBtn = document.getElementById("side-toggle");
  if (sideBtn) sideBtn.addEventListener("click", function () {
    var on = root.classList.toggle("side-min");
    try { localStorage.setItem("cfh_side", on ? "min" : ""); } catch (_) {}
  });
  // ------------------------------------------------------------ nút chuyển 2D/3D (`cfh_view`, docs/dev-notes/office-3d.md)
  // Không JS → nút là link thường tới /office. Có JS: bật → nhớ `cfh_view=3d` + đi /office; tắt → xoá + đang ở /office thì về /.
  var VIEW = "cfh_view";
  function view3d() {
    try { return localStorage.getItem(VIEW) === "3d"; } catch (_) { return false; }
  }
  var onOffice = location.pathname === "/office";
  var viewBtns = document.querySelectorAll("[data-view-toggle]");
  function paintView(on) {
    root.classList.toggle("view-3d", on);
    for (var vi = 0; vi < viewBtns.length; vi++) {
      var vb = viewBtns[vi];
      vb.setAttribute("aria-pressed", on ? "true" : "false");
      vb.setAttribute("title", on ? "Về giao diện 2D" : "Chế độ 3D văn phòng");
      vb.setAttribute("href", on ? "/" : "/office");
      var lb = vb.querySelector(".view-lbl");
      if (lb) lb.textContent = on ? "2D" : "3D";
    }
  }
  if (viewBtns.length) paintView(view3d() || onOffice);
  for (var vj = 0; vj < viewBtns.length; vj++) viewBtns[vj].addEventListener("click", function (e) {
    e.preventDefault();
    var on = this.getAttribute("aria-pressed") === "true";
    try { if (on) localStorage.removeItem(VIEW); else localStorage.setItem(VIEW, "3d"); } catch (_) {}
    if (!on) { location.href = "/office"; return; }
    if (onOffice) { location.href = "/"; return; }
    paintView(false);
  });
  // khung "Thông tin điều phối viên": cột phải trên màn rộng (nhớ đóng/mở), ngăn kéo trên màn hẹp
  var wide = window.matchMedia ? window.matchMedia("(min-width: 1280px)") : null;
  function mgrInfo(open) {
    if (wide && wide.matches) {
      var closed = open === undefined ? !root.classList.contains("mgr-info-closed") : !open;
      root.classList.toggle("mgr-info-closed", closed);
      try { localStorage.setItem("cfh_mgr_info", closed ? "closed" : ""); } catch (_) {}
    } else {
      root.classList.toggle("mgr-info-open", open === undefined ? !root.classList.contains("mgr-info-open") : open);
    }
  }
  // uỷ quyền ở document: khung chat được thay khi đổi tab (không tải lại trang) nên không gắn trực tiếp vào nút
  document.addEventListener("click", function (e) {
    var t = e.target && e.target.closest ? e.target : null;
    if (t && t.closest("#mgr-info-toggle")) { mgrInfo(); return; }
    if (t && t.closest("#mgr-info-close")) { mgrInfo(false); return; }
    if (!root.classList.contains("mgr-info-open")) return;
    var side = document.getElementById("mgr-side");
    if (side && !side.contains(e.target)) root.classList.remove("mgr-info-open");
  });

  // ------------------------------------------------------------ ô nhập tự giãn (chat, steer)
  function autoGrow(t) { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight + 2, 240) + "px"; }
  document.addEventListener("input", function (e) {
    var t = e.target;
    if (t && t.matches && t.matches(".mgr-form textarea, .composer textarea")) autoGrow(t);
  });

  // ------------------------------------------------------------ khoá phiên ngoài cookie (WT-1)
  // Server trao khoá MỘT lần qua fragment `#k=…` lúc đăng nhập (fragment không gửi lên server). Giữ trong localStorage của origin
  // web (origin khác — kể cả cổng khác cùng host — không đọc được) và gắn vào mọi form POST. HTML không bao giờ chứa khoá.
  var KEY = "ahub_k";
  function getKey() { try { return localStorage.getItem(KEY) || ""; } catch (_) { return ""; } }
  (function takeKey() {
    var m = /^#k=([A-Za-z0-9_\-]+)$/.exec(location.hash || "");
    if (!m) return;
    try { localStorage.setItem(KEY, m[1]); } catch (_) {}
    history.replaceState(null, "", location.pathname + location.search);
  })();
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || (form.method || "").toLowerCase() !== "post") return;
    var action = form.getAttribute("action") || "";
    if (action === "/login") return;
    if (action === "/logout") { try { localStorage.removeItem(KEY); localStorage.removeItem("cfh_view"); } catch (_) {} return; }
    var inp = form.querySelector('input[name="k"]');
    if (!inp) { inp = document.createElement("input"); inp.type = "hidden"; inp.name = "k"; form.appendChild(inp); }
    inp.value = getKey();
  }, true);

  // ------------------------------------------------------------ hộp xác nhận
  var dlg = document.getElementById("confirm-dlg");
  function openConfirm(form) {
    if (!dlg || typeof dlg.showModal !== "function") return false;
    var f = document.getElementById("confirm-form");
    f.setAttribute("action", form.getAttribute("action"));
    document.getElementById("confirm-msg").textContent = form.getAttribute("data-confirm");
    var box = document.getElementById("confirm-fields");
    box.innerHTML = "";
    var inputs = form.querySelectorAll("input[name], select[name], textarea[name]");
    for (var i = 0; i < inputs.length; i++) {
      var el = inputs[i];
      if ((el.type === "checkbox" || el.type === "radio") && !el.checked) continue;
      if (el.tagName === "SELECT" && el.multiple) {
        for (var j = 0; j < el.options.length; j++) if (el.options[j].selected) addHidden(box, el.name, el.options[j].value);
        continue;
      }
      addHidden(box, el.name, el.value);
    }
    addHidden(box, "confirm", "yes");
    var noteLabel = form.getAttribute("data-note");
    var wrap = document.getElementById("confirm-note-wrap");
    var note = document.getElementById("confirm-note");
    note.value = "";
    if (noteLabel) {
      wrap.hidden = false;
      document.getElementById("confirm-note-label").textContent = noteLabel;
      note.setAttribute("name", "note");
    } else {
      wrap.hidden = true;
      note.removeAttribute("name");
    }
    var ok = document.getElementById("confirm-ok");
    var danger = form.querySelector("button.danger");
    ok.className = "btn " + (danger ? "danger" : "primary");
    dlg.showModal();
    (noteLabel ? note : ok).focus();
    return true;
  }
  function addHidden(box, name, value) {
    var h = document.createElement("input");
    h.type = "hidden"; h.name = name; h.value = value;
    box.appendChild(h);
  }
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-confirm")) return;
    if (openConfirm(form)) e.preventDefault();
  }, true);
  var cancel = document.getElementById("confirm-cancel");
  if (cancel) cancel.addEventListener("click", function () { dlg.close(); });

  // chống bấm hai lần
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || !(form instanceof HTMLFormElement) || (form.method || "").toLowerCase() !== "post") return;
    var b = form.querySelector("button[type=submit]");
    if (b) setTimeout(function () { b.disabled = true; }, 0);
  });

  window.addEventListener("pageshow", function () {
    var bs = document.querySelectorAll("button[disabled]");
    for (var i = 0; i < bs.length; i++) bs[i].disabled = false;
  });

  // ------------------------------------------------------------ live run
  function nearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 120; }
  function fromHTML(html) {
    var t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }

  var tr = document.querySelector("[data-sse-run]");
  if (tr && window.EventSource) {
    var es = new EventSource(tr.getAttribute("data-sse-run"));
    var cur = null, curKind = null;
    var state = document.getElementById("live-state");
    var empty = tr.querySelector(".empty");
    tr.scrollTop = tr.scrollHeight;
    window.addEventListener("load", function () { tr.scrollTop = tr.scrollHeight; });
    function scrollMaybe(stick) { if (stick) tr.scrollTop = tr.scrollHeight; }
    function endBlock() { cur = null; curKind = null; }
    es.onmessage = function (ev) {
      var m;
      try { m = JSON.parse(ev.data); } catch (_) { return; }
      var stick = nearBottom(tr);
      if (empty) { empty.remove(); empty = null; }
      if (m.t === "delta") {
        if (curKind !== m.k) {
          if (m.k === "text") {
            cur = fromHTML('<div class="ti msg streaming"><div class="md"></div></div>');
            tr.appendChild(cur);
          } else {
            cur = fromHTML('<details class="ti reasoning"><summary>Suy nghĩ…</summary><pre></pre></details>');
            tr.appendChild(cur);
          }
          curKind = m.k;
        }
        var tgt = m.k === "text" ? cur.querySelector(".md") : cur.querySelector("pre");
        tgt.textContent += m.text;
        scrollMaybe(stick);
      } else if (m.t === "item") {
        var node = fromHTML(m.html);
        if (!node) return;
        if ((m.kind === "msg" && curKind === "text" || m.kind === "reasoning" && curKind === "reasoning") && cur) {
          cur.replaceWith(node); endBlock();
        } else if (m.dom && document.getElementById(m.dom)) {
          document.getElementById(m.dom).replaceWith(node);
          if (curKind) endBlock();
        } else {
          if (curKind) endBlock();
          tr.appendChild(node);
        }
        scrollMaybe(stick);
      } else if (m.t === "stats") {
        var c = document.getElementById("st-cost"), k = document.getElementById("st-tok");
        if (c) c.textContent = m.cost;
        if (k) k.textContent = m.tokens;
      } else if (m.t === "refresh") {
        var bar = document.getElementById("runbar");
        if (bar && window.htmx && bar.getAttribute("hx-get")) window.htmx.ajax("GET", bar.getAttribute("hx-get"), { target: bar, swap: "innerHTML" });
      } else if (m.t === "end") {
        es.close();
        if (state) { state.textContent = "đã kết thúc"; state.className = "pill muted"; }
        var box = document.querySelector("#steer-form textarea");
        if (!box || !box.value.trim()) setTimeout(function () { location.reload(); }, 800);
      }
    };
    es.onerror = function () { if (state) { state.textContent = "đang nối lại…"; state.className = "pill warn"; } };
    es.onopen = function () { if (state) { state.textContent = "trực tiếp"; state.className = "pill ok live-dot"; } };
  }

  // ------------------------------------------------------------ firehose tổng quan
  var fh = document.querySelector("[data-sse-firehose]");
  if (fh && window.EventSource) {
    var st = document.getElementById("fh-state");
    var src = new EventSource(fh.getAttribute("data-sse-firehose"));
    src.onopen = function () { if (st) { st.textContent = "trực tiếp"; st.className = "pill ok"; } };
    src.onerror = function () { if (st) { st.textContent = "đang nối lại…"; st.className = "pill warn"; } };
    src.onmessage = function (ev) {
      var m;
      try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m.t !== "line") return;
      var li = fromHTML(m.html);
      if (!li) return;
      var fe = document.getElementById("fh-empty"); if (fe) fe.remove();
      fh.insertBefore(li, fh.firstChild);
      refreshTimes(li);
      while (fh.children.length > 120) fh.removeChild(fh.lastChild);
    };
  }
  // ------------------------------------------------------------ Công việc realtime (SSE /sse/tasks → tải lại partial)
  // Server chỉ gửi {"t":"refresh"}; ở đây gom loạt (400 ms), tải /partials/... cùng query lọc, thay đúng khung (outerHTML).
  // SSE chết (daemon tắt, mạng) → tự nối lại có giãn; trong lúc chưa nối được thì hỏi lại mỗi 15 s (chỉ khi SSE KHÔNG mở → không tải đôi).
  var tb = document.querySelector("[data-sse-tasks]");
  if (tb && window.EventSource && window.htmx) (function () {
    var boxId = tb.id, sseUrl = tb.getAttribute("data-sse-tasks"), partUrl = tb.getAttribute("data-refresh") + location.search;
    var timer = null, busy = false, again = false, es = null, live = false, poll = null, backoff = 2000, everOpen = false;
    function box() { return document.getElementById(boxId); }
    function snap(el) {  // id → "trạng thái|chữ" để biết thẻ nào vừa đổi
      var m = {}, cs = el ? el.querySelectorAll("[data-id]") : [];
      for (var i = 0; i < cs.length; i++) m[cs[i].getAttribute("data-id")] = (cs[i].getAttribute("data-status") || "") + "|" + cs[i].textContent;
      return m;
    }
    function editing(el) {  // người đang thao tác trong khung (gõ câu trả lời, hộp xác nhận mở) → hoãn, không làm mất chữ
      if (document.querySelector("dialog[open]")) return true;
      var a = document.activeElement;
      if (a && el.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return true;
      var fs = el.querySelectorAll("textarea, input[type=text], input:not([type])");
      for (var i = 0; i < fs.length; i++) if (fs[i].value !== fs[i].defaultValue) return true;
      return false;
    }
    function refresh() {
      timer = null;
      var el = box();
      if (!el) return;
      if (busy || editing(el)) { again = true; if (!busy) schedule(3000); return; }
      busy = true; again = false;
      var before = snap(el), focusId = null, a = document.activeElement;
      if (a && el.contains(a)) { var c = a.closest("[data-id]"); focusId = c && c.getAttribute("data-id"); }
      window.htmx.ajax("GET", partUrl, { target: el, swap: "outerHTML" }).then(function () {
        var now = box();
        if (!now) return;
        refreshTimes(now);
        var after = now.querySelectorAll("[data-id]");
        for (var i = 0; i < after.length; i++) {
          var id = after[i].getAttribute("data-id"), sig = (after[i].getAttribute("data-status") || "") + "|" + after[i].textContent;
          if (before[id] !== sig && after[i].classList.contains("tcard")) {
            after[i].classList.add("tcard-changed");
            setTimeout((function (n) { return function () { n.classList.remove("tcard-changed"); }; })(after[i]), 2500);
          }
          if (focusId && id === focusId) after[i].focus({ preventScroll: true });
        }
      }, function () {}).then(function () { busy = false; if (again) schedule(400); });
    }
    function schedule(ms) { if (!timer) timer = setTimeout(refresh, ms); }
    function startPoll() { if (!poll) poll = setInterval(function () { if (!live) schedule(0); }, 15000); }
    function connect() {
      es = new EventSource(sseUrl);
      es.onopen = function () {
        live = true; backoff = 2000;
        if (everOpen) schedule(0);  // nối lại: server không phát lại (since = seq hiện tại) → tải lại một lần cho chắc
        everOpen = true;
      };
      es.onmessage = function (ev) {
        var m;
        try { m = JSON.parse(ev.data); } catch (_) { return; }
        if (m.t === "refresh") schedule(400);
        else if (m.t === "end") { es.close(); live = false; startPoll(); setTimeout(connect, backoff); }
      };
      es.onerror = function () {
        live = false; startPoll();
        if (es.readyState === 2) { backoff = Math.min(backoff * 2, 60000); setTimeout(connect, backoff); }  // CLOSED (401/5xx): trình duyệt bỏ cuộc → tự nối lại
      };
    }
    connect();
    window.addEventListener("pagehide", function () { if (es) es.close(); });
  })();
  // ------------------------------------------------------------ Điều phối (trò chuyện với điều phối viên)
  // Mục HTML do server vẽ sẵn (autoescape) — chữ chạy (delta) chỉ gán bằng textContent. Không bao giờ dựng HTML từ chữ của agent.
  // Đổi tab luồng / hội thoại KHÔNG tải lại cả trang: tải /manager?… bằng fetch, thay #mgr-grid + danh sách hội thoại ở thanh bên,
  // pushState; luồng SSE + ô nhập khởi động lại qua mgrBoot(). Lỗi/không phải trang điều phối → điều hướng thường (an toàn).
  var mgrStop = null, mgrNav = null, mgrDrafts = {};
  function mgrBoot() {
    if (mgrStop) { mgrStop(); mgrStop = null; }
    var el = document.querySelector("[data-sse-manager]");
    if (el) mgrStop = initManager(el);
  }
  function draftKey() {
    var f = document.getElementById("mgr-form");
    if (!f) return "";
    var c = f.querySelector('input[name="c"]'), w = f.querySelector('input[name="w"]');
    return (c && c.value) ? "c:" + c.value : "w:" + (w ? w.value : "");
  }
  function mgrUrlOk(a) {
    if (!a || a.target || a.hasAttribute("download")) return null;
    var u;
    try { u = new URL(a.getAttribute("href"), location.href); } catch (_) { return null; }
    return u.origin === location.origin && u.pathname === "/manager" ? u.pathname + u.search : null;
  }
  function mgrGo(url, push, a) {
    var grid = document.getElementById("mgr-grid");
    if (!grid || !window.fetch || !window.DOMParser) { location.href = url; return; }
    var tx = document.getElementById("mgr-text");
    if (tx) { var dk = draftKey(); if (tx.value.trim()) mgrDrafts[dk] = tx.value; else delete mgrDrafts[dk]; }
    if (mgrNav && mgrNav.abort) mgrNav.abort();
    var ctl = window.AbortController ? new AbortController() : {};
    mgrNav = ctl;
    grid.classList.add("mgr-loading");
    if (a && a.classList.contains("ws-tab")) {  // đánh dấu tab ngay, không chờ server
      var tabs = document.querySelectorAll("#ws-tabs .ws-tab");
      for (var i = 0; i < tabs.length; i++) tabs[i].classList.toggle("on", tabs[i] === a);
    }
    var open = document.querySelectorAll("details[open]");
    for (var j = 0; j < open.length; j++) if (!open[j].closest(".mgr-side")) open[j].removeAttribute("open");
    root.classList.remove("mgr-info-open");
    fetch(url, { credentials: "same-origin", signal: ctl.signal })
      .then(function (r) {
        if (!r.ok || new URL(r.url, location.href).pathname !== "/manager") throw new Error("nav");
        return r.text();
      })
      .then(function (html) {
        if (mgrNav !== ctl) return;
        mgrNav = null;
        var doc = new DOMParser().parseFromString(html, "text/html");
        var ng = doc.getElementById("mgr-grid"), cur = document.getElementById("mgr-grid");
        if (!ng || !cur) throw new Error("nav");
        if (push) history.pushState({ mgr: 1 }, "", url);
        document.title = doc.title;
        cur.replaceWith(document.importNode(ng, true));
        var os = document.querySelector(".side-recents"), ns = doc.querySelector(".side-recents");
        if (os && ns) os.replaceWith(document.importNode(ns, true));
        var ot = document.getElementById("toasts"), nt = doc.getElementById("toasts");
        if (ot && nt) for (var k = 0; k < nt.children.length; k++) ot.appendChild(document.importNode(nt.children[k], true));
        var g = document.getElementById("mgr-grid");
        if (window.htmx) window.htmx.process(g);
        refreshTimes(g);
        var on = document.querySelector("#ws-tabs .ws-tab.on");
        if (on && on.scrollIntoView) on.scrollIntoView({ block: "nearest", inline: "nearest" });
        mgrBoot();
        var t2 = document.getElementById("mgr-text"), d = mgrDrafts[draftKey()];
        if (t2 && d) { t2.value = d; autoGrow(t2); }
        if (t2 && window.matchMedia && window.matchMedia("(pointer: fine)").matches) t2.focus({ preventScroll: true });
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        location.href = url;
      });
  }
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (!document.getElementById("mgr-grid")) return;
    var a = e.target.closest && e.target.closest("a[href]");
    var url = mgrUrlOk(a);
    if (!url) return;
    e.preventDefault();
    mgrGo(url, true, a);
  });
  if (document.getElementById("mgr-grid")) {
    try { history.replaceState({ mgr: 1 }, "", location.pathname + location.search); } catch (_) {}
    window.addEventListener("popstate", function () {
      if (location.pathname === "/manager" && document.getElementById("mgr-grid")) mgrGo(location.pathname + location.search, false);
    });
  }

  function initManager(ml) {
    var mes = null, tabsT = null, tabsPoll = null;
    var mform = document.getElementById("mgr-form");
    var mtext = document.getElementById("mgr-text");
    var merr = document.getElementById("mgr-err");
    var mlive = document.getElementById("mgr-live");
    var conv = ml.getAttribute("data-conv") || "";
    var runs = {};
    (ml.getAttribute("data-runs") || "").split(" ").forEach(function (r) { if (r) runs[r] = true; });
    var pend = {};       // run → phần tử "đang trả lời"
    var early = {};      // run → [tin SSE tới trước khi fetch trả run id]
    var replied = {};
    var p0 = ml.querySelector("[data-pending]");
    if (p0) pend[p0.getAttribute("data-pending")] = p0;
    ml.scrollTop = ml.scrollHeight;
    window.addEventListener("load", function () { ml.scrollTop = ml.scrollHeight; });
    setTimeout(function () { ml.scrollTop = ml.scrollHeight; }, 300);
    function dropEmpty() { var e = document.getElementById("mgr-empty"); if (e) e.remove(); }
    function stick() { return nearBottom(ml); }
    function put(node, atBottom) { if (!node) return; dropEmpty(); ml.appendChild(node); if (atBottom) ml.scrollTop = ml.scrollHeight; }
    function handle(m) {
      var r = m.run, s = stick();
      if (m.t === "tick" || m.t === "status" || m.t === "run_end" || m.t === "reply") tabsSoon();
      if (m.t === "status") return;
      if (m.t === "tick") { document.body.dispatchEvent(new Event("mgr-tick")); return; }
      if (m.t === "question") {
        if (!conv || m.conv !== conv) return;  // câu hỏi của cuộc trò chuyện khác
        var ex = document.getElementById(m.dom);
        if (m.answered) {
          if (!ex) return;
          var rm = ex.querySelectorAll(".q-form, .q-quick, .xcard-actions, .q-opts, .q-free");
          for (var k = 0; k < rm.length; k++) rm[k].remove();
          ex.classList.add("done");
          var ans = fromHTML(m.html); if (ans) ex.appendChild(ans);
          return;
        }
        var qn = fromHTML(m.html);
        if (!qn) return;
        if (ex) ex.replaceWith(qn); else put(qn, s);
        refreshTimes(qn);
        return;
      }
      if (m.t === "action") {
        var side = document.getElementById("mgr-actions");
        if (side && m.side && !m.sideDone) {
          m.sideDone = true;
          var e0 = document.getElementById("mgr-actions-empty"); if (e0) e0.remove();
          var li = fromHTML(m.side); if (li) side.insertBefore(li, side.firstChild);
        }
      }
      var mine = m.t === "reply" && m.conv && conv && m.conv === conv;  // tin của đúng cuộc trò chuyện này (vd notify từ lượt nhịp)
      if (r && !runs[r] && !mine) { (early[r] = early[r] || []).push(m); if (early[r].length > 400) early[r].shift(); return; }
      if (m.t === "delta") {
        var p = pend[r];
        if (p) { p.querySelector(".mm-text").textContent += m.text; if (s) ml.scrollTop = ml.scrollHeight; }
      } else if (m.t === "reply" && m.notify) {
        var nn = fromHTML(m.html);  // báo chủ động giữa lượt: chèn trước khung "đang trả lời", không thay nó
        if (pend[r]) { dropEmpty(); ml.insertBefore(nn, pend[r]); } else put(nn, s);
        if (s) ml.scrollTop = ml.scrollHeight;
      } else if (m.t === "reply") {
        replied[r] = true;
        var node = fromHTML(m.html);
        if (pend[r]) { pend[r].replaceWith(node); delete pend[r]; if (s) ml.scrollTop = ml.scrollHeight; }
        else put(node, s);
      } else if (m.t === "action") {
        // gom vào khối "Đã làm N việc" của cùng lượt nếu khối đó là mục cuối (trước khung "đang trả lời")
        var anchor = pend[r] || null;
        var last = anchor ? anchor.previousElementSibling : ml.lastElementChild;
        if (last && last.classList.contains("mm-acts") && last.getAttribute("data-run") === r) {
          var one = fromHTML(m.one);
          if (one) last.appendChild(one);
          var n = last.querySelector(".n"); if (n) n.textContent = String(last.querySelectorAll(".mm-act").length);
          if (!m.ok) last.classList.add("err");
        } else {
          var a = fromHTML(m.html);
          if (anchor) { dropEmpty(); ml.insertBefore(a, anchor); } else put(a, s);
        }
        if (s) ml.scrollTop = ml.scrollHeight;
      } else if (m.t === "run_end") {
        setTimeout(function () {
          if (replied[r]) return;
          var n = fromHTML(m.html);
          if (pend[r]) {
            if (m.ok) { pend[r].querySelector(".mm-who").textContent = "Điều phối viên · đã xong — tải lại trang nếu chưa thấy trả lời"; }
            else { pend[r].replaceWith(n); delete pend[r]; }
          } else if (!m.ok) put(n, stick());
        }, 2500);
      }
    }
    function track(r) {
      runs[r] = true;
      var q = early[r] || []; delete early[r];
      for (var i = 0; i < q.length; i++) handle(q[i]);
    }
    // hàng tab luồng: chấm "đang chạy / cần bạn / đã xong" — làm mới khi SSE báo run bắt đầu/kết thúc, và hỏi lại mỗi 20 s
    // (task của agent khác không đi qua luồng SSE của điều phối viên)
    function tabsSoon(ms) { if (!tabsT) tabsT = setTimeout(tabsLoad, ms == null ? 500 : ms); }
    function tabsLoad() {
      tabsT = null;
      var el = document.getElementById("ws-tabs");
      if (!el || !window.fetch) return;
      fetch("/partials/manager/tabs?w=" + encodeURIComponent(el.getAttribute("data-w") || "") + (conv ? "&c=" + encodeURIComponent(conv) : ""), { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (html) {
          var cur = document.getElementById("ws-tabs"), n = html && fromHTML(html);
          if (!cur || !n || n.id !== "ws-tabs" || mgrNav) return;  // đang chuyển tab → trang mới tự mang hàng tab mới
          var x = cur.scrollLeft;
          cur.replaceWith(n);
          n.scrollLeft = x;
        }, function () {});
    }
    tabsPoll = setInterval(function () { if (!document.hidden) tabsSoon(0); }, 20000);
    if (window.EventSource) {
      mes = new EventSource(ml.getAttribute("data-sse-manager"));
      mes.onopen = function () { if (mlive) { mlive.textContent = "● trực tiếp"; mlive.className = "pill ok live-dot"; } };
      mes.onerror = function () { if (mlive) { mlive.textContent = "↻ đang nối lại…"; mlive.className = "pill warn"; } };
      mes.onmessage = function (ev) { var m; try { m = JSON.parse(ev.data); } catch (_) { return; } if (m.t === "end") { mes.close(); return; } handle(m); };
    }
    if (mform && window.fetch && window.FormData) {
      // ---- đính kèm: nút 📎 (input file), kéo–thả vào ô nhập, dán ảnh. Giới hạn = của daemon; server kiểm lại theo nội dung.
      var IMG_TYPES = { "image/png": 1, "image/jpeg": 1, "image/gif": 1, "image/webp": 1 };
      var MAX_IMG = 4, MAX_DOC = 4, IMG_BYTES = 5 * 1024 * 1024, DOC_BYTES = 10 * 1024 * 1024;
      var picked = [];
      var finput = document.getElementById("mgr-file");
      var flist = document.getElementById("mgr-files");
      function isImg(f) { return !!IMG_TYPES[(f.type || "").toLowerCase()]; }
      function human(n) { return n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB"; }
      function showErr(msg) { if (merr) { merr.textContent = msg; merr.hidden = !msg; } }
      function drawFiles() {
        if (!flist) return;
        flist.textContent = "";
        flist.hidden = !picked.length;
        picked.forEach(function (f, i) {
          var li = document.createElement("li");
          if (isImg(f) && window.FileReader) {
            var im = document.createElement("img"); im.alt = "";
            var rd = new FileReader(); rd.onload = function () { im.src = rd.result; }; rd.readAsDataURL(f);
            li.appendChild(im);
          } else {
            var ic = document.createElement("span"); ic.className = "fi-ic";
            var dot = f.name.lastIndexOf("."); ic.textContent = dot > 0 ? f.name.slice(dot + 1, dot + 5) : "txt";
            li.appendChild(ic);
          }
          var nm = document.createElement("span"); nm.className = "fi-name"; nm.textContent = f.name; nm.title = f.name; li.appendChild(nm);
          var sz = document.createElement("span"); sz.className = "fi-size"; sz.textContent = human(f.size); li.appendChild(sz);
          var x = document.createElement("button"); x.type = "button"; x.textContent = "×"; x.title = "Bỏ " + f.name; x.setAttribute("aria-label", "Bỏ " + f.name);
          x.addEventListener("click", function () { picked.splice(i, 1); drawFiles(); });
          li.appendChild(x);
          flist.appendChild(li);
        });
      }
      function addFiles(list) {
        var errs = [];
        for (var i = 0; i < list.length; i++) {
          var f = list[i];
          if (!f || !f.size) { if (f) errs.push(f.name + ": tệp rỗng"); continue; }
          var img = isImg(f);
          var n = picked.filter(function (p) { return isImg(p) === img; }).length;
          if (img && f.size > IMG_BYTES) { errs.push(f.name + ": ảnh quá lớn (tối đa 5 MB)"); continue; }
          if (!img && f.size > DOC_BYTES) { errs.push(f.name + ": tệp quá lớn (tối đa 10 MB)"); continue; }
          if (n >= (img ? MAX_IMG : MAX_DOC)) { errs.push("tối đa " + (img ? MAX_IMG + " ảnh" : MAX_DOC + " tệp") + " mỗi tin — bỏ " + f.name); continue; }
          picked.push(f);
        }
        drawFiles();
        showErr(errs.join("; "));
      }
      if (finput) finput.addEventListener("change", function () { addFiles(finput.files); finput.value = ""; });
      var attachBtn = document.getElementById("mgr-attach");
      if (attachBtn && finput) attachBtn.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); finput.click(); }
      });
      var dropT = 0;
      mform.addEventListener("dragover", function (e) {
        if (!e.dataTransfer || Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") < 0) return;
        e.preventDefault(); mform.classList.add("drop"); clearTimeout(dropT);
        dropT = setTimeout(function () { mform.classList.remove("drop"); }, 150);
      });
      mform.addEventListener("drop", function (e) {
        if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
        e.preventDefault(); mform.classList.remove("drop"); addFiles(e.dataTransfer.files);
      });
      if (mtext) mtext.addEventListener("paste", function (e) {
        var fs = e.clipboardData && e.clipboardData.files;
        if (!fs || !fs.length) return;  // dán chữ: để trình duyệt tự xử lý
        e.preventDefault();
        var out = [];
        for (var i = 0; i < fs.length; i++) {
          var f = fs[i];
          // ảnh chụp màn hình dán vào thường tên "image.png" → đặt tên dễ phân biệt
          if (isImg(f) && /^image\.\w+$/.test(f.name) && window.File) {
            try { f = new File([f], "anh-dan-" + (picked.length + out.length + 1) + "." + f.name.split(".").pop(), { type: f.type }); } catch (_) {}
          }
          out.push(f);
        }
        addFiles(out);
      });
      if (mtext) mtext.addEventListener("keydown", function (e) {
        // Enter gửi; Shift+Enter xuống dòng; không gửi khi đang gõ dấu (IME)
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); if (mform.requestSubmit) mform.requestSubmit(); else mform.submit(); }
      });
      var sug = document.getElementById("mgr-suggest");
      if (sug && mtext) sug.addEventListener("click", function (e) {
        var b = e.target.closest("button[data-fill]");
        if (!b) return;
        mtext.value = b.getAttribute("data-fill"); mtext.focus(); autoGrow(mtext);
        try { mtext.setSelectionRange(mtext.value.length, mtext.value.length); } catch (_) {}
      });
      mform.addEventListener("submit", function (e) {
        e.preventDefault();  // khoá phiên `k` đã được gắn ở pha capture (trên)
        if (!mtext.value.trim() && !picked.length) return;
        var btn = mform.querySelector("button[type=submit]");
        if (btn) btn.disabled = true;
        if (merr) merr.hidden = true;
        var fd = new FormData(mform);
        fd.delete("attach");
        picked.forEach(function (f) { fd.append("attach", f, f.name); });
        fetch(mform.getAttribute("action"), { method: "POST", body: fd, credentials: "same-origin", headers: { "X-Ahub-Fetch": "1" } })
          .then(function (resp) { return resp.json().catch(function () { return { error: "Lỗi " + resp.status }; }); })
          .then(function (j) {
            if (j.error) { if (merr) { merr.textContent = j.error; merr.hidden = false; } return; }
            if (j.conversation_id && j.conversation_id !== conv) {
              conv = j.conversation_id;
              var ci = mform.querySelector('input[name="c"]'); if (ci) ci.value = conv;
              try { history.replaceState(null, "", "/manager?c=" + encodeURIComponent(conv)); } catch (_) {}
            }
            put(fromHTML(j.html_user), true);
            if (j.run_id && pend[j.run_id]) { track(j.run_id); }  // tin được steer vào lượt đang chạy
            else if (j.run_id) {
              var pn = fromHTML(j.html_pending);
              if (pn) { put(pn, true); pend[j.run_id] = pn; }
              track(j.run_id);
            }
            mtext.value = ""; autoGrow(mtext);
            picked = []; drawFiles();
          })
          .catch(function () { if (merr) { merr.textContent = "Không gửi được — kiểm tra kết nối rồi thử lại."; merr.hidden = false; } })
          .then(function () { if (btn) btn.disabled = false; mtext.focus(); });
      });
    }
    return function stop() {
      if (mes) mes.close();
      clearInterval(tabsPoll);
      clearTimeout(tabsT);
    };
  }
  mgrBoot();
})();
