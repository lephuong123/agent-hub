/* cfharness web — đồ thị graph memory (/memory/graph/view). SVG + bố cục lực tự viết, không thư viện ngoài.
 * Tuân CSP script-src 'self': không inline, không eval. Chữ memory là DỮ LIỆU không tin được → chỉ gán textContent /
 * setAttribute cho thuộc tính không chạy được (không gán HTML thô, không href từ dữ liệu ngoài đường dẫn nội bộ đã encode).
 * Dữ liệu: GET data-src (JSON của RPC learn.graph_view): {nodes:[{id,name,type,scope,owner,facts,blocked,...}],
 * edges:[{id,src,dst,relation,fact,valid,confidence,blocked,...}], center, truncated}.
 */
(function () {
  "use strict";
  var host = document.getElementById("mg");
  if (!host) return;
  var NS = "http://www.w3.org/2000/svg";
  var statusEl = document.getElementById("mg-status");
  var side = {
    title: document.getElementById("mg-side-title"), sub: document.getElementById("mg-side-sub"),
    links: document.getElementById("mg-side-links"), facts: document.getElementById("mg-side-facts")
  };

  function el(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    if (attrs) for (var k in attrs) if (Object.prototype.hasOwnProperty.call(attrs, k)) e.setAttribute(k, String(attrs[k]));
    if (parent) parent.appendChild(e);
    return e;
  }
  function h(tag, cls, text, parent) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = String(text);
    if (parent) parent.appendChild(e);
    return e;
  }
  function setStatus(t) { if (statusEl) { statusEl.textContent = t; statusEl.hidden = !t; } }
  function name(n) { return n && n.name != null ? n.name : "(tên bị ẩn)"; }
  function link(href, text, parent, cls) { var a = h("a", cls || "btn sm ghost", text, parent); a.setAttribute("href", href); return a; }

  // ------------------------------------------------------------------ tải dữ liệu
  var src = host.getAttribute("data-src") || "";
  if (src.charAt(0) !== "/" || src.charAt(1) === "/") { setStatus("Nguồn dữ liệu không hợp lệ."); return; }
  fetch(src, { credentials: "same-origin", headers: { "Accept": "application/json" } }).then(function (r) {
    if (r.status === 401) throw new Error("Phiên đã hết — tải lại trang để đăng nhập.");
    return r.json().then(function (j) { if (!r.ok) throw new Error((j && j.error) || ("Lỗi " + r.status)); return j; });
  }).then(draw).catch(function (e) { setStatus(String(e && e.message || e)); });

  // ------------------------------------------------------------------ vẽ
  function draw(data) {
    var nodes = (data.nodes || []).map(function (n, i) {
      var a = 2 * Math.PI * i / Math.max(1, (data.nodes || []).length);
      var rr = 60 + 12 * Math.sqrt(i);
      return { d: n, id: n.id, x: Math.cos(a) * rr, y: Math.sin(a) * rr, vx: 0, vy: 0, fixed: false,
               r: 7 + 3.2 * Math.sqrt(Math.max(1, n.facts || n.degree || 1)) };
    });
    var byId = {};
    nodes.forEach(function (n) { byId[n.id] = n; });
    var edges = (data.edges || []).filter(function (e) { return byId[e.src]; });
    var links = edges.filter(function (e) { return e.dst && byId[e.dst] && e.dst !== e.src; });
    if (!nodes.length) { setStatus("Không có thực thể nào khớp bộ lọc."); return; }
    setStatus(data.truncated ? "Đã cắt bớt ở " + nodes.length + " nút — thu hẹp bộ lọc để xem đủ." : "");
    if (data.center && byId[data.center]) { byId[data.center].x = 0; byId[data.center].y = 0; }

    // cạnh song song (cùng cặp nút) → cong dần để không chồng nhau
    var pairCount = {};
    links.forEach(function (e) {
      var k = e.src < e.dst ? e.src + "|" + e.dst : e.dst + "|" + e.src;
      e._k = (pairCount[k] = (pairCount[k] || 0) + 1) - 1;
    });

    var svg = el("svg", { "class": "mg-svg", width: "100%", height: "100%", "aria-hidden": "true" });
    host.appendChild(svg);
    var vp = el("g", { "class": "mg-vp" }, svg);
    var gEdges = el("g", { "class": "mg-edges" }, vp);
    var gLabels = el("g", { "class": "mg-labels" }, vp);
    var gNodes = el("g", { "class": "mg-nodes" }, vp);
    var view = { x: 0, y: 0, s: 1 }, drag = null, dragMoved = false, fitted = false, running = false, selected = null;

    links.forEach(function (e) {
      e._p = el("path", { "class": "mg-edge" + (e.valid ? "" : " invalid") + (e.blocked ? " blocked" : "") }, gEdges);
      var t = el("title", null, e._p);
      t.textContent = (e.relation || "?") + (e.blocked ? " — (bị ẩn: không qua quét an toàn)" : e.fact ? " — " + e.fact : "");
      e._t = el("text", { "class": "mg-elabel" + (e.valid ? "" : " invalid"), "text-anchor": "middle" }, gLabels);
      e._t.textContent = e.relation || "?";
    });
    nodes.forEach(function (n) {
      var g = el("g", { "class": "mg-node t-" + (n.d.scope || "agent") + (n.id === data.center ? " center" : "") + (n.d.blocked ? " blocked" : ""), tabindex: "0", role: "button" }, gNodes);
      el("circle", { r: n.r.toFixed(1) }, g);
      var tt = el("title", null, g);
      tt.textContent = name(n.d) + " · " + (n.d.scope === "global" ? "toàn hub" : n.d.scope + " " + n.d.owner) + " · " + (n.d.facts || 0) + " fact";
      var lab = el("text", { "class": "mg-nlabel", dy: (n.r + 12).toFixed(1), "text-anchor": "middle" }, g);
      var nm = name(n.d);
      lab.textContent = nm.length > 28 ? nm.slice(0, 27) + "…" : nm;
      n._g = g;
      g.addEventListener("keydown", function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); select(n); } });
      g.addEventListener("pointerdown", function (ev) { startDrag(ev, n); });
    });

    // ------------------------------------------------------------------ bố cục lực
    var alpha = 1;
    function tick() {
      var i, j, a, b, dx, dy, d2, d, f;
      var k = 90;
      for (i = 0; i < nodes.length; i++) {
        a = nodes[i];
        for (j = i + 1; j < nodes.length; j++) {
          b = nodes[j];
          dx = a.x - b.x; dy = a.y - b.y; d2 = dx * dx + dy * dy;
          if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.5; }
          if (d2 > 250000) continue;
          d = Math.sqrt(d2);
          f = (k * k) / d2 * 0.6;
          var minD = a.r + b.r + 14;
          if (d < minD) f += (minD - d) * 0.5 / d;
          a.vx += dx * f / d; a.vy += dy * f / d; b.vx -= dx * f / d; b.vy -= dy * f / d;
        }
      }
      links.forEach(function (e) {
        a = byId[e.src]; b = byId[e.dst];
        dx = b.x - a.x; dy = b.y - a.y; d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        f = (d - k) * 0.04;
        a.vx += dx / d * f; a.vy += dy / d * f; b.vx -= dx / d * f; b.vy -= dy / d * f;
      });
      nodes.forEach(function (n) {
        n.vx -= n.x * 0.006; n.vy -= n.y * 0.006;
        if (n.fixed) { n.vx = 0; n.vy = 0; return; }
        var sp = Math.sqrt(n.vx * n.vx + n.vy * n.vy), cap = 30 * alpha + 1;
        if (sp > cap) { n.vx *= cap / sp; n.vy *= cap / sp; }
        n.x += n.vx * alpha; n.y += n.vy * alpha;
        n.vx *= 0.55; n.vy *= 0.55;
      });
      alpha *= 0.985;
    }
    function render() {
      links.forEach(function (e) {
        var a = byId[e.src], b = byId[e.dst];
        var mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        var dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
        var off = e._k ? (e._k % 2 ? 1 : -1) * Math.ceil(e._k / 2) * 22 : 0;
        var cx = mx - dy / d * off, cy = my + dx / d * off;
        e._p.setAttribute("d", "M" + a.x.toFixed(1) + "," + a.y.toFixed(1) + " Q" + cx.toFixed(1) + "," + cy.toFixed(1) + " " + b.x.toFixed(1) + "," + b.y.toFixed(1));
        e._t.setAttribute("x", ((mx + cx) / 2).toFixed(1));
        e._t.setAttribute("y", ((my + cy) / 2 - 3).toFixed(1));
      });
      nodes.forEach(function (n) { n._g.setAttribute("transform", "translate(" + n.x.toFixed(1) + "," + n.y.toFixed(1) + ")"); });
    }
    function loop() {
      for (var s = 0; s < 3 && alpha > 0.01; s++) tick();
      render();
      if (alpha > 0.01) requestAnimationFrame(loop); else { running = false; if (!fitted) { fit(); fitted = true; } }
    }
    function heat(v) { alpha = Math.max(alpha, v); if (!running) { running = true; requestAnimationFrame(loop); } }
    // làm nóng trước vài trăm bước (không vẽ) để hình hiện ra gần ổn định
    var pre = Math.min(250, Math.max(60, 30000 / Math.max(1, nodes.length)));
    for (var p = 0; p < pre; p++) tick();
    render();
    fit();
    heat(Math.max(alpha, 0.2));

    // ------------------------------------------------------------------ khung nhìn: kéo nền, cuộn phóng to
    function applyView() { vp.setAttribute("transform", "translate(" + view.x.toFixed(1) + "," + view.y.toFixed(1) + ") scale(" + view.s.toFixed(4) + ")"); }
    function size() { var r = svg.getBoundingClientRect(); return { w: r.width || 800, h: r.height || 560, l: r.left, t: r.top }; }
    function fit() {
      var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      nodes.forEach(function (n) { minX = Math.min(minX, n.x - n.r); minY = Math.min(minY, n.y - n.r); maxX = Math.max(maxX, n.x + n.r); maxY = Math.max(maxY, n.y + n.r + 16); });
      var sz = size(), w = Math.max(1, maxX - minX), hh = Math.max(1, maxY - minY);
      view.s = Math.max(0.15, Math.min(2.2, Math.min((sz.w - 40) / w, (sz.h - 40) / hh)));
      view.x = sz.w / 2 - (minX + w / 2) * view.s;
      view.y = sz.h / 2 - (minY + hh / 2) * view.s;
      applyView();
    }
    function zoomAt(f, cx, cy) {
      var ns = Math.max(0.1, Math.min(5, view.s * f));
      view.x = cx - (cx - view.x) * ns / view.s; view.y = cy - (cy - view.y) * ns / view.s; view.s = ns;
      applyView();
    }
    function toGraph(ev) { var sz = size(); return { x: (ev.clientX - sz.l - view.x) / view.s, y: (ev.clientY - sz.t - view.y) / view.s }; }
    svg.addEventListener("wheel", function (ev) {
      ev.preventDefault();
      var sz = size();
      zoomAt(ev.deltaY < 0 ? 1.12 : 1 / 1.12, ev.clientX - sz.l, ev.clientY - sz.t);
    }, { passive: false });
    function startDrag(ev, n) {
      ev.stopPropagation();
      dragMoved = false;
      drag = { node: n, sx: ev.clientX, sy: ev.clientY };
      n.fixed = true;
      try { svg.setPointerCapture(ev.pointerId); } catch (e) { /* bỏ qua */ }
    }
    svg.addEventListener("pointerdown", function (ev) {
      if (drag) return;
      dragMoved = false;
      drag = { pan: true, sx: ev.clientX, sy: ev.clientY, vx: view.x, vy: view.y };
      try { svg.setPointerCapture(ev.pointerId); } catch (e) { /* bỏ qua */ }
    });
    svg.addEventListener("pointermove", function (ev) {
      if (!drag) return;
      if (Math.abs(ev.clientX - drag.sx) + Math.abs(ev.clientY - drag.sy) > 3) dragMoved = true;
      if (drag.pan) { view.x = drag.vx + ev.clientX - drag.sx; view.y = drag.vy + ev.clientY - drag.sy; applyView(); return; }
      var g = toGraph(ev);
      drag.node.x = g.x; drag.node.y = g.y;
      render();
      heat(0.15);
    });
    function endDrag() {
      // chọn nút ở đây (không dùng "click"): pointer capture trên <svg> làm click rơi vào svg chứ không vào nút
      if (drag && drag.node) { drag.node.fixed = false; if (!dragMoved) select(drag.node); }
      drag = null;
    }
    svg.addEventListener("pointerup", endDrag);
    svg.addEventListener("pointercancel", endDrag);
    var zi = document.getElementById("mg-zoom-in"), zo = document.getElementById("mg-zoom-out"), zf = document.getElementById("mg-fit");
    if (zi) zi.addEventListener("click", function () { var s = size(); zoomAt(1.25, s.w / 2, s.h / 2); });
    if (zo) zo.addEventListener("click", function () { var s = size(); zoomAt(0.8, s.w / 2, s.h / 2); });
    if (zf) zf.addEventListener("click", fit);
    var inv = document.getElementById("mg-show-invalid"), lb = document.getElementById("mg-labels");
    if (inv) inv.addEventListener("change", function () { svg.classList.toggle("hide-invalid", !inv.checked); });
    if (lb) lb.addEventListener("change", function () { svg.classList.toggle("hide-labels", !lb.checked); });

    // ------------------------------------------------------------------ panel bên: fact của nút
    function select(n) {
      if (selected) selected._g.classList.remove("sel");
      selected = n; n._g.classList.add("sel");
      side.title.textContent = name(n.d);
      side.sub.textContent = (n.d.type ? n.d.type + " · " : "") + (n.d.scope === "global" ? "toàn hub" : n.d.scope + " " + n.d.owner) +
        (n.d.aliases && n.d.aliases.length ? " · alias: " + n.d.aliases.join(", ") : "") + (n.d.blocked ? " · một phần văn bản bị ẩn (quét an toàn)" : "");
      while (side.links.firstChild) side.links.removeChild(side.links.firstChild);
      var hist = /[?&]history=1/.test(src) ? "&history=1" : "";
      link("/memory/graph/entity/" + encodeURIComponent(n.id), "Trang thực thể", side.links);
      link("/memory/graph/view?entity=" + encodeURIComponent(n.id) + hist, "Vẽ quanh nút này", side.links);
      while (side.facts.firstChild) side.facts.removeChild(side.facts.firstChild);
      var mine = edges.filter(function (e) { return e.src === n.id || e.dst === n.id; });
      mine.sort(function (a, b) { return (a.valid === b.valid ? 0 : a.valid ? -1 : 1) || (b.confidence || 0) - (a.confidence || 0); });
      if (!mine.length) h("li", "muted", "Không có fact nào trong phạm vi đang vẽ.", side.facts);
      mine.forEach(function (e) {
        var li = h("li", e.valid ? "" : "muted", null, side.facts);
        var head = h("div", "mg-f-head", null, li);
        var other = e.src === n.id ? (e.dst ? byId[e.dst] : null) : byId[e.src];
        h("span", "g-rel", e.relation || "?", head);
        if (other) { head.appendChild(document.createTextNode(e.src === n.id ? " → " : " ← ")); h("b", null, name(other.d), head); }
        if (!e.valid) h("span", "pill muted", "lịch sử", head);
        var body = h("a", "mg-f-text", e.blocked ? "(bị ẩn: không qua quét an toàn lúc nạp)" : (e.fact || ""), li);
        body.setAttribute("href", "/memory/graph/fact/" + encodeURIComponent(e.id));
        h("div", "xs muted", (e.valid_from || "?").slice(0, 10) + " → " + ((e.valid_to || "").slice(0, 10) || (e.valid ? "nay" : "đã đóng")) +
          " · tin cậy " + Number(e.confidence || 0).toFixed(2) + " · " + (e.created_by || "?"), li);
      });
    }
    if (data.center && byId[data.center]) select(byId[data.center]);
  }
})();
