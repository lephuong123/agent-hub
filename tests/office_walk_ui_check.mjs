// Kiểm chính sách giao diện của chế độ đi bộ KHÔNG cần trình duyệt (Node ≥ 18) — các hàm thuần trong static/office/{walk,util}.js:
// dòng gợi ý ẩn khi có bảng modal / chat / bảng đang xem và nằm ở nửa dưới màn hình (không đè biển tên khu vực, đầu hộp thoại);
// không tự chọn agent theo khoảng cách khi đang mở các thứ đó; nhãn tên tránh chồng (declutter). In "ok …" mỗi ca; lỗi → exit 1.
// Chạy: node tests/office_walk_ui_check.mjs <thư mục gốc repo>   (tests/test_office_walk.py gọi script này)
const root = process.argv[2].replace(/\\/g, '/');
const url = (p) => (root.startsWith('/') ? 'file://' : 'file:///') + root + '/src/cfharness/web/static/office/' + p;
const ctx = new Proxy({}, {
  get: (t, k) => (k === 'measureText' ? () => ({ width: 10 }) : k in t ? t[k] : () => ({ addColorStop() {} })),
  set: (t, k, v) => { t[k] = v; return true; },
});
globalThis.document = { createElement: () => ({ width: 0, height: 0, getContext: () => ctx, style: {} }) };
const { walkBusy, hintShown, hintLayout } = await import(url('walk.js'));
const { declutter } = await import(url('util.js'));

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
process.exit(fails ? 1 : 0);
