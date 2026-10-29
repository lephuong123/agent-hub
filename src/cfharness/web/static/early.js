/* cfharness web — chạy trong <head> (trước khi vẽ) để không nháy: đánh dấu có JS + nhớ thanh bên thu gọn (tiện ích theo người xem)
 * + chế độ 3D "Văn phòng" (`cfh_view`, docs/dev-notes/office-3d.md): nhớ tới khi đăng nhập lại; đang 3D thì MỌI trang → /office. */
(function () {
  "use strict";
  var d = document.documentElement;
  d.classList.add("js");
  try { if (localStorage.getItem("cfh_side") === "min") d.classList.add("side-min"); } catch (_) {}
  try { if (localStorage.getItem("cfh_mgr_info") === "closed") d.classList.add("mgr-info-closed"); } catch (_) {}
  try {
    if ((location.hash || "").indexOf("#k=") === 0) {
      // vừa đăng nhập: về lại 2D, KHÔNG chuyển trang (giữ fragment cho app.js lấy khoá phiên)
      localStorage.removeItem("cfh_view");
    } else if (localStorage.getItem("cfh_view") === "3d") {
      // chế độ 3D = TOÀN TRANG 3D: mọi trang (trừ /office, /login) chuyển sang văn phòng 3D
      var pth = location.pathname;
      if (pth !== "/office" && pth !== "/login") { location.replace("/office"); return; }
      d.classList.add("view-3d");
    }
  } catch (_) {}
})();
