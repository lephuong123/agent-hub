// Trang "Chuyển hub" (web/transfer_pages.py): tải gói .cfhx lên bằng XHR THÂN THÔ (application/octet-stream) — server ghi từng khúc
// xuống HUB_HOME/imports/ (không parse multipart vào RAM). CSRF + khoá phiên (localStorage "ahub_k", WT-1) đi qua header.
// Chữ từ server chỉ gán qua textContent.
(function () {
  "use strict";
  var form = document.getElementById("hub-upload");
  if (!form) return;
  var input = document.getElementById("hub-upload-file");
  var bar = document.getElementById("hub-upload-bar");
  var msg = document.getElementById("hub-upload-msg");
  var btn = document.getElementById("hub-upload-btn");
  function say(t) { if (msg) msg.textContent = t; }
  function key() { try { return localStorage.getItem("ahub_k") || ""; } catch (_) { return ""; } }
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var f = input && input.files && input.files[0];
    if (!f) { say("Chọn file gói .cfhx trước."); return; }
    var maxMb = parseInt(form.getAttribute("data-max-mb") || "0", 10);
    if (maxMb && f.size > maxMb * 1024 * 1024) { say("File lớn hơn trần " + maxMb + " MB — dùng `cfharness hub import <file>` ở terminal."); return; }
    var xhr = new XMLHttpRequest();
    xhr.open("POST", form.getAttribute("data-upload"));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("X-CSRF-Token", form.getAttribute("data-csrf") || "");
    xhr.setRequestHeader("X-Ahub-Key", key());
    if (bar) { bar.hidden = false; bar.value = 0; }
    if (btn) btn.disabled = true;
    xhr.upload.onprogress = function (ev) {
      if (ev.lengthComputable && bar) { bar.value = Math.floor(ev.loaded * 100 / ev.total); say("Đang tải lên… " + bar.value + "%"); }
    };
    xhr.onload = function () {
      var r = null;
      try { r = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status === 200 && r && r.next && /^\/hub-transfer\/import\/up-[0-9a-f]{16}$/.test(r.next)) {
        say("Đã tải lên — đang kiểm gói và lập kế hoạch…");
        location.assign(r.next);
        return;
      }
      if (btn) btn.disabled = false;
      say("Không tải lên được (" + xhr.status + "): " + ((r && r.error) || xhr.responseText || "lỗi"));
    };
    xhr.onerror = function () { if (btn) btn.disabled = false; say("Mất kết nối khi tải lên — thử lại."); };
    xhr.send(f);
  });
})();
