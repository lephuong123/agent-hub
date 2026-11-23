/* Island Điều phối — chạy chữ markdown của lượt đang trả lời: streaming-markdown (parser tăng dần → node DOM, chữ luôn là text node) +
 * DOMPurify kiểm từng thuộc tính URL/lớp trước khi gắn. Chỉ dùng cho bản NHÁP đang chạy; `msg.done` thay bằng HTML server (present.md).
 * Chỉ CHỖ NÀY (và `it.html` của views) tạo phần tử từ chữ của agent. */
import * as smd from "streaming-markdown";
import DOMPurify from "dompurify";
import { safeUrl } from "./util.js";

const ATTR = { [smd.HREF]: "href", [smd.SRC]: "src", [smd.LANG]: "class" };

function renderer(root) {
  const r = smd.default_renderer(root), set = r.set_attr;
  r.set_attr = (data, type, value) => {
    const el = data.nodes[data.index], name = ATTR[type];
    if (!name) return set(data, type, value);  // checked / start: không phải chuỗi tự do
    let v = name === "class" ? "language-" + String(value).replace(/[^\w+-]/g, "").slice(0, 32) : safeUrl(value);
    if (!v || !DOMPurify.isValidAttribute(el.tagName.toLowerCase(), name, v)) return;
    set(data, type, v);
    if (name === "href") { el.setAttribute("target", "_blank"); el.setAttribute("rel", "noopener noreferrer nofollow"); }
  };
  return r;
}

/** Bộ chạy chữ gắn vào `el`: `write(toàn bộ chữ hiện có)` chỉ đẩy PHẦN MỚI vào parser (gọi một lần mỗi khung hình); chữ đổi khác đầu → làm lại. */
export function liveMd(el) {
  let done = "", p = null;
  function reset() { el.textContent = ""; done = ""; p = smd.parser(renderer(el)); }
  reset();
  return {
    write(text) {
      text = text || "";
      if (text.length < done.length || text.slice(0, done.length) !== done) reset();
      if (text.length > done.length) { smd.parser_write(p, text.slice(done.length)); done = text; }
    },
  };
}
