/* cfharness web — chip file (web/file_pages.py, docs/dev-notes/file-open.md). CSP script-src 'self': không inline/eval.
 * - Đang dùng web trên CHÍNH máy chạy hub (host *.localhost / loopback) → hiện nút "Mở"/"Thư mục" (class html.file-local). Đây chỉ là
 *   gợi ý giao diện: server tự quyết định cục bộ hay không (POST /files/open trả 403 khi qua link public/tunnel).
 * - Bấm nút (ủy quyền sự kiện → chạy cả với mục transcript đổ vào bằng SSE): POST /files/open (CSRF + khoá phiên WT-1) → toast.
 * Không dựng HTML từ dữ liệu: thông báo gán bằng textContent.
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
})();
