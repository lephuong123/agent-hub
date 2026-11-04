// Kiểm bộ điều nhịp khung vẽ (static/office/pacer.js) KHÔNG cần trình duyệt (Node ≥ 18): mô phỏng chuỗi rAF, đếm khung vẽ, khoá,
// dt (docs/dev-notes/office-3d-perf.md, phase 2). In "ok …" mỗi ca; lỗi → exit 1.
// Chạy: node tests/office_pacer_check.mjs <thư mục gốc repo>   (tests/test_office_walk.py gọi script này)
const root = process.argv[2].replace(/\\/g, '/');
const url = (p) => (root.startsWith('/') ? 'file://' : 'file:///') + root + '/src/cfharness/web/static/office/' + p;
const mod = await import(url('pacer.js'));
const { createPacer } = mod;

let fails = 0;
const check = (name, cond) => { if (cond) console.log('ok ' + name); else { fails++; console.log('FAIL ' + name); } };

// chạy pacer trên chuỗi thời điểm rAF; motion(ts) → có chuyển động; trả các thời điểm vẽ + dt
function sim(times, motion, p) {
  p = p || createPacer({ now: () => 0 });
  const drawn = [], dts = [];
  let first = true;
  for (const ts of times) {
    const m = motion(ts);
    if (first || p.shouldDraw(ts, m)) { first = false; if (drawn.length === 0) p.shouldDraw(ts, m); dts.push(p.drew(ts)); drawn.push(ts); }
  }
  return { p, drawn, dts };
}
const even = (hz, ms, t0 = 0) => { const out = [], g = 1000 / hz; for (let t = t0 + g; t <= t0 + ms + 1e-6; t += g) out.push(t + Math.sin(t) * 0.3); return out; };
const ivs = (a) => a.slice(1).map((t, i) => t - a[i]);
const sd = (a) => { const m = a.reduce((x, y) => x + y, 0) / a.length; return Math.sqrt(a.reduce((x, y) => x + (y - m) ** 2, 0) / a.length); };
const inWin = (a, t0, t1) => a.filter((t) => t >= t0 && t < t1).length;

// 60 Hz đều: động 60, yên 30, không khoá
{
  const r = sim(even(60, 4000), (t) => t < 2000);
  const mv = inWin(r.drawn, 1000, 2000), id = inWin(r.drawn, 3000, 4000);
  check(`60 Hz: động ${mv} ≈ 60, yên ${id} ≈ 30, locks ${r.p.state().locks}`, mv >= 59 && mv <= 61 && id >= 29 && id <= 31 && r.p.state().locks === 0);
}
// 120 Hz: động 60, yên 30
{
  const r = sim(even(120, 4000), (t) => t < 2000);
  const mv = inWin(r.drawn, 1000, 2000), id = inWin(r.drawn, 3000, 4000);
  check(`120 Hz: động ${mv} ≈ 60, yên ${id} ≈ 30`, mv >= 59 && mv <= 61 && id >= 29 && id <= 31);
}
// 40 Hz đều (máy bận): vẽ mọi rAF dù đứng yên
{
  const t = even(40, 3000), r = sim(t, () => false);
  check(`40 Hz yên: vẽ mọi rAF (${r.drawn.length}/${t.length})`, r.drawn.length === t.length);
}
// Quá tải (CPU bị tranh): mỗi khung vẽ thứ 3 tốn 20 ms JS (> 16,7) → rAF sau đó trễ 1 vsync → khoảng khung 16,7/33,4 xen kẽ ~30 %.
// Mô phỏng theo sự kiện: rAF kế tiếp = vsync đầu tiên sau khi luồng chính rảnh. Phải khoá trong ≤ 30 khung, sau đó đều 33,4 (sd ≤ 1 ms).
{
  const V = 1000 / 60, p = createPacer({ now: () => 0 });
  let t = 0, first = true, n = 0, at = -1, k = 0;
  const drawn = [];
  while (t < 5000) {
    const m = t > 1000; // 1 s đầu đứng yên (ước lượng vsync), sau đó động
    let cost = 1;
    if (first || p.shouldDraw(t, m)) {
      first = false; p.drew(t); drawn.push(t);
      cost = m ? ((k++ % 3) === 1 ? 20 : 10) : 10;
      if (m) { n++; if (at < 0 && p.state().locks) at = n; }
    }
    t = Math.max(t + V, Math.ceil((t + cost) / V - 1e-9) * V);
  }
  const tail = drawn.filter((x) => x > 2000 && x < 2900), tiv = ivs(tail), mean = tiv.reduce((a, b) => a + b, 0) / tiv.length;
  check(`quá tải 16,7/33,4: khoá sau ${at} khung (≤ 30), sau đó đều (sd ${sd(tiv).toFixed(2)} ms, TB ${mean.toFixed(1)} ms)`,
    at > 0 && at <= 30 && sd(tiv) <= 1 && Math.abs(mean - 33.3) < 1);
}
// gai đơn 120 ms khi đang động: không khoá
{
  const t = even(60, 3000).filter((x) => x < 1500 || x > 1620);
  const r = sim(t, () => true);
  check(`gai đơn 120 ms: không khoá (locks ${r.p.state().locks})`, r.p.state().locks === 0);
}
// hitch(500) rồi 5 khung trễ: không khoá
{
  const p = createPacer({ now: () => 0 });
  const base = even(60, 1500);
  const lateT = []; let t = 1500; for (let i = 0; i < 5; i++) { t += 1000 / 30; lateT.push(t); }
  const rest = even(60, 1500, t);
  let first = true;
  for (const x of base) { if (first || p.shouldDraw(x, true)) { first = false; p.drew(x); } }
  p.hitch(500, 1500);
  for (const x of [...lateT, ...rest]) if (p.shouldDraw(x, true)) p.drew(x);
  check(`hitch(500) + 5 khung trễ: không khoá (locks ${p.state().locks})`, p.state().locks === 0);
}
// khoá lại liên tiếp: lockMs 2000 → 4000 → 8000, sạch 10 s thì về 2000
{
  const p = createPacer({ now: () => 0 });
  let t = 0; const step = (g, m) => { t += g; if (p.shouldDraw(t, m)) p.drew(t); };
  for (let i = 0; i < 60; i++) step(1000 / 60, false);
  const overload = (ms) => { const end = t + ms; let k = 0; while (t < end) step(1000 / 60 * ((k++ % 3) === 1 ? 2 : 1), true); };
  const seen = [];
  let prevLocks = 0;
  for (let round = 0; round < 3; round++) {
    // quá tải tới khi khoá
    let guard = 0; while (p.state().locks === prevLocks && guard++ < 400) overload(17);
    prevLocks = p.state().locks; seen.push(p.state().lockMs);
    // chờ tới khi nhả khoá (vẫn quá tải ngay sau đó → khoá lại trong 1,5 s)
    guard = 0; while (p.state().cap && guard++ < 2000) overload(17);
  }
  // sạch 10 s
  for (let i = 0; i < 660; i++) step(1000 / 60, true);
  const back = p.state().lockMs;
  check(`khoá lại liên tiếp: lockMs ${seen.join(' → ')}, sạch 10 s → ${back}`, seen.join() === '2000,4000,8000' && back === 2000);
}
// dt lượng tử: vsync 16,67 ổn định, khung đến sau 17,9 ms → dt = 1/60; 34 ms → 2/60
{
  const p = createPacer({ now: () => 0 });
  let t = 0, first = true;
  for (let i = 0; i < 60; i++) { t += 1000 / 60; if (first || p.shouldDraw(t, true)) { first = false; p.drew(t); } }
  t += 17.9; p.shouldDraw(t, true); const d1 = p.drew(t);
  t += 34; p.shouldDraw(t, true); const d2 = p.drew(t);
  check(`dt lượng tử: 17,9 ms → ${d1.toFixed(4)} (1/60), 34 ms → ${d2.toFixed(4)} (2/60)`, Math.abs(d1 - 1 / 60) < 2e-4 && Math.abs(d2 - 2 / 60) < 4e-4);
}
// motion tắt: giữ nhịp động thêm ~150 ms rồi mới về 30 (không lật nhịp mỗi khung khi damping sắp tắt)
{
  const r = sim(even(60, 2000), (t) => t < 1000 || (t > 1050 && t < 1060));
  const h = inWin(r.drawn, 1000, 1150), id = inWin(r.drawn, 1300, 2000);
  check(`hysteresis: 150 ms sau motion vẫn ~60 (${h} khung), sau đó 30 (${id}/700 ms)`, h >= 8 && id >= 20 && id <= 22);
}
// onSkip: việc phụ chạy trong rAF bị bỏ; chờ > 250 ms (không có rAF bị bỏ) → chạy trên khung vẽ
{
  const p = createPacer({ now: () => 0 });
  let t = 0, first = true, ran = 0, ranAt = 0;
  for (let i = 0; i < 30; i++) { t += 1000 / 60; if (first || p.shouldDraw(t, false)) { first = false; p.drew(t); } }
  p.onSkip(() => { ran++; ranAt = t; }, 'card');
  for (let i = 0; i < 4; i++) { t += 1000 / 60; if (p.shouldDraw(t, false)) p.drew(t); }
  const idle = ran;
  p.onSkip(() => { ran++; ranAt = t; }, 'card'); const t0 = t;
  for (let i = 0; i < 30; i++) { t += 1000 / 60; if (p.shouldDraw(t, true)) p.drew(t); }
  check(`onSkip: chạy trong rAF bị bỏ (${idle}), đang động → chạy sau ${(ranAt - t0).toFixed(0)} ms (≥ 250)`, idle === 1 && ran === 2 && ranAt - t0 >= 250 && ranAt - t0 < 300);
}
process.exit(fails ? 1 : 0);
