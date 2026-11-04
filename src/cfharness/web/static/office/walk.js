/* Văn phòng 3D — chế độ "Vào văn phòng" (đi bộ): người dùng điều khiển chính nhân vật ĐIỀU PHỐI VIÊN (nhãn + màu của agent `is_manager`;
 * không có điều phối viên → avatar "Bạn · chủ hub"), xuất hiện cạnh bàn trong phòng điều phối viên, [E] ngồi vào bàn ra lệnh.
 * Avatar đi trong văn phòng, camera góc thứ ba bám sau lưng
 * hoặc góc thứ nhất (phím V). Chỉ là một cách XEM: không gọi endpoint nào, mọi thao tác vẫn đi qua flows/chat như chế độ toàn cảnh.
 * Điều khiển: WASD / phím mũi tên đi (Shift chạy), kéo chuột/kéo tay xoay hướng nhìn, bấm/chạm sàn để tự đi tới (A* của Grid),
 * con lăn đổi khoảng cách camera. Va chạm theo lưới chướng ngại của World (tường, vách, đồ đạc; ghế không chặn).
 * reduced-motion: bấm sàn → dịch chuyển tức thời, camera không trễ. Tường che camera → làm mờ (World.fade). */
import * as THREE from '../vendor/three-0.169.0/three.module.min.js';
import { Chibi } from './chibi.js';
import { lerpAngle } from './util.js';

export const WALK_R = 0.12;          // bán kính va chạm avatar (m): nhỏ hơn đệm của tường (0,15) để qua được cửa 1 ô lưới
const SPEED = 2.4, RUN = 4.2, EYE = 1.02, LOOK_Y = 1.0;
const KEYS = {
  KeyW: [0, 1], ArrowUp: [0, 1], KeyS: [0, -1], ArrowDown: [0, -1],
  KeyA: [-1, 0], ArrowLeft: [-1, 0], KeyD: [1, 0], ArrowRight: [1, 0],
};

// Ô (x, z) có bị chặn với bán kính r không: tâm + 4 điểm trục + 4 điểm chéo.
export function blockedAt(grid, x, z, r) {
  const k = r * 0.7071;
  for (const [ox, oz] of [[0, 0], [r, 0], [-r, 0], [0, r], [0, -r], [k, k], [k, -k], [-k, k], [-k, -k]]) {
    const [c, w] = grid.cellOf(x + ox, z + oz);
    if (grid.blocked(c, w)) return true;
  }
  return false;
}

// Va chạm hình tròn (tâm x, z, bán kính r) với các ô bị chặn của lưới (ngoài lưới = bị chặn): cắt bước thành bước con ≤ r/2; mỗi bước con
// đẩy tâm ra khỏi mọi ô chạm vào theo PHÁP TUYẾN tới điểm gần nhất của ô (2 vòng — góc trong). Đi chéo vào tường → trượt dọc tường liền
// mạch (chỉ mất thành phần đâm vào tường), không còn "đổi trục" giật như thử-cả-bước-rồi-từng-trục (tốc độ 2,4 → 0,57 → 2,05 m/s).
export function circleMove(grid, x, z, dx, dz, r) {
  const n = Math.max(1, Math.ceil(Math.hypot(dx, dz) / (r * 0.5)));
  const sx = dx / n, sz = dz / n, cell = grid.cell, mx = grid.minX, mz = grid.minZ;
  for (let i = 0; i < n; i++) {
    x += sx; z += sz;
    for (let it = 0; it < 2; it++) {
      const c0 = Math.floor((x - r - mx) / cell), c1 = Math.floor((x + r - mx) / cell);
      const r0 = Math.floor((z - r - mz) / cell), r1 = Math.floor((z + r - mz) / cell);
      for (let rr = r0; rr <= r1; rr++) {
        for (let cc = c0; cc <= c1; cc++) {
          if (!grid.blocked(cc, rr)) continue;
          const ax = mx + cc * cell, az = mz + rr * cell, bx = ax + cell, bz = az + cell;
          const qx = x < ax ? ax : x > bx ? bx : x, qz = z < az ? az : z > bz ? bz : z;
          const ex = x - qx, ez = z - qz, d = Math.hypot(ex, ez);
          if (d >= r) continue;
          if (d > 1e-9) { x += (ex / d) * (r - d); z += (ez / d) * (r - d); continue; }
          // tâm nằm trong ô: đẩy theo trục xuyên ít nhất
          const pl = x - ax, pr = bx - x, pt = z - az, pb = bz - z, m = Math.min(pl, pr, pt, pb);
          if (m === pl) x = ax - r; else if (m === pr) x = bx + r; else if (m === pt) z = az - r; else z = bz + r;
        }
      }
    }
  }
  return { x, z };
}

// Bước đi có va chạm (giữ chữ ký cũ cho test/bố cục): = circleMove — không xuyên tường, qua được cửa 1 ô, trượt dọc tường.
export function slideMove(grid, x, z, dx, dz, r) {
  return circleMove(grid, x, z, dx, dz, r);
}

// Điểm xuất hiện khi bấm 🚶: ô trống gần tâm khu làm việc nhất (giữa dãy phòng phía bắc và cafe, ngoài mọi phòng), xa cửa ra —
// không còn đứng sát cửa thoát (camera dí vào cánh cửa, gợi ý đầu tiên là "[E] Thoát"). Hướng nhìn: phía bắc, vào trong văn phòng.
export function spawnPoint(world) {
  const g = world.grid, north = world.rooms.find((r) => r.key === 'server');
  const tx = world.W / 2, tz = ((north ? north.z1 : 6) + (world.cafe ? world.cafe.z0 : world.D / 2)) / 2;
  let best = null, bd = Infinity;
  for (let r = 0; r < g.rows; r++) {
    for (let c = 0; c < g.cols; c++) {
      const [x, z] = g.center(c, r), d = Math.hypot(x - tx, z - tz);
      if (d < bd && !world.roomAt(x, z) && !blockedAt(g, x, z, WALK_R)) { best = { x, z }; bd = d; }
    }
  }
  return best || { x: world.W - 1.4, z: world.D - 1.2 };
}

// Điểm xuất hiện của Điều phối viên: đứng cạnh bàn trong phòng điều phối viên (chỗ đứng `stand` của ghế giám đốc), nhìn về phía bàn.
// Ô bị chặn (bố cục đổi) → ô trống gần nhất còn trong phòng; không được → null (dùng spawnPoint).
export function deskSpawn(world) {
  const s = world.mgrSpot, g = world.grid;
  if (!s) return null;
  const st = s.stand || s;
  if (!blockedAt(g, st.x, st.z, WALK_R)) return { x: st.x, z: st.z, yaw: s.rot };
  const [c, r] = g.nearestFree(...g.cellOf(st.x, st.z)), [x, z] = g.center(c, r);
  const room = world.roomAt(x, z);
  return room && room.key === 'mgr' && !blockedAt(g, x, z, WALK_R) ? { x, z, yaw: s.rot } : null;
}
// Điểm vào chế độ đi bộ của Điều phối viên: ngay NGOÀI cửa phòng điều phối viên (khu làm việc), nhìn vào giữa văn phòng.
// Đứng cạnh bàn (deskSpawn) thì camera góc thứ ba bị kẹt sát tường bắc — dí vào gáy avatar, nhãn tên che nửa màn hình.
// Không có phòng / ô bị chặn / rơi vào phòng khác → null (dùng deskSpawn, rồi spawnPoint).
export function doorSpawn(world) {
  const room = world.rooms.find((r) => r.key === 'mgr'), g = world.grid;
  if (!room || !room.door) return null;
  // ưu tiên chỗ cách mọi ghế agent ≥ 2,4 m (không vừa vào đã tự chọn agent bên cạnh → thẻ thông tin che cảnh), không được thì bỏ điều kiện đó
  const seats = world.seats ? [...world.seats.values()] : [];
  const clear = (x, z) => seats.every((s) => s === world.mgrSpot || Math.hypot(s.x - x, s.z - z) >= 2.4);
  let x = 0, z = 0, ok = false;
  for (const strict of [true, false]) for (const dz of [1.3, 1.0, 1.7, 2.0]) for (const dx of [0, -0.5, 0.5, -1, 1, -1.5, 1.5]) {
    if (ok) break;
    x = room.door.x + dx; z = room.door.z + dz;
    ok = !world.roomAt(x, z) && !blockedAt(g, x, z, WALK_R) && (!strict || clear(x, z));
  }
  if (!ok) return null;
  const north = world.rooms.find((q) => q.key === 'server');
  const tx = world.W / 2, tz = ((north ? north.z1 : 6) + (world.cafe ? world.cafe.z0 : world.D / 2)) / 2;
  return { x, z, yaw: Math.atan2(tx - x, tz - z) };
}
// Actor điều phối viên do AI điều khiển mà avatar thay chỗ khi đi bộ (id = state.manager_id, không có thì actor isManager đầu tiên).
export function coordinatorOf(actors, managerId) {
  return (managerId && actors.get(managerId)) || [...actors.values()].find((a) => a.isManager) || null;
}
// Đứng gần ghế của mình (≤ SIT_R) trong phòng điều phối viên → [E] ngồi vào bàn.
export const SIT_R = 1.3;
export const nearDesk = (world, p) => !!(world && world.mgrSpot && p && Math.hypot(world.mgrSpot.x - p.x, world.mgrSpot.z - p.z) <= SIT_R);

// ui = { walk, modal, chat, board } (đang đi bộ / có bảng modal / chat / đang xem bảng). Khi có thứ đang mở: KHÔNG tự chọn agent theo
// khoảng cách (chọn → bảng/chat tự đóng) và ẩn dòng gợi ý (nó đè lên đầu hộp thoại).
export const walkBusy = (ui) => !!(ui.modal || ui.chat || ui.board);
export const hintShown = (ui) => !!ui.walk && !walkBusy(ui);
// Dòng gợi ý gắn camera, cách d: gọn (≤ 70 % bề ngang, ≤ 0,8 chiều cao) và nằm sát MÉP DƯỚI khung nhìn — không trùng biển tên khu vực
// (treo cao trong cảnh) và đầu hộp thoại. Trả { w, h, y, d } theo đơn vị thế giới (hệ toạ độ camera).
export function hintLayout(fov, aspect) {
  const d = 1.5, vh = 2 * d * Math.tan((fov * Math.PI) / 360), vw = vh * aspect;
  const w = Math.min(vw * 0.7, vh * 0.8), h = (w * 150) / 1100;
  return { w, h, y: -vh / 2 + h * 0.5 + vh * 0.025, d };
}

export class Walker {
  constructor(opts) {
    this.camera = opts.camera;
    this.scene = opts.scene; this.ident = '';
    this.setIdentity(null);
    this.seated = null; // ghế đang ngồi (spot) — ngồi bàn ra lệnh
    this.active = false; this.pov = 'third';
    this.world = null; this.pos = null; this.yaw = Math.PI; this.view = Math.PI; this.pitch = -0.12; this.dist = 3.4;
    this.keys = new Set(); this.run = false; this.path = [];
    this.camPos = new THREE.Vector3(); this.camInit = false;
    this._ray = new THREE.Raycaster(); this._v = new THREE.Vector3(); this._head = new THREE.Vector3();
  }

  // Nhân vật người dùng điều khiển: điều phối viên ({id, name, color}) hoặc null = "Bạn · chủ hub". Đổi → dựng lại chibi.
  setIdentity(who) {
    const name = who ? who.name || who.id : 'Bạn · chủ hub', color = who ? who.color || '#c8413a' : '#c96442';
    const key = (who ? 'm:' : 'o:') + name + color;
    if (key === this.ident) return;
    this.ident = key; this.isCoordinator = !!who;
    const vis = this.chibi ? this.chibi.group.visible : false;
    if (this.chibi) { this.chibi.group.removeFromParent(); this.chibi.dispose(); }
    this.chibi = new Chibi({ id: who ? 'cfh-coord-' + who.id : 'cfh-owner', name, color, isManager: !!who });
    this.chibi.group.userData.actorId = null; // avatar người dùng: bấm vào không chọn gì (actor AI bị ẩn)
    // nhãn tên trên đầu chính mình: camera góc thứ ba ở ngay sau gáy → nhãn phóng to che nửa màn hình; dòng gợi ý đã nói mình là ai
    if (this.chibi.label) this.chibi.label.visible = false;
    this.chibi.group.visible = vis;
    this.chibi.pose = 'stand';
    this.scene.add(this.chibi.group);
  }
  // bong bóng trạng thái run của điều phối viên (đang chạy, chờ duyệt…) hiện trên avatar
  setStatus(b) { this.chibi.setBubble(b ? b.emoji : null, b ? b.tone : ''); this.chibi.setRing(!!(b && b.ring)); }

  // Vào chế độ đi bộ: điều phối viên đứng ngay ngoài cửa phòng mình nhìn vào văn phòng (doorSpawn; không được → cạnh bàn, deskSpawn);
  // không phải điều phối viên → giữa khu làm việc (spawnPoint).
  enter(world) {
    this.active = true; this.camInit = false; this.keys.clear(); this.path = []; this.seated = null;
    this.setWorld(world, true);
    this.chibi.group.visible = this.pov === 'third';
  }
  // Ngồi vào ghế (spot của World): khoá di chuyển, tư thế ngồi, camera nhìn vào màn hình bàn. stand() đứng dậy ở chỗ đứng cạnh ghế.
  sit(spot) {
    if (!spot || !this.active) return false;
    this.seated = spot; this.keys.clear(); this.path = [];
    this.pos = { x: spot.x, z: spot.z }; this.yaw = this.view = spot.rot; this.camInit = false;
    return true;
  }
  stand() {
    const s = this.seated;
    if (!s) return false;
    this.seated = null; this.camInit = false;
    const st = s.stand || s;
    this.pos = { x: st.x, z: st.z };
    if (this.world) this.setWorld(this.world);
    return true;
  }
  exit() {
    this.active = false; this.keys.clear(); this.path = []; this.seated = null;
    this.chibi.group.visible = false;
    if (this.world) this.world.fade(new Set());
  }
  // Văn phòng dựng lại (thêm/bớt agent): giữ vị trí nếu còn đi được, không thì về ô trống gần nhất.
  setWorld(world, reset) {
    this.world = world;
    if (reset || !this.pos) {
      const d = this.isCoordinator ? doorSpawn(world) || deskSpawn(world) : null;
      if (d) { this.pos = { x: d.x, z: d.z }; this.yaw = this.view = d.yaw; } else { this.pos = spawnPoint(world); this.yaw = this.view = Math.PI; }
    }
    if (this.seated) {
      // ghế cũ thuộc World cũ: bám sang ghế điều phối viên của World mới; World mới không còn ghế đó → đứng dậy ở chỗ đứng cũ
      const s = this.seated, sp = s.zone === 'mgr' ? world.mgrSpot : null;
      if (sp) { this.seated = sp; this.pos = { x: sp.x, z: sp.z }; this.yaw = this.view = sp.rot; this.path = []; return; }
      const st = s.stand || s;
      this.seated = null; this.camInit = false; this.pos = { x: st.x, z: st.z };
    }
    if (blockedAt(world.grid, this.pos.x, this.pos.z, WALK_R)) {
      const g = world.grid, [c, r] = g.nearestFree(...g.cellOf(this.pos.x, this.pos.z)), [x, z] = g.center(c, r);
      this.pos = { x, z };
    }
    this.path = [];
  }
  togglePov() {
    this.pov = this.pov === 'third' ? 'first' : 'third';
    this.chibi.group.visible = this.active && this.pov === 'third';
    this.pitch = this.pov === 'first' ? 0 : -0.12;
    this.camInit = false;
    return this.pov;
  }

  key(code, down, shift) {
    this.run = !!shift;
    if (!KEYS[code]) return false;
    if (down && this.seated) return true; // đang ngồi bàn: phím đi không làm gì (Esc/E để đứng dậy)
    if (down) { this.keys.add(code); this.path = []; } else this.keys.delete(code);
    return true;
  }
  // kéo chuột/tay: dx, dy pixel
  look(dx, dy) {
    this.view -= dx * 0.006;
    this.pitch = Math.max(-0.9, Math.min(this.pov === 'first' ? 0.7 : 0.25, this.pitch - dy * 0.004));
  }
  zoom(dy) { this.dist = Math.max(1.8, Math.min(7, this.dist * (dy > 0 ? 1.1 : 1 / 1.1))); }

  // bấm/chạm sàn: tự đi tới bằng A*; reduced-motion → dịch chuyển tức thời tới điểm cuối
  goTo(x, z, reduced) {
    const w = this.world;
    if (!w || !this.pos || this.seated) return;
    const pts = w.grid.findPath(this.pos.x, this.pos.z, x, z);
    // điểm cuối nằm trong đồ đạc → dừng ở ô trống cuối cùng của đường
    while (pts.length > 1 && blockedAt(w.grid, pts[pts.length - 1].x, pts[pts.length - 1].z, WALK_R)) pts.pop();
    if (pts.length && blockedAt(w.grid, pts[0].x, pts[0].z, WALK_R)) return;
    if (reduced) { const p = pts[pts.length - 1]; if (p) { this.pos = { x: p.x, z: p.z }; } this.path = []; return; }
    this.path = pts;
  }

  update(dt, t, reduced) {
    const w = this.world;
    if (!this.active || !w || !this.pos) return;
    if (this.seated) {
      const c = this.chibi, s = this.seated;
      c.walking = false; c.pose = 'type'; c.seatY = s.seatY || 0.37;
      c.animate(dt, t, reduced);
      c.group.position.set(s.x, 0, s.z); c.group.rotation.y = s.rot;
      return;
    }
    let mx = 0, mz = 0;
    for (const k of this.keys) { mx += KEYS[k][0]; mz += KEYS[k][1]; }
    let walking = false;
    if (mx || mz) {
      // hướng theo camera: tiến = hướng nhìn, ngang = vuông góc
      const fx = Math.sin(this.view), fz = Math.cos(this.view), len = Math.hypot(mx, mz);
      const dx = (fz * -mx + fx * mz) / len, dz = (fx * mx + fz * mz) / len;
      const sp = (this.run ? RUN : SPEED) * dt;
      const p = slideMove(w.grid, this.pos.x, this.pos.z, dx * sp, dz * sp, WALK_R);
      walking = Math.hypot(p.x - this.pos.x, p.z - this.pos.z) > 1e-4;
      this.pos = p;
      this.yaw = this.pov === 'first' ? this.view : lerpAngle(this.yaw, Math.atan2(dx, dz), Math.min(1, dt * 12));
    } else if (this.path.length) {
      let step = SPEED * dt;
      walking = true;
      while (step > 0 && this.path.length) {
        const p = this.path[0], dx = p.x - this.pos.x, dz = p.z - this.pos.z, d = Math.hypot(dx, dz);
        if (d > 1e-4) this.yaw = lerpAngle(this.yaw, Math.atan2(dx, dz), Math.min(1, dt * 12));
        if (d <= step) { this.pos = { x: p.x, z: p.z }; this.path.shift(); step -= d; } else { this.pos.x += (dx / d) * step; this.pos.z += (dz / d) * step; step = 0; }
      }
      // tự đi: camera từ từ quay theo hướng đi (góc thứ ba)
      if (this.pov === 'third') this.view = lerpAngle(this.view, this.yaw, Math.min(1, dt * 1.5));
      else this.view = this.yaw;
    } else if (this.pov === 'first') this.yaw = this.view;
    const c = this.chibi;
    c.walking = walking; c.pose = 'stand';
    c.animate(dt, t, reduced);
    c.group.position.set(this.pos.x, 0, this.pos.z);
    c.group.rotation.y = this.yaw;
  }

  // camera vừa được trả lại (đóng bảng/chat): đi mượt từ chỗ camera đang đứng về sau lưng avatar
  resume(reduced) { this.camPos.copy(this.camera.position); this.camInit = !reduced; }

  // Camera (gọi khi chế độ đi bộ đang lái camera, tức không có bảng/chat/bay camera).
  updateCamera(dt, reduced) {
    const w = this.world, cam = this.camera;
    if (!this.active || !w || !this.pos) return;
    const fx = Math.sin(this.view), fz = Math.cos(this.view);
    if (this.seated) {
      // ngồi bàn: camera sau vai, nhìn thẳng vào màn hình trên bàn (bàn điều khiển chat phủ trước camera)
      const s = this.seated, sx = Math.sin(s.rot), sz = Math.cos(s.rot);
      this._v.set(s.x - sx * 0.55, 1.35, s.z - sz * 0.55);
      if (!this.camInit || reduced) { this.camPos.copy(this._v); this.camInit = true; } else this.camPos.lerp(this._v, Math.min(1, dt * 6));
      cam.position.copy(this.camPos);
      cam.lookAt(this._head.set(s.x + sx * 1.1, 0.95, s.z + sz * 1.1));
      w.fade(new Set());
      return;
    }
    if (this.pov === 'first') {
      cam.position.set(this.pos.x, EYE, this.pos.z);
      this._v.set(this.pos.x + fx * Math.cos(this.pitch), EYE + Math.sin(this.pitch), this.pos.z + fz * Math.cos(this.pitch));
      cam.lookAt(this._v);
      w.fade(new Set());
      this.camInit = true;
      return;
    }
    const head = this._head.set(this.pos.x, LOOK_Y, this.pos.z);
    const hd = this.dist * Math.cos(this.pitch - 0.35), up = this.dist * Math.sin(0.35 - this.pitch);
    this._v.set(this.pos.x - fx * hd, LOOK_Y + up, this.pos.z - fz * hd);
    // giữ camera trong văn phòng (không chui ra sau tường bắc/tây cao)
    this._v.x = Math.max(0.3, Math.min(w.W + 2, this._v.x)); this._v.z = Math.max(0.3, Math.min(w.D + 2, this._v.z));
    this._v.y = Math.max(0.6, this._v.y);
    if (!this.camInit || reduced) { this.camPos.copy(this._v); this.camInit = true; } else this.camPos.lerp(this._v, Math.min(1, dt * 8));
    cam.position.copy(this.camPos);
    cam.lookAt(head);
    // tường/vách nằm giữa camera và avatar → làm mờ
    const dir = this._v.copy(head).sub(this.camPos), len = dir.length();
    const hit = new Set();
    if (len > 1e-3) {
      this._ray.set(this.camPos, dir.multiplyScalar(1 / len)); this._ray.far = len;
      for (const h of this._ray.intersectObjects(w.occluders, false)) hit.add(h.object);
    }
    w.fade(hit);
  }

  dispose() { this.chibi.group.removeFromParent(); this.chibi.dispose(); }
}
