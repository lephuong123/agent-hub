// Kiểm chính sách giao diện của chế độ đi bộ KHÔNG cần trình duyệt (Node ≥ 18) — các hàm thuần trong static/office/{walk,util}.js:
// dòng gợi ý ẩn khi có bảng modal / chat / bảng đang xem và nằm ở nửa dưới màn hình (không đè biển tên khu vực, đầu hộp thoại);
// không tự chọn agent theo khoảng cách khi đang mở các thứ đó; nhãn tên tránh chồng (declutter); nhịp vẽ (frameGate) và LOD nhân vật
// (docs/dev-notes/office-3d-perf.md). In "ok …" mỗi ca; lỗi → exit 1.
// Chạy: node tests/office_walk_ui_check.mjs <thư mục gốc repo>   (tests/test_office_walk.py gọi script này)
const root = process.argv[2].replace(/\\/g, '/');
const url = (p) => (root.startsWith('/') ? 'file://' : 'file:///') + root + '/src/cfharness/web/static/office/' + p;
const ctx = new Proxy({}, {
  get: (t, k) => (k === 'measureText' ? () => ({ width: 10 }) : k in t ? t[k] : () => ({ addColorStop() {} })),
  set: (t, k, v) => { t[k] = v; return true; },
});
globalThis.document = { createElement: () => ({ width: 0, height: 0, getContext: () => ctx, style: {} }) };
const { walkBusy, hintShown, hintLayout, lookDrag, LOOK_SENS } = await import(url('walk.js'));
const { declutter, frameGate, MOTION_FPS, IDLE_FPS } = await import(url('util.js'));
const { Chibi, updateChibiLod } = await import(url('chibi.js'));

let fails = 0;
const check = (name, cond) => { if (cond) console.log('ok ' + name); else { fails++; console.log('FAIL ' + name); } };
const base = { walk: true, modal: false, chat: false, board: false };
check('hint: hiện khi đi bộ, không có gì mở', hintShown(base) === true);
check('hint: ẩn ngoài chế độ đi bộ', hintShown({ ...base, walk: false }) === false);
for (const k of ['modal', 'chat', 'board']) {
  check(`hint: ẩn khi ${k} mở`, hintShown({ ...base, [k]: true }) === false);
  check(`auto-select: bỏ qua khi ${k} mở`, walkBusy({ ...base, [k]: true }) === true);
}
check('auto-select: được khi không có gì mở', walkBusy(base) === false);
for (const [fov, aspect] of [[45, 16 / 9], [45, 0.5], [60, 2.4], [45, 1]]) {
  const L = hintLayout(fov, aspect), vh = 2 * L.d * Math.tan((fov * Math.PI) / 360), vw = vh * aspect;
  check(`hint layout fov=${fov} aspect=${aspect.toFixed(2)}: nửa dưới, trong khung, gọn`,
    L.y + L.h / 2 < -vh * 0.25 && L.y - L.h / 2 > -vh / 2 && L.w <= vw * 0.8 && L.h <= vh * 0.12);
}
// declutter: hai nhãn chồng nhau → giữ nhãn ưu tiên cao; nhãn thưa → giữ hết
const keep = declutter([
  { id: 'a', x: 100, y: 100, w: 80, h: 20, pri: 1 },
  { id: 'b', x: 110, y: 105, w: 80, h: 20, pri: 5 },
  { id: 'c', x: 400, y: 100, w: 80, h: 20, pri: 0 },
]);
check('declutter: giữ nhãn ưu tiên cao khi chồng, giữ nhãn thưa', keep.has('b') && !keep.has('a') && keep.has('c'));
const crowd = [];
for (let i = 0; i < 19; i++) crowd.push({ id: 'g' + i, x: 300 + (i % 5) * 12, y: 300 + Math.floor(i / 5) * 6, w: 90, h: 22, pri: 0 });
const kc = declutter(crowd);
check(`declutter: cụm 19 nhãn chồng → còn ít (${kc.size})`, kc.size >= 1 && kc.size <= 2);

// kéo nhìn: dưới ngưỡng 5 px = còn là cú bấm; lần đầu vượt ngưỡng không nhảy (delta đầu ≤ 1 px), sau đó delta đúng từng bước
{
  const down = { x: 100, y: 100, lx: 100, ly: 100, drag: false };
  const a = lookDrag(down, 103, 102, 5), b = lookDrag(down, 106, 101, 5), c = lookDrag(down, 110, 101, 5);
  check(`kéo nhìn: dưới ngưỡng null, vượt ngưỡng: delta đầu ${b && Math.hypot(b.dx, b.dy)} px (≤ 1), sau đó ${c && c.dx} px`,
    a === null && b && Math.hypot(b.dx, b.dy) <= 1 && down.drag && c && c.dx === 4 && c.dy === 0 && LOOK_SENS === 0.006);
}
// nhịp vẽ: mô phỏng rAF theo tần số màn hình, đếm khung được vẽ trong 1 s
const drawn = (hz, active) => {
  const gap = 1000 / hz; let last = 0, n = 0;
  for (let ts = gap; ts <= 1000 + 1e-6; ts += gap) {
    const t = ts + Math.sin(ts) * 0.3; // rung nhịp rAF ±0,3 ms
    if (!last || frameGate(t, last, gap, active)) { last = t; n++; }
  }
  return n;
};
check('frameGate: 60 Hz động = mọi khung (60)', drawn(60, true) === 60);
check('frameGate: 60 Hz yên = 30 đều', drawn(60, false) === 30);
check('frameGate: 120 Hz động = 60, yên = 30', drawn(120, true) === 60 && drawn(120, false) === 30);
check('frameGate: 144 Hz động ≤ 72, ≥ 48', drawn(144, true) <= 72 && drawn(144, true) >= 48);
check('frameGate: rAF chậm (40 Hz, máy bận) yên vẫn vẽ mọi rAF (không tụt về 20)', drawn(40, false) === 40);
check('frameGate: hằng số MOTION 60 / IDLE 30', MOTION_FPS === 60 && IDLE_FPS === 30);

// LOD nhân vật: hình xa ≈ ¼ tam giác, cùng thuộc tính (cùng xương/vật liệu); đổi theo cỡ trên màn hình, có trễ
for (const [id, mgr] of [['ag1', false], ['ag7', false], ['mgr', true]]) {
  const c = new Chibi({ id, name: id, color: '#c96442', isManager: mgr });
  const tn = c.geoNear.attributes.position.count / 3, tf = c.geoFar.attributes.position.count / 3;
  const same = Object.keys(c.geoNear.attributes).sort().join() === Object.keys(c.geoFar.attributes).sort().join();
  check(`LOD ${id}: xa ${tf} < 35 % gần ${tn}, cùng thuộc tính, mặc định gần`, tf < tn * 0.35 && tf > 800 && same && c.mesh.geometry === c.geoNear);
  const cam = { fov: 36, matrixWorld: { elements: new Array(16).fill(0) } };
  c.group.updateMatrixWorld(true);
  const at = (d) => { cam.matrixWorld.elements[14] = d; updateChibiLod(cam, 900); return c.mesh.geometry === c.geoFar; };
  check(`LOD ${id}: xa 50 m → hình xa, gần 6 m → hình gần, trễ quanh mốc`, at(50) && !at(6) && !at(22) && at(30) && at(22));
  c.dispose();
}
process.exit(fails ? 1 : 0);
