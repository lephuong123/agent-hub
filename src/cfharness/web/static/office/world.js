/* Văn phòng 3D — dựng mặt bằng từ dữ liệu: dãy phòng phía bắc (server, thư viện, phòng họp, điều phối viên), cụm bàn theo team ở giữa,
 * dải phía nam (pantry, phòng nghỉ sofa, hàng ghế chờ). Tường bắc + tây cao (kiểu nhà búp bê), vách trong thấp để camera nhìn được.
 * Mọi "điểm đứng/ngồi" (spot) có hướng + tư thế; spot dùng chung được giữ chỗ để nhân vật không chồng lên nhau. */
import * as THREE from '../vendor/three-0.169.0/three.module.min.js';
import { Grid } from './path.js';
import { Board } from './boards.js';
import { TEAM_COLORS, NO_TEAM_COLOR, mat, basic, box, cyl, sphere, seeded, hashStr, labelSprite, textCanvas, roundRect, FONT, fitText, scaleLabel } from './util.js';

export const ZONE_NAMES = {
  desk: 'Bàn làm việc', server: 'Phòng server', library: 'Thư viện', meeting: 'Phòng họp', mgr: 'Phòng điều phối viên',
  mgrVisit: 'Phòng điều phối viên', pantry: 'Pantry', lounge: 'Phòng nghỉ', bench: 'Hàng ghế chờ',
};

const ROOM_D = 6;
const WALL_H = 2.6;

// Khoá bố cục: đổi team/thành viên/điều phối viên → dựng lại văn phòng.
export function layoutKey(data) {
  const teams = (data.teams || []).map((t) => t.id).join(',');
  const ag = (data.agents || []).map((a) => a.id + ':' + (a.team || '') + ':' + (a.is_manager ? 1 : 0)).join(',');
  return teams + '|' + ag + '|' + (data.manager_id || '');
}

function darker(hex, k) { const c = new THREE.Color(hex); c.multiplyScalar(k); return '#' + c.getHexString(); }
function lighter(hex, k) { const c = new THREE.Color(hex); c.lerp(new THREE.Color('#ffffff'), k); return '#' + c.getHexString(); }

export class World {
  constructor(data) {
    this.group = new THREE.Group();
    this.textures = [];
    this.zones = { server: [], library: [], meeting: [], mgrVisit: [], pantry: [], lounge: [], bench: [] };
    this.seats = new Map();      // agentId → spot bàn riêng
    this.leds = [];
    this.signs = [];
    this.teamFx = new Map();     // teamId → {overlay, lamp}
    this.teamColor = new Map();
    this.estop = false;
    this.clockFlash = 0;
    this._plan(data);
    this._build(data);
  }

  // ------------------------------------------------------------ mặt bằng
  _plan(data) {
    const mgrId = data.manager_id || null;
    const teams = data.teams || [];
    const members = new Map(teams.map((t) => [t.id, []]));
    const none = [];
    for (const a of data.agents || []) {
      if (a.is_manager || a.id === mgrId) continue;
      if (a.team && members.has(a.team)) members.get(a.team).push(a.id); else none.push(a.id);
    }
    const clusters = teams.map((t, i) => ({ id: t.id, name: t.name || t.id, color: TEAM_COLORS[i % TEAM_COLORS.length], members: members.get(t.id) }));
    if (none.length || !clusters.length) clusters.push({ id: '', name: 'Không team', color: NO_TEAM_COLOR, members: none });
    for (const c of clusters) {
      c.k = Math.ceil(Math.max(2, c.members.length) / 2);
      c.w = c.k * 1.4 + 1.4; c.d = 4.6;
      this.teamColor.set(c.id, c.color);
    }
    let W = 28;
    const maxW = Math.max(...clusters.map((c) => c.w));
    if (maxW + 4 > W) W = Math.ceil(maxW + 4);
    // xếp cụm theo hàng, chừa lối đi sát tường tây (bảng kanban)
    let x = 2.4, z = ROOM_D + 1.6, rowH = 0;
    for (const c of clusters) {
      if (x + c.w > W - 1.2 && x > 2.4) { x = 2.4; z += rowH + 1.4; rowH = 0; }
      c.x = x; c.z = z; c.cx = x + c.w / 2; c.cz = z + c.d / 2;
      x += c.w + 1.4; rowH = Math.max(rowH, c.d);
    }
    const zs = z + rowH + 1.2;
    this.W = W; this.zs = zs; this.D = zs + 6; this.clusters = clusters; this.mgrId = mgrId;
    this.openZ0 = ROOM_D; this.openZ1 = zs;
    this.grid = new Grid(0, 0, W, this.D, 0.5);
  }

  _spot(zone, x, z, rot, pose, seatY, extra) {
    const s = Object.assign({ zone, x, z, rot, pose, seatY: seatY || 0, owner: null }, extra || {});
    if (this.zones[zone]) this.zones[zone].push(s);
    return s;
  }

  _build(data) {
    const g = this.group, W = this.W, D = this.D, grid = this.grid;
    // sàn gỗ (texture vân ván vẽ tay)
    const wood = textCanvas(256, 256, (ctx) => {
      ctx.fillStyle = '#ead6b6'; ctx.fillRect(0, 0, 256, 256);
      const r = seeded(7);
      for (let i = 0; i < 8; i++) {
        ctx.fillStyle = i % 2 ? '#e6cfab' : '#eedcbf'; ctx.fillRect(0, i * 32, 256, 32);
        ctx.fillStyle = 'rgba(120,85,50,0.18)'; ctx.fillRect(0, i * 32, 256, 2);
        ctx.fillRect(Math.floor(r() * 256), i * 32, 2, 32);
      }
    });
    wood.wrapS = wood.wrapT = THREE.RepeatWrapping; wood.repeat.set(W / 3, D / 3);
    this.textures.push(wood);
    const floor = new THREE.Mesh(new THREE.BoxGeometry(W, 0.1, D), new THREE.MeshLambertMaterial({ map: wood }));
    floor.position.set(W / 2, -0.05, D / 2); g.add(floor);
    this.floor = floor;

    // tường bắc + tây (cao), viền chân tường
    box(g, W + 0.25, WALL_H, 0.25, '#f4ece0', W / 2 - 0.125, 0, -0.125);
    box(g, 0.25, WALL_H, D, '#efe5d6', -0.125, 0, D / 2);
    box(g, W, 0.12, 0.04, '#d6c4a8', W / 2, 0, 0.02);
    box(g, 0.04, 0.12, D, '#d6c4a8', 0.02, 0, D / 2);
    box(g, W + 0.3, 0.08, 0.3, '#e2d3bd', W / 2 - 0.15, WALL_H, -0.15);
    box(g, 0.3, 0.08, D, '#e2d3bd', -0.15, WALL_H, D / 2);
    grid.rect(0, 0, W, 0.05, 0.1); grid.rect(0, 0, 0.05, D, 0.1);

    this._rooms(data);
    this._clusters();
    this._south();
    this._walls();

    // ánh sáng ấm: trời + một nguồn hướng (không shadow map)
    this.hemi = new THREE.HemisphereLight('#fff6e8', '#cdb497', 2.1);
    this.dir = new THREE.DirectionalLight('#fff0d8', 1.5);
    this.dir.position.set(W * 0.25, 20, D * 0.95); this.dir.target.position.set(W / 2, 0, D / 2);
    g.add(this.hemi, this.dir, this.dir.target);
    this.hemiBase = this.hemi.color.clone(); this.dirBase = this.dir.color.clone();
  }

  // ------------------------------------------------------------ dãy phòng phía bắc
  _rooms() {
    const g = this.group, rw = this.W / 4, grid = this.grid;
    this.rw = rw;
    const roomFloor = (i, color) => box(g, rw - 0.04, 0.012, ROOM_D - 0.04, color, rw * i + rw / 2, 0, ROOM_D / 2);
    const sign = (i, text, color) => {
      const s = labelSprite(text, color, { scale: 1.15, bg: 'rgba(61,57,41,0.88)', fg: '#fffaf0', stroke: 'rgba(0,0,0,0)' });
      s.position.set(rw * i + rw / 2, 2.75, ROOM_D); g.add(s); this.textures.push(s.material.map); this.signs.push(s);
    };
    // cửa sổ cao trên tường bắc
    for (let i = 0; i < 4; i++) {
      box(g, rw * 0.62, 0.55, 0.02, '#cfe6f2', rw * i + rw / 2, 1.85, 0.01, basic('#d4ebf6'));
      box(g, rw * 0.62 + 0.12, 0.06, 0.06, '#ffffff', rw * i + rw / 2, 1.8, 0.03);
    }

    // 0 — phòng server
    roomFloor(0, '#cbd5dc');
    const ledMat = { g: basic('#59f08a'), a: basic('#ffc857'), b: basic('#6cc6ff'), off: basic('#26303a') };
    this.ledMat = ledMat;
    const nR = 4, rackW = 0.9, gap = 0.45, span = nR * rackW + (nR - 1) * gap;
    const r0 = (rw - span) / 2 + rackW / 2;
    const rnd = seeded(42);
    for (let i = 0; i < nR; i++) {
      const x = r0 + i * (rackW + gap);
      box(g, rackW, 2.0, 0.8, '#3b4252', x, 0, 0.65);
      box(g, rackW - 0.1, 1.8, 0.02, '#4c566a', x, 0.1, 1.06);
      for (let j = 0; j < 6; j++) for (const ox of [-0.25, 0.25]) {
        const led = box(g, 0.09, 0.04, 0.02, '', x + ox, 0.35 + j * 0.27, 1.08, ledMat.off);
        this.leds.push({ m: led, kind: ['g', 'g', 'a', 'b'][Math.floor(rnd() * 4)], rate: 0.3 + rnd() * 1.6, ph: rnd() * 6 });
      }
      grid.rect(x - rackW / 2, 0.25, x + rackW / 2, 1.05, 0.1);
      this._spot('server', x, 1.7, Math.PI, 'standType');
    }
    // điều hoà phòng máy
    box(g, 0.7, 1.6, 0.5, '#e8edf0', rw - 0.6, 0, ROOM_D - 1.2);
    box(g, 0.5, 0.05, 0.02, '#9aa5ad', rw - 0.6, 1.2, ROOM_D - 0.94);
    grid.rect(rw - 0.95, ROOM_D - 1.45, rw - 0.25, ROOM_D - 0.95, 0.1);
    sign(0, 'Phòng server', '#4f8fc0');

    // 1 — thư viện
    const lx = rw;
    roomFloor(1, '#e2cda9');
    const bookGeo = new THREE.BoxGeometry(1, 1, 1);
    const books = new THREE.InstancedMesh(bookGeo, new THREE.MeshLambertMaterial(), 2 * 3 * 10);
    const BC = ['#c8413a', '#4f8fc0', '#7fb069', '#e9b44c', '#9c89b8', '#ef8a9a', '#3fb6a8', '#f2efe6'];
    const m4 = new THREE.Matrix4(), col = new THREE.Color();
    let bi = 0;
    for (const fx of [0.3, 0.7]) {
      const cx = lx + rw * fx;
      box(g, 2.4, 2.1, 0.45, '#9b6b3f', cx, 0, 0.475);
      for (const y of [0.32, 0.92, 1.52]) {
        box(g, 2.3, 0.04, 0.4, '#b98858', cx, y, 0.5);
        let bx = cx - 1.08;
        for (let k = 0; k < 10 && bi < books.count; k++) {
          const bw = 0.13 + rnd() * 0.08, bh = 0.32 + rnd() * 0.18;
          m4.makeScale(bw, bh, 0.3); m4.setPosition(bx + bw / 2, y + 0.04 + bh / 2, 0.52);
          books.setMatrixAt(bi, m4); books.setColorAt(bi, col.set(BC[Math.floor(rnd() * BC.length)])); bi++;
          bx += bw + 0.02 + (rnd() < 0.15 ? 0.12 : 0);
        }
      }
      grid.rect(cx - 1.2, 0.25, cx + 1.2, 0.7, 0.1);
      this._spot('library', cx - 0.6, 1.25, Math.PI, 'read');
      this._spot('library', cx + 0.6, 1.25, Math.PI, 'read');
    }
    books.count = bi; books.instanceMatrix.needsUpdate = true; if (books.instanceColor) books.instanceColor.needsUpdate = true;
    g.add(books);
    // bàn đọc tròn + 2 ghế bành + đèn
    const tcx = lx + rw / 2, tcz = 3.9;
    cyl(g, 0.5, 0.5, 0.05, '#c79a6b', tcx, 0.55, tcz, 20); cyl(g, 0.06, 0.1, 0.55, '#8a5f39', tcx, 0, tcz);
    cyl(g, 0.12, 0.08, 0.12, '#ffffff', tcx + 0.2, 0.6, tcz - 0.1);
    grid.rect(tcx - 0.5, tcz - 0.5, tcx + 0.5, tcz + 0.5, 0.05);
    for (const s of [-1, 1]) {
      const ax = tcx + s * 1.05;
      box(g, 0.62, 0.3, 0.62, '#7aa6c2', ax, 0, tcz); box(g, 0.14, 0.45, 0.62, '#6893b0', ax + s * 0.28, 0.3, tcz);
      box(g, 0.62, 0.18, 0.12, '#6893b0', ax, 0.3, tcz - 0.31); box(g, 0.62, 0.18, 0.12, '#6893b0', ax, 0.3, tcz + 0.31);
      this._spot('library', ax, tcz, s < 0 ? Math.PI / 2 : -Math.PI / 2, 'sitRead', 0.32);
      grid.rect(ax - 0.31, tcz - 0.31, ax + 0.31, tcz + 0.31, 0.02);
    }
    this._plant(lx + rw - 0.6, ROOM_D - 0.7, 1);
    sign(1, 'Thư viện', '#7fb069');

    // 2 — phòng họp
    const mx = rw * 2, mcx = mx + rw / 2, mcz = 3.3;
    roomFloor(2, '#efe3cf');
    box(g, 2.7, 0.06, 1.15, '#d9b48a', mcx, 0.6, mcz);
    for (const sx of [-1.1, 1.1]) box(g, 0.08, 0.6, 0.9, '#9b6b3f', mcx + sx, 0, mcz);
    grid.rect(mcx - 1.35, mcz - 0.58, mcx + 1.35, mcz + 0.58, 0.08);
    for (const side of [-1, 1]) for (const ox of [-0.85, 0, 0.85]) {
      const cz = mcz + side * 1.0;
      this._chair(mcx + ox, cz, side, '#c9705a');
      this._spot('meeting', mcx + ox, cz, side < 0 ? 0 : Math.PI, 'sit', 0.33);
    }
    // bảng trắng trên tường bắc
    box(g, 2.6, 1.15, 0.05, '#ffffff', mcx, 0.85, 0.06);
    box(g, 2.7, 0.06, 0.1, '#b0b6bb', mcx, 0.82, 0.08);
    const wr = seeded(9);
    for (let i = 0; i < 4; i++) box(g, 0.6 + wr() * 1.2, 0.04, 0.01, ['#4f8fc0', '#c8413a', '#7fb069', '#e9b44c'][i], mcx - 0.9 + wr() * 0.4 + (0.6 + wr() * 0.2) / 2, 1.7 - i * 0.22, 0.09);
    sign(2, 'Phòng họp', '#9c89b8');

    // 3 — phòng điều phối viên (vách kính)
    const ox0 = rw * 3, ocx = ox0 + rw / 2;
    roomFloor(3, '#eadccb');
    box(g, 3.2, 0.012, 2.4, '#d98c6a', ocx, 0.012, 2.6);
    box(g, 1.9, 0.07, 0.85, '#7a4f33', ocx, 0.62, 2.2);
    box(g, 1.8, 0.62, 0.06, '#6a432b', ocx, 0, 2.55);
    for (const sx of [-0.88, 0.88]) box(g, 0.08, 0.62, 0.8, '#6a432b', ocx + sx, 0, 2.2);
    grid.rect(ocx - 0.95, 1.78, ocx + 0.95, 2.62, 0.08);
    // ghế giám đốc (lưng cao) + màn hình hướng về ghế
    box(g, 0.55, 0.09, 0.55, '#3d3a48', ocx, 0.28, 1.3); box(g, 0.55, 0.7, 0.09, '#3d3a48', ocx, 0.37, 1.0);
    cyl(g, 0.035, 0.035, 0.22, '#777777', ocx, 0.06, 1.3); cyl(g, 0.24, 0.24, 0.05, '#555555', ocx, 0.0, 1.3, 16);
    const mgrScreen = this._monitor(ocx, 2.4, -1, 0.68, 0.72);
    this.mgrSpot = { zone: 'mgr', x: ocx, z: 1.3, rot: 0, pose: 'sit', seatY: 0.37, owner: this.mgrId, screen: mgrScreen, stand: { x: ocx - 0.75, z: 1.4 } };
    if (this.mgrId) this.seats.set(this.mgrId, this.mgrSpot);
    for (const s of [-1, 1]) {
      this._chair(ocx + s * 0.55, 3.35, 1, '#8a6a9a');
      this._spot('mgrVisit', ocx + s * 0.55, 3.35, Math.PI, 'sit', 0.33);
    }
    this._spot('mgrVisit', ocx - 1.3, 3.9, Math.PI * 0.85, 'stand');
    this._spot('mgrVisit', ocx + 1.3, 3.9, -Math.PI * 0.85, 'stand');
    // tủ hồ sơ + tranh + cây
    box(g, 0.45, 1.3, 1.2, '#9b6b3f', ox0 + 0.4, 0, 2.4); grid.rect(ox0 + 0.15, 1.8, ox0 + 0.65, 3.0, 0.08);
    // màn hình chat với điều phối viên (giai đoạn 2): treo ở vách đông, quay vào trong phòng
    // đặt lệch về phía cửa (z 2.1…5.7) để điều phối viên (ngồi z≈1.3) đứng BÊN CẠNH màn hình, không che chữ
    box(g, 0.1, 2.3, 3.9, '#2a2e38', this.W - 0.08, 0.4, 3.9);
    this.chatAnchor = { x: this.W - 0.15, y: 1.55, z: 3.9, rotY: -Math.PI / 2, w: 3.6, h: 2.06 };
    box(g, 1.0, 0.65, 0.03, '#f2c76e', ocx, 1.2, 0.04); box(g, 0.85, 0.5, 0.03, '#7fb3d5', ocx, 1.27, 0.06);
    this._plant(ox0 + 0.5, 0.6, 1.2);
    this._plant(ox0 + 0.5, ROOM_D - 0.6, 1);
    sign(3, 'Điều phối viên', '#c8413a');
  }

  // ------------------------------------------------------------ vách phòng (thấp + kính)
  _walls() {
    const g = this.group, rw = this.rw, grid = this.grid;
    const glass = new THREE.MeshLambertMaterial({ color: '#bfe2f2', transparent: true, opacity: 0.22, depthWrite: false });
    const half = (x0, z0, x1, z1, withGlass) => {
      const w = Math.max(0.12, x1 - x0), d = Math.max(0.12, z1 - z0);
      box(g, w, 1.0, d, '#f1e8da', (x0 + x1) / 2, 0, (z0 + z1) / 2);
      box(g, w + 0.02, 0.05, d + 0.02, '#d8c7ad', (x0 + x1) / 2, 1.0, (z0 + z1) / 2);
      if (withGlass) {
        box(g, w, 1.2, d, '', (x0 + x1) / 2, 1.05, (z0 + z1) / 2, glass);
        box(g, w + 0.02, 0.05, d + 0.02, '#c9d6dc', (x0 + x1) / 2, 2.25, (z0 + z1) / 2);
      }
      grid.rect(x0, z0, x1, z1, 0.15);
    };
    // vách giữa các phòng (kính cho phòng họp + điều phối viên)
    for (let i = 1; i < 4; i++) half(rw * i - 0.06, 0, rw * i + 0.06, ROOM_D, i >= 2);
    half(this.W - 0.12, 0, this.W, ROOM_D, true);
    // vách trước có cửa ở giữa
    for (let i = 0; i < 4; i++) {
      const x0 = rw * i, cx = x0 + rw / 2, door = 0.85;
      half(x0, ROOM_D - 0.06, cx - door, ROOM_D + 0.06, i >= 2);
      half(cx + door, ROOM_D - 0.06, x0 + rw, ROOM_D + 0.06, i >= 2);
      box(g, door * 2, 0.012, 0.6, '#c9b597', cx, 0.006, ROOM_D);
    }
  }

  // ------------------------------------------------------------ cụm bàn theo team
  _clusters() {
    const g = this.group;
    for (const c of this.clusters) {
      box(g, c.w, 0.012, c.d, lighter(c.color, 0.72), c.cx, 0.004, c.cz);
      box(g, c.w + 0.1, 0.008, c.d + 0.1, lighter(c.color, 0.45), c.cx, 0.001, c.cz);
      const deskX0 = c.cx - (c.k * 1.4) / 2 + 0.7;
      const rnd = seeded(hashStr(c.id || 'none'));
      let mi = 0;
      for (let j = 0; j < c.k; j++) {
        const x = deskX0 + j * 1.4;
        for (const side of [-1, 1]) {
          const dz = c.cz + side * 0.4;
          const id = c.members[mi++];
          const screen = this._desk(x, dz, side, rnd);
          this._chair(x, c.cz + side * 1.35, side, darker(c.color, 0.75));
          if (id) this.seats.set(id, { zone: 'desk', x, z: c.cz + side * 1.35, rot: side < 0 ? 0 : Math.PI, pose: 'sit', seatY: 0.33, owner: id, screen, team: c.id, stand: { x, z: c.cz + side * 1.78 } });
        }
      }
      this.grid.rect(deskX0 - 0.68, c.cz - 0.8, deskX0 + (c.k - 1) * 1.4 + 0.68, c.cz + 0.8, 0.08);
      // biển tên team lơ lửng
      const s = labelSprite(c.name, c.color, { scale: 1.25 });
      s.position.set(c.x + 0.15, 1.95, c.cz); g.add(s); this.textures.push(s.material.map); this.signs.push(s);
      // hiệu ứng ESTOP team: lớp đỏ trên sàn + đèn đỏ
      const ov = box(g, c.w, 0.02, c.d, '', c.cx, 0.02, c.cz, new THREE.MeshBasicMaterial({ color: '#ff3b30', transparent: true, opacity: 0.3, depthWrite: false }));
      const lamp = new THREE.Mesh(new THREE.ConeGeometry(Math.max(c.w, c.d) * 0.55, 3, 24, 1, true), new THREE.MeshBasicMaterial({ color: '#ff4d40', transparent: true, opacity: 0.12, depthWrite: false, side: THREE.DoubleSide }));
      lamp.position.set(c.cx, 1.5, c.cz);
      ov.visible = lamp.visible = false; g.add(lamp);
      this.teamFx.set(c.id, { ov, lamp, on: false });
    }
  }

  _desk(x, z, side, rnd) {
    const g = this.group;
    box(g, 1.3, 0.05, 0.78, '#f3e6d1', x, 0.6, z);
    box(g, 0.05, 0.6, 0.7, '#d8c3a5', x - 0.6, 0, z); box(g, 0.05, 0.6, 0.7, '#d8c3a5', x + 0.6, 0, z);
    box(g, 1.2, 0.35, 0.03, '#e3d2b8', x, 0.25, z - side * 0.36);
    box(g, 0.42, 0.02, 0.13, '#ffffff', x, 0.65, z + side * 0.18);
    box(g, 0.1, 0.03, 0.14, '#ffffff', x + 0.33, 0.65, z + side * 0.18);
    const screen = this._monitor(x, z - side * 0.16, side, 0.65, 0.6);
    const r = rnd();
    if (r < 0.35) this._plant(x - 0.45, z - side * 0.2, 0.35, 0.65);
    else if (r < 0.7) cyl(g, 0.045, 0.04, 0.1, ['#ef8a9a', '#4f8fc0', '#e9b44c', '#ffffff'][Math.floor(rnd() * 4)], x - 0.45, 0.65, z + side * 0.1);
    return screen;
  }

  // màn hình: mặt màn hình (MeshBasic riêng) hướng về phía ghế (side<0 → ghế ở phía bắc)
  _monitor(x, z, side, y0, w) {
    const g = this.group, ww = w || 0.6;
    box(g, 0.08, 0.16, 0.06, '#4a4a52', x, y0, z);
    box(g, 0.26, 0.02, 0.18, '#4a4a52', x, y0, z);
    box(g, ww, 0.38, 0.04, '#33343a', x, y0 + 0.14, z);
    const scr = new THREE.MeshBasicMaterial({ color: '#2c3138' });
    box(g, ww - 0.06, 0.32, 0.01, '', x, y0 + 0.17, z + side * 0.025, scr);
    return scr;
  }

  _chair(x, z, side, color) {
    const g = this.group;
    box(g, 0.44, 0.07, 0.44, color, x, 0.26, z);
    box(g, 0.44, 0.42, 0.07, color, x, 0.33, z + side * 0.2);
    cyl(g, 0.03, 0.03, 0.2, '#777777', x, 0.06, z, 8);
    cyl(g, 0.2, 0.2, 0.05, '#666666', x, 0.0, z, 14);
  }

  _plant(x, z, s, y0) {
    const g = this.group, k = s || 1, y = y0 || 0;
    cyl(g, 0.16 * k, 0.12 * k, 0.28 * k, '#d98c6a', x, y, z, 12);
    sphere(g, 0.2 * k, '#6fae5f', x, y + 0.42 * k, z, 10);
    sphere(g, 0.15 * k, '#7fc06b', x + 0.1 * k, y + 0.55 * k, z + 0.05 * k, 10);
    sphere(g, 0.13 * k, '#5e9d52', x - 0.1 * k, y + 0.52 * k, z - 0.06 * k, 10);
    if (!y0) this.grid.rect(x - 0.2 * k, z - 0.2 * k, x + 0.2 * k, z + 0.2 * k, 0.05);
  }

  // ------------------------------------------------------------ dải phía nam + tường tây (kanban, đồng hồ)
  _south() {
    const g = this.group, W = this.W, zs = this.zs, grid = this.grid;
    const sign = (x, text, color) => {
      const s = labelSprite(text, color, { scale: 1.1, bg: 'rgba(61,57,41,0.88)', fg: '#fffaf0', stroke: 'rgba(0,0,0,0)' });
      s.position.set(x, 2.5, zs + 0.6); g.add(s); this.textures.push(s.material.map); this.signs.push(s);
    };
    // pantry
    box(g, 7.6, 0.012, 5.8, '#f4ead9', 3.8, 0, zs + 3);
    box(g, 0.75, 0.9, 3.8, '#d8b48a', 0.45, 0, zs + 2.8); box(g, 0.82, 0.05, 3.85, '#f7f2e8', 0.45, 0.9, zs + 2.8);
    grid.rect(0, zs + 0.9, 0.85, zs + 4.7, 0.1);
    box(g, 0.42, 0.48, 0.38, '#3a3a40', 0.42, 0.95, zs + 1.6); box(g, 0.3, 0.06, 0.2, '#22262b', 0.5, 1.2, zs + 1.6);
    sphere(g, 0.03, '#ff5a4a', 0.64, 1.33, zs + 1.5).material = basic('#ff5a4a');
    cyl(g, 0.05, 0.04, 0.09, '#ffffff', 0.55, 0.95, zs + 1.6);
    cyl(g, 0.12, 0.12, 0.16, '#c8413a', 0.45, 0.95, zs + 3.2); // hộp bánh
    for (let i = 0; i < 3; i++) cyl(g, 0.045, 0.04, 0.1, ['#ffffff', '#ef8a9a', '#4f8fc0'][i], 0.4, 0.95, zs + 3.7 + i * 0.15);
    box(g, 0.85, 1.9, 0.8, '#e9eef2', 0.45, 0, zs + 5.15); box(g, 0.03, 0.5, 0.04, '#9aa5ad', 0.9, 1.0, zs + 4.9);
    grid.rect(0, zs + 4.75, 0.9, zs + 5.55, 0.1);
    for (const dz of [1.6, 2.6, 3.6]) this._spot('pantry', 1.45, zs + dz, -Math.PI / 2, 'drink');
    // bàn cao + 2 ghế quầy
    const hx = 4.0, hz = zs + 3.0;
    cyl(g, 0.45, 0.45, 0.05, '#f7f2e8', hx, 0.95, hz, 20); cyl(g, 0.05, 0.12, 0.95, '#555555', hx, 0, hz);
    grid.rect(hx - 0.45, hz - 0.45, hx + 0.45, hz + 0.45, 0.05);
    for (const s of [-1, 1]) {
      cyl(g, 0.2, 0.2, 0.06, '#e9b44c', hx + s * 0.75, 0.56, hz, 16); cyl(g, 0.03, 0.03, 0.56, '#666666', hx + s * 0.75, 0, hz, 8);
      this._spot('pantry', hx + s * 0.75, hz, s < 0 ? Math.PI / 2 : -Math.PI / 2, 'sitDrink', 0.62);
    }
    this._plant(7.2, zs + 5.4, 1.1);
    sign(3.8, 'Pantry ☕', '#d98c3f');
    // bình nước
    box(g, 0.4, 0.9, 0.4, '#dfe6ea', 6.8, 0, zs + 0.7); cyl(g, 0.16, 0.16, 0.45, '#9fd3f0', 6.8, 0.9, zs + 0.7);
    grid.rect(6.6, zs + 0.5, 7.0, zs + 0.9, 0.08);

    // phòng nghỉ (sofa)
    const lx0 = 8, lx1 = W - 8, lcx = (lx0 + lx1) / 2, lcz = zs + 3;
    box(g, Math.min(6, lx1 - lx0 - 0.5), 0.014, 4.2, '#e9c2ad', lcx, 0, lcz);
    box(g, Math.min(6, lx1 - lx0 - 0.5) - 0.3, 0.016, 3.9, '#f3d6c4', lcx, 0, lcz);
    const sofa = (z, side, color, dark) => {
      // side = -1: lưng ở phía bắc, người ngồi nhìn về +z
      box(g, 2.5, 0.3, 0.85, color, lcx, 0.05, z);
      box(g, 2.5, 0.5, 0.2, dark, lcx, 0.3, z + side * 0.36);
      for (const sx of [-1.15, 1.15]) box(g, 0.22, 0.42, 0.85, dark, lcx + sx, 0.05, z);
      for (const sx of [-0.72, 0, 0.72]) box(g, 0.66, 0.06, 0.62, lighter(color, 0.2), lcx + sx, 0.35, z - side * 0.06);
      grid.rect(lcx - 1.26, z - 0.43, lcx + 1.26, z + 0.43, 0.05);
      for (const sx of [-0.72, 0, 0.72]) this._spot('lounge', lcx + sx, z - side * 0.05, side < 0 ? 0 : Math.PI, 'relax', 0.4);
    };
    sofa(zs + 1.4, -1, '#7aa6c2', '#5f8aa6');
    sofa(zs + 4.6, 1, '#e59a7a', '#c97d5d');
    box(g, 1.4, 0.32, 0.7, '#b98858', lcx, 0, lcz); box(g, 1.5, 0.04, 0.8, '#d7ab7a', lcx, 0.32, lcz);
    cyl(g, 0.07, 0.06, 0.12, '#ffffff', lcx - 0.3, 0.36, lcz); sphere(g, 0.12, '#f2c76e', lcx + 0.35, 0.44, lcz + 0.1, 10);
    grid.rect(lcx - 0.75, lcz - 0.4, lcx + 0.75, lcz + 0.4, 0.05);
    this._plant(lcx - 2.6, zs + 0.7, 1.2); this._plant(lcx + 2.6, zs + 5.3, 1.2);
    cyl(g, 0.03, 0.03, 1.6, '#555555', lcx + 2.7, 0, zs + 1.3, 8); cyl(g, 0.12, 0.25, 0.28, '#fff1c7', lcx + 2.7, 1.55, zs + 1.3, 14);
    sign(lcx, 'Phòng nghỉ', '#7aa6c2');

    // hàng ghế chờ
    const bx = W - 4.3;
    box(g, 6.8, 0.012, 5.8, '#e6e1d6', W - 3.9, 0, zs + 3);
    for (const bz of [zs + 1.3, zs + 2.8]) {
      box(g, 2.6, 0.07, 0.5, '#4f8fc0', bx, 0.28, bz); box(g, 2.6, 0.4, 0.06, '#3f78a5', bx, 0.35, bz - 0.24);
      for (const sx of [-1.2, 1.2]) box(g, 0.06, 0.28, 0.45, '#555555', bx + sx, 0, bz);
      grid.rect(bx - 1.3, bz - 0.27, bx + 1.3, bz + 0.25, 0.04);
      for (const sx of [-0.85, 0, 0.85]) this._spot('bench', bx + sx, bz, 0, 'sit', 0.35);
    }
    // thảm lối vào + bảng "Cần bạn" (đứng nghiêng như giá vẽ, quay về phía camera)
    box(g, 1.8, 0.012, 1.0, '#a65f43', W - 1.4, 0, this.D - 0.7);
    // cửa ra vào "Thoát" (bấm = đăng xuất, có xác nhận 3D)
    const door = new THREE.Group(); door.position.set(W - 1.4, 0, this.D - 0.08); g.add(door);
    for (const sx of [-0.62, 0.62]) box(door, 0.14, 2.3, 0.16, '#6a432b', sx, 0, 0);
    box(door, 1.38, 0.16, 0.16, '#6a432b', 0, 2.3, 0);
    const leaf = box(door, 1.06, 2.2, 0.06, '#c98b5a', 0, 0.02, 0.02);
    sphere(door, 0.05, '#f2c76e', 0.4, 1.05, 0.08, 10);
    const exitSign = new Board(1.2, 0.36, 360, 'exit', 'cửa ra vào');
    exitSign.draw('exit', (ctx, w, h) => {
      ctx.fillStyle = '#2f7d4a'; ctx.fillRect(0, 0, w, h); ctx.fillStyle = '#ffffff'; ctx.font = '800 64px ' + FONT;
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText('🚪 Thoát', w / 2, h / 2 + 2); ctx.textAlign = 'left';
    });
    exitSign.mesh.position.set(0, 2.62, 0.04); door.add(exitSign.mesh);
    leaf.userData.exit = true; exitSign.mesh.userData.exit = true;
    this.exitBoard = exitSign;
    this.exitMeshes = [leaf, exitSign.mesh];
    grid.rect(W - 2.1, this.D - 0.25, W - 0.7, this.D, 0.02);
    const needs = new Board(3.6, 2.25, 1024, 'needs', 'bảng Cần bạn');
    const ng = new THREE.Group(); ng.position.set(bx, 0, this.D - 0.45); g.add(ng);
    const tilt = new THREE.Group(); tilt.position.y = 0.3; tilt.rotation.x = -0.22; ng.add(tilt);
    box(tilt, 3.78, 2.42, 0.08, '#8a5f39', 0, -0.08, -0.06);
    needs.mesh.position.set(0, 2.25 / 2, 0.0); tilt.add(needs.mesh);
    for (const sx of [-1.6, 1.6]) box(ng, 0.08, 0.75, 0.08, '#6a432b', sx, 0, -0.2);
    grid.rect(bx - 1.9, this.D - 0.7, bx + 1.9, this.D - 0.2, 0.05);
    this._plant(W - 0.6, zs + 0.7, 1.1);
    sign(bx, 'Hàng ghế chờ ⏳', '#4f8fc0');

    // tường tây: bảng kanban + đồng hồ
    const kz = Math.max(ROOM_D + 2.2, (this.openZ0 + this.openZ1) / 2);
    const kanban = new Board(3.6, 2.25, 1024, 'kanban', 'bảng công việc');
    box(g, 0.06, 2.4, 3.76, '#8a5f39', 0.04, 0.22, kz);
    kanban.mesh.rotation.y = Math.PI / 2; kanban.mesh.position.set(0.08, 1.42, kz); g.add(kanban.mesh);
    // màn hình phòng server (tường tây trong phòng server): run đang chạy
    const runs = new Board(2.6, 1.6, 900, 'runs', 'màn hình phòng server');
    box(g, 0.06, 1.74, 2.74, '#22262b', 0.04, 0.58, 3.4);
    runs.mesh.rotation.y = Math.PI / 2; runs.mesh.position.set(0.08, 1.45, 3.4); g.add(runs.mesh);
    // màn hình lớn trên tường bắc: HUD của hub
    const hud = new Board(9, 2.25, 1600, 'hud', 'màn hình trạng thái hub');
    box(g, 9.3, 2.5, 0.14, '#2a2e38', W / 2, 2.72, -0.05);
    for (const sx of [-3.5, 3.5]) box(g, 0.18, 0.5, 0.3, '#3a3f4a', W / 2 + sx, 2.4, -0.1);
    hud.mesh.position.set(W / 2, 3.97, 0.03); g.add(hud.mesh);
    this.boards = { kanban, runs, hud, needs };
    this.focusables = [kanban.mesh, runs.mesh, hud.mesh, needs.mesh];
    const cz = kz + 2.6;
    const face = new THREE.Mesh(new THREE.CylinderGeometry(0.34, 0.34, 0.06, 32), new THREE.MeshBasicMaterial({ color: '#fffdf6' }));
    face.rotation.z = Math.PI / 2; face.position.set(0.06, 1.95, cz); g.add(face);
    const rim = new THREE.Mesh(new THREE.TorusGeometry(0.34, 0.035, 8, 32), mat('#c96442'));
    rim.rotation.y = Math.PI / 2; rim.position.set(0.09, 1.95, cz); g.add(rim);
    this.clockFace = face;
    for (let i = 0; i < 12; i++) {
      const a = (i / 12) * Math.PI * 2;
      box(g, 0.01, i % 3 ? 0.03 : 0.06, 0.02, '#3d3929', 0.1, 1.95 + Math.cos(a) * 0.27 - (i % 3 ? 0.015 : 0.03), cz - Math.sin(a) * 0.27);
    }
    const hand = (len, w, color) => {
      const p = new THREE.Group(); p.position.set(0.11, 1.95, cz);
      const m = new THREE.Mesh(new THREE.BoxGeometry(0.012, len, w), mat(color)); m.position.y = len / 2 - 0.03; p.add(m);
      g.add(p); return p;
    };
    this.hHour = hand(0.17, 0.035, '#3d3929'); this.hMin = hand(0.25, 0.022, '#3d3929'); this.hSec = hand(0.27, 0.01, '#c96442');
  }


  // ------------------------------------------------------------ giữ chỗ
  seatFor(id) { return this.seats.get(id) || null; }
  reserve(zone, id, near) {
    const spots = this.zones[zone];
    if (!spots) return null;
    let best = null, bd = Infinity;
    for (const s of spots) {
      if (s.owner && s.owner !== id) continue;
      if (s.owner === id) return s;
      const d = near ? (s.x - near.x) ** 2 + (s.z - near.z) ** 2 : 0;
      // xáo nhẹ theo id để không ai cũng chọn đúng một ghế
      const j = d + ((hashStr(id + s.x + ':' + s.z) % 100) / 100) * 6;
      if (j < bd) { bd = j; best = s; }
    }
    if (best) best.owner = id;
    return best;
  }
  release(spot, id) { if (spot && this.zones[spot.zone] && spot.owner === id) spot.owner = null; }

  setScreen(id, on) {
    const s = this.seats.get(id);
    if (s && s.screen && s._on !== on) { s._on = on; s.screen.color.set(on ? '#9fd8ff' : '#2c3138'); }
  }
  setEstop(on) { this.estop = !!on; if (!on) { this.hemi.color.copy(this.hemiBase); this.dir.color.copy(this.dirBase); this.hemi.intensity = 2.1; } }
  setTeamEstop(id, on) {
    const fx = this.teamFx.get(id || '');
    if (fx) { fx.on = !!on; fx.ov.visible = fx.lamp.visible = !!on; }
  }
  setLabelScale(f) { for (const s of this.signs) scaleLabel(s, f); }
  // ẩn biển phòng/team khi camera đang nhìn một bảng (biển nổi che chữ trên bảng)
  setSignsVisible(on) { for (const s of this.signs) s.visible = on; }
  flashClock() { this.clockFlash = 2.5; }

  update(dt, t, reduced) {
    for (const l of this.leds) {
      const on = reduced ? true : Math.sin(t * l.rate * 4 + l.ph) > -0.2;
      const m = on ? this.ledMat[l.kind] : this.ledMat.off;
      if (l.m.material !== m) l.m.material = m;
    }
    const now = new Date();
    const s = now.getSeconds() + now.getMilliseconds() / 1000, mi = now.getMinutes() + s / 60, h = (now.getHours() % 12) + mi / 60;
    this.hSec.rotation.x = -(s / 60) * Math.PI * 2; this.hMin.rotation.x = -(mi / 60) * Math.PI * 2; this.hHour.rotation.x = -(h / 12) * Math.PI * 2;
    if (this.clockFlash > 0) {
      this.clockFlash -= dt;
      const on = this.clockFlash > 0 && (reduced || Math.sin(t * 14) > 0);
      this.clockFace.material.color.set(on ? '#ffd27a' : '#fffdf6');
    }
    if (this.estop) {
      const p = reduced ? 0.6 : 0.5 + 0.5 * Math.sin(t * 3);
      this.hemi.color.set('#ff6b5e').lerp(this.hemiBase, 0.25 + p * 0.25);
      this.dir.color.set('#ff7b6e');
      this.hemi.intensity = 1.6 + p * 0.5;
    }
    for (const fx of this.teamFx.values()) {
      if (!fx.on) continue;
      const p = reduced ? 0.6 : 0.5 + 0.5 * Math.sin(t * 4);
      fx.ov.material.opacity = 0.18 + p * 0.22; fx.lamp.material.opacity = 0.06 + p * 0.1;
    }
  }

  dispose() {
    for (const t of this.textures) t.dispose();
    for (const b of Object.values(this.boards || {})) b.dispose();
    if (this.exitBoard) this.exitBoard.dispose();
    this.group.traverse((o) => {
      if (o.isInstancedMesh) { o.geometry.dispose(); o.material.dispose(); }
    });
    this.group.removeFromParent();
  }
}
