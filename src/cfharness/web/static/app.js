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
  document.body.addEventListener("htmx:load", function (e) { refreshTimes(e.target); });  // phần tử mới (vd "Hiện các mục trước", swap outerHTML)

  // P2 mobile: bàn phím ảo — trên iOS (và Chrome Android mặc định) bàn phím chỉ thu nhỏ VISUAL viewport, 100dvh không đổi → ô nhập bị che.
  // Trang chat/run: đặt --vvh = chiều cao visual viewport + lớp `kb-open` (CSS cho .main cao đúng phần nhìn thấy, ẩn thanh tab đáy).
  // Chỉ CSSOM (style.setProperty) — không có thuộc tính style= trong HTML (CSP).
  if (window.visualViewport && (document.body.classList.contains("chat-page") || document.body.classList.contains("run-page"))) (function () {
    var vv = window.visualViewport, root = document.documentElement, raf = 0;
    function fit() {
      raf = 0;
      var kb = window.innerHeight - vv.height > 120;
      root.classList.toggle("kb-open", kb);
      if (kb) { root.style.setProperty("--vvh", Math.round(vv.height) + "px"); if (window.scrollY || vv.offsetTop) window.scrollTo(0, 0); }
      else root.style.removeProperty("--vvh");
    }
    function soon() { if (!raf) raf = requestAnimationFrame(fit); }
    vv.addEventListener("resize", soon);
    vv.addEventListener("scroll", soon);
    fit();
  })();

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
  // trần = max-height CSS của chính ô (vd .mgr-form textarea 15rem) — một nguồn duy nhất, không lệch số giữa JS và CSS
  function autoGrow(t) {
    var cap = parseFloat(window.getComputedStyle ? getComputedStyle(t).maxHeight : "") || 240;
    t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight + 2, cap) + "px";
  }
  document.addEventListener("input", function (e) {
    var t = e.target;
    if (t && t.matches && t.matches(".mgr-form textarea, .composer textarea")) autoGrow(t);
  });

  // ------------------------------------------------------------ khoá phiên ngoài cookie (WT-1)
  // Server trao khoá MỘT lần qua fragment `#k=…` lúc đăng nhập (fragment không gửi lên server). Giữ trong localStorage của origin
  // web (origin khác — kể cả cổng khác cùng host — không đọc được) và gắn vào mọi form POST. HTML không bao giờ chứa khoá.
  var KEY = "ahub_k";
  function getKey() { try { return localStorage.getItem(KEY) || ""; } catch (_) { return ""; } }
  // `&d=1`: phiên gắn thiết bị tin cậy (cookie `__Host-…_dev` HttpOnly nên JS không tự biết) → nhớ để trang /login tự tiếp tục lần sau
  var DEV = "cfh_dev", DEV_PAUSE = "cfh_dev_pause";
  (function takeKey() {
    var m = /^#k=([A-Za-z0-9_\-]+)(&d=1)?$/.exec(location.hash || "");
    if (!m) return;
    try { localStorage.setItem(KEY, m[1]); if (m[2]) localStorage.setItem(DEV, "1"); sessionStorage.removeItem(DEV_PAUSE); } catch (_) {}
    history.replaceState(null, "", location.pathname + location.search);
  })();
  // ------------------------------------------------------------ "tiếp tục bằng thiết bị tin cậy" (trang /login, chỉ host public)
  // SameSite=Strict: mở link từ Telegram/Slack không mang cookie → tự POST một lần (POST cùng nguồn thì mang). Vừa đăng xuất trong tab
  // này → không tự vào lại (chỉ hiện nút). Server trả `data-failed` → quên dấu "đã tin cậy".
  (function deviceLogin() {
    var f = document.getElementById("device-login");
    if (!f) return;
    var trusted = false, paused = false;
    try {
      if (f.getAttribute("data-failed") === "1") localStorage.removeItem(DEV);
      trusted = localStorage.getItem(DEV) === "1";
      paused = sessionStorage.getItem(DEV_PAUSE) === "1";
    } catch (_) {}
    if (!trusted) return;
    f.hidden = false;
    if (f.getAttribute("data-auto") === "1" && !paused) f.submit();
  })();
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || (form.method || "").toLowerCase() !== "post") return;
    var action = form.getAttribute("action") || "";
    if (action === "/login") return;
    if (action === "/logout") {
      try {
        localStorage.removeItem(KEY); localStorage.removeItem("cfh_view");
        var fg = form.querySelector('input[name="forget"]');
        if (fg && fg.value === "1") localStorage.removeItem(DEV); else sessionStorage.setItem(DEV_PAUSE, "1");
      } catch (_) {}
      return;
    }
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

  // ------------------------------------------------------------ transcript dùng chung (trang run; trang Điều phối sẽ dùng lại)
  function nearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 120; }
  function fromHTML(html) {
    var t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }
  // vùng cuộn của transcript: phần tử tổ tiên gần nhất có [data-tx-scroll] (trang run), không có → chính container (Điều phối)
  function txScroller(c) { return (c.closest && c.closest("[data-tx-scroll]")) || c; }
  function byIdIn(container, id) {
    var el = id ? document.getElementById(id) : null;
    return el && container.contains(el) ? el : null;
  }
  // <details> có `data-auto` (trạng thái mở mặc định do server tính): người CHƯA tự bấm (open == auto) → theo server; đã bấm → giữ.
  function keepOpen(old, node) {
    function one(o, n) {
      if (!o || !n || o.tagName !== "DETAILS" || n.tagName !== "DETAILS") return;
      var a = o.getAttribute("data-auto");
      if (a !== null) { if (o.open !== (a === "1")) n.open = o.open; } else if (o.open) n.open = true;
    }
    one(old, node);
    var ds = node.querySelectorAll ? node.querySelectorAll("details[id]") : [];
    for (var i = 0; i < ds.length; i++) one(document.getElementById(ds[i].id), ds[i]);
  }
  // cập nhật tiêu đề một khung chứa (nhóm tool / tool cha) từ bản rỗng server gửi: class, data-auto, <summary>
  function syncHead(el, sh) {
    var was = el.getAttribute("data-auto"), auto = sh.getAttribute("data-auto");
    if (was !== null && auto !== null && was !== auto && el.open === (was === "1")) el.open = auto === "1";
    if (auto !== null) el.setAttribute("data-auto", auto);
    el.className = sh.className;
    var a = el.querySelector(":scope > summary"), b = sh.querySelector(":scope > summary");
    if (a && b) a.replaceWith(b);
  }
  /* txAppend(container, msg) — áp MỘT message SSE dạng transcript vào `container`, trả phần tử vừa chèn/thay (hoặc null):
   *   {t:"delta", k:"text"|"reasoning", text}  → nối vào khối đang chạy chữ bằng textContent (không bao giờ là HTML);
   *   {t:"part", k:"text", html}               → (P1-1) markdown của cả khối đang chạy chữ do SERVER render (md()) → thay nội dung
   *                                              khối; delta sau đó nối vào đuôi `.tx-tail` (textContent) tới `part` kế;
   *   {t:"item", kind, dom?, html, parent?, heads?} → HTML do SERVER vẽ: thay khối đang chạy chữ cùng loại (msg/reasoning), thay
   *       phần tử cùng id `dom` (giữ trạng thái mở của <details>), không thì nối vào ô con `#<parent>-k` (nhóm tool / tool cha của
   *       subagent) hoặc cuối container. `heads` [{dom, html}] (ngoài → trong): khung chứa — có rồi → cập nhật tiêu đề, chưa có
   *       (nhóm mới) → nối cuối container.
   * Giữ vị trí cuộn: chỉ bám đáy khi người đang ở gần đáy (`container._tx.stuck` cho biết). Trạng thái khối đang chạy chữ: `_tx`. */
  function txAppend(container, m) {
    var st = container._tx || (container._tx = { text: null, think: null, tail: null, stuck: true });
    var sc = txScroller(container);
    var stick = nearBottom(sc), node = null;
    function reset() { st.text = null; st.think = null; st.tail = null; }
    var empty = container.querySelector(".empty");
    if (empty && empty.parentNode === container) empty.remove();
    if (m.t === "delta") {
      var key = m.k === "text" ? "text" : "think";
      // suy nghĩ mới SAU chữ trả lời → khối suy nghĩ mới (khối cũ vẫn chờ `reasoning.complete` thay tại chỗ nếu có)
      if (key === "think" && st.text) reset();
      if (!st[key]) {
        st[key] = fromHTML(key === "text" ? '<div class="ti msg tx-msg streaming"><div class="md"></div></div>'
          : '<details class="ti reasoning tx-thinking live"><summary>Suy nghĩ…</summary><pre></pre></details>');
        if (key === "text") st.tail = null;
        container.appendChild(st[key]);
      }
      node = st[key];
      var box = key === "text" ? (st.tail || node.querySelector(".md")) : node.querySelector("pre");
      box.textContent += m.text || "";
    } else if (m.t === "part" && m.html) {
      if (!st.text) return null;
      var md = st.text.querySelector(".md"), tpl = document.createElement("template");
      tpl.innerHTML = m.html;  // đầu ra md() của server (đã cứng hoá) — như mọi mục `item`
      md.replaceChildren(tpl.content);
      st.tail = document.createElement("span");
      st.tail.className = "tx-tail";
      // đuôi chữ nối tiếp đoạn văn cuối (không tách dòng giữa câu); khối khác (code, bảng, list) → sau khối
      var lastEl = md.lastElementChild;
      (lastEl && lastEl.tagName === "P" ? lastEl : md).appendChild(st.tail);
      st.text.classList.add("parted");
      node = st.text;
    } else if (m.t === "item" && m.html) {
      node = fromHTML(m.html);
      if (!node) return null;
      var heads = m.heads || [];
      for (var i = 0; i < heads.length; i++) {
        var sh = fromHTML(heads[i].html || ""), hel = byIdIn(container, heads[i].dom);
        if (!sh) continue;
        if (hel) syncHead(hel, sh);
        else if (i === 0 && sh.classList.contains("tx-group")) { reset(); container.appendChild(sh); }
      }
      var old = byIdIn(container, m.dom);
      var live = m.kind === "msg" ? st.text : (m.kind === "reasoning" ? st.think : null);
      if (live && live.parentNode && !old) {
        if (live.tagName === "DETAILS" && live.open && node.tagName === "DETAILS") node.open = true;
        live.replaceWith(node);
        if (m.kind === "msg") { st.text = null; st.tail = null; } else st.think = null;
      } else if (old) {
        keepOpen(old, node);
        old.replaceWith(node);
      } else {
        var slot = m.parent ? byIdIn(container, m.parent + "-k") : null;
        if (slot) slot.appendChild(node);
        else { reset(); container.appendChild(node); }
      }
      refreshTimes(node);
    }
    st.stuck = stick;
    if (stick) sc.scrollTop = sc.scrollHeight;
    return node;
  }
  window.cfhTx = { append: txAppend, fromHTML: fromHTML, nearBottom: nearBottom, scroller: txScroller };

  // P1-11: dòng duyệt/câu hỏi trong transcript trỏ tới thẻ (#ap-… / #q-…); thẻ không có trên trang (vd câu hỏi mới mở khi đang xem)
  // → sang trang Cần bạn (`data-alt`, đường dẫn nội bộ do server dựng từ id)
  document.addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest("a.tx-line-a[data-alt]") : null;
    if (!a) return;
    var id = (a.getAttribute("href") || "").slice(1);
    var card = id ? document.getElementById(id) : null;
    if (card) { e.preventDefault(); card.scrollIntoView({ block: "center" }); card.classList.add("tcard-changed"); setTimeout(function () { card.classList.remove("tcard-changed"); }, 2000); return; }
    var alt = a.getAttribute("data-alt") || "";
    if (alt.charAt(0) === "/" && alt.charAt(1) !== "/") { e.preventDefault(); location.href = alt; }
  });

  // nút "Sao chép" của khối code (md()): một handler uỷ quyền; trang không phải secure context → textarea ẩn + execCommand
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (ok, fail) {
      var ta = document.createElement("textarea");
      ta.value = text; ta.setAttribute("readonly", ""); ta.className = "tx-copy-buf";
      document.body.appendChild(ta); ta.select();
      var done = false;
      try { done = document.execCommand("copy"); } catch (_) { done = false; }
      ta.remove();
      if (done) ok(); else fail(new Error("copy"));
    });
  }
  document.addEventListener("click", function (e) {
    var b = e.target && e.target.closest ? e.target.closest(".tx-copy") : null;
    if (!b) return;
    // P2: cả tin / output tool — văn bản GỐC nằm trong <template class="tx-src"> ngay cạnh nút (không phải chữ đã vẽ)
    if (b.classList.contains("tx-copy-src")) {
      e.preventDefault();  // nút nằm trong <summary>/<details> → không đóng/mở khung
      var tpl = b.nextElementSibling;
      if (!tpl || tpl.tagName !== "TEMPLATE") return;
      var t = b.querySelector(".tx-copy-t");
      copyText(tpl.content.textContent).then(function () { b.classList.add("done"); if (t) t.textContent = "Đã chép"; },
        function () { if (t) t.textContent = "Không chép được"; })
        .then(function () { setTimeout(function () { b.classList.remove("done"); if (t) t.textContent = "Sao chép"; }, 1500); });
      return;
    }
    var box = b.closest(".tx-code"), pre = box && box.querySelector("pre");
    if (!pre) return;
    var label = b.getAttribute("data-label") || b.textContent;
    b.setAttribute("data-label", label);
    copyText(pre.textContent).then(function () { b.textContent = "Đã chép"; }, function () { b.textContent = "Không chép được"; })
      .then(function () { setTimeout(function () { b.textContent = label; }, 1500); });
  });

  // ------------------------------------------------------------ live run
  var tr = document.querySelector("[data-sse-run]");
  var runScroll = document.getElementById("run-scroll");
  // trang run (cả run đã xong): mở trang → xuống cuối transcript, như một cuộc chat
  if (runScroll && location.hash === "") {
    var toEnd = function () { runScroll.scrollTop = runScroll.scrollHeight; };
    toEnd();
    window.addEventListener("load", toEnd);
  }
  if (tr && window.EventSource) {
    var es = new EventSource(tr.getAttribute("data-sse-run"));
    var state = document.getElementById("live-state");
    // P1-6: nút "↓ Có mục mới" — hiện khi có mục mới lúc người đang cuộn lên đọc; bấm → xuống cuối; tự ẩn khi đã ở gần cuối
    var jump = document.getElementById("tx-jump");
    var sc = txScroller(tr);
    if (jump) {
      jump.addEventListener("click", function () { sc.scrollTo({ top: sc.scrollHeight, behavior: "smooth" }); jump.hidden = true; });
      sc.addEventListener("scroll", function () { if (!jump.hidden && nearBottom(sc)) jump.hidden = true; }, { passive: true });
    }
    // run xong: thay #run-tail bằng thẻ Kết quả (partial) + ô steer → ô "Nói tiếp" (hx-swap-oob) — KHÔNG tải lại trang,
    // transcript (khối đang mở, suy nghĩ live) giữ nguyên
    var finishTries = 0;
    function finishRun() {
      var msgs = tr.querySelectorAll(".tx-msg:not(.sub)");
      for (var i = 0; i < msgs.length; i++) msgs[i].classList.toggle("final", i === msgs.length - 1);
      var card = document.getElementById("steer-card"), tail = document.getElementById("run-tail");
      if (!card || !tail || !window.htmx) return;
      var box = card.querySelector("textarea"), draft = box ? box.value : "";
      window.htmx.ajax("GET", card.getAttribute("data-result"), { target: tail, swap: "outerHTML" }).then(function () {
        if (document.getElementById("run-tail")) {  // 204: daemon chưa ghi trạng thái cuối → thử lại
          if (++finishTries < 5) setTimeout(finishRun, 1200);
          return;
        }
        var t = document.querySelector('#run-dock textarea[name="prompt"]');
        if (t && draft.trim()) t.value = draft;  // chữ đang gõ dở trong ô steer chuyển sang ô "Nói tiếp"
      });
    }
    // P2: dòng trạng thái kiểu Claude Code "✳ Đang nghĩ… 12s · 3.4k token" — giây đếm từ lúc run bắt đầu (data-start), chữ từ SSE
    // `status` (server) hoặc loại delta đang tới; token = `stats.out`. Chỉ textContent.
    var stl = document.getElementById("tx-status");
    var stT = stl && stl.querySelector(".tx-status-t"), stM = stl && stl.querySelector(".tx-status-m");
    var st0 = stl ? Date.parse(stl.getAttribute("data-start") || "") : NaN, stTok = "", thinkTok = "";
    if (stl && isNaN(st0)) st0 = Date.now();
    function fmtSec(s) { s = Math.max(0, Math.floor(s)); return s < 60 ? s + "s" : Math.floor(s / 60) + "m " + ("0" + (s % 60)).slice(-2) + "s"; }
    function paintStatus() {
      if (!stM) return;
      stM.textContent = fmtSec((Date.now() - st0) / 1000) + (stTok ? " · " + stTok + " token" : "");
    }
    function setStatus(label) { if (stT && label && stT.textContent !== label) stT.textContent = label; paintStatus(); }
    var stTimer = stl ? setInterval(paintStatus, 1000) : null;
    paintStatus();
    es.onmessage = function (ev) {
      var m;
      try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m.t === "delta" || m.t === "item" || m.t === "part") {
        var node = txAppend(tr, m);
        if (node && jump && !tr._tx.stuck) jump.hidden = false;
        if (m.t === "delta") {
          setStatus(m.k === "text" ? "Đang viết…" : "Đang nghĩ…");
          var th = m.k !== "text" && tr._tx && tr._tx.think;
          if (th && thinkTok) { var ts0 = th.querySelector("summary"); if (ts0) ts0.textContent = "Suy nghĩ… · " + thinkTok + " token"; }
        }
      } else if (m.t === "status") {
        if (m.think_tokens) {
          thinkTok = m.think_tokens;
          var lt = tr._tx && tr._tx.think, ls = lt && lt.querySelector("summary");
          if (ls) ls.textContent = "Suy nghĩ… · " + thinkTok + " token";
        }
        if (m.label) { setStatus(m.label); if (m.state !== "think") thinkTok = ""; }
      } else if (m.t === "stats") {
        var c = document.getElementById("st-cost"), k = document.getElementById("st-tok");
        if (c) c.textContent = m.cost;
        if (k) k.textContent = m.tokens;
        if (m.out) { stTok = m.out; paintStatus(); }
      } else if (m.t === "refresh") {
        var bar = document.getElementById("runbar");
        if (bar && window.htmx && bar.getAttribute("hx-get")) window.htmx.ajax("GET", bar.getAttribute("hx-get"), { target: bar, swap: "innerHTML" });
      } else if (m.t === "end") {
        es.close();
        if (stTimer) clearInterval(stTimer);
        if (stl) stl.hidden = true;
        if (state) { state.textContent = "đã kết thúc"; state.className = "pill muted"; }
        setTimeout(finishRun, 300);
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
  var mgrStop = null, mgrNav = null;
  // bản nháp ô nhập theo hội thoại/luồng — sessionStorage (giữ khi đổi tab và khi tải lại trang; riêng từng tab trình duyệt)
  function draftGet(k) { try { return sessionStorage.getItem("cfh_mgr_draft:" + k) || ""; } catch (_) { return ""; } }
  function draftSet(k, v) {
    try { if (v && v.trim()) sessionStorage.setItem("cfh_mgr_draft:" + k, v); else sessionStorage.removeItem("cfh_mgr_draft:" + k); } catch (_) {}
  }
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
    if (tx) draftSet(draftKey(), tx.value);
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
        var t2 = document.getElementById("mgr-text");  // bản nháp: initManager tự khôi phục từ sessionStorage
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
    var mes = null, tabsT = null, tabsPoll = null, retryT = null, backoff = 1000, dead = false;
    var mform = document.getElementById("mgr-form");
    var mtext = document.getElementById("mgr-text");
    var merr = document.getElementById("mgr-err");
    var mlive = document.getElementById("mgr-live");
    var msend = document.getElementById("mgr-send");
    var mjump = document.getElementById("mgr-jump");
    var mstatus = document.getElementById("mgr-status-live");
    var conv = ml.getAttribute("data-conv") || "";
    var runs = {};
    (ml.getAttribute("data-runs") || "").split(" ").forEach(function (r) { if (r) runs[r] = true; });
    var pend = {};       // run → phần tử "đang trả lời"
    var early = {};      // run → [tin SSE tới trước khi fetch trả run id]
    var replied = {};
    var picked = [];
    var p0 = ml.querySelector("[data-pending]");
    if (p0) pend[p0.getAttribute("data-pending")] = p0;
    // P0-4: bỏ trùng theo seq của event gốc. Nhiều message của CÙNG một event (delta + draft) có cùng seq → seq == lastSeq chỉ bị bỏ khi
    // tới từ một kết nối KHÁC (nối lại với since=lastSeq thì daemon chỉ phát seq > lastSeq — đây là chốt chặn phụ).
    var lastSeq = 0, lastConn = 0, connN = 0, epoch = "";
    try {
      var u0 = new URL(ml.getAttribute("data-sse-manager"), location.href);
      lastSeq = parseInt(u0.searchParams.get("since") || "0", 10) || 0;
      epoch = u0.searchParams.get("epoch") || "";
    } catch (_) {}
    function bottom() { ml.scrollTop = ml.scrollHeight; }
    bottom();
    window.addEventListener("load", bottom);
    setTimeout(bottom, 300);
    function dropEmpty() { var e = document.getElementById("mgr-empty"); if (e) e.remove(); }
    function stick() { return nearBottom(ml); }
    // nút "↓ Tin mới nhất": hiện khi người đã cuộn lên mà có nội dung mới tới; cuộn mượt CHỈ khi bấm nút
    var unseen = false;
    function jumpPaint() { if (mjump) mjump.hidden = !(unseen && !stick()); }
    function after(s) { if (s) bottom(); else { unseen = true; jumpPaint(); } }
    ml.addEventListener("scroll", function () { if (stick()) unseen = false; jumpPaint(); }, { passive: true });
    if (mjump) mjump.addEventListener("click", function () {
      unseen = false; mjump.hidden = true;
      try { ml.scrollTo({ top: ml.scrollHeight, behavior: "smooth" }); } catch (_) { bottom(); }
    });
    function put(node, s) { if (!node) return; dropEmpty(); ml.appendChild(node); refreshTimes(node); after(s); }
    function announce(t) { if (mstatus) { mstatus.textContent = ""; setTimeout(function () { mstatus.textContent = t; }, 60); } }
    // chỉ tin điều phối viên CUỐI có nút "Thử lại"
    function markLast() {
      var ms = ml.querySelectorAll(".mm.mgr:not(.pending):not(.notify)");
      for (var i = 0; i < ms.length; i++) ms[i].classList.toggle("last", i === ms.length - 1);
    }
    markLast();
    function pendingRun() { var ks = Object.keys(pend); return ks.length ? ks[ks.length - 1] : null; }
    // P0-6: nút gửi = nút Dừng khi có lượt đang trả lời và ô nhập trống; có chữ → gửi (steer). Trống + không chạy → disabled thật.
    function paintSend() {
      if (!msend || !mtext) return;
      var has = !!mtext.value.trim() || picked.length > 0, stopMode = !has && !!pendingRun();
      msend.classList.toggle("stop", stopMode);
      msend.disabled = !has && !stopMode;
      msend.title = stopMode ? "Dừng trả lời (Esc)" : "Gửi";
      msend.setAttribute("aria-label", stopMode ? "Dừng trả lời" : "Gửi");
    }
    function busyChips(r, on) {
      var cs = ml.querySelectorAll(".mm-acts");
      for (var i = 0; i < cs.length; i++) if (cs[i].getAttribute("data-run") === r) cs[i].classList.toggle("running", !!on);
    }
    function settle(r) { delete pend[r]; busyChips(r, false); paintSend(); }
    function parts(p) {
      return { md: p.querySelector(".mm-live .md"), tail: p.querySelector(".mm-tail"), think: p.querySelector(".mm-think"),
               tool: p.querySelector(".mm-tool"), toolT: p.querySelector(".mm-tool-t") };
    }
    function stopRun() {
      var r = pendingRun();
      if (!r || !mform || !window.fetch) return;
      var p = pend[r], fd = new FormData();
      var cs = mform.querySelector('input[name="csrf"]');
      fd.append("csrf", cs ? cs.value : ""); fd.append("k", getKey()); fd.append("run", r); fd.append("c", conv);
      var st = p && p.querySelector(".mm-state .spin");
      if (st) st.textContent = "đang dừng…";
      fetch("/manager/stop", { method: "POST", body: fd, credentials: "same-origin", headers: { "X-Ahub-Fetch": "1" } })
        .then(function (resp) { return resp.json().catch(function () { return { error: "Lỗi " + resp.status }; }); })
        .then(function (j) { if (j.error && merr) { merr.textContent = j.error; merr.hidden = false; } }, function () {
          if (merr) { merr.textContent = "Không dừng được — kiểm tra kết nối rồi thử lại."; merr.hidden = false; }
        });
    }
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
      var p = pend[r], q = p ? parts(p) : null;
      if (m.t === "delta" || m.t === "draft" || m.t === "seg") {
        if (!q) return;
        // chữ chạy: delta thô → textContent ở đuôi; `draft` = HTML của present.md() do SERVER vẽ → thay phần đã render, xoá đuôi
        if (m.t === "delta") q.tail.textContent += m.text || "";
        else if (m.t === "seg") { if (q.tail.textContent) q.tail.textContent += "\n\n"; }
        else { q.md.innerHTML = m.html || ""; q.tail.textContent = ""; }
        if (q.tail.textContent || q.md.firstChild) p.classList.add("has-text");
        after(s);
      } else if (m.t === "think") {
        if (!q) return;
        q.think.hidden = false;
        q.think.querySelector("pre").textContent += m.text || "";
        after(s);
      } else if (m.t === "tool") {
        if (!q) return;
        var on = p._tools || (p._tools = {});
        var id = m.id || m.name;
        if (m.done) delete on[id]; else on[id] = m.label || m.name;
        var left = Object.keys(on);
        q.tool.hidden = !left.length;
        if (left.length) q.toolT.textContent = "Đang dùng " + on[left[left.length - 1]] + (left.length > 1 ? " (+" + (left.length - 1) + ")" : "") + "…";
        after(s);
      } else if (m.t === "todo") {
        // P2: checklist TodoWrite (HTML server vẽ, macro tx_todo) — một khối trong khung đang trả lời, thay tại chỗ
        if (!p) return;
        var td = fromHTML(m.html || ""), oldTd = p.querySelector(".tx-todo");
        if (!td) return;
        if (oldTd) { if (!oldTd.open) td.open = false; oldTd.replaceWith(td); }
        else { var lv = p.querySelector(".mm-live"); if (lv) p.insertBefore(td, lv); else p.appendChild(td); }
        after(s);
      } else if (m.t === "reply" && m.notify) {
        var nn = fromHTML(m.html);  // báo chủ động giữa lượt: chèn trước khung "đang trả lời", không thay nó
        if (!nn) return;
        if (p) { dropEmpty(); ml.insertBefore(nn, p); refreshTimes(nn); after(s); } else put(nn, s);
      } else if (m.t === "reply") {
        var dup = r && ml.querySelector('.mm.mgr:not(.pending):not(.notify)[data-run="' + r + '"]');
        if (replied[r] && dup) return;  // phát lại (daemon khởi động lại / nối lại) — đã có câu trả lời
        replied[r] = true;
        var node = fromHTML(m.html);
        if (!node) return;
        if (p) {
          var th = q.think;  // giữ khối suy nghĩ (chữ đã gán bằng textContent) trong câu trả lời cuối
          if (th && !th.hidden && th.querySelector("pre").textContent) {
            var who = node.querySelector(".mm-who");
            th.querySelector("summary").textContent = "Suy nghĩ";
            if (who) who.after(th); else node.insertBefore(th, node.firstChild);
          }
          p.replaceWith(node); refreshTimes(node); after(s);
        } else put(node, s);
        settle(r); markLast();
        announce("Điều phối viên đã trả lời");
      } else if (m.t === "action") {
        // gom vào khối "Đã làm N việc" của cùng lượt nếu khối đó là mục cuối (trước khung "đang trả lời")
        var last = p ? p.previousElementSibling : ml.lastElementChild;
        if (last && last.classList.contains("mm-acts") && last.getAttribute("data-run") === r) {
          var one = fromHTML(m.one), list = last.querySelector(".mm-acts-list") || last;
          if (one) list.appendChild(one);
          var n = last.querySelector(".n"); if (n) n.textContent = String(last.querySelectorAll(".mm-act").length);
          var lt = last.querySelector(".mm-acts-last"), sum = one && one.querySelector(".mm-act-sum");
          if (lt && sum) lt.textContent = sum.textContent.length > 60 ? sum.textContent.slice(0, 59) + "…" : sum.textContent;
          if (!m.ok) last.classList.add("err");
        } else {
          var a = fromHTML(m.html);
          if (!a) return;
          if (p) { dropEmpty(); ml.insertBefore(a, p); } else put(a, s);
          last = a;
        }
        if (p && last) last.classList.add("running");
        after(s);
      } else if (m.t === "run_end") {
        busyChips(r, false);
        setTimeout(function () {
          if (replied[r]) return;
          var n2 = fromHTML(m.html), p2 = pend[r];
          if (p2) {
            p2.removeAttribute("aria-busy");
            if (m.ok) { var w = p2.querySelector(".mm-state"); if (w) w.textContent = " · đã xong — tải lại trang nếu chưa thấy trả lời"; }
            else if (n2) p2.replaceWith(n2);
            settle(r);
          } else if (!m.ok) put(n2, stick());
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
    // P0-4: SSE tự nối lại có giãn (cả sau `end`) với since = seq cuối đã nhận → không lặp chữ, không chết luồng
    function liveState(ok) {
      if (!mlive) return;
      mlive.textContent = ok ? "● trực tiếp" : "↻ đang nối lại…";
      mlive.className = ok ? "pill ok live-dot" : "pill warn";
    }
    function reconnect() {
      if (dead) return;
      liveState(false);
      clearTimeout(retryT);
      retryT = setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 30000);
    }
    function connect() {
      if (dead || !window.EventSource) return;
      var my = ++connN;
      var es = mes = new EventSource("/sse/manager?since=" + lastSeq + "&epoch=" + encodeURIComponent(epoch));
      es.onopen = function () { if (my === connN) { backoff = 1000; liveState(true); } };
      es.onerror = function () { if (my !== connN) return; es.close(); reconnect(); };  // tự nối (trình duyệt sẽ dùng lại since CŨ)
      es.onmessage = function (ev) {
        if (my !== connN) return;
        var m;
        try { m = JSON.parse(ev.data); } catch (_) { return; }
        if (m.t === "hello") {
          if (m.epoch && epoch && m.epoch !== epoch) { lastSeq = 0; lastConn = my; }  // daemon khởi động lại: seq đếm lại từ đầu
          if (m.epoch) epoch = m.epoch;
          return;
        }
        if (m.t === "end") { es.close(); reconnect(); return; }
        if (typeof m.seq === "number") {
          if (m.seq < lastSeq || (m.seq === lastSeq && lastConn !== my)) return;
          lastSeq = m.seq; lastConn = my;
        }
        handle(m);
      };
    }
    connect();
    // ---- ô nhập
    function saveDraft() { if (mtext) draftSet(draftKey(), mtext.value); }
    function fill(text, send) {
      if (!mtext) return;
      mtext.value = text; autoGrow(mtext); saveDraft(); paintSend();
      mtext.focus();
      try { mtext.setSelectionRange(mtext.value.length, mtext.value.length); } catch (_) {}
      if (send && mform) { if (mform.requestSubmit) mform.requestSubmit(); else mform.dispatchEvent(new Event("submit", { cancelable: true })); }
    }
    function srcOf(el) { var t = el && el.querySelector("template.mm-src"); return t ? t.content.textContent : ""; }
    // P1-5: thao tác dưới tin (uỷ quyền): Sao chép (văn bản gốc trong <template>), Thử lại (tin cuối), Sửa (tin của người)
    ml.addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest("[data-mm]");
      if (!b || !ml.contains(b)) return;
      var msg = b.closest(".mm"), act = b.getAttribute("data-mm");
      if (act === "copy") {
        copyText(srcOf(msg)).then(function () { b.classList.add("done"); b.title = "Đã chép"; }, function () { b.title = "Không chép được"; })
          .then(function () { setTimeout(function () { b.classList.remove("done"); b.title = "Sao chép"; }, 1500); });
      } else if (act === "edit") {
        fill(srcOf(msg), false);
      } else if (act === "retry") {
        var prev = msg.previousElementSibling;
        while (prev && !prev.classList.contains("user")) prev = prev.previousElementSibling;
        if (prev && !pendingRun()) fill(srcOf(prev), true);
      }
    });
    if (mtext) {
      var dv = draftGet(draftKey());
      if (dv && !mtext.value) { mtext.value = dv; autoGrow(mtext); }
      mtext.addEventListener("input", function () { saveDraft(); paintSend(); });
    }
    paintSend();
    if (mform && window.fetch && window.FormData) {
      // ---- đính kèm: nút 📎 (input file), kéo–thả vào ô nhập, dán ảnh. Giới hạn = của daemon; server kiểm lại theo nội dung.
      var IMG_TYPES = { "image/png": 1, "image/jpeg": 1, "image/gif": 1, "image/webp": 1 };
      var MAX_IMG = 4, MAX_DOC = 4, IMG_BYTES = 5 * 1024 * 1024, DOC_BYTES = 10 * 1024 * 1024;
      var finput = document.getElementById("mgr-file");
      var flist = document.getElementById("mgr-files");
      var isImg = function (f) { return !!IMG_TYPES[(f.type || "").toLowerCase()]; };
      var human = function (n) { return n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB"; };
      var showErr = function (msg) { if (merr) { merr.textContent = msg; merr.hidden = !msg; } };
      var drawFiles = function () {
        paintSend();
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
      };
      var addFiles = function (list) {
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
      };
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
      var coarse = window.matchMedia ? window.matchMedia("(pointer: coarse)") : null;
      if (mtext) mtext.addEventListener("keydown", function (e) {
        // Esc (ô trống): dừng lượt đang trả lời. Enter gửi; Shift+Enter xuống dòng; màn cảm ứng: Enter = xuống dòng (gửi bằng nút);
        // không gửi khi đang gõ dấu (IME)
        if (e.key === "Escape" && pendingRun() && !mtext.value.trim()) { e.preventDefault(); stopRun(); return; }
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229 && !(coarse && coarse.matches)) {
          e.preventDefault(); if (mform.requestSubmit) mform.requestSubmit(); else mform.submit();
        }
      });
      var sug = document.getElementById("mgr-suggest");
      if (sug && mtext) sug.addEventListener("click", function (e) {
        var b = e.target.closest("button[data-fill]");
        if (!b) return;
        fill(b.getAttribute("data-fill"), false);
      });
      mform.addEventListener("submit", function (e) {
        e.preventDefault();  // khoá phiên `k` đã được gắn ở pha capture (trên)
        if (!mtext.value.trim() && !picked.length) { if (pendingRun()) stopRun(); return; }
        if (msend) msend.disabled = true;
        if (merr) merr.hidden = true;
        var fd = new FormData(mform);
        fd.delete("attach");
        picked.forEach(function (f) { fd.append("attach", f, f.name); });
        fetch(mform.getAttribute("action"), { method: "POST", body: fd, credentials: "same-origin", headers: { "X-Ahub-Fetch": "1" } })
          .then(function (resp) { return resp.json().catch(function () { return { error: "Lỗi " + resp.status }; }); })
          .then(function (j) {
            if (j.error) { if (merr) { merr.textContent = j.error; merr.hidden = false; } return; }
            draftSet(draftKey(), "");
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
          .then(function () { paintSend(); mtext.focus(); });
      });
    }
    return function stop() {
      dead = true;
      if (mes) mes.close();
      clearTimeout(retryT);
      clearInterval(tabsPoll);
      clearTimeout(tabsT);
    };
  }
  mgrBoot();
})();
