/* Văn phòng 3D — đo hiệu năng CHỈ cho gỡ lỗi (docs/dev-notes/office-3d-perf.md). Bật bằng hash:
 *   #perf3d   → ghi thời gian từng khung (khoảng cách giữa 2 khung vẽ, thời gian JS trong frame(), thời gian GPU nếu trình duyệt có
 *               EXT_disjoint_timer_query_webgl2) + draw call/tam giác; đọc qua window.__office3dPerf.report() / reset().
 *   #uncap3d  → bỏ mọi giới hạn khung (vẽ MỌI rAF, kể cả lúc cảnh đứng yên) để đo khoảng dư.
 * Không bật thì không làm gì (không global, không query GPU). Không log, không chứa chữ của agent. */

export const perfFlags = () => {
  const h = (typeof location !== 'undefined' && location.hash) || '';
  return { perf: h.indexOf('perf3d') >= 0, uncap: h.indexOf('uncap3d') >= 0 };
};

const pct = (arr, p) => {
  if (!arr.length) return 0;
  const s = Float64Array.from(arr).sort();
  return Math.round(s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))] * 100) / 100;
};
const stats = (a) => ({ n: a.length, p50: pct(a, 50), p95: pct(a, 95), p99: pct(a, 99), max: pct(a, 100),
  mean: a.length ? Math.round((a.reduce((x, y) => x + y, 0) / a.length) * 100) / 100 : 0 });

// renderer: THREE.WebGLRenderer; info(): { tier, ... } thêm vào báo cáo
export function createPerf(renderer, info) {
  if (!perfFlags().perf || !renderer) return null;
  const gl = renderer.getContext();
  let ext = null;
  try { ext = gl.getExtension('EXT_disjoint_timer_query_webgl2'); } catch (_) { ext = null; }
  let iv = [], js = [], gpu = [], calls = [], tris = [], longs = [], skipped = 0, t0 = performance.now(), lastDraw = 0, fs = 0;
  const pending = [];
  let q = null;
  const api = {
    gpuSupported: !!ext,
    reset() { iv = []; js = []; gpu = []; calls = []; tris = []; longs = []; skipped = 0; t0 = performance.now(); lastDraw = 0; },
    // rAF đã chạy nhưng không vẽ (giới hạn khung / cảnh đứng yên)
    skip() { skipped++; },
    begin(ts) {
      fs = performance.now();
      if (lastDraw) iv.push(ts - lastDraw);
      lastDraw = ts;
      // thu kết quả query GPU của các khung trước
      while (pending.length) {
        const p = pending[0];
        if (!gl.getQueryParameter(p, gl.QUERY_RESULT_AVAILABLE)) break;
        pending.shift();
        if (!gl.getParameter(ext.GPU_DISJOINT_EXT)) gpu.push(gl.getQueryParameter(p, gl.QUERY_RESULT) / 1e6);
        gl.deleteQuery(p);
      }
    },
    gpuBegin() { if (ext && pending.length < 8) { q = gl.createQuery(); gl.beginQuery(ext.TIME_ELAPSED_EXT, q); } },
    gpuEnd() { if (q) { gl.endQuery(ext.TIME_ELAPSED_EXT); pending.push(q); q = null; } },
    end() {
      const j = performance.now() - fs, jp = js.length ? js[js.length - 1] : 0;
      js.push(j);
      // khung dài: khoảng tới khung trước > 50 ms — ghi [khoảng, JS khung trước, JS khung này] (JS thấp → nghẽn GPU/trình duyệt/GC)
      if (iv.length && iv[iv.length - 1] > 50 && longs.length < 20) longs.push([Math.round(iv[iv.length - 1]), Math.round(jp * 10) / 10, Math.round(j * 10) / 10]);
      const r = renderer.info.render;
      calls.push(r.calls); tris.push(r.triangles);
    },
    report() {
      const secs = (performance.now() - t0) / 1000;
      return Object.assign({
        secs: Math.round(secs * 10) / 10, drawn: js.length, skipped, fps: Math.round((js.length / Math.max(0.001, secs)) * 10) / 10,
        interval: stats(iv), js: stats(js), gpu: ext ? stats(gpu) : null,
        long50: iv.filter((x) => x > 50).length, longs, long100: iv.filter((x) => x > 100).length,
        calls: stats(calls), triangles: stats(tris), dpr: window.devicePixelRatio, pr: renderer.getPixelRatio(),
      }, info ? info() : {});
    },
  };
  window.__office3dPerf = api;
  return api;
}
