/* cfharness web — chạy trong <head> (trước khi vẽ) để không nháy: đánh dấu có JS + nhớ thanh bên thu gọn (tiện ích theo người xem)
 * + chuyển link cũ /settings#<id> sang tab Cài đặt mới + chế độ 3D "Văn phòng" (`cfh_view`, docs/dev-notes/office-3d.md): nhớ tới khi đăng nhập lại; đang 3D thì MỌI trang → /office. */
(function () {
  "use strict";
  var d = document.documentElement;
  d.classList.add("js");
  try { if (localStorage.getItem("cfh_side") === "min") d.classList.add("side-min"); } catch (_) {}
  try { var mi = localStorage.getItem("cfh_mgr_info"); if (mi === "closed" || (mi !== "open" && window.innerWidth < 1700)) d.classList.add("mgr-info-closed"); } catch (_) {}
  // thanh bên: chiều cao phần Luồng việc (đường chia kéo được) + nhóm hội thoại đang gập — áp trước khi vẽ để không nhảy
  try { var nh = parseInt(localStorage.getItem("cfh_side_nav_h"), 10); if (nh > 0) { d.style.setProperty("--side-nav-h", nh + "px"); d.classList.add("side-nav-sized"); } } catch (_) {}
  try { ["ws", "recent"].forEach(function (k) { if (localStorage.getItem("cfh_sf_" + k) === "1") d.classList.add("sf-" + k); }); } catch (_) {}
  // link cũ #slack / #slack2 (Slack từng nằm trên trang Thông báo & kênh) → trang riêng /settings/notifications/slack
  // (#slack = webhook một chiều → mục Nâng cao "#webhook"; #slack2 = 2 chiều → đầu trang). Không JS: neo vẫn nằm trên thẻ Slack.
  try {
    var SLACK_OLD = { slack: "#webhook", slack2: "" };
    var oh = (location.hash || "").slice(1);
    if ((location.pathname === "/settings" || location.pathname === "/settings/notifications") && Object.prototype.hasOwnProperty.call(SLACK_OLD, oh)) {
      location.replace("/settings/notifications/slack" + SLACK_OLD[oh]); return;
    }
  } catch (_) {}
  // link cũ /settings#<id> (thẻ của trang Cài đặt dài trước đây) → tab /settings/<tab>, giữ #id (id cũ vẫn là neo trong tab mới)
  try {
    var sh = location.pathname === "/settings" ? (location.hash || "").slice(1) : "";
    if (sh) {
      var SET_TAB = { estop: "safety", gates: "safety", danger: "safety", accounts: "accounts", claudeai: "accounts", config: "config",
        "cfg-notify": "notifications", notify: "notifications", slack: "notifications", slack2: "notifications", secrets: "secrets",
        filehub: "filehub", "filehub-lane": "filehub", devices: "security", "cfg-security": "security", health: "system",
        "health-slot": "system", update: "system", system: "system", backup: "backup", "hub-transfer-link": "backup", support: "support",
        "desktop-app": "desktop" };
      var st = Object.prototype.hasOwnProperty.call(SET_TAB, sh) ? SET_TAB[sh] : /^acct-/.test(sh) ? "accounts" : /^cfg-/.test(sh) ? "config" : "";
      if (st) { location.replace("/settings/" + st + "#" + sh); return; }
    }
  } catch (_) {}
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
