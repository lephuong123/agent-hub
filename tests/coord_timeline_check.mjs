// Island Điều phối (GĐ2 phần 2) — kiểm logic THUẦN bằng Node: cuộn dính đáy, gom delta theo khung hình, bỏ trùng theo id, xếp thẻ duyệt
// theo giờ, nhãn ngày, lọc URL chữ đang chạy, duyệt/trả lời lạc quan + hoàn tác. Chạy: node tests/coord_timeline_check.mjs <gốc repo>
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
const SC = await imp("coord/scroll.js");

let fails = 0;
function ok(name, cond, extra) {
  if (cond) console.log("ok " + name);
  else { fails++; console.log("FAIL " + name + (extra !== undefined ? " " + JSON.stringify(extra) : "")); }
}
const tab = (ws) => ({ ws, key: null, title: ws, label: ws, status: "active", dot: "none", dot_label: "", unread: 0, tasks: {} });
function snap(o) {
  return { v: 1, cursor: "b1.10", boot: "b1", manager: { available: true, agent_id: "dp" }, conv: "c1", ws: null, scope: "_", title: null,
           conversations: [{ id: "c1", unread: 0, running: false }], workstreams: { enabled: true, lobby: tab("_"), active: [], closed: [] },
           items: [], pending: null, actions: [], side_actions: [], conv_runs: [], workstream: null, ws_convs: [], ...o };
}
const now = new Date();
const iso = (d) => d.toISOString();

// ---------------------------------------------------------------- cuộn dính đáy
{
  let s = SC.initScroll();
  const m = (top) => ({ top, height: 2000, client: 500 });  // đáy = 1500
  s = SC.onScroll(s, m(1500));
  ok("ở đáy → dính", s.stick && !s.unseen);
  let r = SC.onGrow(s, true);
  ok("dính + nội dung lớn thêm → ghim đáy", r.pin && !r.s.unseen);
  const clamp = SC.onScroll(s, { top: 1498, height: 2200, client: 502 });
  ok("trình duyệt kẹp scrollTop (ô nhập co lại) + nội dung lớn thêm, KHÔNG do người → vẫn dính", clamp.stick, clamp);
  s = SC.onScroll(s, m(1480), true);
  ok("người cuộn LÊN dù chỉ 20px (trong ngưỡng) → bỏ dính", !s.stick, s);
  r = SC.onGrow(s, true);
  ok("không dính + nội dung mới → không ghim, hiện 'Tin mới nhất'", !r.pin && r.s.unseen);
  s = r.s;
  ok("nội dung co lại (không lớn thêm) không bật nút", !SC.onGrow({ ...s, unseen: false }, false).s.unseen);
  s = SC.onScroll(s, m(1000), true);
  ok("cuộn lên tiếp: vẫn bỏ dính, còn tin chưa thấy", !s.stick && s.unseen);
  s = SC.onScroll(s, m(1470));
  ok("cuộn XUỐNG vào gần đáy → dính lại, hết tin chưa thấy", s.stick && !s.unseen);
  s = SC.onUserUp(s);
  ok("lăn chuột lên → bỏ dính trước cả sự kiện scroll", !s.stick);
  s = SC.onJump(s);
  ok("bấm 'Tin mới nhất' → dính", s.stick && !s.unseen);
  const hid = SC.onScroll({ stick: false, top: 900, unseen: true }, { top: 0, height: 0, client: 0 });
  ok("khung ẩn (display:none, scrollTop về 0) không ghi đè vị trí đã nhớ", hid.top === 900 && !hid.stick);
  const shrink = SC.onScroll({ stick: true, top: 1500, unseen: false }, { top: 1300, height: 1800, client: 500 });
  ok("nội dung co lại kéo scrollTop xuống nhưng vẫn ở đáy → vẫn dính", shrink.stick);
}

// ---------------------------------------------------------------- gom delta: MỘT lần đổ mỗi khung hình
{
  const frames = [];
  let flushes = 0;
  const soon = U.rafBatch((f) => frames.push(f), () => flushes++);
  for (let i = 0; i < 100; i++) soon();
  ok("100 lần gọi → 1 khung hình được hẹn", frames.length === 1);
  frames.shift()();
  soon();
  ok("sau khi chạy, lần gọi mới hẹn khung kế", flushes === 1 && frames.length === 1);

  const store = createStore(S, { storage: null });
  const p = store.applySnapshot(snap({}), null);
  store.activate(p);
  let renders = 0;
  S.effect(() => { void p.pending.value; renders++; });
  const r0 = renders, runs = [];
  const kick = U.rafBatch((f) => runs.push(f), () => store.flush());
  store.apply({ t: "run.start", run: "r1", conv: "c1" });
  const base = renders;
  let words = "";
  for (let i = 0; i < 100; i++) {
    const t = "từ" + i + " ";
    words += t;
    for (const f of store.apply({ t: "msg.delta", run: "r1", conv: "c1", text: t })) if (f.fx === "flush") kick();
  }
  ok("100 delta: chưa vẽ lại lần nào trước khung hình", renders === base && p.pending.value.text === "", { renders, base, r0 });
  ok("100 delta: hẹn đúng một khung hình", runs.length === 1);
  runs.shift()();
  ok("khung hình: đổ MỘT lần, đủ chữ, đúng thứ tự", renders === base + 1 && p.pending.value.text === words);
  ok("computed runId/busy không đổi khi chỉ chữ đổi", p.runId.value === "r1" && p.busy.value === true);
  store.apply({ t: "tool.start", run: "r1", conv: "c1", id: "a", name: "Read", label: "Đọc a" });
  store.apply({ t: "tool.end", run: "r1", conv: "c1", id: "a" });
  store.apply({ t: "tool.start", run: "r1", conv: "c1", id: "b", name: "Bash", label: "Chạy b" });
  ok("đếm tool của lượt (Đang dùng X (+n))", p.pending.value.tools === 2 && p.pending.value.tool === "Chạy b");
  store.apply({ t: "msg.delta", run: "r1", conv: "c1", text: "cuối" });
  store.apply({ t: "msg.done", run: "r1", conv: "c1", item: { kind: "manager", id: "e9", text: words + "cuối", html: "<p>x</p>", run_id: "r1", ts: iso(now) } });
  ok("msg.done: bộ đệm đổ trước khi thay bằng HTML server, khung chờ đóng", !p.pending.value && !store.bufs.has("r1") && p.empty.value === false);
}

// ---------------------------------------------------------------- bỏ trùng theo id (SSE + mục dòng thời gian)
{
  const store = createStore(S, { storage: null });
  const it = { kind: "manager", id: "m7", text: "đã lưu", html: "<p>đã lưu</p>", run_id: "r7", ts: iso(now) };
  const p = store.applySnapshot(snap({ items: [it], conv_runs: ["r7"] }), null);
  store.apply({ t: "msg.done", run: "r7", conv: "c1", item: { ...it, id: "e70" } });
  ok("msg.done của lượt đã có trong ảnh chụp (m<id>) → bỏ", p.items.value.filter((x) => x.kind === "manager").length === 1);
  store.apply({ t: "action.pending", run: "r7", conv: "c1", action: { kind: "question", id: 3, text: "?", status: "open", options: [] } });
  store.apply({ t: "action.pending", run: "r7", conv: "c1", action: { kind: "question", id: 3, text: "? (sửa)", status: "open", options: [] } });
  ok("action.pending lặp → cập nhật tại chỗ, không nhân đôi", p.items.value.filter((x) => x.id === "q-3").length === 1 && p.actions.value.length === 1
     && p.items.value.find((x) => x.id === "q-3").q.text === "? (sửa)");
  const got = [];
  class ES { constructor() { this.readyState = 1; ES.last = this; } close() {} }
  const feed = createFeed({ EventSource: ES, tab: "t", since: "", onMessage: (m) => got.push(m) });
  const send = (id, n) => ES.last.onmessage({ data: JSON.stringify({ t: "msg.delta", n }), lastEventId: id });
  send("b1.1", 1); send("b1.2", 2); send("b1.2", 2); send("b1.1", 1); send("b1.3", 3);
  ok("SSE: id đã xử lý (nối lại phát lại) → bỏ, 0 trùng 0 mất", got.map((m) => m.n).join() === "1,2,3");
  feed.close();
}

// ---------------------------------------------------------------- dòng thời gian: thẻ duyệt theo giờ, nhãn ngày
{
  const t = (h) => iso(new Date(2026, 10, 23, h));
  const items = [{ kind: "day", id: "d1", key: "2026-11-23" }, { kind: "user", id: "a", ts: t(9) }, { kind: "day", id: "d2", key: "2026-11-24" },
                 { kind: "manager", id: "b", ts: iso(new Date(2026, 10, 24, 8)) }];
  const out = U.mergeTimeline(items, [{ kind: "approval", id: "ap1", ts: t(10) }, { kind: "approval", id: "ap0", ts: t(8) }, { kind: "approval", id: "apx" }]);
  ok("thẻ duyệt xếp theo giờ (trước mục mới hơn, không chen giữa vạch ngày và mục)", out.map((x) => x.id).join() === "d1,ap0,a,ap1,d2,b,apx", out.map((x) => x.id));
  ok("không có thẻ → trả nguyên mảng (giữ object cho Row)", U.mergeTimeline(items, []) === items);
  const today = new Date(2026, 10, 23, 15);
  ok("nhãn ngày như present.day_label", U.dayLabel("2026-11-23", today) === "Hôm nay" && U.dayLabel("2026-11-22", today) === "Hôm qua"
     && U.dayLabel("2026-11-16", today) === "Thứ Hai, 16/11/2026" && U.dayLabel("2026-11-15", today) === "Chủ nhật, 15/11/2026" && U.dayLabel("2026-11-10", today) === "Thứ Ba, 10/11/2026", U.dayLabel("2026-11-10", today));
  const store = createStore(S, { storage: null });
  const p = store.applySnapshot(snap({}), null);
  const y = new Date(now); y.setDate(y.getDate() - 1);
  store.apply({ t: "act.item", run: "rY", conv: "c1", item: { kind: "action", summary: "x", ok: true, audit_id: 1, run_id: "rY", ts: iso(y) } });
  ok("vạch ngày client: 'Hôm qua' (không còn YYYY-MM-DD thô)", p.items.value[0].kind === "day" && p.items.value[0].text === "Hôm qua", p.items.value[0]);
}

// ---------------------------------------------------------------- hội thoại dài: vẽ dần + content-visibility cố định theo mục
{
  const ST = await imp("coord/store.js");
  const store = createStore(S, { storage: null });
  const many = Array.from({ length: 2000 }, (_, i) => ({ kind: i % 2 ? "manager" : "user", id: "m" + i, text: "x", ts: iso(now) }));
  const p = store.applySnapshot(snap({ items: many }), null);
  ok("2000 mục: chỉ vẽ FIRST_SHOW mục cuối lúc mở", p.hide.value === 2000 - ST.FIRST_SHOW && ST.FIRST_SHOW <= 120, p.hide.value);
  ok("content-visibility: mục cũ có, 12 mục cuối không", p.cv.has("m0") && p.cv.has("m1987") && !p.cv.has("m1988") && !p.cv.has("m1999"));
  store.apply({ t: "act.item", run: "rN", conv: "c1", item: { kind: "action", summary: "mới", ok: true, audit_id: 99, run_id: "rN", ts: iso(now) } });
  ok("mục mới tới sau: không đổi số mục ẩn, không content-visibility", p.hide.value === 2000 - ST.FIRST_SHOW && !p.cv.has(p.items.value[p.items.value.length - 1].id));
  const q = store.applySnapshot(snap({ conv: "c9", items: many.slice(0, 10) }), store.makePane("_", null));
  ok("hội thoại ngắn: vẽ hết, không content-visibility", q.hide.value === 0 && q.cv.size === 0);
}

// ---------------------------------------------------------------- URL trong chữ đang chạy (markdown của agent là dữ liệu)
{
  const bad = ["javascript:alert(1)", " JaVaScRiPt:alert(1)", "data:text/html,<b>", "vbscript:x", "java\tscript:x", "//evil.example/x", "/\\evil.example", "file:///etc/passwd"];
  const good = ["https://a.example/x?y=1", "http://a", "mailto:a@b.c", "/runs/r1", "./a.md", "#neo", "docs/a.md"];
  ok("safeUrl chặn scheme nguy hiểm", bad.every((u) => U.safeUrl(u) === null), bad.map(U.safeUrl));
  ok("safeUrl giữ link thường", good.every((u) => U.safeUrl(u) === u.trim()), good.map(U.safeUrl));
  const a = U.newIdem(), b = U.newIdem();
  ok("idem khác nhau mỗi lần bấm", a !== b && /^i\d+-[a-z0-9]+$/i.test(a), [a, b]);
}

// ---------------------------------------------------------------- duyệt / trả lời lạc quan + đối chiếu + hoàn tác
{
  const store = createStore(S, { storage: null });
  const ap = { kind: "approval", id: "ap1", run_id: "r1", tool: "Bash", text: "rm -rf build", status: "waiting_human", created_at: iso(now) };
  const p = store.applySnapshot(snap({ actions: [ap] }), null);
  const cur = () => p.actions.value.find((x) => x.id === "ap1");
  let tok = store.actBegin(p, "approve", "ap1", { idem: "k1" });
  ok("cho phép: hiện 'Đã cho phép' ngay (đang gửi)", cur().done === "allowed" && cur().busy);
  store.actFail(tok, "Lỗi 503", { retry: true });
  ok("lỗi mạng → hoàn tác + lỗi + giữ idem để bấm lại", !cur().done && !cur().busy && cur().err === "Lỗi 503" && cur().retryIdem === "k1");
  tok = store.actBegin(p, "approve", "ap1", { idem: cur().retryIdem });
  ok("bấm lại dùng CÙNG idem", tok.idem === "k1");
  store.actOk(tok);
  ok("server nhận → hết 'đang gửi', vẫn hiện đã cho phép", cur().done === "allowed" && !cur().busy);
  store.apply({ t: "action.resolved", run: "r1", conv: "c1", kind: "approval", id: "ap1", status: "allowed" });
  ok("action.resolved chốt trạng thái (thẻ còn, đã xong)", cur().done === "allowed" && cur().status === "allowed" && !p.empty.value);
  // server chốt TRƯỚC khi phản hồi lỗi tới → không hoàn tác
  const p2 = store.applySnapshot(snap({ conv: "c2", actions: [{ ...ap, id: "ap2" }] }), store.makePane("_", null));
  const t2 = store.actBegin(p2, "deny", "ap2", { idem: "k2" });
  store.apply({ t: "action.resolved", conv: "c2", kind: "approval", id: "ap2", status: "denied" });
  store.actFail(t2, "timeout");
  const c2 = p2.actions.value[0];
  ok("đã chốt bởi action.resolved → lỗi tới sau KHÔNG hoàn tác", c2.done === "denied" && !c2.err, c2);

  const q = { id: 9, agent_id: "dev", text: "Chọn?", options: [{ key: "1", label: "A" }, { key: "2", label: "B" }], status: "open", allow_free_text: true };
  const p3 = store.applySnapshot(snap({ conv: "c3", items: [{ kind: "question", id: "q-9", q }], actions: [{ kind: "question", ...q }] }), store.makePane("_", null));
  const qq = () => p3.items.value.find((x) => x.id === "q-9").q;
  const t3 = store.actBegin(p3, "answer", 9, { idem: "k3", answer: { keys: ["2"], labels: ["B"], text: "thêm" } });
  ok("trả lời: hiện 'đã trả lời' ngay", qq().status === "answered" && qq().busy && qq().answer.keys[0] === "2");
  store.actFail(t3, "Câu hỏi đã đóng", { draft: { keys: ["2"], text: "thêm" } });
  ok("lỗi → câu hỏi mở lại, giữ lựa chọn + chữ đã nhập", qq().status === "open" && qq().err === "Câu hỏi đã đóng" && qq().draft.keys[0] === "2" && qq().draft.text === "thêm");
  const t4 = store.actBegin(p3, "answer", 9, { idem: "k4", answer: { keys: ["1"], labels: ["A"] } });
  store.apply({ t: "action.resolved", conv: "c3", kind: "question", id: 9, status: "answered", answer: { keys: ["1"], labels: ["A"] }, answered_by: "user" });
  store.actOk(t4);
  ok("action.resolved + ok → đã trả lời, hết việc chờ", qq().status === "answered" && !qq().busy && p3.actions.value.length === 0);
}

if (fails) { console.log("FAIL tổng " + fails); process.exit(1); }
console.log("ALL OK");
