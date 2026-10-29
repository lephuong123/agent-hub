/* Văn phòng 3D — lưới sàn + A* viết tay (8 hướng, không cắt góc) + làm mượt đường bằng kiểm tra tầm nhìn. */

class Heap {
  constructor(score) { this.a = []; this.score = score; }
  get size() { return this.a.length; }
  push(v) {
    const a = this.a; a.push(v);
    let i = a.length - 1;
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (this.score(a[p]) <= this.score(a[i])) break;
      [a[p], a[i]] = [a[i], a[p]]; i = p;
    }
  }
  pop() {
    const a = this.a, top = a[0], last = a.pop();
    if (a.length) {
      a[0] = last;
      let i = 0;
      for (;;) {
        const l = i * 2 + 1, r = l + 1;
        let m = i;
        if (l < a.length && this.score(a[l]) < this.score(a[m])) m = l;
        if (r < a.length && this.score(a[r]) < this.score(a[m])) m = r;
        if (m === i) break;
        [a[m], a[i]] = [a[i], a[m]]; i = m;
      }
    }
    return top;
  }
}

const SQ2 = Math.SQRT2;
const DIRS = [[1, 0, 1], [-1, 0, 1], [0, 1, 1], [0, -1, 1], [1, 1, SQ2], [1, -1, SQ2], [-1, 1, SQ2], [-1, -1, SQ2]];

export class Grid {
  constructor(minX, minZ, w, d, cell) {
    this.cell = cell || 0.5;
    this.minX = minX; this.minZ = minZ;
    this.cols = Math.ceil(w / this.cell); this.rows = Math.ceil(d / this.cell);
    this.block = new Uint8Array(this.cols * this.rows);
  }
  inside(c, r) { return c >= 0 && r >= 0 && c < this.cols && r < this.rows; }
  blocked(c, r) { return !this.inside(c, r) || this.block[r * this.cols + c] === 1; }
  cellOf(x, z) { return [Math.floor((x - this.minX) / this.cell), Math.floor((z - this.minZ) / this.cell)]; }
  center(c, r) { return [this.minX + (c + 0.5) * this.cell, this.minZ + (r + 0.5) * this.cell]; }

  // Đánh dấu chướng ngại theo hình chữ nhật thế giới (mọi ô có tâm nằm trong vùng + đệm).
  rect(x0, z0, x1, z1, pad, val) {
    const p = pad == null ? 0.12 : pad, v = val == null ? 1 : val;
    const [c0, r0] = this.cellOf(Math.min(x0, x1) - p, Math.min(z0, z1) - p);
    const [c1, r1] = this.cellOf(Math.max(x0, x1) + p, Math.max(z0, z1) + p);
    for (let r = Math.max(0, r0); r <= Math.min(this.rows - 1, r1); r++) {
      for (let c = Math.max(0, c0); c <= Math.min(this.cols - 1, c1); c++) this.block[r * this.cols + c] = v;
    }
  }

  // Ô trống gần nhất (vòng BFS) — dùng khi điểm đầu/cuối nằm trong đồ đạc (ghế sofa, tủ…).
  nearestFree(c, r) {
    if (!this.blocked(c, r)) return [c, r];
    for (let rad = 1; rad < 40; rad++) {
      let best = null, bd = 1e9;
      for (let dr = -rad; dr <= rad; dr++) {
        for (let dc = -rad; dc <= rad; dc++) {
          if (Math.max(Math.abs(dr), Math.abs(dc)) !== rad) continue;
          if (this.blocked(c + dc, r + dr)) continue;
          const d = dc * dc + dr * dr;
          if (d < bd) { bd = d; best = [c + dc, r + dr]; }
        }
      }
      if (best) return best;
    }
    return [c, r];
  }

  lineFree(ax, az, bx, bz) {
    const dx = bx - ax, dz = bz - az, len = Math.hypot(dx, dz);
    const n = Math.max(1, Math.ceil(len / (this.cell * 0.35)));
    for (let i = 0; i <= n; i++) {
      const [c, r] = this.cellOf(ax + dx * i / n, az + dz * i / n);
      if (this.blocked(c, r)) return false;
    }
    return true;
  }

  // Trả danh sách điểm {x, z} (không gồm điểm đầu), điểm cuối đúng (tx, tz).
  findPath(sx, sz, tx, tz) {
    const [sc0, sr0] = this.cellOf(sx, sz), [tc0, tr0] = this.cellOf(tx, tz);
    const [sc, sr] = this.nearestFree(sc0, sr0), [tc, tr] = this.nearestFree(tc0, tr0);
    const cols = this.cols, N = cols * this.rows;
    const start = sr * cols + sc, goal = tr * cols + tc;
    const g = new Float32Array(N).fill(Infinity), from = new Int32Array(N).fill(-1), closed = new Uint8Array(N);
    const h = (i) => { const dc = Math.abs(i % cols - tc), dr = Math.abs((i / cols | 0) - tr); return (dc + dr) + (SQ2 - 2) * Math.min(dc, dr); };
    const f = new Float32Array(N);
    const open = new Heap((i) => f[i]);
    g[start] = 0; f[start] = h(start); open.push(start);
    let found = start === goal, guard = 0;
    while (open.size && !found && guard++ < N * 4) {
      const cur = open.pop();
      if (closed[cur]) continue;
      if (cur === goal) { found = true; break; }
      closed[cur] = 1;
      const cc = cur % cols, cr = cur / cols | 0;
      for (const [dc, dr, cost] of DIRS) {
        const nc = cc + dc, nr = cr + dr;
        if (this.blocked(nc, nr)) continue;
        if (dc && dr && (this.blocked(cc + dc, cr) || this.blocked(cc, cr + dr))) continue; // không cắt góc
        const ni = nr * cols + nc;
        if (closed[ni]) continue;
        const ng = g[cur] + cost;
        if (ng < g[ni]) { g[ni] = ng; from[ni] = cur; f[ni] = ng + h(ni); open.push(ni); }
      }
    }
    const pts = [];
    if (found) {
      for (let i = goal; i !== -1 && i !== start; i = from[i]) { const [x, z] = this.center(i % cols, i / cols | 0); pts.push({ x, z }); }
      pts.reverse();
    }
    pts.push({ x: tx, z: tz });
    // Làm mượt: bỏ điểm trung gian khi nhìn thẳng được tới điểm sau.
    const out = [];
    let ax = sx, az = sz, i = 0;
    while (i < pts.length) {
      let j = pts.length - 1;
      while (j > i && !this.lineFree(ax, az, pts[j].x, pts[j].z)) j--;
      // Chặng cuối (vào ghế sofa/băng ghế) có thể xuyên đồ đạc — vẫn chấp nhận.
      out.push(pts[j]); ax = pts[j].x; az = pts[j].z; i = j + 1;
    }
    return out;
  }
}
