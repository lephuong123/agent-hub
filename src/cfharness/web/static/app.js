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

  // ------------------------------------------------------------ polling htmx chỉ khi tab đang hiện
  // Mỗi lượt `hx-trigger="every Ns"` là một RPC tới daemon (vd tui.snapshot 170–370 ms trên hub lớn) mà daemon phục vụ trên MỘT
  // vòng sự kiện → tab nền làm chậm mọi người. Không dùng bộ lọc `every 3s [document.visibilityState=='visible']`: base.html
  // đặt allowEval:false (CSP script-src 'self') nên htmx từ chối biểu thức lọc và vẫn hỏi. Ở đây: lượt poll lúc tab ẩn bị huỷ
  // (htmx vẫn hẹn lượt sau) và phần tử bị đánh dấu "cũ"; tab hiện lại → mỗi phần tử cũ tải lại ĐÚNG MỘT lần.
  // Lượt poll của htmx 2 gọi handler không kèm event (triggeringEvent rỗng) với elt = chính phần tử có hx-trigger "every …";
  // click/submit/`mgr-tick from:body` có event, htmx.ajax() không nguồn có elt = body → không bao giờ bị chặn.
  var POLL_RE = /(^|,)\s*every\s/;
  var pollStale = typeof Set === "function" ? new Set() : null;
  var pollLast = typeof WeakMap === "function" ? new WeakMap() : null;
  function isPoll(d) {
    var elt = d && d.elt, c = d && d.requestConfig;
    if (!elt || !c || c.triggeringEvent || !elt.getAttribute) return false;
    return POLL_RE.test(elt.getAttribute("hx-trigger") || "");
  }
  if (pollStale && pollLast) {
    document.body.addEventListener("htmx:beforeRequest", function (e) {
      if (!isPoll(e.detail)) return;
      var elt = e.detail.elt;
      if (document.hidden) { e.preventDefault(); pollStale.add(elt); return; }
      // vừa tải lại khi quay về tab → bỏ lượt poll định kỳ rơi ngay sau đó (không gửi đôi)
      var t = pollLast.get(elt);
      if (t && Date.now() - t < 1500) e.preventDefault();
    });
    document.addEventListener("visibilitychange", function () {
      if (document.hidden || !window.htmx || !pollStale.size) return;
      var els = Array.from(pollStale);
      pollStale.clear();
      for (var i = 0; i < els.length; i++) {
        var el = els[i], url = el.getAttribute("hx-get");
        if (!el.isConnected || !url || !POLL_RE.test(el.getAttribute("hx-trigger") || "")) continue;
        pollLast.set(el, Date.now());
        // source = chính phần tử → htmx tự áp hx-target/hx-swap của nó, như một lượt poll; có event → không bị coi là poll
        window.htmx.ajax("GET", url, { source: el, event: new CustomEvent("cfh:poll-resume") }).catch(function () {});
      }
    });
  }

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
    // hỏi lại dự phòng chỉ khi tab đang hiện; quay về tab lúc SSE vẫn chết → tải lại một lần ngay
    function startPoll() { if (!poll) poll = setInterval(function () { if (!live && !document.hidden) schedule(0); }, 15000); }
    document.addEventListener("visibilitychange", function () { if (poll && !live && !document.hidden) schedule(0); });
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
})();
