/* Island Điều phối — giao diện (Preact + htm, không bước build). Dùng lại class CSS của trang cũ (app.css) để cùng hình.
 *
 * An toàn: chữ của agent/người luôn là children của htm (Preact gán textContent — tự escape). HTML DUY NHẤT được chèn là `it.html` của mục
 * `manager` (present.md phía server đã cứng hoá: không raw HTML, link lọc, không style=). Link nội bộ chỉ từ `target.href` do server dựng.
 * Thao tác ghi khác (đổi tên, đóng luồng, chạy nhịp…) là <form method=post action=/do/…> như trang cũ: app.js gắn khoá phiên `k` + hộp xác nhận.
 */
import { h } from "preact";
import { useRef, useLayoutEffect, useEffect } from "preact/hooks";
import htm from "htm";
import { LOBBY, fmtTs, relText, paneUrl } from "./util.js";
import { Composer } from "./composer.js";

export const html = htm.bind(h);

/** Cầu nối tới main.js (điều hướng, gửi, dừng…) — main gán các hàm vào đây trước khi vẽ. */
export const ui = { icons: "/static/icons.svg", csrf: "", store: null };

export function Icon({ n, c }) {
  return html`<svg class=${"ic " + (c || "")} aria-hidden="true" focusable="false"><use href=${ui.icons + "#i-" + n}></use></svg>`;
}

export function Time({ ts }) {
  if (!ts) return null;
  return html`<time datetime=${ts} data-rel title=${fmtTs(ts)}>${relText(ts)}</time>`;
}

function Hidden({ name, value }) { return html`<input type="hidden" name=${name} value=${value == null ? "" : value} />`; }

/** Nút thao tác /do/<name> (form POST thật — confirm + khoá phiên do app.js). */
export function Act({ name, label, fields, cls, confirm, next, ic, title }) {
  return html`<form method="post" action=${"/do/" + name} class="inline" data-confirm=${confirm || undefined}>
    <${Hidden} name="csrf" value=${ui.csrf} />
    ${Object.keys(fields || {}).map((k) => html`<${Hidden} key=${k} name=${k} value=${fields[k]} />`)}
    <${Hidden} name="next" value=${next || location.pathname + location.search} />
    <button type="submit" class=${cls || "btn"} title=${title || undefined}>${ic ? html`<${Icon} n=${ic} />` : null}<span>${label}</span></button>
  </form>`;
}

function onNav(e, url) {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  ui.go(url, true);
}

// ------------------------------------------------------------------ danh sách hội thoại (thanh bên + menu trên màn hẹp)
export function ConvList({ limit }) {
  const s = ui.store, p = s.active.value, scope = p ? p.scope : LOBBY;
  const tabs = s.tabs.value, rows = (s.convs.value[scope] || []).slice(0, limit || 30);
  const wsTab = scope !== LOBBY ? [...tabs.active, ...tabs.closed].find((t) => t.ws === scope) : null;
  const cur = p && p.conv.value;
  const head = wsTab ? "Hội thoại · " + wsTab.title : tabs.enabled ? "Hội thoại ở Sảnh" : "Gần đây";
  const newUrl = paneUrl(null, scope);
  return html`
    <div class="nav-group ws-head"><span>${head}</span>
      <a class="ws-add" href=${newUrl} onClick=${(e) => onNav(e, newUrl)} title="Hội thoại mới" aria-label="Hội thoại mới">＋</a></div>
    <ul class="mgr-convs">${rows.length ? rows.map((c) => {
      const url = paneUrl(c.id, scope), title = c.title || "Cuộc trò chuyện " + fmtTs(c.updated_at);
      return html`<li key=${c.id}><a href=${url} class=${c.id === cur ? "on" : ""} title=${title} onClick=${(e) => onNav(e, url)}>
        <span class="ct">${c.running ? html`<span class="ws-dot run" title="Đang chạy" aria-hidden="true"></span><span class="sr-only">Đang chạy. </span>` : null}${title}${
          c.unread && c.id !== cur ? html`<span class="ws-unread">${c.unread}</span>` : null}</span>
        <span class="cm"><${Time} ts=${c.updated_at} />${c.count ? " · " + c.count + " tin" : ""}</span></a></li>`;
    }) : html`<li class="muted small side-none">Chưa có hội thoại — gõ tin để bắt đầu.</li>`}</ul>`;
}

// ------------------------------------------------------------------ hàng tab luồng
function Dot({ cls, label }) {
  return html`<span class=${"ws-dot " + (cls || "none")} title=${label || ""} aria-hidden="true"></span><span class="sr-only">${label ? label + ". " : ""}</span>`;
}

function TabBar() {
  const s = ui.store, tabs = s.tabs.value, p = s.active.value;
  if (!tabs.enabled) return null;
  const scope = p ? p.scope : LOBBY, lobby = tabs.lobby || { ws: LOBBY, dot: "none", dot_label: "", unread: 0 };
  const lobbyUrl = "/manager?ui=next";
  const closedCur = tabs.closed.find((t) => t.ws === scope);
  const ws = p && p.workstream.value;
  return html`<nav class="ws-bar" aria-label="Luồng việc">
    <div class="ws-tabs" id="ws-tabs">
      <a class=${"ws-tab" + (scope === LOBBY ? " on" : "")} href=${lobbyUrl} title=${"Sảnh — " + (lobby.dot_label || "")}
         aria-current=${scope === LOBBY ? "page" : undefined} onClick=${(e) => onNav(e, lobbyUrl)}>
        <${Dot} cls=${lobby.dot} label=${lobby.dot_label} />Sảnh${lobby.unread ? html`<span class="ws-unread">${lobby.unread}</span>` : null}</a>
      ${tabs.active.map((t) => {
        const url = "/manager?ui=next&w=" + encodeURIComponent(t.ws), tk = t.tasks || {};
        return html`<a key=${t.ws} class=${"ws-tab" + (scope === t.ws ? " on" : "")} href=${url} data-ws=${t.ws}
          title=${t.label + " — " + (t.dot_label || "") + " · task " + (tk.done || 0) + "/" + (tk.total || 0)}
          aria-current=${scope === t.ws ? "page" : undefined} onClick=${(e) => onNav(e, url)}>
          <${Dot} cls=${t.dot} label=${t.dot_label} />${t.title}${t.unread ? html`<span class="ws-unread">${t.unread}</span>` : null}</a>`;
      })}
      ${closedCur ? html`<a class="ws-tab on closed" href=${"/manager?ui=next&w=" + encodeURIComponent(closedCur.ws)}
          onClick=${(e) => onNav(e, "/manager?ui=next&w=" + encodeURIComponent(closedCur.ws))}><${Dot} cls=${closedCur.dot} label=${closedCur.dot_label} />${closedCur.title} · đã đóng</a>` : null}
    </div>
    ${ws ? html`<${WsManage} ws=${ws} />` : null}
    <details class="ws-pop-wrap" id="ws-new"><summary class="ws-tab add" title="Mở luồng mới (mỗi epic một luồng)">＋ Luồng</summary>
      <form method="post" action="/do/ws_create" class="ws-pop ws-inline"><${Hidden} name="csrf" value=${ui.csrf} /><${Hidden} name="next" value="/manager?ui=next" />
        <input name="title" maxlength="120" placeholder="Tên luồng (bỏ trống = tự đặt)" aria-label="Tên luồng" autocomplete="off" />
        <button class="btn sm primary" type="submit">Mở</button></form>
    </details>
    ${tabs.closed.length ? html`<details class="ws-pop-wrap"><summary class="ws-tab more" title="Luồng đã đóng">Đã đóng ${tabs.closed.length}</summary>
      <div class="ws-pop"><ul class="mgr-convs">${tabs.closed.map((t) => {
        const url = "/manager?ui=next&w=" + encodeURIComponent(t.ws);
        return html`<li key=${t.ws}><a href=${url} onClick=${(e) => onNav(e, url)}><span class="ct">${t.label}</span></a></li>`;
      })}</ul></div></details>` : null}
  </nav>`;
}

function wsWho(by) { return !by || by === "user" ? "bạn" : String(by).startsWith("manager") ? "điều phối viên" : by; }

function WsForms({ ws }) {
  const back = "/manager?ui=next&w=" + encodeURIComponent(ws.id);
  return html`
    <form method="post" action="/do/ws_edit" class="ws-inline"><${Hidden} name="csrf" value=${ui.csrf} /><${Hidden} name="id" value=${ws.id} /><${Hidden} name="next" value=${back} />
      <input name="title" maxlength="120" value=${ws.label} required aria-label="Tên luồng" title="Bắt đầu bằng mã Jira (LH-1 Tên) để đổi cả mã" />
      <button class="btn sm" type="submit">Đổi tên</button></form>
    <div class="btn-row">${ws.status === "active"
      ? html`<${Act} name="ws_close" label="Đóng luồng" fields=${{ id: ws.id, label: ws.label }} cls="btn sm ghost" confirm=${"Đóng luồng " + ws.label + "? Mở lại được bất cứ lúc nào."} next="/manager?ui=next" />`
      : html`<${Act} name="ws_reopen" label="Mở lại luồng" fields=${{ id: ws.id }} cls="btn sm" next=${back} />`}
      <${Act} name="ws_delete" label="Xoá luồng…" fields=${{ id: ws.id, label: ws.label }} cls="btn sm danger"
        confirm=${"XOÁ HẲN luồng " + ws.label + "? Hội thoại về Sảnh, task bỏ gắn luồng (không bị xoá). Không hoàn tác được."} next="/manager?ui=next" /></div>`;
}

function WsManage({ ws }) {
  const tk = ws.tasks || {};
  return html`<details class="ws-pop-wrap" id="ws-manage"><summary class="ws-tab more" title=${"Thông tin, đổi tên, đóng, xoá luồng " + ws.label}>⚙ Luồng này</summary>
    <div class="ws-pop ws-info">
      <div class="ws-side-head"><h3>${ws.title}</h3>${ws.key ? html`<span class="pill muted">${ws.key}</span>` : null}</div>
      <dl class="ws-meta">
        <dt>Trạng thái</dt><dd>${ws.status === "active" ? "Đang mở" : "Đã đóng"}${ws.closed_at ? html` · <${Time} ts=${ws.closed_at} />` : null}</dd>
        <dt>Mã</dt><dd>${ws.key || "—"}</dd>
        ${ws.sprint ? html`<dt>Sprint</dt><dd>${ws.sprint}</dd>` : null}
        <dt>Task</dt><dd>${tk.done || 0}/${tk.total || 0} xong${tk.running ? " · " + tk.running + " đang chạy" : ""}${tk.blocked ? html` · <span class="danger-t">${tk.blocked} bị chặn</span>` : null}</dd>
        <dt>Cần bạn</dt><dd>${ws.needs_human || 0}</dd>
        <dt>Hội thoại</dt><dd>${ws.conversations || 0}</dd>
        <dt>Tạo</dt><dd><${Time} ts=${ws.created_at} /> · ${wsWho(ws.created_by)}</dd>
        <dt>Hoạt động cuối</dt><dd>${ws.last_activity ? html`<${Time} ts=${ws.last_activity} />` : "—"}</dd>
        <dt>Ghi chú luồng</dt><dd>${ws.state_updated_at ? html`sửa <${Time} ts=${ws.state_updated_at} />` : "chưa có"}</dd>
        <dt>ID</dt><dd><code>${ws.id}</code></dd>
      </dl>
      <${WsForms} ws=${ws} />
    </div></details>`;
}

// ------------------------------------------------------------------ đầu khung chat
function LivePill() {
  const v = ui.store.live.value;
  const t = v === "live" ? "● trực tiếp" : v === "connecting" ? "đang kết nối…" : "↻ đang nối lại…";
  return html`<span class=${v === "live" ? "pill ok live-dot" : v === "connecting" ? "pill muted" : "pill warn"} id="mgr-live">${t}</span>`;
}

function ConvMenu({ p }) {
  const s = ui.store, conv = p.conv.value, tabs = s.tabs.value;
  if (!conv || !tabs.enabled) return null;
  const back = paneUrl(conv, p.scope);
  return html`<details class="menu conv-menu"><summary class="btn ghost sm icon" title="Đổi tên, chuyển luồng, lưu trữ" aria-label="Thao tác hội thoại">⋯</summary>
    <div class="menu-pop ws-pop">
      <form method="post" action="/do/conv_rename" class="ws-inline"><${Hidden} name="csrf" value=${ui.csrf} /><${Hidden} name="conversation_id" value=${conv} /><${Hidden} name="next" value=${back} />
        <input name="title" maxlength="80" value=${p.title.value || ""} required aria-label="Tên hội thoại" placeholder="Tên hội thoại" /><button class="btn sm" type="submit">Đổi tên</button></form>
      <form method="post" action="/do/conv_move" class="ws-inline"><${Hidden} name="csrf" value=${ui.csrf} /><${Hidden} name="conversation_id" value=${conv} /><${Hidden} name="next" value=${back} />
        <select name="workstream" aria-label="Chuyển sang luồng"><option value="">Sảnh</option>${tabs.active.map((w) =>
          html`<option key=${w.ws} value=${w.ws} selected=${p.scope === w.ws}>${w.label}</option>`)}</select><button class="btn sm" type="submit">Chuyển</button></form>
      <${Act} name="conv_archive" label="Lưu trữ hội thoại" fields=${{ conversation_id: conv, w: p.scope !== LOBBY ? p.scope : "" }} cls="btn sm ghost danger-t"
        confirm="Lưu trữ hội thoại này?" next="/manager?ui=next" />
    </div></details>`;
}

function Header({ p }) {
  const s = ui.store, m = s.manager.value || {}, tabs = s.tabs.value;
  const scope = p ? p.scope : LOBBY, wsTab = scope !== LOBBY ? [...tabs.active, ...tabs.closed].find((t) => t.ws === scope) : null;
  const conv = p && p.conv.value;
  const cur = conv ? (s.convs.value[scope] || []).find((c) => c.id === conv) : null;
  const title = (p && p.title.value) || (cur && cur.title) || (conv ? "Cuộc trò chuyện" : "Cuộc trò chuyện mới");
  const newUrl = paneUrl(null, scope);
  return html`<header class="chat-head">
    <details class="mgr-convs-m menu"><summary class="btn ghost sm icon" title="Hội thoại" aria-label="Hội thoại"><${Icon} n="menu" /></summary>
      <div class="menu-pop"><${ConvList} limit=${15} /></div></details>
    <div class="chat-title">
      <h1 class="sr-only">Trò chuyện với điều phối viên</h1>
      <span class="ct">${title}</span>
      <span class="chat-sub">${wsTab ? "trong luồng " + wsTab.title : tabs.enabled ? "ở Sảnh"
        : html`với điều phối viên <a href=${"/agents/" + encodeURIComponent(m.agent_id || "")}>${m.name || m.agent_id}</a>`}</span>
    </div>
    <div class="chat-tools">
      <${LivePill} />
      ${p ? html`<${ConvMenu} p=${p} />` : null}
      <a class="btn ghost sm icon" href=${newUrl} onClick=${(e) => onNav(e, newUrl)} title="Hội thoại mới" aria-label="Hội thoại mới"><${Icon} n="pen-new" /></a>
      <button type="button" class="btn ghost sm icon" id="mgr-info-toggle" aria-controls="mgr-side" title="Thông tin" aria-label="Thông tin"><${Icon} n="panel" /></button>
    </div>
  </header>`;
}

// ------------------------------------------------------------------ mục dòng thời gian (bản tối giản — GĐ2 phần 2 thêm chạy chữ markdown, cuộn, thẻ duyệt)
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  return new Promise((ok, fail) => {
    const t = document.createElement("textarea");
    t.value = text; t.setAttribute("readonly", ""); t.className = "sr-only";
    document.body.appendChild(t); t.select();
    try { document.execCommand("copy") ? ok() : fail(); } catch (e) { fail(e); } finally { t.remove(); }
  });
}
function CopyBtn({ text, label }) {
  return html`<button type="button" class="mm-btn" title="Sao chép" aria-label=${label || "Sao chép"} onClick=${(e) => {
    const b = e.currentTarget;
    copyText(text).then(() => { b.classList.add("done"); b.title = "Đã chép"; }, () => { b.title = "Không chép được"; })
      .then(() => setTimeout(() => { b.classList.remove("done"); b.title = "Sao chép"; }, 1500));
  }}><${Icon} n="copy" c="sm" /></button>`;
}

function FileChip({ f, agent }) {
  const q = "agent=" + encodeURIComponent(agent) + "&path=" + encodeURIComponent(f.path);
  return html`<span class="mm-file"><code>${f.name}</code><span class="fchip" data-path=${f.path} data-agent=${agent}>
    <a class="fchip-view" href=${"/files/view?" + q} target="_blank" rel="noopener" title=${"Xem " + f.path + " trong trình duyệt (chỉ đọc — dùng được cả qua link public)"}><${Icon} n="external" c="sm" /><span>Xem</span></a>
    <button type="button" class="fchip-act" data-fopen="open" title=${"Mở " + f.name + " bằng ứng dụng mặc định trên máy chạy hub (chỉ khi dùng web trên chính máy đó)"}><${Icon} n="clip" c="sm" /><span>Mở</span></button>
    <button type="button" class="fchip-act" data-fopen="reveal" title="Mở thư mục chứa file trong Explorer/Finder trên máy chạy hub"><${Icon} n="inbox" c="sm" /><span>Thư mục</span></button>
  </span></span>`;
}

function ActionRow({ a }) {
  const t = a.target || {};
  return html`<div class=${"mm-act" + (a.ok === false ? " err" : "")}><span class="mm-act-ic"><${Icon} n=${a.ok === false ? "x" : "check"} c="sm" /></span>
    <div class="mm-act-body"><div class="mm-act-sum">${a.summary}</div><div class="mm-act-meta">
      ${t.href ? html`<a href=${t.href}>${t.label}</a>` : t.label ? html`<span>${t.label}</span>` : null}
      ${a.tool ? html`<code>${a.tool}</code>` : null}${a.ts ? html`<span>${fmtTs(a.ts)}</span>` : null}
      ${a.error ? html`<span class="danger-t">${a.error}</span>` : null}</div></div></div>`;
}

const Q_STATUS = { with_manager: ["Đang chờ điều phối viên", "info"], open: ["Chờ bạn trả lời", "ask"], answered: ["Đã trả lời", "ok"], cancelled: ["Đã huỷ", "muted"] };

function Question({ q, back }) {
  const [label, tone] = Q_STATUS[q.status] || [q.status || "", "muted"];
  const pending = q.status === "open" || q.status === "with_manager";
  const a = q.answer || {}, keys = a.keys || [], labels = a.labels || [];
  const by = String(q.answered_by || ""), who = by.startsWith("manager:") ? "Điều phối viên" : by === "hub" ? "Hub" : "Bạn";
  const opts = q.options || [];
  return html`<article class=${"xcard ask" + (pending ? "" : " done")} id=${"q-" + q.id}>
    <div class="xcard-head"><span class="xcard-ic"><${Icon} n=${pending ? "question" : "check"} /></span>
      <div class="xcard-title"><div class="t">${q.agent_id} hỏi ${q.status === "with_manager" ? "điều phối viên" : "bạn"} <span class=${"pill " + tone}>${label}</span></div>
        <div class="m"><a href=${"/agents/" + encodeURIComponent(q.agent_id || "")}>${q.agent_id}</a>${q.task_id ? html`<span>·</span><a href=${"/tasks/" + encodeURIComponent(q.task_id)}>task ${q.task_id}</a>` : null}${
          q.run_id ? html`<span>·</span><a href=${"/runs/" + encodeURIComponent(q.run_id)}>lượt chạy</a>` : null}<span>·</span><${Time} ts=${q.created_at} /></div></div></div>
    <div class="q-text">${q.text}</div>
    ${q.escalation_note && q.status === "open" ? html`<div class="q-note"><${Icon} n="chat" /><div class="pre"><b>Ghi chú:</b> ${q.escalation_note}</div></div>` : null}
    ${q.status === "answered" ? html`<div class="q-answer"><${Icon} n="check" /><div class="pre"><b>${who}</b> đã trả lời: ${keys.map((k, i) => k + ". " + (labels[i] || "")).join("; ")}${
      a.text ? (keys.length ? " — " : "") + a.text : ""}</div></div>` : null}
    ${q.status === "open" ? html`<form method="post" action="/do/question_answer" class="q-form">
      <${Hidden} name="csrf" value=${ui.csrf} /><${Hidden} name="id" value=${q.id} /><${Hidden} name="next" value=${back} />
      ${opts.length ? html`<fieldset class="q-opts"><legend class="sr-only">Chọn ${q.multi ? "một hoặc nhiều" : "một"} phương án</legend>${opts.map((o) =>
        html`<label class="q-opt" key=${o.key}><input type=${q.multi ? "checkbox" : "radio"} name="keys" value=${o.key} /><span class="q-opt-k">${o.key}</span><span class="q-opt-body"><span class="q-opt-l">${o.label}</span>${
          o.description ? html`<span class="q-opt-d">${o.description}</span>` : null}</span></label>`)}</fieldset>` : null}
      ${q.allow_free_text ? html`<div class="q-free"><label>${opts.length ? "Câu trả lời khác hoặc ghi chú thêm (tuỳ chọn)" : "Câu trả lời của bạn"}<textarea data-voice name="text" rows="2" maxlength="4000" required=${!opts.length}
        placeholder=${opts.length ? "Câu trả lời khác…" : "Nhập câu trả lời…"}></textarea></label></div>` : null}
      <div class="xcard-actions"><button class="btn primary" type="submit"><${Icon} n="send" /><span>Gửi câu trả lời</span></button></div>
    </form>
    <div class="xcard-actions q-cancel"><span class="spacer"></span><${Act} name="question_cancel" label="Huỷ câu hỏi" fields=${{ id: q.id }} cls="btn ghost sm" next=${back}
      confirm="Huỷ câu hỏi này? Agent sẽ không nhận được câu trả lời (task vẫn bị chặn tới khi bạn gỡ)." /></div>` : null}
  </article>`;
}

function Approval({ a }) {
  return html`<article class="xcard approval human" id=${"ap-" + a.id}>
    <div class="xcard-head"><span class="xcard-ic"><${Icon} n="shield" /></span>
      <div class="xcard-title"><div class="t">Chờ bạn duyệt <span class="pill danger">phá huỷ</span></div>
        <div class="m"><code>${a.tool}</code><span>·</span><${Time} ts=${a.created_at} /></div></div></div>
    <pre class="cmd-full" tabindex="0">${a.text}</pre>
    ${a.reason ? html`<p class="xcard-why">${a.reason}</p>` : null}
    <div class="xcard-actions"><a class="btn primary" href=${a.href || "/approvals"}>Mở trang Duyệt</a></div>
  </article>`;
}

function Item({ it, p }) {
  const agent = (ui.store.manager.value || {}).agent_id;
  switch (it.kind) {
    case "day": return html`<div class="mm-day" role="separator" data-day=${it.key}><span>${it.text}</span></div>`;
    case "user":
      return html`<div class=${"mm user" + (it.optimistic ? " sending" : "")} data-run=${it.run_id || undefined}>
        <div class="mm-text">${it.text}</div>
        <div class="mm-foot">${it.optimistic ? html`<span class="muted small">đang gửi…</span>` : html`<${Time} ts=${it.ts} />`}
          <${CopyBtn} text=${it.text} label="Sao chép tin" />
          <button type="button" class="mm-btn" title="Sửa — đưa tin này vào ô nhập" aria-label="Sửa tin" onClick=${() => ui.fill(p, it.text, false)}><${Icon} n="edit" c="sm" /></button></div></div>`;
    case "manager": {
      const files = it.files || [], fa = it.agent || agent;
      return html`<div class=${"mm mgr" + (it.notify ? " notify" : "")} data-run=${it.run_id || undefined}>
        <div class="mm-who"><span class="mm-mark" aria-hidden="true"><${Icon} n="logo" /></span>${it.notify
          ? html`<${Icon} n="alert" c="sm" />Điều phối viên báo` : it.tick ? "Điều phối viên · nhịp" : "Điều phối viên"}</div>
        <div class="ti msg tx-msg mm-md"><div class="md" dangerouslySetInnerHTML=${{ __html: it.html || "" }}></div>
          ${it.cut ? html`<p class="tx-cutnote"><${Icon} n="info" c="sm" /><span>${it.cut}${it.run_id ? html` <code>cfharness runs logs ${it.run_id}</code>` : null}</span></p>` : null}</div>
        ${files.length && fa ? html`<div class="mm-files" aria-label="Tệp được nhắc tới">${files.map((f) => html`<${FileChip} key=${f.path} f=${f} agent=${fa} />`)}</div>` : null}
        <div class="mm-foot"><${CopyBtn} text=${it.text} label="Sao chép câu trả lời" />
          ${it.notify ? null : html`<button type="button" class="mm-btn mm-retry" title="Thử lại — gửi lại tin cuối của bạn" aria-label="Thử lại" onClick=${() => ui.retry(p)}><${Icon} n="refresh" c="sm" /></button>`}
          <${Time} ts=${it.ts} />${it.run_id ? html`<a href=${"/runs/" + encodeURIComponent(it.run_id)}>xem lượt chạy</a>` : null}</div></div>`;
    }
    case "actions": {
      const n = (it.items || []).length, last = n ? it.items[n - 1].summary || "" : "";
      return html`<details class=${"mm-acts" + (it.ok === false ? " err" : "")} data-run=${it.run_id || undefined}>
        <summary><span class="mm-acts-ic"><${Icon} n=${it.ok === false ? "alert" : "check"} c="sm" /><span class="tx-spin" aria-hidden="true"></span></span>
          <span class="mm-acts-n">Đã làm <span class="n">${n}</span> việc</span><span class="mm-acts-last">${last.split("\n")[0].slice(0, 60)}</span><${Icon} n="chev-right" c="chev" /></summary>
        <div class="mm-acts-list">${(it.items || []).map((a, i) => html`<${ActionRow} key=${a.audit_id || i} a=${a} />`)}</div></details>`;
    }
    case "action": return html`<${ActionRow} a=${it} />`;
    case "question": return html`<${Question} q=${it.q} back=${paneUrl(p.conv.value, p.scope)} />`;
    case "note":
      return html`<div class=${"mm-note " + (it.tone || "")} data-run=${it.run_id || undefined}>${it.text}${it.run_id
        ? html` · <a href=${"/runs/" + encodeURIComponent(it.run_id)}>xem lượt chạy</a>` : null}</div>`;
    default: return null;
  }
}

function Todo({ d }) {
  if (!d || !d.rows) return null;
  return html`<details class="ti tx-todo" open><summary class="tx-todo-sum"><span class="tx-todo-h">Việc cần làm</span> <span class="tx-meta">${d.done}/${d.n} xong</span>${
    d.current ? html` <span class="tx-todo-cur">· ${d.current}</span>` : null}<${Icon} n="chev-down" c="tx-chev" /></summary>
    <ul class="tx-todo-list">${d.rows.map((r, i) => html`<li key=${i} class=${"td " + r.status}><input type="checkbox" disabled checked=${r.status === "completed"} aria-label=${r.label || r.status} /><span>${r.text}</span>${
      r.status === "in_progress" ? html` <span class="tx-todo-now">đang làm</span>` : null}</li>`)}${d.more ? html`<li class="td more">… còn ${d.more} mục</li>` : null}</ul></details>`;
}

function Pending({ pd }) {
  return html`<div class=${"mm mgr pending" + (pd.text ? " has-text" : "")} aria-busy="true" data-pending=${pd.run || ""}>
    <div class="mm-who"><span class="mm-mark thinking" aria-hidden="true"><${Icon} n="logo" /></span>Điều phối viên<span class="mm-state"> · <span class="spin">${pd.waiting ? "đang gửi…" : "đang nghĩ…"}</span></span>${
      pd.run ? html` · <a href=${"/runs/" + encodeURIComponent(pd.run)}>xem lượt chạy</a>` : null}</div>
    ${pd.think ? html`<details class="mm-think"><summary>Suy nghĩ…</summary><pre>${pd.think}</pre></details>` : null}
    <${Todo} d=${pd.todo} />
    <div class="ti msg tx-msg mm-md mm-live"><div class="md"></div><div class="mm-tail">${pd.text}</div></div>
    <div class="mm-tool" hidden=${!pd.tool}><span class="tx-spin" aria-hidden="true"></span><span class="mm-tool-t">${pd.tool ? "Đang dùng " + pd.tool + "…" : ""}</span></div>
  </div>`;
}

function Empty({ p }) {
  const tabs = ui.store.tabs.value, ws = p.scope !== LOBBY ? [...tabs.active, ...tabs.closed].find((t) => t.ws === p.scope) : null;
  const conv = p.conv.value;
  return html`<div class="empty mgr-hero coord-empty"><${Icon} n="logo" c="hero-mark" /><b>${ws && !conv ? ws.title : !conv ? "Hôm nay giao việc gì cho đội?" : "Chưa có tin nhắn"}</b>
    <span>${ws && !conv ? "Nhắn gì ở đây cũng thuộc luồng này. Điều phối viên nhớ luồng qua ghi chú ở panel bên phải."
      : "Nói mục tiêu bằng lời — điều phối viên tự tạo agent/team, giao việc, theo dõi và báo lại. Mỗi epic nên mở một luồng (＋ Luồng) để việc không lẫn vào nhau."}</span></div>`;
}

/** Một khung = một vùng cuộn `.mgr-log`; khung ẩn vẫn ở trong DOM (giữ chỗ cuộn, nháp, đính kèm ở store). */
function Pane({ p, on }) {
  const ref = useRef(null);
  const items = p.items.value, pd = p.pending.value, loaded = p.loaded.value, err = p.error.value;
  if (!p.scroll) p.scroll = { top: 0, stick: true };
  // ghi nhớ vị trí cuộn của từng khung (display:none làm Chrome mất scrollTop → tự lưu khi cuộn, khôi phục khi hiện lại)
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const onScroll = () => { if (el.clientHeight) { p.scroll.top = el.scrollTop; p.scroll.stick = el.scrollHeight - el.scrollTop - el.clientHeight < 120; } };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || !on) return;
    el.scrollTop = p.scroll.stick ? el.scrollHeight : p.scroll.top;
  }, [on, items, pd, loaded]);
  const empty = loaded && !items.length && !pd && !p.actions.value.length;
  return html`<div class=${"mgr-log" + (empty ? " is-empty" : "")} ref=${ref} hidden=${!on} data-pane=${p.id} aria-label="Dòng trò chuyện">
    ${!loaded ? html`<div class="coord-loading" role="status">${err ? html`<span class="danger-t">${err}</span>` : html`<span class="tx-spin" aria-hidden="true"></span>Đang tải…`}</div>` : null}
    ${empty ? html`<${Empty} p=${p} />` : null}
    ${items.map((it, i) => html`<${Item} key=${it.id || "i" + i} it=${it} p=${p} />`)}
    ${p.actions.value.filter((a) => a.kind === "approval").map((a) => html`<${Approval} key=${"ap-" + a.id} a=${a} />`)}
    ${pd ? html`<${Pending} pd=${pd} />` : null}
  </div>`;
}

// ------------------------------------------------------------------ cột phải
function Status() {
  const m = ui.store.manager.value || {};
  return html`<div class="mgr-st" id="mgr-status">
    <div class="mgr-st-who"><span class=${"avatar " + (m.avatar || "c0")} aria-hidden="true">${(m.name || "?").slice(0, 1)}</span>
      <div class="acard-name"><a href=${"/agents/" + encodeURIComponent(m.agent_id || "")}>${m.name || m.agent_id}</a><div class="r">${m.agent_id}</div></div>
      <span class=${"pill " + (m.status_tone || "muted")}><${Icon} n=${m.status_icon || "circle"} c=${m.status_icon === "dot" && m.status_tone === "ok" ? "pulse" : ""} />${m.status_label || ""}</span></div>
    <dl class="kv">
      <div><dt>Nhịp gần nhất</dt><dd>${m.last_tick ? html`${fmtTs(m.last_tick)} <span class="muted small">(${relText(m.last_tick)})</span>` : "chưa chạy"}</dd></div>
      <div><dt>Nhịp kế tiếp</dt><dd>${m.next_tick ? fmtTs(m.next_tick) : "khi có sự kiện"}</dd></div>
      <div><dt>Hôm nay</dt><dd>${m.cost_today || "—"}</dd></div>
    </dl></div>`;
}

function WsSide({ ws }) {
  const tk = ws.tasks || {};
  const back = "/manager?ui=next&w=" + encodeURIComponent(ws.id);
  return html`
    <section class="mgr-side-sec ws-side">
      <div class="ws-side-head"><h3>${ws.title}</h3>${ws.key ? html`<span class="pill muted">${ws.key}</span>` : null}</div>
      <progress class="ws-progress" max=${tk.total || 1} value=${tk.done || 0} aria-label="Tiến độ task"></progress>
      <p class="muted small">${tk.done || 0}/${tk.total || 0} task xong${tk.blocked ? html` · <span class="danger-t">${tk.blocked} bị chặn</span>` : null}${ws.needs_human ? " · " + ws.needs_human + " cần bạn" : ""}</p>
      <ul class="ws-tasks">${(ws.open_tasks || []).map((t) => html`<li key=${t.id}><span class=${"ws-dot " + (t.status === "blocked" ? "danger" : t.status === "running" ? "run" : "idle")} title=${t.status}></span><a href=${"/tasks/" + encodeURIComponent(t.id)}>${t.title}</a></li>`)}
        ${(ws.recent_done || []).slice().reverse().map((t) => html`<li key=${"d" + t.id} class="done"><span class="ws-dot done" title="done"></span><a href=${"/tasks/" + encodeURIComponent(t.id)}>${t.title}</a></li>`)}</ul>
    </section>
    <section class="mgr-side-sec ws-side">
      <details class="ws-state"><summary class="ws-side-head"><h3>Ghi chú luồng</h3><span class="btn sm ghost">Sửa</span></summary>
        <form method="post" action="/do/ws_state" class="ws-form"><${Hidden} name="csrf" value=${ui.csrf} /><${Hidden} name="id" value=${ws.id} /><${Hidden} name="next" value=${back} />
          <textarea name="state" rows="10" maxlength="12000" aria-label="Ghi chú luồng" value=${ws.state || ""}></textarea>
          <button class="btn sm primary" type="submit">Lưu</button></form></details>
      ${ws.state ? html`<pre class="ws-state-pre">${ws.state}</pre>` : html`<p class="muted small">Chưa có. Điều phối viên tự ghi quyết định, PR, việc tiếp theo vào đây và đọc lại mỗi khi bắt đầu session mới.</p>`}
    </section>
    <section class="mgr-side-sec ws-side"><${WsForms} ws=${ws} /></section>`;
}

function Side({ p }) {
  const s = ui.store, m = s.manager.value || {}, side = s.side.value, ws = p && p.workstream.value;
  const back = p ? paneUrl(p.conv.value, p.scope) : "/manager?ui=next";
  return html`<aside class="mgr-side" id="mgr-side" aria-label="Thông tin điều phối viên">
    <div class="mgr-side-head"><h2>Điều phối viên</h2><button type="button" class="btn ghost sm icon" id="mgr-info-close" title="Đóng" aria-label="Đóng"><${Icon} n="x" /></button></div>
    <section class="mgr-side-sec">
      <${Status} />
      <div class="btn-row mgr-ctl">
        <${Act} name="manager_tick" label="Chạy nhịp ngay" cls="btn sm" ic="zap" next=${back} title="manager.tick — gom sự kiện đang chờ và cho điều phối viên xử lý ngay"
          confirm="Chạy một nhịp của điều phối viên NGAY? Nó xem sự kiện đang chờ + việc còn mở và xử lý (một run thật, tốn tiền)." />
        <${Act} name="manager_unset" label="Huỷ chỉ định" fields=${{ current: m.agent_id || "" }} cls="btn sm danger" next="/manager?ui=next"
          confirm=${"Huỷ chỉ định điều phối viên " + (m.agent_id || "") + "? Agent vẫn còn, chỉ mất vai trò."} />
      </div>
    </section>
    ${ws ? html`<${WsSide} ws=${ws} />` : null}
    <section class="mgr-side-sec"><h3>Hành động gần đây</h3>
      <ol class="feed" id="mgr-actions">${side.length ? side.map((a, i) => html`<li key=${a.audit_id || i} class=${"fl " + (a.ok === false ? "danger" : "ok")}><span class="fl-dot" aria-hidden="true"></span>
        <span class="fl-main"><span class="fl-text">${a.summary}</span>${a.target && a.target.href ? html` · <a href=${a.target.href}>${a.target.label}</a>` : null}</span>
        <span class="fl-ts">${fmtTs(a.ts)}</span></li>`) : html`<li class="empty">Chưa có hành động nào.</li>`}</ol>
    </section>
  </aside>`;
}

// ------------------------------------------------------------------ gốc
export function App() {
  const s = ui.store, p = s.active.value, panes = s.panes.value;
  const empty = !!p && p.loaded.value && !p.items.value.length && !p.pending.value;
  return html`
    <section class=${"mgr-chat" + (empty ? " is-empty" : "")}>
      <${Header} p=${p} />
      <${TabBar} />
      <div class="coord-panes">${panes.map((x) => html`<${Pane} key=${x.id} p=${x} on=${x === p} />`)}</div>
      <p class="sr-only" id="mgr-status-live" role="status"></p>
      ${p ? html`<${Composer} p=${p} />` : null}
    </section>
    <${Side} p=${p} />`;
}
