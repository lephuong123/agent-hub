/* cfharness web — chip file (web/file_pages.py, docs/dev-notes/file-open.md). CSP script-src 'self': không inline/eval.
 * - Đang dùng web trên CHÍNH máy chạy hub (host *.localhost / loopback) → hiện nút "Mở"/"Thư mục" (class html.file-local). Đây chỉ là
 *   gợi ý giao diện: server tự quyết định cục bộ hay không (POST /files/open trả 403 khi qua link public/tunnel).
 * - Bấm nút (ủy quyền sự kiện → chạy cả với mục transcript đổ vào bằng SSE): POST /files/open (CSRF + khoá phiên WT-1) → toast.
 * Không dựng HTML từ dữ liệu: thông báo gán bằng textContent.
 * - Panel xem file (base.html #fpanel, kiểu claude.ai): bấm "Xem" ở chip (không giữ Ctrl/⌘/Shift, không chuột giữa) → nạp
 *   /files/view?…&panel=1 (HTML do server dựng + escape) vào panel trượt từ phải; Esc / × để đóng, nút ↗ mở tab mới như cũ.
 *   HTML nhận về được parse bằng DOMParser rồi gắn node (script trong đó không bao giờ chạy; CSP script-src 'self' chặn thêm một lớp).
 * - Markdown: nút "Xem đẹp" / "Mã nguồn" (data-fmode) đổi data-mode của .fview-body — nhớ lựa chọn theo người xem (localStorage).
 */
(function () {
  "use strict";
  var h = (location.hostname || "").toLowerCase().replace(/^\[|\]$/g, "");
  var local = h === "localhost" || /\.localhost$/.test(h) || /^127\./.test(h) || h === "::1";
  if (local) document.documentElement.classList.add("file-local");

  function csrf() {
    var i = document.querySelector('input[name="csrf"]');
    return i ? i.value : "";
  }
  function key() {
    try { return localStorage.getItem("ahub_k") || ""; } catch (_) { return ""; }
  }
  function toast(ok, msg) {
    var box = document.getElementById("toasts");
    if (!box) { window.alert(msg); return; }
    var d = document.createElement("div");
    d.className = "flash " + (ok ? "ok" : "error");
    var t = document.createElement("span");
    t.className = "t";
    t.textContent = msg;
    var x = document.createElement("button");
    x.type = "button"; x.className = "toast-x"; x.setAttribute("aria-label", "Đóng"); x.textContent = "×";
    d.appendChild(t); d.appendChild(x);
    box.appendChild(d);
    if (ok) setTimeout(function () { if (d.parentNode) d.remove(); }, 6000);
  }

  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-fopen]");
    if (!b) return;
    e.preventDefault();
    e.stopPropagation();
    var chip = b.closest("[data-path]");
    if (!chip || b.disabled) return;
    var fd = new FormData();
    fd.append("csrf", csrf());
    fd.append("k", key());
    if (chip.getAttribute("data-run")) fd.append("run", chip.getAttribute("data-run"));
    else fd.append("agent", chip.getAttribute("data-agent") || "");
    fd.append("path", chip.getAttribute("data-path"));
    if (b.getAttribute("data-fopen") === "reveal") fd.append("reveal", "1");
    b.disabled = true;
    fetch("/files/open", { method: "POST", body: fd, credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) {
        return r.json().catch(function () { return { ok: false, message: "Lỗi " + r.status + " — tải lại trang rồi thử lại" }; });
      })
      .then(function (j) { toast(!!j.ok, j.message || (j.ok ? "Đã mở" : "Không mở được")); })
      .catch(function () { toast(false, "Không gửi được yêu cầu tới web"); })
      .then(function () { b.disabled = false; });
  }, true);
  // ------------------------------------------------------------ markdown: Xem đẹp ↔ Mã nguồn
  var MODE_KEY = "ahub_fmode";
  function savedMode() { try { return localStorage.getItem(MODE_KEY) || ""; } catch (_) { return ""; } }
  function setMode(body, mode) {
    if (!body || !body.querySelector(".fview-modes")) return;
    body.setAttribute("data-mode", mode);
    var bs = body.querySelectorAll("[data-fmode]");
    for (var i = 0; i < bs.length; i++) bs[i].setAttribute("aria-pressed", bs[i].getAttribute("data-fmode") === mode ? "true" : "false");
  }
  function applySaved(scope) {
    var m = savedMode();
    if (m !== "md" && m !== "src") return;
    var bodies = (scope || document).querySelectorAll(".fview-body");
    for (var i = 0; i < bodies.length; i++) setMode(bodies[i], m);
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-fmode]");
    if (!b) return;
    var mode = b.getAttribute("data-fmode");
    setMode(b.closest(".fview-body"), mode);
    try { localStorage.setItem(MODE_KEY, mode); } catch (_) { /* chế độ riêng tư: chỉ không nhớ */ }
  });
  applySaved(document);

  // ------------------------------------------------------------ panel xem file trượt từ phải
  var panel = document.getElementById("fpanel");
  if (!panel) return;
  var pbody = document.getElementById("fpanel-body");
  var ptitle = document.getElementById("fpanel-title");
  var ptab = document.getElementById("fpanel-tab");
  var root = document.documentElement;
  var cur = null;      // chip "Xem" đang mở trong panel
  var seq = 0;         // chống lệch khi bấm nhanh nhiều file
  var closeTimer = 0;

  function msg(text) {
    var d = document.createElement("div");
    d.className = "fpanel-msg";
    d.textContent = text;
    pbody.replaceChildren(d);
  }
  var curHref = "";    // so theo href: SSE/htmx thay node chip thì vẫn nhận ra đúng file đang mở
  function mark(a) {
    var on = document.querySelectorAll("a.fchip-view.on");
    for (var i = 0; i < on.length; i++) on[i].classList.remove("on");
    cur = a;
    curHref = a ? a.getAttribute("href") : "";
    if (cur) cur.classList.add("on");
  }
  function openPanel() {
    clearTimeout(closeTimer);
    if (panel.hidden) {
      panel.hidden = false;
      void panel.offsetWidth;  // để transition chạy từ vị trí ẩn
    }
    root.classList.add("fpanel-open");
  }
  function closePanel() {
    if (panel.hidden) return;
    seq++;
    root.classList.remove("fpanel-open");
    pbody.classList.remove("loading");
    var back = cur;
    mark(null);
    closeTimer = setTimeout(function () { panel.hidden = true; pbody.replaceChildren(); }, 240);
    if (back && document.contains(back)) back.focus({ preventScroll: true });  // không cuộn dòng thời gian: khung tự giữ mục đang đọc khi rộng lại
  }
  function show(a) {
    var href = a.getAttribute("href");
    var chip = a.closest("[data-path]");
    var name = (chip && chip.getAttribute("data-path")) || "";
    name = name.split("\\").join("/").split("/").pop() || "Xem file";
    ptitle.textContent = name;
    ptitle.title = (chip && chip.getAttribute("data-path")) || "";
    ptab.href = href;
    mark(a);
    openPanel();
    var my = ++seq;
    pbody.classList.add("loading");
    if (!pbody.firstChild) msg("Đang tải…");
    fetch(href + (href.indexOf("?") < 0 ? "?" : "&") + "panel=1", { credentials: "same-origin", headers: { "Accept": "text/html" } })
      .then(function (r) {
        return r.text().then(function (t) { return { ok: r.ok, status: r.status, text: t, redirected: r.redirected }; });
      })
      .then(function (res) {
        if (my !== seq) return;
        var doc = new DOMParser().parseFromString(res.text, "text/html");
        // hết phiên → middleware chuyển hướng về /login (fetch đi theo, 200): không nhét trang khác vào panel — mở bằng cả trang
        if (res.ok && (res.redirected || !doc.querySelector(".fview-card") || doc.getElementById("fpanel"))) {
          location.href = href;
          return;
        }
        if (!res.ok) {
          var em = doc.querySelector("main .card p");
          msg("Không xem được file (" + res.status + ")" + (em && em.textContent.trim() ? ": " + em.textContent.trim().slice(0, 300) : ""));
          return;
        }
        var nodes = [];
        for (var n = doc.body.firstChild; n; n = n.nextSibling) nodes.push(document.importNode(n, true));
        pbody.replaceChildren.apply(pbody, nodes);
        applySaved(pbody);
        pbody.scrollTop = 0;
        var ae = document.activeElement;  // không cướp focus nếu người dùng đã chuyển sang gõ chỗ khác trong lúc chờ
        if (!ae || ae === document.body || ae === cur || panel.contains(ae)) panel.focus({ preventScroll: true });
      })
      .catch(function () { if (my === seq) msg("Không tải được file — kiểm tra kết nối rồi thử lại."); })
      .then(function () { if (my === seq) pbody.classList.remove("loading"); });
  }

  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a.fchip-view");
    if (!a || a.classList.contains("fchip-fh")) return;
    if (e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;  // giữ phím → tab mới như cũ
    e.preventDefault();
    if (a.getAttribute("href") === curHref && root.classList.contains("fpanel-open")) { closePanel(); return; }
    show(a);
  });
  document.getElementById("fpanel-x").addEventListener("click", closePanel);
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape" || e.defaultPrevented || !root.classList.contains("fpanel-open") || document.querySelector("dialog[open]")) return;
    // Esc trong ô gõ ngoài panel thuộc về ô đó (vd. composer: Esc = dừng lượt chạy) — không đóng panel cùng lúc
    var t = e.target;
    if (t && !panel.contains(t) && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    closePanel();
  });
})();
