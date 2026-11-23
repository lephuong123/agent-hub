/* Island Điều phối — store bằng signal + bộ áp message SSE v2 (reducer). KHÔNG đụng DOM/mạng: trả về "hiệu ứng" (fx) cho main.js làm.
 *
 * `createStore({signal, batch}, {storage})` — signal/batch tiêm vào (trình duyệt: @preact/signals; test Node: @preact/signals-core vendored)
 * nên module này không import thư viện nào và chạy được dưới Node.
 *
 * Mô hình: MỖI "khung" (pane) = một hội thoại đang mở (hoặc hội thoại MỚI chưa có id của một luồng). Khung đã mở giữ nguyên trong bộ nhớ
 * (và trong DOM, ẩn bằng `hidden`) → đổi qua lại không tải lại. Dữ liệu chung: điều phối viên, tab luồng, danh sách hội thoại theo
 * phạm vi (luồng / Sảnh "_"), "Hành động gần đây".
 *
 * Chữ của agent là DỮ LIỆU: store chỉ giữ chữ thô (`text`, `label`, …) — chỗ DUY NHẤT là HTML là `item.html` của mục `manager` (server đã
 * cứng hoá bằng present.md). Không có hàm nào ở đây dựng HTML.
 */
import { LOBBY, draftKey, dayKey, dayLabel } from "./util.js";

const EARLY_MAX = 400;   // message của lượt chưa biết thuộc khung nào (tin vừa gửi, chưa có run id) — giữ tối đa chừng này
const SIDE_MAX = 12;
const CV_TAIL = 12;      // số mục cuối của ảnh chụp KHÔNG dùng content-visibility (thấy ngay khi mở → đo thật từ khung hình đầu)
export const FIRST_SHOW = 60;  // mục vẽ ngay khi mở một hội thoại dài; cũ hơn vẽ dần khi cuộn lên (views/More) — 2000 mục mở ~45 ms

export function createStore(S, opts) {
  const { signal, batch, computed } = S;
  const storage = (opts && opts.storage) || null;
  let seq = 0, tmpSeq = 0, actSeq = 0;
  const st = {
    manager: signal(null),
    hub: signal({}),              // {estop, admin_estop} — dải ESTOP
    tabs: signal({ enabled: false, lobby: null, active: [], closed: [] }),
    convs: signal({}),            // phạm vi → [hội thoại]
    side: signal([]),             // "Hành động gần đây" (mới → cũ)
    panes: signal([]),            // khung đã mở (thứ tự tạo — DOM giữ nguyên thứ tự này)
    active: signal(null),         // khung đang hiện
    live: signal("connecting"),   // connecting | live | reconnecting
    boot: null,                   // boot của luồng SSE (từ snapshot/hello)
    early: [],                    // message tới trước khi biết khung
    bufs: new Map(),              // run → {text, think} chưa đổ vào signal (gom theo khung hình)
    lastByScope: new Map(),       // phạm vi → khung xem gần nhất
    extraTabs: new Map(),         // ws.status của luồng chưa có trong danh sách (chờ tabs.patch)
  };

  // ---------------------------------------------------------------- nháp (sessionStorage, cùng khoá với trang cũ)
  function draftLoad(conv, scope) {
    try { return (storage && storage.getItem("cfh_mgr_draft:" + draftKey(conv, scope))) || ""; } catch (_) { return ""; }
  }
  function draftSave(p) {
    const k = "cfh_mgr_draft:" + draftKey(p.conv.value, p.scope);
    try { if (!storage) return; const v = p.draft.value; if (v && v.trim()) storage.setItem(k, v); else storage.removeItem(k); } catch (_) {}
  }
  function setDraft(p, text) { p.draft.value = text; draftSave(p); }

  // ---------------------------------------------------------------- khung
  function makePane(scope, conv) {
    const p = {
      id: "p" + (++seq), scope: scope || LOBBY, conv: signal(conv || null), title: signal(null),
      items: signal([]), pending: signal(null), actions: signal([]), workstream: signal(null), wsConvs: signal([]),
      runs: new Set(), loaded: signal(false), stale: false, error: signal(""),
      draft: signal(""), files: signal([]), sendErr: signal(""), sending: signal(0), voice: "",
      jump: signal(false), scroll: null, el: null, hide: signal(0),  // hide = số mục ĐẦU chưa vẽ (cuộn lên → vẽ dần)  // nút "Tin mới nhất" + trạng thái cuộn dính đáy (scroll.js) + phần tử cuộn
    };
    // tín hiệu dẫn xuất: chỉ đổi khi GIÁ TRỊ đổi → chữ chạy (pending đổi mỗi khung hình) không làm vẽ lại khung/dòng thời gian/ô nhập
    p.runId = computed(() => (p.pending.value ? p.pending.value.run || null : null));
    p.busy = computed(() => !!(p.pending.value && p.pending.value.run));
    p.empty = computed(() => p.loaded.value && !p.items.value.length && !p.pending.value && !p.actions.value.length);
    p.draft.value = draftLoad(p.conv.value, p.scope);
    st.panes.value = st.panes.value.concat([p]);
    return p;
  }
  function paneByConv(conv) { return conv ? st.panes.value.find((p) => p.conv.value === conv) || null : null; }
  function paneFresh(scope) { return st.panes.value.find((p) => !p.conv.value && p.scope === (scope || LOBBY)) || null; }
  function closePane(p) { st.panes.value = st.panes.value.filter((x) => x !== p); for (const [k, v] of st.lastByScope) if (v === p) st.lastByScope.delete(k); }

  /** Khung cho một đích điều hướng ĐÃ có sẵn (không cần mạng) — null = phải tải ảnh chụp. */
  function paneFor(nav) {
    if (nav.c) return paneByConv(nav.c);
    if (nav.fresh) return paneFresh(nav.w || LOBBY);
    if (nav.w) return st.lastByScope.get(nav.w) || null;
    return st.lastByScope.get(LOBBY) || null;
  }

  function activate(p) {
    if (!p) return;
    st.lastByScope.set(p.scope, p);
    st.active.value = p;
  }

  // ---------------------------------------------------------------- tab luồng
  function allTabs(t) { return [t.lobby, ...(t.active || []), ...(t.closed || [])].filter(Boolean); }
  function setTab(tab) {
    const t = st.tabs.value;
    if (tab.ws === LOBBY) { st.tabs.value = { ...t, lobby: tab }; return; }
    const ia = t.active.findIndex((x) => x.ws === tab.ws), ic = t.closed.findIndex((x) => x.ws === tab.ws);
    if (ia >= 0 && tab.status !== "closed") { const a = t.active.slice(); a[ia] = tab; st.tabs.value = { ...t, active: a }; return; }
    if (ic >= 0 && tab.status === "closed") { const c = t.closed.slice(); c[ic] = tab; st.tabs.value = { ...t, closed: c }; return; }
    // luồng mới / vừa đóng–mở lại: server luôn phát `tabs.patch` (thứ tự đổi) ngay sau → giữ tab ở đây chờ xếp
    st.extraTabs.set(tab.ws, tab);
  }
  function patchOrder(active, closed) {
    const t = st.tabs.value, idx = new Map();
    for (const x of allTabs(t)) idx.set(x.ws, x);
    for (const [k, v] of st.extraTabs) idx.set(k, v);
    const pick = (ids) => ids.map((w) => idx.get(w)).filter(Boolean);
    st.tabs.value = { ...t, active: pick(active), closed: pick(closed) };
    for (const w of active.concat(closed)) if (idx.has(w)) st.extraTabs.delete(w);
  }

  // ---------------------------------------------------------------- danh sách hội thoại
  function convPatch(conv, fn) {
    const all = st.convs.value;
    let hit = false;
    const next = {};
    for (const k in all) {
      next[k] = all[k].map((c) => { if (c.id !== conv) return c; hit = true; return { ...c, ...fn(c) }; });
    }
    if (hit) st.convs.value = next;
    return hit;
  }
  function convAdd(scope, c) {
    const all = st.convs.value, list = all[scope] || [];
    if (list.some((x) => x.id === c.id)) return;
    st.convs.value = { ...all, [scope]: [c].concat(list) };
  }
  function markSeen(conv) { convPatch(conv, () => ({ unread: 0 })); }
  function bumpUnread(conv) { convPatch(conv, (c) => ({ unread: (c.unread || 0) + 1 })); }

  // ---------------------------------------------------------------- ảnh chụp
  function pendingOf(sp, old) {
    if (!sp || !sp.run) return null;
    const keep = old && old.run === sp.run ? old : null;  // resync giữa lúc chạy chữ: giữ phần chữ đã có
    return { run: sp.run, text: keep ? keep.text : "", think: keep ? keep.think : "", tool: sp.tool || (keep && keep.tool) || "",
             todo: sp.todo || (keep && keep.todo) || null, waiting: false };
  }

  /** Áp ảnh chụp v2 vào khung `target` (khung đang chờ tải) hoặc khung đã có của cùng hội thoại / khung mới. Trả khung. */
  function applySnapshot(snap, target) {
    let p = null;
    batch(() => {
      if (snap.manager) st.manager.value = snap.manager;
      if (snap.boot) st.boot = snap.boot;
      if (snap.workstreams) { st.tabs.value = { enabled: !!snap.workstreams.enabled, lobby: snap.workstreams.lobby || null,
                                                active: snap.workstreams.active || [], closed: snap.workstreams.closed || [] };
                              st.extraTabs.clear(); }
      const scope = snap.scope || LOBBY;
      if (snap.conversations) st.convs.value = { ...st.convs.value, [scope]: snap.conversations };
      if (snap.side_actions) st.side.value = snap.side_actions;
      const same = paneByConv(snap.conv);
      p = same || target || (snap.conv ? null : paneFresh(scope)) || makePane(scope, snap.conv);
      if (target && same && same !== target) closePane(target);  // tải xong mới biết là hội thoại đã mở → dùng khung cũ
      const wasFresh = !p.conv.value;
      p.scope = scope;
      p.conv.value = snap.conv || null;
      if (wasFresh && p.conv.value && !p.draft.value) p.draft.value = draftLoad(p.conv.value, scope);
      p.title.value = snap.title || null;
      p.items.value = snap.items || [];
      p.hide.value = Math.max(0, p.items.value.length - FIRST_SHOW);
      // mục cũ (trừ CV_TAIL mục cuối — phần thấy ngay khi mở) dùng content-visibility; tập cố định tới lần ảnh chụp sau (views/Item)
      p.cv = new Set(p.items.value.slice(0, Math.max(0, p.items.value.length - CV_TAIL)).map((x) => x.id).filter(Boolean));
      p.pending.value = pendingOf(snap.pending, p.pending.value);
      p.actions.value = snap.actions || [];
      p.workstream.value = snap.workstream || null;
      p.wsConvs.value = snap.ws_convs || [];
      p.runs = new Set(snap.conv_runs || []);
      if (snap.pending && snap.pending.run) p.runs.add(snap.pending.run);
      p.loaded.value = true;
      p.stale = false;
      p.error.value = "";
    });
    replayEarly(p);
    return p;
  }

  // ---------------------------------------------------------------- áp message SSE
  function panesFor(m) {
    return st.panes.value.filter((p) => (m.conv && p.conv.value === m.conv) || (m.run && p.runs.has(m.run)));
  }
  function hasReply(p, run) { return !!run && p.items.value.some((it) => it.kind === "manager" && it.run_id === run && !it.notify); }
  function ensurePending(p, run) {
    const cur = p.pending.value;
    if (cur && (cur.run === run || (!cur.run && cur.waiting))) { if (!cur.run) p.pending.value = { ...cur, run, waiting: false }; return true; }
    if (hasReply(p, run)) return false;
    p.pending.value = { run, text: "", think: "", tool: "", todo: null, waiting: false };
    p.runs.add(run);
    return true;
  }
  function pushItem(p, item) {
    const items = p.items.value;
    if (item.id && items.some((x) => x.id === item.id)) return false;
    const add = [];
    if (item.kind !== "day") {
      const key = dayKey(item.ts);
      let last = null;
      for (let i = items.length - 1; i >= 0; i--) if (items[i].kind === "day") { last = items[i].key; break; }
      if (key && last !== key) add.push({ kind: "day", id: "d" + key, key, text: dayLabel(key) });
    }
    add.push(item);
    p.items.value = items.concat(add);
    return true;
  }
  function stash(m) {
    st.early.push(m);
    if (st.early.length > EARLY_MAX) st.early.splice(0, st.early.length - EARLY_MAX);
  }
  function replayEarly(p) {
    if (!st.early.length) return [];
    const mine = [], rest = [];
    for (const m of st.early) ((m.conv && m.conv === p.conv.value) || (m.run && p.runs.has(m.run)) ? mine : rest).push(m);
    if (!mine.length) return [];
    st.early = rest;
    let fx = [];
    for (const m of mine) fx = fx.concat(apply(m));
    return fx;
  }

  function onPane(p, m, fx) {
    const run = m.run;
    switch (m.t) {
      case "run.start":
        if (run && ensurePending(p, run)) convPatch(p.conv.value, () => ({ running: true }));
        break;
      case "msg.delta": case "think.delta": {
        if (!run || !ensurePending(p, run)) break;
        let b = st.bufs.get(run);
        if (!b) st.bufs.set(run, (b = { text: "", think: "" }));
        if (m.t === "msg.delta") b.text += m.text || ""; else b.think += m.text || "";
        fx.push({ fx: "flush" });
        break;
      }
      case "msg.seg": {
        if (!run) break;
        const b = st.bufs.get(run);
        if (b) b.text += "\n\n"; else { const cur = p.pending.value; if (cur && cur.run === run && cur.text) p.pending.value = { ...cur, text: cur.text + "\n\n" }; }
        break;
      }
      case "tool.start": {
        if (!run || !ensurePending(p, run)) break;
        const c0 = p.pending.value;
        p.pending.value = { ...c0, tool: m.label || m.name || "", toolId: m.id || "", tools: (c0.tools || 0) + 1 };
        fx.push({ fx: "event", name: "tool", detail: { run, label: m.label || m.name || "" } });
        break;
      }
      case "tool.end": {
        const cur = p.pending.value;
        if (cur && cur.run === run && (!m.id || cur.toolId === m.id)) p.pending.value = { ...cur, tool: "", toolId: "" };
        break;
      }
      case "todo": {
        if (!run || !ensurePending(p, run)) break;
        p.pending.value = { ...p.pending.value, todo: m.todo || null };
        break;
      }
      case "msg.done": {
        const it = m.item;
        if (!it) break;
        flushRun(run);
        const dup = (it.id && p.items.value.some((x) => x.id === it.id)) || (!it.notify && hasReply(p, it.run_id));
        if (!dup) pushItem(p, it);
        const cur = p.pending.value;
        if (cur && run && cur.run === run) p.pending.value = null;
        st.bufs.delete(run);
        if (!dup) fx.push({ fx: "reply", pane: p, conv: p.conv.value, item: it });
        break;
      }
      case "act.item": {
        const a = m.item;
        if (!a) break;
        const items = p.items.value;
        const gi = items.findIndex((x) => x.kind === "actions" && x.run_id && x.run_id === (a.run_id || run));
        if (gi >= 0) {
          const g = items[gi];
          if (a.audit_id && g.items.some((x) => x.audit_id === a.audit_id)) break;
          const ng = { ...g, items: g.items.concat([a]), ok: g.ok && a.ok !== false };
          const next = items.slice(); next[gi] = ng; p.items.value = next;
        } else {
          pushItem(p, { kind: "actions", id: "g" + (a.audit_id || run), ok: a.ok !== false, items: [a], ts: a.ts, run_id: a.run_id || run });
        }
        break;
      }
      case "action.pending": {
        const a = m.action;
        if (!a) break;
        const acts = p.actions.value.filter((x) => !(x.kind === a.kind && x.id === a.id));
        p.actions.value = acts.concat([a]);
        if (a.kind === "question") {
          const qid = "q-" + a.id, items = p.items.value, i = items.findIndex((x) => x.id === qid);
          const { kind, ...q } = a;
          if (i >= 0) { const n = items.slice(); n[i] = { ...n[i], q }; p.items.value = n; }
          else pushItem(p, { kind: "question", id: qid, q, ts: a.created_at, run_id: a.run_id });
        }
        break;
      }
      case "action.resolved": {
        p.actions.value = m.kind === "approval"
          ? p.actions.value.map((x) => (x.kind === "approval" && x.id === m.id ? { ...x, status: m.status, done: m.status, busy: false, opt: 0, err: "" } : x))
          : p.actions.value.filter((x) => !(x.kind === m.kind && x.id === m.id));
        if (m.kind === "question") {
          const qid = "q-" + m.id, items = p.items.value, i = items.findIndex((x) => x.id === qid);
          if (i >= 0) {
            const n = items.slice();
            n[i] = { ...n[i], q: { ...n[i].q, status: m.status, answer: m.answer != null ? m.answer : n[i].q.answer, answered_by: m.answered_by || n[i].q.answered_by,
                                   busy: false, opt: 0, err: "" } };
            p.items.value = n;
          }
        }
        break;
      }
      case "run.end": {
        flushRun(run);
        const cur = p.pending.value;
        if (cur && cur.run === run) p.pending.value = null;
        st.bufs.delete(run);
        convPatch(p.conv.value, () => ({ running: false }));
        if (!m.ok && run) pushItem(p, { kind: "note", id: "n" + run, text: m.label || "Lượt chạy kết thúc", tone: m.tone || "warn", run_id: run });
        fx.push({ fx: "event", name: "run_end", detail: { run, ok: !!m.ok } });
        break;
      }
      default:
        break;
    }
  }

  /** Áp MỘT message v2 → danh sách hiệu ứng cho main.js: {fx:"resync"|"status"|"flush"|"reply"|"event"|"tabs"}. */
  function apply(m) {
    const fx = [];
    if (!m || !m.t) return fx;
    switch (m.t) {
      case "hello":
        if (m.boot) st.boot = m.boot;
        return fx;
      case "resync":
        for (const p of st.panes.value) p.stale = true;
        st.early = [];
        st.bufs.clear();
        fx.push({ fx: "resync", reason: m.reason || "" });
        return fx;
      case "ws.status": {
        const { t, ...tab } = m;
        setTab(tab);
        fx.push({ fx: "status" });
        return fx;
      }
      case "tabs.patch":
        patchOrder(m.active || [], m.closed || []);
        return fx;
      case "tick":
        fx.push({ fx: "status" });
        return fx;
      default:
        break;
    }
    if (m.t === "act.item" && m.item) {
      const side = st.side.value;
      if (!m.item.audit_id || !side.some((x) => x.audit_id === m.item.audit_id)) st.side.value = [m.item].concat(side).slice(0, SIDE_MAX);
    }
    const ps = panesFor(m);
    if (!ps.length) {
      if (m.run || m.conv) stash(m);
      if (m.t === "run.start" || m.t === "run.end") convPatch(m.conv, () => ({ running: m.t === "run.start" }));
      if (m.t === "msg.done" && m.conv) fx.push({ fx: "reply", pane: null, conv: m.conv, item: m.item });
      return fx;
    }
    batch(() => { for (const p of ps) onPane(p, m, fx); });
    return fx;
  }

  function flushRun(run) {
    const b = run && st.bufs.get(run);
    if (!b || (!b.text && !b.think)) return;
    for (const p of st.panes.value) {
      const cur = p.pending.value;
      if (cur && cur.run === run) p.pending.value = { ...cur, text: cur.text + b.text, think: cur.think + b.think };
    }
    b.text = ""; b.think = "";
  }
  /** Đổ chữ đang chạy đã gom vào signal — main.js gọi MỘT lần mỗi requestAnimationFrame. */
  function flush() { batch(() => { for (const run of st.bufs.keys()) flushRun(run); }); }

  // ---------------------------------------------------------------- gửi lạc quan
  function beginSend(p, text, files) {
    const names = (files || []).map((f) => f.name);
    const shown = (text + (names.length ? "\n📎 " + names.join(", ") : "")).trim();
    const tok = { id: "tmp" + (++tmpSeq), text, files: files || [], hadPending: !!p.pending.value };
    batch(() => {
      pushItem(p, { kind: "user", id: tok.id, text: shown, ts: new Date().toISOString(), run_id: null, optimistic: true });
      if (!p.pending.value) p.pending.value = { run: null, text: "", think: "", tool: "", todo: null, waiting: true };
      p.sending.value = p.sending.value + 1;
      p.sendErr.value = "";
      setDraft(p, "");
      p.files.value = [];
    });
    return tok;
  }
  function sendOk(p, tok, res) {
    let fx = [];
    batch(() => {
      const conv = res.conversation_id || p.conv.value;
      if (conv && conv !== p.conv.value) {
        const old = draftKey(null, p.scope);
        try { if (storage) storage.removeItem("cfh_mgr_draft:" + old); } catch (_) {}
        p.conv.value = conv;
        convAdd(p.scope, { id: conv, title: null, updated_at: new Date().toISOString(), count: 1, unread: 0, running: !!res.run_id });
      }
      p.items.value = p.items.value.map((x) => (x.id === tok.id ? { ...x, optimistic: false, run_id: res.run_id || null } : x));
      if (res.run_id) {
        p.runs.add(res.run_id);
        const cur = p.pending.value;
        if (cur && !cur.run) p.pending.value = { ...cur, run: res.run_id, waiting: false };
        else if (!cur && !hasReply(p, res.run_id)) p.pending.value = { run: res.run_id, text: "", think: "", tool: "", todo: null, waiting: false };
      } else if (p.pending.value && !p.pending.value.run) p.pending.value = null;
      p.sending.value = Math.max(0, p.sending.value - 1);
    });
    fx = replayEarly(p);
    flush();
    return fx;
  }
  function sendFail(p, tok, err) {
    batch(() => {
      p.items.value = p.items.value.filter((x) => x.id !== tok.id);
      const cur = p.pending.value;
      if (cur && !cur.run && !tok.hadPending) p.pending.value = null;
      if (!p.draft.value.trim()) setDraft(p, tok.text);           // trả chữ về ô nhập (người chưa gõ gì mới)
      if (!p.files.value.length) p.files.value = tok.files;
      p.sendErr.value = err || "Không gửi được.";
      p.sending.value = Math.max(0, p.sending.value - 1);
    });
  }

  // ---------------------------------------------------------------- duyệt / trả lời lạc quan (idempotency key do main sinh mỗi lần bấm)
  function findQ(p, id) { const items = p.items.value, i = items.findIndex((x) => x.id === "q-" + id); return [items, i]; }
  function patchQ(p, id, fn) {
    const [items, i] = findQ(p, id);
    if (i < 0) return null;
    const n = items.slice(); n[i] = { ...n[i], q: fn(n[i].q) }; p.items.value = n;
    return n[i].q;
  }
  function patchAp(p, id, fn) {
    let hit = null;
    p.actions.value = p.actions.value.map((x) => (x.kind === "approval" && x.id === id ? (hit = fn(x)) : x));
    return hit;
  }
  /** Bắt đầu thao tác: hiện kết quả mong đợi NGAY (`busy`). kind: approve|deny|answer. Trả token cho actOk/actFail. */
  function actBegin(p, kind, id, extra) {
    const tok = { p, kind, id, seq: ++actSeq, prev: null, idem: (extra && extra.idem) || "" };
    batch(() => {
      if (kind === "answer") {
        const a = (extra && extra.answer) || {};
        patchQ(p, id, (q) => { tok.prev = q; return { ...q, status: "answered", answer: a, answered_by: "user", busy: true, opt: tok.seq, err: "", draft: null }; });
      } else {
        patchAp(p, id, (x) => { tok.prev = x; return { ...x, done: kind === "approve" ? "allowed" : "denied", busy: true, opt: tok.seq, err: "", ask: "" }; });
      }
    });
    return tok;
  }
  function mine(tok) {
    if (tok.kind === "answer") { const [items, i] = findQ(tok.p, tok.id); return i >= 0 && items[i].q.opt === tok.seq; }
    return tok.p.actions.value.some((x) => x.kind === "approval" && x.id === tok.id && x.opt === tok.seq);
  }
  /** Server nhận: bỏ `busy` (trạng thái cuối do `action.resolved` chốt — tới trước hay sau đều được). */
  function actOk(tok) {
    if (!mine(tok)) return;
    if (tok.kind === "answer") patchQ(tok.p, tok.id, (q) => ({ ...q, busy: false }));
    else patchAp(tok.p, tok.id, (x) => ({ ...x, busy: false }));
  }
  /** Lỗi → hoàn tác về trạng thái trước (giữ lựa chọn/chữ người đã nhập ở `draft`) + lỗi. `retry` = mạng lỗi: giữ idem để bấm lại cùng khoá. */
  function actFail(tok, err, o) {
    if (!mine(tok) || !tok.prev) return;
    const keep = { err: err || "Không gửi được.", busy: false, opt: 0, retryIdem: o && o.retry ? tok.idem : "" };
    if (tok.kind === "answer") patchQ(tok.p, tok.id, () => ({ ...tok.prev, ...keep, draft: (o && o.draft) || null }));
    else patchAp(tok.p, tok.id, () => ({ ...tok.prev, ...keep, ask: (o && o.ask) || "" }));
  }

  return Object.assign(st, {
    actBegin, actOk, actFail, patchAp, patchQ,
    makePane, paneByConv, paneFresh, paneFor, activate, closePane, applySnapshot, apply, flush,
    setDraft, beginSend, sendOk, sendFail, markSeen, bumpUnread, convPatch,
  });
}
