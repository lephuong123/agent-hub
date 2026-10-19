/* cfharness web — chạy trong <head> (trước khi vẽ) để không nháy: đánh dấu có JS + nhớ thanh bên thu gọn (tiện ích theo người xem). */
(function () {
  "use strict";
  var d = document.documentElement;
  d.classList.add("js");
  try { if (localStorage.getItem("cfh_side") === "min") d.classList.add("side-min"); } catch (_) {}
  try { if (localStorage.getItem("cfh_mgr_info") === "closed") d.classList.add("mgr-info-closed"); } catch (_) {}
})();
