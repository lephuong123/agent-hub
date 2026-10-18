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
})();
