/* Island Điều phối — ô nhập. MỘT ô nhập cho cả trang, gắn với khung đang hiện: chữ nháp (`p.draft`, sessionStorage) và tệp đính kèm
 * (`p.files`, File trong bộ nhớ) nằm ở store theo khung → đổi tab/hội thoại rồi quay lại vẫn còn nguyên.
 *
 * Giữ đúng id của trang cũ (#mgr-form, #mgr-text, #mgr-mic, #mgr-voice, input[name=voice], #mgr-err) → static/voice.js chạy không đổi:
 * nó điền chữ vào #mgr-text + bắn `input`, đặt input[name=voice], rồi requestSubmit() form này.
 */
import { h } from "preact";
import { useRef, useLayoutEffect } from "preact/hooks";
import htm from "htm";
import { LOBBY, addFiles, isImg, humanSize, MAX_TEXT } from "./util.js";
import { ui, Icon, jumpBottom } from "./views.js";

const html = htm.bind(h);
const coarse = typeof matchMedia === "function" ? matchMedia("(pointer: coarse)") : null;

function grow(t) {
  if (!t) return;
  const cap = parseFloat(getComputedStyle(t).maxHeight) || 240;
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight + 2, cap) + "px";
}

function Thumb({ f }) {
  const ref = useRef(null);
  useLayoutEffect(() => {
    if (!ref.current || !window.FileReader) return;
    const rd = new FileReader(), im = ref.current;
    rd.onload = () => { im.src = rd.result; };
    rd.readAsDataURL(f);
  }, [f]);
  return html`<img ref=${ref} alt="" />`;
}

function Files({ p }) {
  const files = p.files.value;
  return html`<ul class="mgr-files" id="mgr-files" hidden=${!files.length} aria-label="Tệp đính kèm">${files.map((f, i) => {
    const dot = f.name.lastIndexOf(".");
    return html`<li key=${f.name + ":" + f.size + ":" + i}>
      ${isImg(f) ? html`<${Thumb} f=${f} />` : html`<span class="fi-ic">${dot > 0 ? f.name.slice(dot + 1, dot + 5) : "txt"}</span>`}
      <span class="fi-name" title=${f.name}>${f.name}</span><span class="fi-size">${humanSize(f.size)}</span>
      <button type="button" title=${"Bỏ " + f.name} aria-label=${"Bỏ " + f.name} onClick=${() => { const n = files.slice(); n.splice(i, 1); p.files.value = n; }}>×</button></li>`;
  })}</ul>`;
}

const SUGGEST_LOBBY = [
  ["inbox", "Có gì cần tôi?", "Có việc gì đang kẹt cần tôi không?"],
  ["home", "Tình hình hôm nay", "Tóm tắt tình hình các agent và task hôm nay."],
  ["dollar", "Chi phí tuần này", "Chi phí tuần này của từng team là bao nhiêu?"],
  ["task", "Giao việc mới…", "Giao việc mới: "],
];
const SUGGEST_WS = [
  ["home", "Tình hình luồng", "Tình hình luồng này: task nào xong, đang chạy, bị chặn? PR/nhánh đến đâu?"],
  ["inbox", "Còn gì kẹt?", "Trong luồng này còn gì kẹt cần tôi quyết không?"],
  ["task", "Cập nhật STATE", "Cập nhật STATE.md của luồng này cho đúng hiện trạng (quyết định, PR, câu hỏi mở, bước tiếp)."],
  ["task", "Giao việc mới…", "Giao việc mới trong luồng này: "],
];

export function Composer({ p }) {
  const ta = useRef(null), form = useRef(null), fin = useRef(null);
  const draft = p.draft.value, files = p.files.value, err = p.sendErr.value;
  const tabs = ui.store.tabs.value;
  const wsTab = p.scope !== LOBBY ? [...tabs.active, ...tabs.closed].find((t) => t.ws === p.scope) : null;
  const busy = p.busy.value;  // computed: KHÔNG vẽ lại ô nhập mỗi khung hình khi chữ đang chạy
  const canStop = busy && !draft.trim() && !files.length;
  useLayoutEffect(() => { grow(ta.current); }, [p, draft]);

  function setErr(m) { p.sendErr.value = m || ""; }
  function add(list) {
    const r = addFiles(p.files.value, Array.from(list || []));
    p.files.value = r.files;
    setErr(r.errors.join("; "));
  }
  function submit(e) {
    e.preventDefault();
    const t = ta.current ? ta.current.value : draft;
    if (!t.trim() && !p.files.value.length) { if (busy) ui.stop(p); return; }
    if (t.length > MAX_TEXT) { setErr("Tin quá dài (" + t.length + " ký tự, tối đa " + MAX_TEXT + ")"); return; }
    const vi = form.current && form.current.querySelector('input[name="voice"]');
    ui.send(p, t, p.files.value, vi ? vi.value : "");
  }
  function onKey(e) {
    // Esc (ô trống): dừng lượt đang trả lời. Enter gửi; Shift+Enter xuống dòng; màn cảm ứng: Enter = xuống dòng; không gửi khi đang gõ dấu (IME)
    if (e.key === "Escape" && busy && !e.currentTarget.value.trim()) { e.preventDefault(); ui.stop(p); return; }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229 && !(coarse && coarse.matches)) {
      e.preventDefault();
      if (form.current.requestSubmit) form.current.requestSubmit(); else submit(e);
    }
  }
  function onPaste(e) {
    const fs = e.clipboardData && e.clipboardData.files;
    if (!fs || !fs.length) return;  // dán chữ: để trình duyệt tự xử lý
    e.preventDefault();
    const out = [];
    for (let f of fs) {
      // ảnh chụp màn hình dán vào thường tên "image.png" → đặt tên dễ phân biệt
      if (isImg(f) && /^image\.\w+$/.test(f.name)) {
        try { f = new File([f], "anh-dan-" + (p.files.value.length + out.length + 1) + "." + f.name.split(".").pop(), { type: f.type }); } catch (_) {}
      }
      out.push(f);
    }
    add(out);
  }
  let dropT = 0;
  function onDrag(e) {
    if (!e.dataTransfer || Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") < 0) return;
    e.preventDefault();
    const f = e.currentTarget;
    f.classList.add("drop"); clearTimeout(dropT);
    dropT = setTimeout(() => f.classList.remove("drop"), 150);
  }
  function onDrop(e) {
    if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
    e.preventDefault(); e.currentTarget.classList.remove("drop"); add(e.dataTransfer.files);
  }
  const sugg = wsTab ? SUGGEST_WS : SUGGEST_LOBBY;
  return html`<div class="composer-wrap">
    <button type="button" class="mgr-jump" id="mgr-jump" hidden=${!p.jump.value} title="Về tin mới nhất" aria-label="Về tin mới nhất"
      onClick=${() => jumpBottom(p, true)}><${Icon} n="chev-down" c="sm" /><span>Tin mới nhất</span></button>
    <form method="post" action="/manager/send" class="mgr-form" id="mgr-form" enctype="multipart/form-data" ref=${form}
          onSubmit=${submit} onDragOver=${onDrag} onDrop=${onDrop}>
      <input type="hidden" name="voice" value="" />
      <label class="sr-only" for="mgr-text">Tin nhắn cho điều phối viên</label>
      <${Files} p=${p} />
      <textarea id="mgr-text" name="text" rows="1" maxlength=${MAX_TEXT} ref=${ta} value=${draft}
        placeholder=${wsTab ? "Nhắn trong " + (wsTab.key || wsTab.title) + "…" : "Nhắn điều phối viên…"}
        onInput=${(e) => ui.store.setDraft(p, e.currentTarget.value)} onKeyDown=${onKey} onPaste=${onPaste}></textarea>
      <div class="composer-bar">
        <input type="file" id="mgr-file" multiple class="sr-only" tabindex="-1" ref=${fin} onChange=${(e) => { add(e.currentTarget.files); e.currentTarget.value = ""; }} />
        <label for="mgr-file" class="btn ghost sm icon attach" id="mgr-attach" tabindex="0" role="button"
          title="Đính kèm ảnh, tệp văn bản, PDF/Excel/Word/PowerPoint (hoặc kéo–thả, dán ảnh)"
          onKeyDown=${(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fin.current.click(); } }}><${Icon} n="clip" /><span class="sr-only">Đính kèm tệp</span></label>
        <button type="button" class="btn ghost sm icon voice-mic" id="mgr-mic" data-voice-for="mgr-text" hidden aria-pressed="false" aria-label="Nhập bằng giọng nói"
          title="Nói để nhập (tiếng Việt) — bấm để bật/tắt, giữ để nói-thả; hoặc giữ Space khi ô trống"><${Icon} n="mic" /></button>
        <span class="composer-hint" title="Enter để gửi · Shift+Enter xuống dòng · Esc dừng · giữ Space (ô trống) để nói · kéo–thả hoặc dán ảnh để đính kèm">Enter gửi · Shift+Enter xuống dòng · Esc dừng</span>
        <button type="button" class="btn ghost sm icon voice-wave" id="mgr-voice" hidden aria-pressed="false" aria-label="Trò chuyện bằng giọng nói"
          title="Trò chuyện bằng giọng nói — nói, điều phối viên trả lời và đọc to"><${Icon} n="wave" /></button>
        <button class=${"btn primary send" + (canStop ? " stop" : "")} type="submit" id="mgr-send" title=${canStop ? "Dừng trả lời (Esc)" : "Gửi"} aria-label=${canStop ? "Dừng trả lời" : "Gửi"}
          disabled=${!busy && !draft.trim() && !files.length}><${Icon} n="arrow-up" c="i-send" /><${Icon} n="stop" c="i-stop" /></button>
      </div>
    </form>
    <div class="mgr-suggest" id="mgr-suggest">${sugg.map(([ic, label, fill]) =>
      html`<button type="button" key=${label} onClick=${() => ui.fill(p, fill, false)}><${Icon} n=${ic} c="sm" />${label}</button>`)}</div>
    <p class="mgr-err flash error" id="mgr-err" role="alert" hidden=${!err}>${err}</p>
    <p class="chat-disclaimer">Điều phối viên là một agent và có thể nhầm. Thao tác phá huỷ luôn chờ bạn duyệt.</p>
  </div>`;
}
