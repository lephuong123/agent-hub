// SAC: hỏi xác nhận trước thao tác, menu điện thoại, sao chép link hiện một lần, làm mới số liệu tổng quan + tiến trình job.
(function () {
  "use strict";
  document.addEventListener("submit", function (e) {
    var f = e.target;
    var msg = f.getAttribute && f.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { e.preventDefault(); }
  }, true);
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (t.matches && t.matches("[data-toggle=nav]")) { document.getElementById("nav").classList.toggle("open"); }
    if (t.matches && t.matches("[data-copy]")) { t.select(); }
  });
  function get(obj, path) { return path.split(".").reduce(function (o, k) { return o == null ? o : o[k]; }, obj); }
  var ov = document.querySelector("[data-refresh]");
  if (ov) {
    var every = (parseInt(ov.getAttribute("data-every"), 10) || 5) * 1000;
    setInterval(function () {
      if (document.hidden) { return; }
      fetch(ov.getAttribute("data-refresh"), { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
        if (!d) { return; }
        ov.querySelectorAll("[data-k]").forEach(function (el) {
          var v = get(d, el.getAttribute("data-k"));
          if (v === undefined || v === null) { return; }
          el.textContent = (el.getAttribute("data-k").indexOf("cost") === 0) ? "$" + Number(v).toFixed(2) : v;
        });
      }).catch(function () {});
    }, every);
  }
  var job = document.querySelector("[data-reload]");
  if (job) { setTimeout(function () { window.location.reload(); }, (parseInt(job.getAttribute("data-reload"), 10) || 3) * 1000); }
})();
