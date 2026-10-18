/* agent-hub web — JS nhỏ, không phụ thuộc gì ngoài htmx (vendored). Tuân CSP script-src 'self' (không inline/eval).
 * 1. Hộp xác nhận cho form [data-confirm] (thao tác nhạy cảm). Không có JS → server tự hiện trang xác nhận.
 * 2. Luồng live của run (SSE /sse/run/<id>) và "hoạt động gần đây" (SSE /sse/firehose).
 */
(function () {
  "use strict";

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
    if (action === "/logout") { try { localStorage.removeItem(KEY); } catch (_) {} return; }
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
        if (m.kind === "msg" && curKind === "text" && cur) {
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
        if (state) { state.textContent = "■ đã kết thúc"; state.className = "pill muted"; }
        var box = document.querySelector("#steer-form textarea");
        if (!box || !box.value.trim()) setTimeout(function () { location.reload(); }, 800);
      }
    };
    es.onerror = function () { if (state) { state.textContent = "↻ đang nối lại…"; state.className = "pill warn"; } };
    es.onopen = function () { if (state) { state.textContent = "● trực tiếp"; state.className = "pill ok live-dot"; } };
  }

  // ------------------------------------------------------------ firehose tổng quan
  var fh = document.querySelector("[data-sse-firehose]");
  if (fh && window.EventSource) {
    var st = document.getElementById("fh-state");
    var src = new EventSource(fh.getAttribute("data-sse-firehose"));
    src.onopen = function () { if (st) st.textContent = "● trực tiếp"; };
    src.onerror = function () { if (st) st.textContent = "↻ đang nối lại…"; };
    src.onmessage = function (ev) {
      var m;
      try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m.t !== "line") return;
      var li = fromHTML(m.html);
      if (!li) return;
      fh.insertBefore(li, fh.firstChild);
      while (fh.children.length > 120) fh.removeChild(fh.lastChild);
    };
  }
  // ------------------------------------------------------------ Điều phối (trò chuyện với điều phối viên)
  // Mục HTML do server vẽ sẵn (autoescape) — chữ chạy (delta) chỉ gán bằng textContent. Không bao giờ dựng HTML từ chữ của agent.
  var ml = document.querySelector("[data-sse-manager]");
  if (ml) {
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
    function dropEmpty() { var e = document.getElementById("mgr-empty"); if (e) e.remove(); }
    function stick() { return nearBottom(ml); }
    function put(node, atBottom) { if (!node) return; dropEmpty(); ml.appendChild(node); if (atBottom) ml.scrollTop = ml.scrollHeight; }
    function handle(m) {
      var r = m.run, s = stick();
      if (m.t === "tick") { document.body.dispatchEvent(new Event("mgr-tick")); return; }
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
        var a = fromHTML(m.html);
        if (pend[r]) { dropEmpty(); ml.insertBefore(a, pend[r]); } else put(a, s);
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
    if (window.EventSource) {
      var mes = new EventSource(ml.getAttribute("data-sse-manager"));
      mes.onopen = function () { if (mlive) { mlive.textContent = "● trực tiếp"; mlive.className = "pill ok live-dot"; } };
      mes.onerror = function () { if (mlive) { mlive.textContent = "↻ đang nối lại…"; mlive.className = "pill warn"; } };
      mes.onmessage = function (ev) { var m; try { m = JSON.parse(ev.data); } catch (_) { return; } if (m.t === "end") { mes.close(); return; } handle(m); };
    }
    if (mform && window.fetch && window.FormData) {
      if (mtext) mtext.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); if (mform.requestSubmit) mform.requestSubmit(); else mform.submit(); }
      });
      mform.addEventListener("submit", function (e) {
        e.preventDefault();  // khoá phiên `k` đã được gắn ở pha capture (trên)
        if (!mtext.value.trim()) return;
        var btn = mform.querySelector("button[type=submit]");
        if (btn) btn.disabled = true;
        if (merr) merr.hidden = true;
        fetch(mform.getAttribute("action"), { method: "POST", body: new FormData(mform), credentials: "same-origin", headers: { "X-Ahub-Fetch": "1" } })
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
            mtext.value = "";
          })
          .catch(function () { if (merr) { merr.textContent = "Không gửi được — kiểm tra kết nối rồi thử lại."; merr.hidden = false; } })
          .then(function () { if (btn) btn.disabled = false; mtext.focus(); });
      });
    }
  }
})();
