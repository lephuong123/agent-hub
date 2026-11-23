// Island Điều phối — kiểm store/reducer + client SSE bằng Node (không trình duyệt). Chạy: node tests/coord_store_check.mjs <gốc repo>
// In "ok <tên>" mỗi ca; lỗi → "FAIL …" và mã thoát 1. tests/test_coord_island.py gọi file này.
import { pathToFileURL } from "node:url";
import path from "node:path";

const ROOT = process.argv[2] || process.cwd();
const ST = path.join(ROOT, "src", "cfharness", "web", "static");
const imp = (p) => import(pathToFileURL(path.join(ST, p)).href);
const S = await imp("vendor/preact-signals-core-1.14.5/signals-core.mjs");
const { createStore } = await imp("coord/store.js");
const { createFeed } = await imp("coord/sse.js");
const U = await imp("coord/util.js");

let fails = 0;
function ok(name, cond, extra) {
  if (cond) console.log("ok " + name);
  else { fails++; console.log("FAIL " + name + (extra !== undefined ? " " + JSON.stringify(extra) : "")); }
}
function mem() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), _m: m };
}
const tab = (ws, extra) => ({ ws, key: null, title: ws, label: ws, status: "active", sprint: null, dot: "none", dot_label: "", unread: 0, needs_human: 0, tasks: {}, ...extra });
function snap(o) {
  return {
    v: 1, cursor: "b1.10", boot: "b1", manager: { available: true, agent_id: "dp", name: "Điều phối" }, conv: null, ws: null, scope: "_",
    title: null, conversations: [], workstreams: { enabled: true, lobby: tab("_"), active: [tab("ws1"), tab("ws2")], closed: [] },
    items: [], pending: null, actions: [], side_actions: [], conv_runs: [], workstream: null, ws_convs: [], ...o,
  };
}
const today = new Date().toISOString();

// ---------------------------------------------------------------- util
ok("parseId", U.parseId("ab.12").n === 12 && U.parseId("a.b.3").boot === "a.b" && U.parseId("x") === null && U.parseId("x.y") === null);
ok("paneUrl", U.paneUrl("c1", "_") === "/manager?ui=next&c=c1" && U.paneUrl(null, "ws1") === "/manager?ui=next&w=ws1&new=1" && U.paneUrl(null, "_") === "/manager?ui=next&new=1");
ok("draftKey tương thích trang cũ", U.draftKey("c1", "ws1") === "c:c1" && U.draftKey(null, "_") === "w:" && U.draftKey(null, "ws1") === "w:ws1");
{
  const big = { name: "a.png", size: 6 * 1024 * 1024, type: "image/png" }, small = (i) => ({ name: i + ".png", size: 10, type: "image/png" });
  const r = U.addFiles([], [big, small(1), small(2), small(3), small(4), small(5), { name: "e.txt", size: 0, type: "" }]);
  ok("giới hạn đính kèm", r.files.length === 4 && r.errors.length === 3, r);
}

// ---------------------------------------------------------------- ảnh chụp + khung giữ nguyên khi đổi tab
{
  const store = createStore(S, { storage: mem() });
  const p1 = store.applySnapshot(snap({ conv: "c1", items: [{ kind: "user", id: "m1", text: "xin chào", ts: today, run_id: "r0" }],
                                        conversations: [{ id: "c1", title: "A", unread: 2, running: false }] }), null);
  store.activate(p1);
  const p2 = store.applySnapshot(snap({ conv: "c2", scope: "ws1", ws: "ws1" }), store.makePane("ws1", null));
  store.activate(p2);
  ok("hai khung song song", store.panes.value.length === 2 && store.active.value === p2);
  ok("paneFor theo hội thoại/luồng (không cần mạng)", store.paneFor({ c: "c1" }) === p1 && store.paneFor({ w: "ws1" }) === p2 && store.paneFor({}) === p1);
  store.setDraft(p1, "nháp dở");
  p1.files.value = [{ name: "x.pdf", size: 5, type: "application/pdf" }];
  store.activate(p2); store.activate(p1);
  ok("nháp + đính kèm còn sau khi đổi", p1.draft.value === "nháp dở" && p1.files.value.length === 1);
  const same = store.applySnapshot(snap({ conv: "c1", conversations: [{ id: "c1", title: "A", unread: 2, running: false }] }), store.makePane("_", null));
  ok("ảnh chụp của hội thoại đã mở dùng lại khung cũ", same === p1 && store.panes.value.length === 2);
  store.markSeen("c1");
  ok("đã đọc → 0", store.convs.value["_"][0].unread === 0);
}

// ---------------------------------------------------------------- nháp ở sessionStorage (khoá như trang cũ)
{
  const sto = mem();
  sto.setItem("cfh_mgr_draft:c:c9", "từ trang cũ");
  const store = createStore(S, { storage: sto });
  const p = store.applySnapshot(snap({ conv: "c9" }), null);
  ok("đọc nháp trang cũ", p.draft.value === "từ trang cũ");
  store.setDraft(p, "  ");
  ok("nháp rỗng → xoá khoá", sto.getItem("cfh_mgr_draft:c:c9") === null);
}

// ---------------------------------------------------------------- luồng message của một lượt
{
  const store = createStore(S, { storage: mem() });
  const p = store.applySnapshot(snap({ conv: "c1", conversations: [{ id: "c1", unread: 0, running: false }] }), null);
  store.activate(p);
  let fx = store.apply({ t: "run.start", run: "r1", conv: "c1", ws: "_", state: "started" });
  ok("run.start → khung đang trả lời", p.pending.value && p.pending.value.run === "r1" && store.convs.value["_"][0].running === true);
  fx = store.apply({ t: "msg.delta", run: "r1", conv: "c1", text: "Xin " });
  fx = fx.concat(store.apply({ t: "msg.delta", run: "r1", conv: "c1", text: "<b>chào</b>" }));
  ok("delta gom (chưa đổ vào signal)", p.pending.value.text === "" && fx.some((f) => f.fx === "flush"));
  store.flush();
  ok("flush một lần — chữ THÔ giữ nguyên", p.pending.value.text === "Xin <b>chào</b>", p.pending.value);
  store.apply({ t: "tool.start", run: "r1", conv: "c1", id: "t1", name: "Read", label: "Đọc a.py" });
  ok("tool.start", p.pending.value.tool === "Đọc a.py");
  store.apply({ t: "tool.end", run: "r1", conv: "c1", id: "t1", name: "Read" });
  ok("tool.end", p.pending.value.tool === "");
  store.apply({ t: "todo", run: "r1", conv: "c1", todo: { rows: [{ text: "x", status: "in_progress" }], n: 1, done: 0 } });
  ok("todo", p.pending.value.todo.n === 1);
  store.apply({ t: "act.item", run: "r1", conv: "c1", item: { kind: "action", summary: "Tạo agent", ok: true, audit_id: 7, run_id: "r1" } });
  store.apply({ t: "act.item", run: "r1", conv: "c1", item: { kind: "action", summary: "Giao task", ok: true, audit_id: 8, run_id: "r1" } });
  store.apply({ t: "act.item", run: "r1", conv: "c1", item: { kind: "action", summary: "Giao task", ok: true, audit_id: 8, run_id: "r1" } });
  const g = p.items.value.find((x) => x.kind === "actions");
  ok("act.item gom theo lượt + bỏ trùng", g && g.items.length === 2 && store.side.value.length === 2, g);
  store.apply({ t: "action.pending", run: "r1", conv: "c1", action: { kind: "question", id: 5, text: "Chọn?", status: "open", options: [] } });
  ok("câu hỏi lên dòng thời gian + việc chờ", p.items.value.some((x) => x.id === "q-5") && p.actions.value.length === 1);
  store.apply({ t: "action.resolved", run: "r1", conv: "c1", kind: "question", id: 5, status: "answered", answer: { keys: ["1"] } });
  ok("câu hỏi đã trả lời", p.items.value.find((x) => x.id === "q-5").q.status === "answered" && p.actions.value.length === 0);
  store.apply({ t: "msg.delta", run: "r1", conv: "c1", text: " nữa" });
  fx = store.apply({ t: "msg.done", run: "r1", conv: "c1", item: { kind: "manager", id: "e50", text: "Xin chào nữa", html: "<p>Xin chào nữa</p>", run_id: "r1", ts: today } });
  ok("msg.done thay khung đang chạy", !p.pending.value && p.items.value.filter((x) => x.kind === "manager").length === 1 && fx.some((f) => f.fx === "reply" && f.pane === p));
  store.apply({ t: "msg.done", run: "r1", conv: "c1", item: { kind: "manager", id: "e51", text: "Xin chào nữa", html: "<p>x</p>", run_id: "r1", ts: today } });
  ok("msg.done lặp của cùng lượt bị bỏ", p.items.value.filter((x) => x.kind === "manager").length === 1);
  store.apply({ t: "run.start", run: "r1", conv: "c1", state: "started" });
  ok("run.start của lượt đã có trả lời (phát lại) không mở lại khung chờ", !p.pending.value);
  store.apply({ t: "run.end", run: "r1", conv: "c1", ok: true, outcome: "completed", label: "Xong", tone: "ok" });
  ok("run.end ok: không ghi chú, hết chạy", !p.items.value.some((x) => x.kind === "note") && store.convs.value["_"][0].running === false);
  store.apply({ t: "run.start", run: "r2", conv: "c1" });
  store.apply({ t: "run.end", run: "r2", conv: "c1", ok: false, outcome: "failed", label: "Lỗi", tone: "danger" });
  ok("run.end lỗi → ghi chú", p.items.value.some((x) => x.kind === "note" && x.run_id === "r2") && !p.pending.value);
  ok("vạch ngày tự thêm", p.items.value[0].kind === "day" && p.items.value.filter((x) => x.kind === "day").length === 1, p.items.value.map((x) => x.kind));
}

// ---------------------------------------------------------------- tab luồng: ws.status + tabs.patch
{
  const store = createStore(S, { storage: mem() });
  store.applySnapshot(snap({}), null);
  store.apply({ t: "ws.status", ...tab("ws1", { dot: "run", unread: 3 }) });
  ok("ws.status cập nhật tại chỗ", store.tabs.value.active[0].dot === "run" && store.tabs.value.active[0].unread === 3);
  store.apply({ t: "ws.status", ...tab("_", { unread: 1 }) });
  ok("ws.status Sảnh", store.tabs.value.lobby.unread === 1);
  store.apply({ t: "ws.status", ...tab("ws3", { title: "Mới" }) });
  ok("luồng mới chờ tabs.patch", store.tabs.value.active.length === 2);
  store.apply({ t: "tabs.patch", active: ["ws3", "ws1"], closed: ["ws2"] });
  ok("tabs.patch xếp lại (luồng đóng sang closed)", store.tabs.value.active.map((t) => t.ws).join() === "ws3,ws1" && store.tabs.value.closed[0].ws === "ws2");
  ok("tick → làm mới trạng thái", store.apply({ t: "tick" }).some((f) => f.fx === "status"));
  const fx = store.apply({ t: "resync", reason: "gap" });
  ok("resync → mọi khung cũ + hiệu ứng", fx[0].fx === "resync" && store.panes.value.every((p) => p.stale));
}

// ---------------------------------------------------------------- gửi lạc quan + message tới trước phản hồi gửi
{
  const store = createStore(S, { storage: mem() });
  const p = store.applySnapshot(snap({ scope: "ws1", ws: "ws1", conversations: [] }), null);
  store.activate(p);
  store.setDraft(p, "làm X");
  const f = [{ name: "a.png", size: 10, type: "image/png" }];
  p.files.value = f;
  const tok = store.beginSend(p, "làm X", f);
  ok("gửi lạc quan: tin hiện ngay, ô trống, khung chờ", p.items.value.some((x) => x.optimistic && x.text.includes("📎 a.png")) && p.draft.value === "" && p.files.value.length === 0 && p.pending.value.waiting);
  // SSE tới TRƯỚC phản hồi của /manager/send (hội thoại mới — khung chưa biết id)
  store.apply({ t: "run.start", run: "r9", conv: "cNew", ws: "ws1" });
  store.apply({ t: "msg.delta", run: "r9", conv: "cNew", text: "đang làm" });
  ok("message sớm được giữ", store.early.length === 2);
  store.sendOk(p, tok, { conversation_id: "cNew", run_id: "r9", steered: false });
  ok("phản hồi gửi: gắn hội thoại + lượt, phát lại message sớm", p.conv.value === "cNew" && p.pending.value.run === "r9" && p.pending.value.text === "đang làm"
     && store.early.length === 0 && !p.items.value.some((x) => x.optimistic), p.pending.value);
  ok("hội thoại mới vào danh sách", (store.convs.value["ws1"] || []).some((c) => c.id === "cNew"));
  const tok2 = store.beginSend(p, "thêm", []);
  store.sendFail(p, tok2, "Hub chưa trả lời kịp");
  ok("gửi lỗi: rút tin lạc quan, trả chữ về ô, báo lỗi, giữ khung đang chạy", !p.items.value.some((x) => x.id === tok2.id) && p.draft.value === "thêm"
     && p.sendErr.value === "Hub chưa trả lời kịp" && p.pending.value && p.pending.value.run === "r9");
}

// ---------------------------------------------------------------- client SSE: bỏ trùng theo id, nối lại với since, boot mới
{
  const made = [];
  class FakeES {
    constructor(url) { this.url = url; this.readyState = 0; made.push(this); }
    close() { this.readyState = 2; }
    emit(data, id) { this.onmessage({ data: JSON.stringify(data), lastEventId: id || "" }); }
  }
  const timers = [];
  const got = [];
  const feed = createFeed({ EventSource: FakeES, tab: "t1", since: "b1.10", scope: () => ({ c: ["c1"], w: ["_"] }),
    onMessage: (m) => got.push(m), timers: { set: (f) => { timers.push(f); return timers.length; }, clear: () => {} } });
  const es = made[0];
  ok("URL đầu có since + phạm vi + tab", es.url.includes("since=b1.10") && es.url.includes("c=c1") && es.url.includes("w=_") && es.url.includes("tab=t1"), es.url);
  es.emit({ t: "hello", boot: "b1" });
  es.emit({ t: "msg.delta", text: "a" }, "b1.11");
  es.emit({ t: "msg.delta", text: "a" }, "b1.11");
  es.emit({ t: "msg.delta", text: "old" }, "b1.9");
  es.emit({ t: "msg.delta", text: "b" }, "b1.12");
  ok("bỏ trùng theo id", got.filter((m) => m.t === "msg.delta").map((m) => m.text).join("") === "ab");
  es.readyState = 2; es.onerror();
  ok("lỗi cứng → hẹn mở lại", timers.length === 1);
  timers[0]();
  ok("mở lại với since = id cuối", made[1].url.includes("since=b1.12"), made[1].url);
  made[1].emit({ t: "hello", boot: "b2" });
  made[1].emit({ t: "resync", reason: "boot" }, "b2.4");
  made[1].emit({ t: "msg.delta", text: "c" }, "b2.5");
  ok("boot mới: nhận resync + id mới", got.some((m) => m.t === "resync") && got[got.length - 1].text === "c" && feed.last === "b2.5");
  feed.close();
}

if (fails) { console.log("FAIL tổng " + fails); process.exit(1); }
console.log("ALL OK");
