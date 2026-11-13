/* Văn phòng 3D — dựng mặt bằng từ dữ liệu: dãy phòng phía bắc (server, thư viện, phòng họp, điều phối viên), cụm bàn theo team ở giữa,
 * khu cafe điện tử (agent rảnh ngồi chơi game, số máy co giãn theo số agent),
 * dải phía nam (pantry, phòng nghỉ sofa, hàng ghế chờ). Tường bắc + tây cao (kiểu nhà búp bê), vách trong thấp để camera nhìn được.
 * Mọi "điểm đứng/ngồi" (spot) có hướng + tư thế; spot dùng chung được giữ chỗ để nhân vật không chồng lên nhau.
 * Hình ảnh (docs/dev-notes/office-3d-visual.md): mô hình kiến trúc cắt lớp — đồ tĩnh gộp theo vật liệu (office/materials.js Batch),
 * PBR + bóng tĩnh ở tier high, AO giả + vũng đèn ở cả hai tier; tường/vách (occluder), màn hình, bảng, cửa, đồng hồ giữ Mesh riêng. */
import * as THREE from '../vendor/three-0.169.0/three.module.min.js';
import { Grid } from './path.js';
import { Board } from './boards.js';
import { TEAM_COLORS, NO_TEAM_COLOR, seeded, hashStr, textCanvas, roundRect, FONT, fitText, scaleLabel } from './util.js';
import { Batch, surface, solidMesh, roundedBoxGeo, mergeGeometries, litMaterial, tex, softTex, getTier } from './materials.js';

export const ZONE_NAMES = {
  desk: 'Bàn làm việc', server: 'Phòng server', library: 'Thư viện', meeting: 'Phòng họp', mgr: 'Phòng điều phối viên',
  mgrVisit: 'Phòng điều phối viên', pantry: 'Pantry', lounge: 'Phòng nghỉ', bench: 'Hàng ghế chờ',
  cafe: 'Cafe điện tử', cafeBar: 'Quầy nước cafe',
};

const ROOM_D = 6;
const WALL_H = 2.6;
// khu cafe điện tử (mét): lối đi hai bên ngoài vách, lối vào phía tây trong phòng, quầy nước phía đông,
// bề rộng một máy, khoảng vách → tâm dãy máy, khoảng cách hai dãy, độ dày dải vách, số máy tối thiểu
const CAFE_SIDE = 2.0, CAFE_AISLE = 1.8, CAFE_BAR = 3.4, CAFE_STEP = 1.2, CAFE_EDGE = 2.7, CAFE_ROW = 3.8, CAFE_WALL = 0.3, CAFE_MIN = 6;
const CAFE_SCREEN_OFF = '#1d2030';
const SCREEN_OFF = '#1b1f26', SCREEN_ON = '#9fd8ff';
// bảng màu (§1)
const CUT = '#3f3a35', BASE = '#6d543c', PLASTER = '#efe9df', LOWWALL = '#e6dfd3', WOOD = '#e6d6bd', WALNUT = '#6b4a33';
const BLACK = '#26272b', ALU = '#a9aeb4', BACKF = '#34363c', LEAVES = ['#4f7d4a', '#5f8f52', '#3f6b3f'];
// nền đất = ĐÚNG màu nền trang (scene.background, không chiếu sáng, không tone mapping) → tan liền vào trang ở mọi tier/theme
const THEME = { light: { ground: '#faf9f5', plinth: '#cfc6b8' }, dark: { ground: '#262624', plinth: '#3a3732' } };
// tier low không có env PMREM: bù phần sáng env bằng hemi (≈ cùng độ sáng high, không ngả nâu) — đo bằng so pixel sàn high/low
const LOW_ENV_HEMI = 1.6;
// tier high cũng không còn IBL cho World (PBR không env map — tiết kiệm sample PMREM mỗi pixel, đắt trên Iris Xe ở góc đi bộ):
// bù bằng hemi như low, hệ số chọn bằng so độ sáng pixel sàn với bản có IBL (9h/14h/18h, theme tối)
const HIGH_ENV_HEMI = 1.9;
// chế độ đi bộ: shadow camera thu nhỏ quanh avatar (±SHADOW_NEAR m, tâm lệch FOCUS_AHEAD m về hướng nhìn) → texel nhỏ ~2,5× (hết bậc thang);
// tâm lệch quá FOCUS_MOVE m mới vẽ lại bóng (1 khung)
const SHADOW_NEAR = 11, FOCUS_AHEAD = 4.5, FOCUS_MOVE = 2.5;
// ánh sáng theo giờ: [giờ, màu trời (hemi), cường độ hemi, màu nắng (dir), cường độ dir, đèn trần, kính cửa sổ, độ sáng 0–1,
//                     cường độ fill, scene.environmentIntensity gợi ý]
const DAYLIGHT = [
  [0, '#9aa6d4', 1.0, '#a9b8ff', 0.6, '#ffd9a0', '#1c2540', 0, 0.2, 0.32],
  [5.5, '#9aa6d4', 1.0, '#a9b8ff', 0.6, '#ffd9a0', '#1c2540', 0, 0.2, 0.32],
  [7.5, '#eef3ff', 0.42, '#fff1e0', 2.5, '#f3ead8', '#cfe6f5', 1, 0.15, 0.18],
  [12, '#fff8ee', 0.45, '#fff2dc', 2.7, '#f3ead8', '#d4ebf6', 1, 0.15, 0.18],
  [16.5, '#ffefdd', 0.43, '#ffd9ab', 2.6, '#f3ead8', '#e6e0cc', 1, 0.15, 0.18],
  [18.5, '#ffd2b8', 0.45, '#ff9a55', 2.0, '#ffd9a0', '#f29c62', 0.5, 0.14, 0.2],
  [20, '#9aa6d4', 1.0, '#a9b8ff', 0.6, '#ffd9a0', '#1c2540', 0, 0.2, 0.32],
  [24, '#9aa6d4', 1.0, '#a9b8ff', 0.6, '#ffd9a0', '#1c2540', 0, 0.2, 0.32],
];

// Khoá bố cục: đổi team/thành viên/điều phối viên → dựng lại văn phòng.
export function layoutKey(data) {
  const teams = (data.teams || []).map((t) => t.id).join(',');
  const ag = (data.agents || []).map((a) => a.id + ':' + (a.team || '') + ':' + (a.is_manager ? 1 : 0)).join(',');
  return teams + '|' + ag + '|' + (data.manager_id || '');
}

const hex = (c) => '#' + c.getHexString();
function darker(h, k) { return hex(new THREE.Color(h).multiplyScalar(k)); }
function mix(a, b, k) { return hex(new THREE.Color(a).lerp(new THREE.Color(b), k)); }
// màu team dùng cho bề mặt lớn: làm dịu về xám ấm; thảm team = rất nhạt
const muted = (c) => mix(c, '#8a8580', 0.25);
const rugCol = (c) => mix(muted(c), '#f3eee6', 0.62);

// danh sách quad → BufferGeometry (position, normal, uv); mỗi quad = 4 góc [x,y,z] theo uv (0,0) (1,0) (1,1) (0,1)
function quadGeo(list) {
  const pos = new Float32Array(list.length * 18), uv = new Float32Array(list.length * 12), nor = new Float32Array(list.length * 18);
  const UV = [0, 0, 1, 0, 1, 1, 0, 0, 1, 1, 0, 1], ORD = [0, 1, 2, 0, 2, 3];
  const a = new THREE.Vector3(), b = new THREE.Vector3(), n = new THREE.Vector3();
  list.forEach((q, i) => {
    a.fromArray(q[1]).sub(new THREE.Vector3().fromArray(q[0])); b.fromArray(q[3]).sub(new THREE.Vector3().fromArray(q[0]));
    n.crossVectors(a, b).normalize();
    ORD.forEach((k, j) => { pos.set(q[k], i * 18 + j * 3); nor.set([n.x, n.y, n.z], i * 18 + j * 3); });
    uv.set(UV, i * 12);
  });
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('normal', new THREE.BufferAttribute(nor, 3));
  g.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
  g.computeBoundingSphere();
  return g;
}
const floorQuad = (x, z, w, d, y) => [[x - w / 2, y, z + d / 2], [x + w / 2, y, z + d / 2], [x + w / 2, y, z - d / 2], [x - w / 2, y, z - d / 2]];

export class World {
  constructor(data) {
    this.group = new THREE.Group();
    this.textures = [];
    this.zones = { server: [], library: [], meeting: [], mgrVisit: [], pantry: [], lounge: [], bench: [], cafe: [], cafeBar: [] };
    this.cafeMats = [];          // vật liệu RGB/màn hình riêng của cafe (đổi màu theo thời gian)
    this.seats = new Map();      // agentId → spot bàn riêng
    this.signs = [];
    this.teamFx = new Map();     // teamId → {overlay, lamp}
    this.teamColor = new Map();
    this.estop = false;
    this.clockFlash = 0;
    this.rooms = [];             // {key, name, x0, z0, x1, z1, p:{x,z}} — phòng có vách (chế độ đi bộ: tên phòng + kiểm đi tới được)
    this.occluders = [];         // mesh tường/vách có thể che camera chế độ đi bộ → làm mờ (fade)
    this.ownMats = [];           // vật liệu riêng (đèn trần, ô cửa sổ, lớp mờ, AO, vũng đèn, glow) — dispose cùng World
    this.ownGeos = [];           // hình riêng (occluder, quad AO/vũng/glow, kim đồng hồ…) — dispose cùng World
    this.daylight = 1;
    this.envInt = 0.55;
    this.shadowDirty = true;     // office.js: true → vẽ lại shadow map tĩnh một lần
    this.batch = new Batch();
    this._ao = [];               // [x, z, w, d, nhẹ?] bóng tiếp xúc dưới đồ đạc
    this._pools = [];            // [x, z, w, d] vũng đèn ban đêm
    this._glow = [[], [], []];   // quad glow neon theo màu cafe
    this._shafts = [];           // quad vệt nắng
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
    // khu cafe điện tử: dải riêng giữa cụm bàn và dải phía nam, số máy co giãn theo số agent không phải điều phối viên
    // (+2 máy dư) để agent rảnh luôn có chỗ. Một "dãy" = 2 hàng máy quay lưng vào nhau; thiếu chỗ → thêm dãy (nới D).
    const zc = z + rowH + 1.2;
    const nAgents = clusters.reduce((n, c) => n + c.members.length, 0);
    const need = Math.max(CAFE_MIN, nAgents + 2);
    const cx0 = CAFE_SIDE, cx1 = W - CAFE_SIDE;
    const kMax = Math.max(2, Math.floor((cx1 - cx0 - CAFE_AISLE - CAFE_BAR) / CAFE_STEP));
    const rows = Math.ceil(need / (2 * kMax));
    const k = Math.ceil(need / (2 * rows));
    const cd = 2 * (CAFE_WALL + CAFE_EDGE) + (rows - 1) * CAFE_ROW;
    this.cafe = { x0: cx0, x1: cx1, z0: zc + CAFE_WALL, z1: zc + cd - CAFE_WALL, rows, k, seats: 2 * k * rows, need };
    const zs = zc + cd;
    this.W = W; this.zs = zs; this.D = zs + 6; this.clusters = clusters; this.mgrId = mgrId;
    this.openZ0 = ROOM_D; this.openZ1 = zc;
    this.grid = new Grid(0, 0, W, this.D, 0.5);
  }

  _spot(zone, x, z, rot, pose, seatY, extra) {
    const s = Object.assign({ zone, x, z, rot, pose, seatY: seatY || 0, owner: null }, extra || {});
    if (this.zones[zone]) this.zones[zone].push(s);
    return s;
  }

  // ------------------------------------------------------------ tiện ích dựng
  // occluder: Mesh riêng (raycast + fade đổi .material) — parts [{geo, matrix, color}] gộp thành một hình
  _occ(parts, kind) {
    const m = new THREE.Mesh(mergeGeometries(parts.map((p) => Object.assign({ uvSize: kind === 'plaster' ? 2 : 1 }, p))), surface(kind));
    m.castShadow = kind !== 'glass'; m.receiveShadow = kind !== 'glass';
    this.ownGeos.push(m.geometry); this.group.add(m); this.occluders.push(m);
    return m;
  }
  _solid(geo, kind, color, x, y, z, parent) {
    const m = solidMesh(geo, kind, color);
    m.position.set(x, y, z); (parent || this.group).add(m); this.ownGeos.push(m.geometry);
    return m;
  }
  _aoq(x, z, w, d, light) { this._ao.push([x, z, w + 0.24, d + 0.24, !!light]); }
  // tấm chữ phẳng MeshBasic (biển kiến trúc) — vào this.signs (ẩn khi xem bảng), không userData.base (không phóng to)
  _plate(texture, w, h, x, y, z, rotY, opts) {
    const o = opts || {};
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ map: texture, toneMapped: false, transparent: !!o.transparent, depthWrite: !o.transparent, side: o.double ? THREE.DoubleSide : THREE.FrontSide, forceSinglePass: true }));
    m.position.set(x, y, z); m.rotation.y = rotY || 0; if (o.rx) m.rotation.x = o.rx;
    if (o.order) m.renderOrder = o.order;
    this.group.add(m); this.signs.push(m); this.textures.push(texture); this.ownMats.push(m.material); this.ownGeos.push(m.geometry);
    return m;
  }
  _signTex(text, dot, w, h) {
    return textCanvas(w || 512, h || 128, (ctx, W, H) => {
      ctx.fillStyle = CUT; roundRect(ctx, 0, 0, W, H, H * 0.14); ctx.fill();
      ctx.fillStyle = 'rgba(255,255,255,0.08)'; ctx.fillRect(0, 0, W, 3);
      ctx.fillStyle = dot; ctx.beginPath(); ctx.arc(H * 0.42, H / 2, H * 0.13, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = '#fffaf0'; ctx.font = `700 ${Math.round(H * 0.44)}px ` + FONT; ctx.textBaseline = 'middle';
      ctx.fillText(fitText(ctx, text, W - H * 0.8), H * 0.72, H / 2 + 2);
    });
  }

  _build(data) {
    const g = this.group, W = this.W, D = this.D, grid = this.grid, B = this.batch;
    // sàn gỗ sồi trên bệ dày 0,3 m (mặt trên y = 0 cho raycast)
    const floor = solidMesh(new THREE.BoxGeometry(W, 0.3, D), 'floor', '#ffffff', { uvSize: 2 });
    floor.castShadow = false;
    floor.position.set(W / 2, -0.15, D / 2); g.add(floor); this.ownGeos.push(floor.geometry);
    this.floor = floor;
    // bệ (plinth) + nền đất tan vào fog — màu theo theme (setTheme)
    this.plinthMat = litMaterial({ color: THEME.light.plinth, roughness: 0.85 });
    this.groundMat = new THREE.MeshBasicMaterial({ color: THEME.light.ground, toneMapped: false });
    this.ownMats.push(this.plinthMat, this.groundMat);
    const plinth = new THREE.Mesh(roundedBoxGeo(W + 1.1, 0.32, D + 1.1, 0.04, 1), this.plinthMat);
    plinth.position.set(W / 2 - 0.1, -0.18, D / 2 + 0.1); plinth.receiveShadow = true; g.add(plinth);
    const groundGeo = new THREE.CircleGeometry(Math.max(W, D) * 3, 48); groundGeo.rotateX(-Math.PI / 2);
    const ground = new THREE.Mesh(groundGeo, this.groundMat);
    ground.position.set(W / 2, -0.34, D / 2); g.add(ground); this.ownGeos.push(groundGeo);
    // tier high: bóng của nhà đổ lên nền đất (lớp ShadowMaterial trong suốt trên nền không chiếu sáng)
    if (getTier() === 'high') {
      const catchMat = new THREE.ShadowMaterial({ color: '#000000', opacity: 0.16, depthWrite: false });
      const catcher = new THREE.Mesh(groundGeo, catchMat); this.ownMats.push(catchMat);
      catcher.position.set(W / 2, -0.335, D / 2); catcher.receiveShadow = true; catcher.renderOrder = 1; g.add(catcher);
    }

    // tường bắc + tây (cao, thạch cao dày 0,25), mặt cắt than ở đỉnh, chân tường sồi sẫm
    const bx = (w, h, d, x, y0, z, color) => ({ geo: new THREE.BoxGeometry(w, h, d), matrix: new THREE.Matrix4().makeTranslation(x, y0 + h / 2, z), color });
    this._occ([bx(W + 0.25, WALL_H, 0.25, W / 2 - 0.125, 0, -0.125, PLASTER)], 'plaster');
    this._occ([bx(0.25, WALL_H, D, -0.125, 0, D / 2, PLASTER)], 'plaster');
    B.box(W + 0.25, 0.02, 0.25, W / 2 - 0.125, WALL_H, -0.125, 'paint', CUT);
    B.box(0.25, 0.02, D, -0.125, WALL_H, D / 2, 'paint', CUT);
    B.box(W, 0.1, 0.02, W / 2, 0, 0.01, 'paint', BASE);
    B.box(0.02, 0.1, D, 0.01, 0, D / 2, 'paint', BASE);
    grid.rect(0, 0, W, 0.05, 0.1); grid.rect(0, 0, 0.05, D, 0.1);

    this._rooms(data);
    this._clusters();
    this._cafe();
    this._south();
    this._walls();
    this._lamps();
    this._finish();

    // ánh sáng: trời (hemi) + nắng (key, đổ bóng ở tier high) + fill mát ngược hướng nắng; màu/cường độ theo giờ thật (setDaylight)
    this.hemi = new THREE.HemisphereLight('#fff8ee', '#b89a7a', 1.1);
    this.dir = new THREE.DirectionalLight('#fff2dc', 3.0);
    this.dir.castShadow = true;
    const L = Math.hypot(W, D) * 0.62, sc = this.dir.shadow.camera;
    sc.left = -L; sc.right = L; sc.top = L; sc.bottom = -L; sc.near = 1; sc.far = 3 * Math.max(W, D); sc.updateProjectionMatrix();
    this.dir.shadow.mapSize.set(1024, 1024); // 1024 cả toàn cảnh lẫn đi bộ (phase 2: refocus khi đi bộ rẻ hơn; không cấp lại render target)
    this.dir.shadow.bias = -0.0004; this.dir.shadow.normalBias = 0.03; this.dir.shadow.radius = 2.5;
    this.dir.target.position.set(W / 2, 0, D / 2);
    this._shadowL = L; this._sunOff = new THREE.Vector3(); this._focus = null;
    this.shadowRefocus = false;  // office.js: true → vẽ lại shadow map 1 khung (tâm bóng đi theo avatar)
    this.fill = new THREE.DirectionalLight('#cfe0ff', 0.55);
    this.fill.target.position.set(W / 2, 0, D / 2);
    // bù phần sáng IBL đã bỏ (tier high): hemi RIÊNG màu trung tính-ấm như env PMREM (phòng xám nâu + đèn hộp trắng), KHÔNG theo màu trời —
    // bù bằng chính hemi trời thì ban đêm (trời #9aa6d4, envInt 0,32) cả văn phòng ám tím-xanh, tối hơn ~10–16 % so với bản có IBL
    this.envHemi = new THREE.HemisphereLight('#fff8ee', '#b89a7a', 0);
    g.add(this.hemi, this.envHemi, this.dir, this.dir.target, this.fill, this.fill.target);
    this.hemiBase = this.hemi.color.clone(); this.dirBase = this.dir.color.clone(); this.fillBase = this.fill.color.clone();
    this.hemiInt = 1.1; this.dirInt = 3.0;
    this._sunKey = null;
    // lớp mờ cho tường che camera (chế độ đi bộ): MỘT vật liệu riêng, gán tạm thay vật liệu gốc rồi trả lại
    this.fadeMat = new THREE.MeshLambertMaterial({ color: '#efe5d6', transparent: true, opacity: 0.18, depthWrite: false });
    this.ownMats.push(this.fadeMat);
    this.faded = new Set();
  }

  // gộp batch + dựng AO / vũng đèn / glow / vệt nắng (mỗi loại một mesh)
  _finish() {
    const g = this.group;
    const emit = new THREE.MeshBasicMaterial({ vertexColors: true, toneMapped: false });
    this.ownMats.push(emit);
    const ov = { emit, sky: this.skyMat, warm: this.warmLed };
    this.rgbMats.forEach((m, i) => { ov['rgb' + i] = m; });
    this.batchMeshes = this.batch.build(g, ov);
    const lb = new Batch();
    this.lampGroup = new THREE.Group(); g.add(this.lampGroup);
    for (const [kind, args] of this._lampParts) lb[kind](...args);
    this.lampBatch = lb;
    lb.build(this.lampGroup, { lamp: this.lampMat });
    // AO: đen, mờ dần ở mép; 2 mức đậm (đồ lớn / ghế, chậu)
    for (const [light, op] of [[false, 0.32], [true, 0.2]]) {
      const list = this._ao.filter((a) => a[4] === light).map((a) => floorQuad(a[0], a[1], a[2], a[3], 0.021));
      if (!list.length) continue;
      const mat = new THREE.MeshBasicMaterial({ color: '#000000', map: softTex('ao'), transparent: true, opacity: op, depthWrite: false,
        polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -4, toneMapped: false });
      const m = new THREE.Mesh(quadGeo(list), mat); m.renderOrder = 1; g.add(m);
      this.ownMats.push(mat); this.ownGeos.push(m.geometry);
    }
    // vũng đèn ban đêm (cộng sáng)
    this.poolMat = new THREE.MeshBasicMaterial({ map: softTex('pool'), transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending, toneMapped: false });
    const pools = new THREE.Mesh(quadGeo(this._pools.map((p) => floorQuad(p[0], p[1], p[2], p[3], 0.026))), this.poolMat);
    pools.renderOrder = 2; g.add(pools); this.ownMats.push(this.poolMat); this.ownGeos.push(pools.geometry);
    // quầng neon cafe: 3 vật liệu (1/màu), màu chép từ rgbMats mỗi khung
    this.glowMats = this._glow.map((list) => {
      const mat = new THREE.MeshBasicMaterial({ map: softTex('glow'), transparent: true, opacity: 0.4, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide, forceSinglePass: true, toneMapped: false });
      if (list.length) { const m = new THREE.Mesh(quadGeo(list), mat); m.renderOrder = 2; g.add(m); this.ownGeos.push(m.geometry); }
      this.ownMats.push(mat);
      return mat;
    });
    // vệt nắng qua cửa sổ bắc (ban ngày)
    // forceSinglePass: vật trong suốt DoubleSide mặc định vẽ 2 lượt (mặt sau rồi mặt trước) và three đặt needsUpdate giữa 2 lượt → tính lại
    // program key (getParameters + getProgramCacheKey) MỖI khung cho từng vật liệu (đo: 60–120 lần/s mỗi cái). Cộng dồn / không ghi depth
    // nên thứ tự mặt không đổi hình.
    this.sunShaftMat = new THREE.MeshBasicMaterial({ color: '#fff2d8', map: softTex('glow'), transparent: true, opacity: 0.1, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide, forceSinglePass: true, toneMapped: false });
    const sh = new THREE.Mesh(quadGeo(this._shafts), this.sunShaftMat); sh.renderOrder = 2; g.add(sh);
    this.ownMats.push(this.sunShaftMat); this.ownGeos.push(sh.geometry);
    this._ao = this._pools = this._glow = this._shafts = this._lampParts = null;
  }

  // đèn LED thanh treo trên mỗi cụm bàn và dãy máy cafe (ẩn ở toàn cảnh — office.js setLampsVisible); cây góc khu làm việc
  _lamps() {
    this.lampMat = new THREE.MeshBasicMaterial({ color: '#f3ead8', toneMapped: false });
    this.ownMats.push(this.lampMat);
    const P = this._lampParts = [];
    const lamp = (x, z) => {
      P.push(['box', [1.2, 0.04, 0.1, x, 2.8, z, 'metal', ALU, { r: 0.012 }]]);
      P.push(['box', [1.14, 0.004, 0.06, x, 2.797, z, 'lamp', '#ffffff']]);
      for (const sx of [-0.5, 0.5]) P.push(['box', [0.006, 0.56, 0.006, x + sx, 2.84, z, 'metal', BLACK]]);
      this._pools.push([x, z, 2.2, 1.6]);
    };
    for (const c of this.clusters) {
      const n = Math.max(1, Math.round(c.w / 4));
      for (let i = 0; i < n; i++) lamp(c.x + (c.w * (i + 0.5)) / n, c.cz);
    }
    const cf = this.cafe;
    for (let i = 0; i < cf.rows; i++) for (const fx of [0.3, 0.6]) lamp(cf.x0 + (cf.x1 - cf.x0) * fx, cf.z0 + CAFE_EDGE + i * CAFE_ROW);
    this._plant(this.W - 0.5, ROOM_D + 0.8, 1.1);
    this._plant(this.W - 0.5, this.openZ1 - 0.5, 1.1);
  }
  setLampsVisible(on) { if (this.lampGroup) this.lampGroup.visible = !!on; }

  // Ánh sáng theo giờ thật (giờ địa phương, số thực 0–24): sáng mát → trưa ấm → chiều cam → tối xanh + đèn trần bật.
  // ESTOP ghi đè màu trong update(); tắt ESTOP trả về mức của giờ hiện tại.
  setDaylight(h) {
    const K = DAYLIGHT;
    let i = 0;
    while (i < K.length - 2 && h >= K[i + 1][0]) i++;
    const a = K[i], b = K[i + 1], t = Math.min(1, Math.max(0, (h - a[0]) / Math.max(1e-6, b[0] - a[0])));
    const col = (x, y) => new THREE.Color(x).lerp(new THREE.Color(y), t);
    const num = (k) => a[k] + (b[k] - a[k]) * t;
    this.hemiBase.copy(col(a[1], b[1])); this.hemiInt = num(2);
    this.dirBase.copy(col(a[3], b[3])); this.dirInt = num(4);
    this.lampMat.color.copy(col(a[5], b[5]));
    this.skyMat.color.copy(col(a[6], b[6]));
    this.daylight = num(7);
    this.fill.intensity = num(8);
    this.envInt = num(9);
    // quỹ đạo mặt trời: đông (sáng) → nam-cao (trưa) → tây (chiều); lượng tử 0,25 giờ → đổi bậc mới vẽ lại bóng
    const hq = Math.round(h * 4) / 4;
    if (hq !== this._sunKey) {
      this._sunKey = hq;
      // lệch pha 0,7 rad: trưa nắng từ tây-nam → bóng đổ chéo sang đông, nhìn thấy được từ camera toàn cảnh (đặt ở đông-nam)
      const W = this.W, D = this.D, L = Math.max(W, D), phi = Math.PI * Math.min(1, Math.max(0, (hq - 6) / 13)) * 0.8 + 0.7;
      this._sunOff.set(0.8 * L * Math.cos(phi), L * (0.36 + 0.26 * Math.sin(phi)), 0.6 * L * Math.sin(phi));
      this._applySun();
      this.fill.position.set(W / 2 - 0.6 * L * Math.cos(phi), L * 0.6, D / 2 - 0.5 * L);
      this.shadowDirty = true;
    }
    const night = 1 - this.daylight;
    this.poolMat.opacity = 0.55 * night;
    this.sunShaftMat.opacity = 0.1 * this.daylight;
    this.sunShaftMat.visible = this.daylight > 0.02;
    this.poolMat.visible = night > 0.02;
    for (const m of this.glowMats) m.opacity = 0.16 + 0.3 * night;
    if (!this.estop) this._lightBase();
  }
  _lightBase() {
    this.hemi.color.copy(this.hemiBase); this.dir.color.copy(this.dirBase); this.fill.color.copy(this.fillBase);
    const low = getTier() === 'low';
    this.hemi.intensity = this.hemiInt + (low ? this.envInt * LOW_ENV_HEMI : 0); this.dir.intensity = this.dirInt;
    this.envHemi.intensity = low ? 0 : this.envInt * HIGH_ENV_HEMI;
    this.envHemi.color.set('#fff8ee');
  }
  // đặt đèn nắng + shadow camera: toàn cảnh = cả văn phòng; đi bộ (setShadowFocus) = ô vuông nhỏ quanh tâm bóng
  _applySun() {
    const f = this._focus, cx = f ? f.x : this.W / 2, cz = f ? f.z : this.D / 2, r = f ? SHADOW_NEAR : this._shadowL;
    this.dir.target.position.set(cx, 0, cz); this.dir.target.updateMatrixWorld();
    this.dir.position.set(cx + this._sunOff.x, this._sunOff.y, cz + this._sunOff.z);
    const sc = this.dir.shadow.camera;
    if (sc.right !== r) { sc.left = -r; sc.right = r; sc.top = r; sc.bottom = -r; sc.updateProjectionMatrix(); }
  }
  // Chế độ đi bộ: bóng nét quanh avatar. (x, z) = vị trí, view = hướng nhìn (walk.view); null → về bóng toàn cảnh.
  // Chỉ đổi khi tâm lệch > FOCUS_MOVE m → shadowRefocus (office.js vẽ lại map 1 khung), không vẽ lại mỗi khung.
  setShadowFocus(x, z, view) {
    if (x == null) {
      if (!this._focus) return;
      this._focus = null; this._applySun(); this.shadowRefocus = true; return;
    }
    const W = this.W, D = this.D;
    const tx = Math.min(W, Math.max(0, x + Math.sin(view || 0) * FOCUS_AHEAD)), tz = Math.min(D, Math.max(0, z + Math.cos(view || 0) * FOCUS_AHEAD));
    if (this._focus && Math.hypot(tx - this._focus.x, tz - this._focus.z) < FOCUS_MOVE) return;
    this._focus = { x: tx, z: tz }; this._applySun(); this.shadowRefocus = true;
  }
  setTheme(dark) {
    const T = dark ? THEME.dark : THEME.light;
    this.groundMat.color.set(T.ground); this.plinthMat.color.set(T.plinth);
    this.shadowDirty = true;
  }
  roomAt(x, z) { return this.rooms.find((r) => x >= r.x0 && x <= r.x1 && z >= r.z0 && z <= r.z1) || null; }
  // làm mờ đúng tập mesh `set` (mesh khác trả lại vật liệu gốc)
  fade(set) {
    for (const m of this.faded) if (!set.has(m)) { m.material = m.userData.mat0; this.faded.delete(m); }
    for (const m of set) if (!this.faded.has(m)) { m.userData.mat0 = m.material; m.material = this.fadeMat; this.faded.add(m); }
  }

  // ------------------------------------------------------------ dãy phòng phía bắc
  _rooms() {
    const g = this.group, rw = this.W / 4, grid = this.grid, B = this.batch;
    this.rw = rw;
    const sign = (i, text, color) => {
      // biển phòng gắn trên đỉnh vách trước, bên trái cửa, quay ra khu làm việc (+z)
      const x = rw * i + rw / 2 - 0.85 - Math.min(1.25, (rw / 2 - 0.85) / 2);
      B.box(1.66, 0.42, 0.04, x, 1.02, ROOM_D + 0.085, 'paint', CUT, { r: 0.01 });
      for (const sx of [-0.6, 0.6]) B.box(0.03, 0.06, 0.03, x + sx, 1.0, ROOM_D + 0.07, 'metal', BLACK);
      this._plate(this._signTex(text, color), 1.6, 0.4, x, 1.23, ROOM_D + 0.108, 0);
    };
    // cửa sổ cao trên tường bắc: 2 ô/phòng, kính = trời (màu theo giờ: setDaylight), khung đen, song chữ thập, bậu gỗ, vệt nắng
    this.skyMat = new THREE.MeshBasicMaterial({ color: '#d4ebf6', map: tex('sky'), toneMapped: false });
    this.ownMats.push(this.skyMat);
    const winGeo = new THREE.PlaneGeometry(0.9, 0.7); this.ownGeos.push(winGeo);
    for (let i = 0; i < 4; i++) for (const f of [-0.28, 0.28]) {
      const x = rw * i + rw / 2 + f * rw, y0 = 1.75;
      B.add(winGeo, new THREE.Matrix4().makeTranslation(x, y0 + 0.35, 0.012), 'sky', '#ffffff', { uv: 'keep' });
      B.box(1.0, 0.05, 0.05, x, y0 - 0.05, 0.025, 'metal', BLACK); B.box(1.0, 0.05, 0.05, x, y0 + 0.7, 0.025, 'metal', BLACK);
      for (const sx of [-0.475, 0.475]) B.box(0.05, 0.7, 0.05, x + sx, y0, 0.025, 'metal', BLACK);
      B.box(0.025, 0.7, 0.03, x, y0, 0.02, 'metal', BLACK); B.box(0.9, 0.025, 0.03, x, y0 + 0.42, 0.02, 'metal', BLACK);
      B.box(1.08, 0.04, 0.14, x, y0 - 0.09, 0.07, 'wood', WOOD);
      this._shafts.push([[x - 0.45, y0, 0.03], [x + 0.45, y0, 0.03], [x + 0.75, 0.02, 2.6], [x - 0.15, 0.02, 2.6]]);
    }

    // 0 — phòng server (sàn nâng)
    B.box(rw - 0.04, 0.012, ROOM_D - 0.04, rw / 2, 0, ROOM_D / 2, 'tiles', '#ffffff');
    const nR = 4, rackW = 0.9, gap = 0.45, span = nR * rackW + (nR - 1) * gap;
    const r0 = (rw - span) / 2 + rackW / 2;
    const rnd = seeded(42);
    const LED = { g: '#59f08a', a: '#ffc857', b: '#6cc6ff', off: '#26303a' };
    this.ledCol = Object.fromEntries(Object.entries(LED).map(([k, v]) => [k, new THREE.Color(v)]));
    this.ledMesh = new THREE.InstancedMesh(new THREE.BoxGeometry(0.09, 0.04, 0.02), new THREE.MeshBasicMaterial({ toneMapped: false }), nR * 12);
    this.leds = [];
    const m4 = new THREE.Matrix4();
    for (let i = 0; i < nR; i++) {
      const x = r0 + i * (rackW + gap);
      B.box(rackW, 2.0, 0.8, x, 0, 0.65, 'paint', '#2b2f36', { r: 0.02 });
      B.box(rackW - 0.1, 1.84, 0.02, x, 0.08, 1.055, 'paint', '#1d2026');
      for (let k = 0; k < 10; k++) B.box(rackW - 0.12, 0.012, 0.012, x, 0.18 + k * 0.18, 1.07, 'metal', '#5a6068');
      B.box(rackW - 0.04, 0.06, 0.06, x, 2.0, 0.65, 'paint', '#1d2026');
      for (let j = 0; j < 6; j++) for (const ox of [-0.25, 0.25]) {
        const n = this.leds.length;
        m4.makeTranslation(x + ox, 0.37 + j * 0.27, 1.085);
        this.ledMesh.setMatrixAt(n, m4); this.ledMesh.setColorAt(n, this.ledCol.off);
        this.leds.push({ kind: ['g', 'g', 'a', 'b'][Math.floor(rnd() * 4)], rate: 0.3 + rnd() * 1.6, ph: rnd() * 6, on: null });
      }
      this._aoq(x, 0.65, rackW, 0.8);
      grid.rect(x - rackW / 2, 0.25, x + rackW / 2, 1.05, 0.1);
      this._spot('server', x, 1.7, Math.PI, 'standType');
    }
    g.add(this.ledMesh);
    // điều hoà phòng máy
    B.box(0.7, 1.6, 0.5, rw - 0.6, 0, ROOM_D - 1.2, 'paint', '#e8edf0', { r: 0.03 });
    for (let k = 0; k < 6; k++) B.box(0.52, 0.012, 0.02, rw - 0.6, 1.1 + k * 0.06, ROOM_D - 0.945, 'metal', '#9aa5ad');
    this._aoq(rw - 0.6, ROOM_D - 1.2, 0.7, 0.5);
    grid.rect(rw - 0.95, ROOM_D - 1.45, rw - 0.25, ROOM_D - 0.95, 0.1, 1, 1.6);
    sign(0, 'Phòng server', '#4f8fc0');

    // 1 — thư viện
    const lx = rw;
    B.box(rw - 1.0, 0.012, ROOM_D - 2.6, lx + rw / 2, 0, 3.75, 'carpet', '#a9b39c');
    const books = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1), litMaterial({ roughness: 0.7 }), 2 * 3 * 10);
    books.castShadow = true; books.receiveShadow = true;
    const BC = ['#b5473f', '#4f7fa8', '#6f9a5c', '#d4a446', '#8c7aa6', '#d98a92', '#3f9a90', '#ece6d8', '#3b3f4a'];
    const col = new THREE.Color();
    let bi = 0;
    for (const fx of [0.3, 0.7]) {
      const cx = lx + rw * fx;
      // tủ: 2 hông + nóc + đế + lưng + 3 kệ (đọc ra là tủ, không phải khối)
      B.box(2.4, 2.1, 0.03, cx, 0, 0.27, 'walnut', darker(WALNUT, 0.85));
      for (const sx of [-1.18, 1.18]) B.box(0.04, 2.1, 0.45, cx + sx, 0, 0.475, 'walnut', WALNUT, { r: 0.008 });
      B.box(2.4, 0.05, 0.47, cx, 2.06, 0.475, 'walnut', WALNUT, { r: 0.008 });
      B.box(2.32, 0.1, 0.43, cx, 0, 0.48, 'walnut', darker(WALNUT, 0.8));
      for (const y of [0.32, 0.92, 1.52]) {
        B.box(2.32, 0.035, 0.42, cx, y, 0.49, 'walnut', WALNUT);
        let bxx = cx - 1.08;
        for (let k = 0; k < 10 && bi < books.count; k++) {
          const bw = 0.07 + rnd() * 0.08, bh = 0.32 + rnd() * 0.18, lean = rnd() < 0.08 ? 0.18 : 0;
          m4.makeRotationZ(-lean).premultiply(new THREE.Matrix4().makeTranslation(bxx + bw / 2 + lean * 0.3, y + 0.035 + bh / 2, 0.5));
          m4.multiply(new THREE.Matrix4().makeScale(bw, bh, 0.28));
          books.setMatrixAt(bi, m4); books.setColorAt(bi, col.set(BC[Math.floor(rnd() * BC.length)])); bi++;
          bxx += bw + 0.012 + (rnd() < 0.15 ? 0.14 : 0);
        }
      }
      this._aoq(cx, 0.475, 2.4, 0.45);
      grid.rect(cx - 1.2, 0.25, cx + 1.2, 0.7, 0.1);
      this._spot('library', cx - 0.6, 1.25, Math.PI, 'read');
      this._spot('library', cx + 0.6, 1.25, Math.PI, 'read');
    }
    books.count = bi; books.instanceMatrix.needsUpdate = true; if (books.instanceColor) books.instanceColor.needsUpdate = true;
    g.add(books); this.books = books;
    // bàn đọc tròn + 2 ghế bành
    const tcx = lx + rw / 2, tcz = 3.9;
    B.cyl(0.5, 0.5, 0.04, tcx, 0.56, tcz, 'wood', WOOD, 32);
    B.cyl(0.035, 0.035, 0.54, tcx, 0.02, tcz, 'metal', BLACK, 10); B.cyl(0.26, 0.28, 0.02, tcx, 0, tcz, 'metal', BLACK, 24);
    B.cyl(0.05, 0.045, 0.1, tcx + 0.2, 0.6, tcz - 0.1, 'ceramic', '#f4f1ea', 14);
    B.box(0.22, 0.03, 0.3, tcx - 0.18, 0.6, tcz + 0.05, 'paint', '#b5473f', { rotY: 0.3 });
    this._aoq(tcx, tcz, 0.7, 0.7, true);
    grid.rect(tcx - 0.5, tcz - 0.5, tcx + 0.5, tcz + 0.5, 0.05, 1, 0.75);
    for (const s of [-1, 1]) {
      const ax = tcx + s * 1.05, TEAL = '#5b8a8c', TD = darker(TEAL, 0.82);
      B.box(0.66, 0.24, 0.66, ax, 0.06, tcz, 'fabric', TD, { r: 0.05, seg: 2 });
      B.box(0.5, 0.12, 0.54, ax - s * 0.03, 0.27, tcz, 'fabric', TEAL, { r: 0.06, seg: 2 });
      B.box(0.14, 0.5, 0.66, ax + s * 0.27, 0.22, tcz, 'fabric', TD, { r: 0.06, seg: 2 });
      for (const oz of [-1, 1]) B.box(0.62, 0.22, 0.12, ax, 0.24, tcz + oz * 0.28, 'fabric', TD, { r: 0.05 });
      for (const ox of [-0.26, 0.26]) for (const oz of [-0.26, 0.26]) B.cyl(0.02, 0.015, 0.06, ax + ox, 0, tcz + oz, 'walnut', WALNUT, 8);
      this._aoq(ax, tcz, 0.66, 0.66);
      this._spot('library', ax, tcz, s < 0 ? Math.PI / 2 : -Math.PI / 2, 'sitRead', 0.32);
      grid.rect(ax - 0.31, tcz - 0.31, ax + 0.31, tcz + 0.31, 0.02, 1, 0.95);
    }
    this._plant(lx + rw - 0.6, ROOM_D - 0.7, 1);
    sign(1, 'Thư viện', '#7fb069');

    // 2 — phòng họp
    const mx = rw * 2, mcx = mx + rw / 2, mcz = 3.3;
    B.box(3.8, 0.012, 3.0, mcx, 0, mcz, 'carpet', '#8c8f93');
    B.box(2.7, 0.05, 1.15, mcx, 0.6, mcz, 'wood', WOOD, { r: 0.02, seg: 2 });
    for (const sx of [-1.0, 1.0]) { B.box(0.06, 0.58, 0.8, mcx + sx, 0.02, mcz, 'metal', BLACK); B.box(0.1, 0.02, 0.9, mcx + sx, 0, mcz, 'metal', BLACK); }
    B.box(1.9, 0.08, 0.04, mcx, 0.48, mcz, 'metal', BLACK);
    this._aoq(mcx, mcz, 2.7, 1.15);
    grid.rect(mcx - 1.35, mcz - 0.58, mcx + 1.35, mcz + 0.58, 0.08, 1, 0.8);
    for (const side of [-1, 1]) for (const ox of [-0.85, 0, 0.85]) {
      const cz = mcz + side * 1.0;
      this._chair(mcx + ox, cz, side, '#b9705c');
      this._spot('meeting', mcx + ox, cz, side < 0 ? 0 : Math.PI, 'sit', 0.33);
    }
    // bảng trắng trên tường bắc: khung nhôm + khay bút
    B.box(2.6, 1.15, 0.03, mcx, 0.85, 0.04, 'ceramic', '#fbfbf8');
    B.box(2.66, 0.03, 0.04, mcx, 2.0, 0.045, 'metal', ALU); B.box(2.66, 0.03, 0.04, mcx, 0.82, 0.045, 'metal', ALU);
    for (const sx of [-1.315, 1.315]) B.box(0.03, 1.18, 0.04, mcx + sx, 0.82, 0.045, 'metal', ALU);
    B.box(1.2, 0.025, 0.08, mcx, 0.8, 0.08, 'metal', ALU);
    const wr = seeded(9);
    for (let i = 0; i < 4; i++) B.box(0.6 + wr() * 1.2, 0.03, 0.006, mcx - 0.9 + wr() * 0.4 + (0.6 + wr() * 0.2) / 2, 1.7 - i * 0.22, 0.058, 'paint', ['#4f7fa8', '#b5473f', '#6f9a5c', '#d4a446'][i]);
    sign(2, 'Phòng họp', '#9c89b8');

    // 3 — phòng điều phối viên (vách kính)
    const ox0 = rw * 3, ocx = ox0 + rw / 2;
    B.box(3.2, 0.012, 2.4, ocx, 0.004, 2.6, 'carpet', '#b5654a');
    B.box(1.9, 0.05, 0.85, ocx, 0.64, 2.2, 'walnut', WALNUT, { r: 0.015, seg: 2 });
    B.box(1.5, 0.006, 0.55, ocx, 0.69, 2.25, 'fabric', '#3a3a40');
    B.box(1.8, 0.56, 0.04, ocx, 0.08, 2.55, 'walnut', darker(WALNUT, 0.9));
    for (const sx of [-0.88, 0.88]) B.box(0.06, 0.64, 0.8, ocx + sx, 0, 2.2, 'walnut', darker(WALNUT, 0.9));
    B.box(0.4, 0.5, 0.6, ocx + 0.55, 0.06, 2.2, 'walnut', darker(WALNUT, 0.95));
    this._aoq(ocx, 2.2, 1.9, 0.85);
    grid.rect(ocx - 0.95, 1.78, ocx + 0.95, 2.62, 0.08, 1, 1.25);
    // ghế giám đốc (lưng cao) + màn hình hướng về ghế
    this._chair(ocx, 1.3, -1, '#3a3a40', { high: true });
    const mgrScreen = this._monitor(ocx, 2.4, -1, 0.69, 0.72);
    this.mgrSpot = { zone: 'mgr', x: ocx, z: 1.3, rot: 0, pose: 'sit', seatY: 0.37, owner: this.mgrId, screen: mgrScreen, stand: { x: ocx - 0.75, z: 1.4 } };
    if (this.mgrId) this.seats.set(this.mgrId, this.mgrSpot);
    for (const s of [-1, 1]) {
      this._chair(ocx + s * 0.55, 3.35, 1, '#8a6a9a');
      this._spot('mgrVisit', ocx + s * 0.55, 3.35, Math.PI, 'sit', 0.33);
    }
    this._spot('mgrVisit', ocx - 1.3, 3.9, Math.PI * 0.85, 'stand');
    this._spot('mgrVisit', ocx + 1.3, 3.9, -Math.PI * 0.85, 'stand');
    // tủ hồ sơ + tranh + cây
    B.box(0.45, 1.3, 1.2, ox0 + 0.4, 0, 2.4, 'walnut', WALNUT, { r: 0.02 });
    for (const oz of [-0.3, 0.3]) for (const y of [0.35, 0.8, 1.15]) B.box(0.02, 0.02, 0.18, ox0 + 0.635, y, 2.4 + oz, 'chrome', '#d0d4d8');
    this._aoq(ox0 + 0.4, 2.4, 0.45, 1.2);
    grid.rect(ox0 + 0.15, 1.8, ox0 + 0.65, 3.0, 0.08, 1, 1.35);
    // màn hình chat với điều phối viên (giai đoạn 2): treo ở vách đông, quay vào trong phòng
    // đặt lệch về phía cửa (z 2.1…5.7) để điều phối viên (ngồi z≈1.3) đứng BÊN CẠNH màn hình, không che chữ
    B.box(0.1, 2.3, 3.9, this.W - 0.08, 0.4, 3.9, 'metal', '#2a2e38', { r: 0.02 });
    this.chatAnchor = { x: this.W - 0.15, y: 1.55, z: 3.9, rotY: -Math.PI / 2, w: 3.6, h: 2.06 };
    // tranh trừu tượng: khung óc chó + canvas mảng màu mềm
    B.box(1.06, 0.72, 0.04, ocx, 1.17, 0.03, 'walnut', WALNUT, { r: 0.01 });
    const art = textCanvas(256, 168, (ctx, w, h) => {
      ctx.fillStyle = '#f2ece2'; ctx.fillRect(0, 0, w, h);
      for (const [c, x, y, ww, hh] of [['#d98c6a', 24, 22, 110, 80], ['#5b8a8c', 110, 60, 120, 84], ['#e9c46a', 60, 110, 70, 40]]) {
        ctx.fillStyle = c; ctx.globalAlpha = 0.85; roundRect(ctx, x, y, ww, hh, 18); ctx.fill();
      }
      ctx.globalAlpha = 1;
    });
    this.textures.push(art);
    const pm = new THREE.Mesh(new THREE.PlaneGeometry(0.96, 0.63), new THREE.MeshBasicMaterial({ map: art }));
    pm.position.set(ocx, 1.53, 0.052); g.add(pm); this.ownMats.push(pm.material); this.ownGeos.push(pm.geometry);
    this._plant(ox0 + 0.5, 0.6, 1.2);
    this._plant(ox0 + 0.5, ROOM_D - 0.6, 1);
    sign(3, 'Điều phối viên', '#c8413a');
  }

  // ------------------------------------------------------------ vách phòng (thấp + kính)
  _walls() {
    const rw = this.rw, grid = this.grid, B = this.batch;
    const half = (x0, z0, x1, z1, withGlass) => {
      const w = Math.max(0.12, x1 - x0), d = Math.max(0.12, z1 - z0), cx = (x0 + x1) / 2, cz = (z0 + z1) / 2;
      this._occ([{ geo: roundedBoxGeo(w, 1.0, d, 0.01, 1), matrix: new THREE.Matrix4().makeTranslation(cx, 0.5, cz), color: LOWWALL }], 'plaster');
      B.box(w + 0.004, 0.02, d + 0.004, cx, 1.0, cz, 'paint', CUT);
      // chân tường hai mặt
      if (w >= d) for (const s of [-1, 1]) B.box(w, 0.1, 0.012, cx, 0, cz + s * (d / 2 + 0.006), 'paint', BASE);
      else for (const s of [-1, 1]) B.box(0.012, 0.1, d, cx + s * (w / 2 + 0.006), 0, cz, 'paint', BASE);
      if (withGlass) {
        this._occ([{ geo: new THREE.BoxGeometry(w * 0.999, 1.2, d * 0.999), matrix: new THREE.Matrix4().makeTranslation(cx, 1.62, cz) }], 'glass');
        const L = Math.max(w, d), n = Math.max(1, Math.round(L / 1.2));
        for (let k = 0; k <= n; k++) {
          const f = -L / 2 + (L * k) / n;
          if (w >= d) B.box(0.04, 1.2, 0.05, cx + f, 1.02, cz, 'metal', ALU); else B.box(0.05, 1.2, 0.04, cx, 1.02, cz + f, 'metal', ALU);
        }
        B.box(w + 0.02, 0.04, d + 0.02, cx, 2.22, cz, 'metal', ALU);
      }
      grid.rect(x0, z0, x1, z1, 0.15);
    };
    // vách giữa các phòng (kính cho phòng họp + điều phối viên)
    for (let i = 1; i < 4; i++) half(rw * i - 0.06, 0, rw * i + 0.06, ROOM_D, i >= 2);
    // phòng phía bắc: p = chỗ trống bên trong (kiểm đi tới được), door = giữa cửa
    for (const [i, key, pz] of [[0, 'server', 3.0], [1, 'library', 2.6], [2, 'meeting', 5.0], [3, 'mgr', 4.6]]) {
      this.rooms.push({ key, name: ZONE_NAMES[key], x0: rw * i, z0: 0, x1: rw * (i + 1), z1: ROOM_D, p: { x: rw * i + rw / 2, z: pz }, door: { x: rw * i + rw / 2, z: ROOM_D } });
    }
    half(this.W - 0.12, 0, this.W, ROOM_D, true);
    // vách trước có cửa ở giữa
    for (let i = 0; i < 4; i++) {
      const x0 = rw * i, cx = x0 + rw / 2, door = 0.85;
      half(x0, ROOM_D - 0.06, cx - door, ROOM_D + 0.06, i >= 2);
      half(cx + door, ROOM_D - 0.06, x0 + rw, ROOM_D + 0.06, i >= 2);
      B.box(door * 2, 0.008, 0.3, cx, 0, ROOM_D, 'wood', '#b89a74');
    }
  }

  // ------------------------------------------------------------ cụm bàn theo team
  _clusters() {
    const g = this.group, B = this.batch;
    for (const c of this.clusters) {
      // thảm 2 lớp: viền màu team dịu + lòng rất nhạt; tên team in trên thảm (mép bắc) + cờ bàn ở đầu cụm
      B.box(c.w + 0.1, 0.008, c.d + 0.1, c.cx, 0.0, c.cz, 'carpet', mix(muted(c.color), '#f3eee6', 0.3));
      B.box(c.w - 0.2, 0.012, c.d - 0.2, c.cx, 0.0, c.cz, 'carpet', rugCol(c.color));
      const tw = Math.min(c.w - 0.6, 3.6);
      const rugTex = textCanvas(512, 96, (ctx, w, h) => {
        ctx.clearRect(0, 0, w, h);
        ctx.fillStyle = darker(muted(c.color), 0.7); ctx.globalAlpha = 0.72;
        ctx.font = '800 58px ' + FONT; ctx.textBaseline = 'middle'; ctx.textAlign = 'center';
        ctx.fillText(fitText(ctx, String(c.name).toUpperCase(), w - 24), w / 2, h / 2 + 3);
        ctx.globalAlpha = 1; ctx.textAlign = 'left';
      });
      const rt = this._plate(rugTex, tw, (tw * 96) / 512, c.cx, 0.014, c.z + 0.12 + (tw * 96) / 1024, 0, { transparent: true, rx: -Math.PI / 2, order: 1 });
      rt.material.polygonOffset = true; rt.material.polygonOffsetFactor = -2;
      const fx = c.x + 0.25, fz = c.cz;
      B.cyl(0.015, 0.015, 1.2, fx, 0.02, fz, 'metal', BLACK, 8); B.cyl(0.12, 0.13, 0.02, fx, 0, fz, 'metal', BLACK, 18);
      const flag = this._signTex(c.name, c.color, 512, 128);
      for (const s of [-1, 1]) this._plate(flag, 0.84, 0.21, fx + s * 0.012, 1.08, fz, s * Math.PI / 2);
      B.box(0.016, 0.23, 0.86, fx, 0.965, fz, 'paint', CUT);
      const deskX0 = c.cx - (c.k * 1.4) / 2 + 0.7;
      const rnd = seeded(hashStr(c.id || 'none'));
      let mi = 0;
      for (let j = 0; j < c.k; j++) {
        const x = deskX0 + j * 1.4;
        for (const side of [-1, 1]) {
          const dz = c.cz + side * 0.4;
          const id = c.members[mi++];
          const screen = this._desk(x, dz, side, rnd, muted(c.color));
          this._chair(x, c.cz + side * 1.35, side, muted(c.color));
          if (id) this.seats.set(id, { zone: 'desk', x, z: c.cz + side * 1.35, rot: side < 0 ? 0 : Math.PI, pose: 'sit', seatY: 0.33, owner: id, screen, team: c.id, stand: { x, z: c.cz + side * 1.78 } });
        }
      }
      this.grid.rect(deskX0 - 0.68, c.cz - 0.8, deskX0 + (c.k - 1) * 1.4 + 0.68, c.cz + 0.8, 0.08, 1, 1.25); // bàn + màn hình
      // hiệu ứng ESTOP team: lớp đỏ trên sàn + đèn đỏ
      const ov = new THREE.Mesh(new THREE.BoxGeometry(c.w, 0.02, c.d), new THREE.MeshBasicMaterial({ color: '#ff3b30', transparent: true, opacity: 0.3, depthWrite: false }));
      ov.position.set(c.cx, 0.03, c.cz); g.add(ov);
      const lamp = new THREE.Mesh(new THREE.ConeGeometry(Math.max(c.w, c.d) * 0.55, 3, 24, 1, true), new THREE.MeshBasicMaterial({ color: '#ff4d40', transparent: true, opacity: 0.12, depthWrite: false, side: THREE.DoubleSide, forceSinglePass: true }));
      lamp.position.set(c.cx, 1.5, c.cz);
      ov.visible = lamp.visible = false; g.add(lamp);
      this.ownGeos.push(ov.geometry, lamp.geometry); this.ownMats.push(ov.material, lamp.material);
      this.teamFx.set(c.id, { ov, lamp, on: false });
    }
  }

  _desk(x, z, side, rnd, accent) {
    const B = this.batch;
    B.box(1.3, 0.035, 0.78, x, 0.615, z, 'wood', WOOD, { r: 0.012, seg: 2 });
    // khung chữ T hai bên + máng cáp
    for (const sx of [-0.58, 0.58]) { B.box(0.04, 0.6, 0.06, x + sx, 0.015, z, 'metal', BLACK); B.box(0.06, 0.03, 0.7, x + sx, 0, z, 'metal', BLACK); B.box(0.04, 0.03, 0.66, x + sx, 0.585, z, 'metal', BLACK); }
    B.box(1.0, 0.05, 0.12, x, 0.5, z - side * 0.25, 'metal', BLACK);
    // tấm chắn vải ở mép xa (giữa hai bàn quay lưng)
    B.box(1.24, 0.3, 0.02, x, 0.65, z - side * 0.375, 'fabric', accent, { r: 0.008 });
    B.box(0.42, 0.018, 0.13, x, 0.65, z + side * 0.18, 'plastic', '#e9e9e9', { r: 0.006 });
    B.box(0.06, 0.02, 0.1, x + 0.33, 0.65, z + side * 0.18, 'plastic', '#e9e9e9', { r: 0.009 });
    const screen = this._monitor(x, z - side * 0.16, side, 0.65, 0.6);
    const r = rnd();
    if (r < 0.35) this._plant(x - 0.45, z - side * 0.2, 0.35, 0.65);
    else if (r < 0.7) B.cyl(0.04, 0.036, 0.1, x - 0.45, 0.65, z + side * 0.1, 'ceramic', ['#d98a92', '#4f7fa8', '#d4a446', '#f4f1ea'][Math.floor(rnd() * 4)], 14);
    this._aoq(x, z, 1.3, 0.78);
    return screen;
  }

  // màn hình: mặt màn hình (MeshBasic riêng, toneMapped:false, map "code") hướng về phía ghế (side<0 → ghế ở phía bắc)
  _monitor(x, z, side, y0, w) {
    const B = this.batch, ww = w || 0.6;
    B.cyl(0.09, 0.1, 0.012, x, y0, z - side * 0.02, 'metal', '#3a3c42', 20);
    B.box(0.03, 0.18, 0.03, x, y0, z - side * 0.03, 'metal', '#3a3c42');
    B.box(ww, 0.38, 0.03, x, y0 + 0.14, z, 'metal', '#18191c', { r: 0.01 });
    const scr = new THREE.MeshBasicMaterial({ color: SCREEN_OFF, map: tex('screenCode'), toneMapped: false });
    this.ownMats.push(scr);
    const geo = this._scrGeo || (this._scrGeo = {});
    const pg = geo[ww] || (geo[ww] = new THREE.PlaneGeometry(ww - 0.04, 0.34));
    if (!this.ownGeos.includes(pg)) this.ownGeos.push(pg);
    const m = new THREE.Mesh(pg, scr);
    m.position.set(x, y0 + 0.33, z + side * 0.0165); m.rotation.y = side < 0 ? Math.PI : 0;
    this.group.add(m);
    return scr;
  }

  // ghế văn phòng: đệm bo, lưng cong nhẹ, trụ chrome, chân sao 5 cánh + bánh xe; opts.high = lưng cao (giám đốc)
  _chair(x, z, side, color, opts) {
    const B = this.batch, o = opts || {}, bh = o.high ? 0.68 : 0.42;
    B.box(0.46, 0.07, 0.46, x, 0.26, z, 'fabric', color, { r: 0.03, seg: 2 });
    B.box(0.44, bh, 0.06, x, 0.36, z + side * 0.22, 'fabric', o.back || (o.high ? color : BACKF), { r: 0.03, seg: 2, rx: side * 0.12 });
    B.box(0.04, 0.12, 0.03, x, 0.25, z + side * 0.2, 'metal', BLACK);
    B.cyl(0.025, 0.025, 0.2, x, 0.06, z, 'chrome', '#c9ccd0', 10);
    for (let i = 0; i < 5; i++) {
      const a = (i / 5) * Math.PI * 2 + 0.3;
      B.box(0.26, 0.025, 0.04, x + Math.cos(a) * 0.13, 0.04, z - Math.sin(a) * 0.13, 'chrome', '#9da2a8', { rotY: a });
      B.sphere(0.025, x + Math.cos(a) * 0.25, 0.025, z - Math.sin(a) * 0.25, 'plastic', '#222222', 8);
    }
    if (o.arms) for (const sx of [-0.25, 0.25]) B.box(0.05, 0.04, 0.32, x + sx, 0.44, z, 'plastic', '#2a2b30', { r: 0.015 });
    if (o.stripe) B.box(0.12, 0.5, 0.064, x, 0.4, z + side * 0.22, 'fabric', o.stripe, { r: 0.02, rx: side * 0.12 });
    this._aoq(x, z, 0.5, 0.5, true);
  }

  // cây: chậu côn + đất, tán = 7–12 lá elip dẹt quanh thân (3 sắc lá); cây cao có thân óc chó
  _plant(x, z, s, y0) {
    const B = this.batch, k = s || 1, y = y0 || 0, r = seeded(hashStr(x.toFixed(2) + ':' + z.toFixed(2)));
    const pot = r() < 0.5 ? '#b9714f' : '#eeeae4';
    B.cyl(0.16 * k, 0.12 * k, 0.28 * k, x, y, z, 'ceramic', pot, 14);
    B.cyl(0.145 * k, 0.145 * k, 0.01, x, y + 0.26 * k, z, 'paint', '#4a3a2c', 14);
    if (k >= 0.8) B.cyl(0.012 * k, 0.018 * k, 0.5 * k, x, y + 0.26 * k, z, 'walnut', WALNUT, 6);
    const n = k < 0.5 ? 7 : 9 + Math.floor(r() * 4);
    for (let i = 0; i < n; i++) {
      const a = (i / n) * Math.PI * 2 + r() * 0.6, hgt = y + (0.38 + r() * 0.42) * k, rad = (0.06 + r() * 0.12) * k, lr = (0.13 + r() * 0.05) * k;
      B.sphere(lr, x + Math.cos(a) * rad, hgt, z - Math.sin(a) * rad, 'plant', LEAVES[Math.floor(r() * 3)], 6,
        { rings: 4, scale: [1, 0.35, 0.6], rotY: a, rz: 0.25 + r() * 0.5 });
    }
    B.sphere(0.12 * k, x, y + 0.72 * k, z, 'plant', LEAVES[1], 6, { rings: 4, scale: [1, 0.5, 1] });
    this._aoq(x, z, 0.32 * k, 0.32 * k, true);
    if (!y0) this.grid.rect(x - 0.2 * k, z - 0.2 * k, x + 0.2 * k, z + 0.2 * k, 0.05, 1, 1.2 * k);
  }

  // ------------------------------------------------------------ khu cafe điện tử (agent rảnh ra ngồi chơi game)
  // Gaming lounge tông ấm hợp văn phòng Bắc Âu (không còn khối tím + viền neon 3 màu): sàn thảm tấm graphite có viền gỗ óc chó,
  // vách lửng (mặt ngoài sáng như vách văn phòng, mặt trong ốp óc chó) với dải LED trắng ấm mảnh trên đỉnh + MỘT dải accent dịu
  // sát chân vách (trôi màu chậm trong dải lạnh, tĩnh khi reduced/ESTOP). Cửa hai đầu ở vách bắc + nam, lối đi 2 m ngoài vách tây/đông.
  // Bên trong: lối vào + 2 máy arcade phía tây, các dãy bàn game (2 hàng quay lưng, vách ngăn giữa) ở giữa, quầy nước phía đông.
  _cafe() {
    const grid = this.grid, c = this.cafe, B = this.batch;
    const { x0, x1, z0, z1 } = c, cx = (x0 + x1) / 2, cz = (z0 + z1) / 2;
    // rgbMats[0] = dải accent chân vách; [1] = đèn nền bàn phím / màn arcade; [2] = khe kính thùng máy (dịu, cùng họ màu)
    const rgb = ['#5fc7d6', '#9fd8e6', '#7a8fe0'].map((col) => new THREE.MeshBasicMaterial({ color: col, toneMapped: false }));
    this.rgbMats = rgb; this.cafeMats.push(...rgb);
    this.warmLed = new THREE.MeshBasicMaterial({ color: '#ffe6c2', toneMapped: false }); this.ownMats.push(this.warmLed);
    // sàn thảm tấm màu yến mạch ấm (sáng hơn sàn sồi một chút, ít bão hoà) + thảm chạy dưới dãy bàn tông nâu xám vừa — không còn mảng tối lớn
    const FLOOR = '#d3c6b3', RUN = '#b3a28c', INNER = '#9a7457';
    B.box(x1 - x0, 0.012, z1 - z0, cx, 0, cz, 'carpet', FLOOR);
    // viền gỗ óc chó quanh sàn thảm
    for (const [w, d, x, z] of [[x1 - x0, 0.14, cx, z0 + 0.13], [x1 - x0, 0.14, cx, z1 - 0.13], [0.14, z1 - z0, x0 + 0.13, cz], [0.14, z1 - z0, x1 - 0.13, cz]]) B.box(w, 0.016, d, x, 0, z, 'walnut', WALNUT);
    // vách lửng: lõi sáng + lớp ốp óc chó mặt trong (một occluder), mặt cắt than, LED trắng ấm mảnh ở mép trong đỉnh vách,
    // dải accent dịu sát chân mặt trong + quầng hắt xuống sàn
    const wall = (ax, az, bx, bz, nx, nz) => {
      const w = Math.max(0.12, bx - ax), d = Math.max(0.12, bz - az), mx = (ax + bx) / 2, mz = (az + bz) / 2;
      const T = (x, y, zz) => new THREE.Matrix4().makeTranslation(x, y, zz);
      this._occ([
        { geo: roundedBoxGeo(w, 1.0, d, 0.01, 1), matrix: T(mx, 0.5, mz), color: LOWWALL },
        { geo: new THREE.BoxGeometry(nx ? 0.012 : w, 0.88, nz ? 0.012 : d), matrix: T(mx + nx * (w / 2 + 0.006), 0.54, mz + nz * (d / 2 + 0.006)), color: INNER },
      ], 'plaster');
      B.box(w + 0.004, 0.02, d + 0.004, mx, 1.0, mz, 'paint', CUT);
      const L = Math.max(w, d);
      B.box(nx ? 0.012 : L, 0.012, nz ? 0.012 : L, mx + nx * (w / 2 + 0.008), 0.975, mz + nz * (d / 2 + 0.008), 'warm', '#ffffff');
      B.box(nx ? 0.012 : L, 0.014, nz ? 0.012 : L, mx + nx * (w / 2 + 0.014), 0.09, mz + nz * (d / 2 + 0.014), 'rgb0', '#ffffff');
      // chân tường mặt ngoài
      B.box(nx ? 0.012 : L, 0.1, nz ? 0.012 : L, mx - nx * (w / 2 + 0.006), 0, mz - nz * (d / 2 + 0.006), 'paint', BASE);
      // quầng accent hắt xuống sàn (dải nằm rộng 0,5 m sát chân vách)
      const g0 = 0.02, g1 = 0.55;
      this._glow[0].push(nx
        ? [[mx + nx * g0, 0.024, mz - L / 2], [mx + nx * g0, 0.024, mz + L / 2], [mx + nx * g1, 0.024, mz + L / 2], [mx + nx * g1, 0.024, mz - L / 2]]
        : [[mx - L / 2, 0.024, mz + nz * g0], [mx + L / 2, 0.024, mz + nz * g0], [mx + L / 2, 0.024, mz + nz * g1], [mx - L / 2, 0.024, mz + nz * g1]]);
      grid.rect(ax, az, bx, bz, 0.15);
    };
    const doors = [[x0 + 0.2, x0 + 1.6], [x1 - 3.2, x1 - 1.8]];
    for (const [wz, nz] of [[z0, 1], [z1, -1]]) {
      wall(x0, wz - 0.06, doors[0][0], wz + 0.06, 0, nz);
      wall(doors[0][1], wz - 0.06, doors[1][0], wz + 0.06, 0, nz);
      wall(doors[1][1], wz - 0.06, x1, wz + 0.06, 0, nz);
      for (const [a, b] of doors) B.box(b - a, 0.012, 0.6, (a + b) / 2, 0.006, wz, 'wood', '#b89a74');
    }
    wall(x0 - 0.06, z0, x0 + 0.06, z1, 1, 0);
    wall(x1 - 0.06, z0, x1 + 0.06, z1, -1, 0);

    // dãy máy: tâm dãy cách vách CAFE_EDGE, máy chia đều trong phần giữa lối vào tây và quầy nước đông
    const sx0 = x0 + CAFE_AISLE, sx1 = x1 - CAFE_BAR, span = c.k * CAFE_STEP;
    const first = (sx0 + sx1) / 2 - span / 2 + CAFE_STEP / 2;
    const rnd = seeded(77);
    for (let i = 0; i < c.rows; i++) {
      const bz = z0 + CAFE_EDGE + i * CAFE_ROW;
      B.box(span + 0.4, 0.014, 3.3, (sx0 + sx1) / 2, 0, bz, 'carpet', RUN);
      // vách ngăn vải giữa hai hàng bàn quay lưng + LED trắng ấm mảnh trên đỉnh
      B.box(span, 0.34, 0.03, (sx0 + sx1) / 2, 0.62, bz, 'fabric', '#8d9a88', { r: 0.01 });
      B.box(span, 0.012, 0.012, (sx0 + sx1) / 2, 0.955, bz, 'warm', '#ffffff');
      for (let j = 0; j < c.k; j++) {
        const x = first + j * CAFE_STEP;
        for (const side of [-1, 1]) {
          const screen = this._gameDesk(x, bz + side * 0.4, side, j, rnd);
          const sz = bz + side * 1.25;
          const tint = muted(TEAM_COLORS[(j * 2 + i + (side > 0 ? 1 : 0)) % TEAM_COLORS.length]);
          this._chair(x, sz, side, tint, { high: true, arms: true, back: mix(tint, '#2b2c31', 0.55), stripe: tint });
          this._spot('cafe', x, sz, side < 0 ? 0 : Math.PI, 'typeFast', 0.34, { screen, ph: i * 3.1 + j * 0.9 + (side > 0 ? 1.7 : 0), stand: { x, z: bz + side * 1.7 } });
        }
      }
      grid.rect(first - 0.58, bz - 0.8, first + (c.k - 1) * CAFE_STEP + 0.58, bz + 0.8, 0.08, 1, 1.25);
      // cây cao ở hai đầu dãy, trên trục vách ngăn (chỉ khi còn ≥ 0,9 m trống — không chắn lối vòng quanh dãy)
      if ((sx1 - sx0 - span) / 2 >= 0.9) for (const px of [first - 0.95, first + (c.k - 1) * CAFE_STEP + 0.95]) this._plant(px, bz, 0.9);
    }

    // 2 máy arcade sát vách tây, quay vào trong (thân than, hông óc chó)
    for (const oz of [-0.45, 0.45]) {
      const az = cz + oz;
      B.box(0.6, 1.6, 0.6, x0 + 0.42, 0, az, 'paint', '#2e2c30', { r: 0.02 });
      for (const s of [-1, 1]) B.box(0.62, 1.58, 0.02, x0 + 0.42, 0.01, az + s * 0.3, 'walnut', WALNUT);
      B.box(0.02, 0.38, 0.44, x0 + 0.73, 0.98, az, 'rgb1', '#ffffff');
      B.box(0.02, 0.12, 0.5, x0 + 0.73, 1.42, az, 'warm', '#ffffff');
      B.box(0.22, 0.07, 0.5, x0 + 0.8, 0.82, az, 'plastic', '#2a2a2e', { r: 0.01 });
      B.sphere(0.03, x0 + 0.84, 0.91, az - 0.1, 'emit', '#e8705a', 8);
      B.sphere(0.03, x0 + 0.84, 0.91, az + 0.1, 'emit', '#e9c46a', 8);
      this._aoq(x0 + 0.42, az, 0.6, 0.6);
    }
    grid.rect(x0, cz - 0.75, x0 + 0.92, cz + 0.75, 0.05, 1, 1.1);

    // quầy nước nhỏ sát vách đông: quầy óc chó mặt terrazzo + tủ lạnh kính + ly; 2 chỗ đứng uống (zone cafeBar)
    const len = Math.min(2.4, z1 - z0 - 1.6), qx = x1 - 0.55;
    B.box(0.6, 1.0, len, qx, 0, cz, 'walnut', WALNUT, { r: 0.02 });
    B.box(0.66, 0.04, len + 0.06, qx, 1.0, cz, 'terrazzo', '#ffffff', { r: 0.01, seg: 2 });
    B.box(0.012, 0.012, len, qx - 0.32, 0.97, cz, 'warm', '#ffffff');
    for (let i = 0; i < 4; i++) B.cyl(0.04, 0.035, 0.12, qx, 1.04, cz - len / 2 + 0.35 + i * 0.5, 'ceramic', ['#f4f1ea', '#d98a92', '#7fc6d0', '#d4a446'][i], 14);
    this._aoq(qx, cz, 0.6, len);
    const fz = cz - len / 2 - 0.45;
    B.box(0.6, 1.5, 0.6, x1 - 0.45, 0, fz, 'paint', '#2e2c30', { r: 0.03 });
    B.box(0.02, 1.1, 0.46, x1 - 0.76, 0.3, fz, 'emit', '#cfe9ee');
    this._aoq(x1 - 0.45, fz, 0.6, 0.6);
    this._pools.push([qx - 0.6, cz, 1.6, len + 0.8]);
    grid.rect(x1 - 0.85, fz - 0.3, x1, cz + len / 2, 0.1);
    for (const oz of [-0.55, 0.55]) this._spot('cafeBar', x1 - 1.45, cz + oz, Math.PI / 2, 'drink');
    this._plant(x1 - 0.5, z1 - 0.55, 1);

    this.rooms.push({ key: 'cafe', name: ZONE_NAMES.cafe, x0, z0, x1, z1, p: { x: x0 + 1.2, z: z0 + 1.0 }, door: { x: (doors[0][0] + doors[0][1]) / 2, z: z0 } });
    // biển: tấm than chữ trắng ấm (backlit) dựng trên đỉnh vách bắc, quay ra khu làm việc / camera
    const signTex = textCanvas(1024, 192, (ctx, w, h) => {
      ctx.fillStyle = '#2b2826'; roundRect(ctx, 0, 0, w, h, 26); ctx.fill();
      ctx.font = '700 96px ' + FONT; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillStyle = '#ffe6c2'; ctx.fillText('Cafe điện tử', w / 2 + 24, h / 2 + 4);
      ctx.fillStyle = '#5fc7d6'; ctx.beginPath(); ctx.arc(w / 2 - ctx.measureText('Cafe điện tử').width / 2 - 20, h / 2 + 2, 14, 0, Math.PI * 2); ctx.fill();
      ctx.textAlign = 'left';
    });
    const nx = (doors[0][1] + doors[1][0]) / 2;
    for (const sx of [-1.2, 1.2]) B.box(0.035, 0.12, 0.035, nx + sx, 1.0, z0 - 0.04, 'metal', BLACK);
    this._plate(signTex, 3.2, 0.6, nx, 1.42, z0 - 0.03, 0);
  }

  // bàn game: mặt óc chó sẫm, khung đen, màn hình rộng (mặt màn hình riêng — sáng dịu khi có người chơi, tối khi trống),
  // bàn phím đèn nền dịu, thùng máy dưới gầm có khe kính hông
  _gameDesk(x, z, side, j, rnd) {
    const B = this.batch;
    B.box(1.1, 0.04, 0.7, x, 0.6, z, 'walnut', '#7a5a43', { r: 0.012, seg: 2 });
    for (const sx of [-0.5, 0.5]) { B.box(0.05, 0.6, 0.06, x + sx, 0, z, 'metal', BLACK); B.box(0.06, 0.03, 0.62, x + sx, 0, z, 'metal', BLACK); }
    B.box(0.8, 0.003, 0.32, x, 0.64, z + side * 0.14, 'fabric', '#3a3a3e');
    B.box(0.44, 0.02, 0.14, x, 0.643, z + side * 0.18, 'plastic', '#1e1f23', { r: 0.006 });
    // đèn nền bàn phím: chỉ một vạch mảnh ở mép sau (trước đây cả mặt phím phát sáng → ô xanh chói nhìn từ trên)
    B.box(0.4, 0.003, 0.015, x, 0.663, z + side * 0.12, 'rgb1', '#ffffff');
    B.box(0.07, 0.025, 0.11, x + 0.3, 0.643, z + side * 0.18, 'plastic', '#1e1f23', { r: 0.01 });
    const mz = z - side * 0.16;
    B.cyl(0.1, 0.11, 0.012, x, 0.64, mz - side * 0.02, 'metal', '#2a2b30', 20);
    B.box(0.03, 0.18, 0.03, x, 0.64, mz - side * 0.03, 'metal', '#2a2b30');
    B.box(0.72, 0.4, 0.03, x, 0.79, mz, 'metal', '#16171a', { r: 0.01 });
    const scr = new THREE.MeshBasicMaterial({ color: CAFE_SCREEN_OFF, map: tex('screenGame'), toneMapped: false });
    this.cafeMats.push(scr);
    const pg = this._gameScr || (this._gameScr = new THREE.PlaneGeometry(0.68, 0.36));
    if (!this.ownGeos.includes(pg)) this.ownGeos.push(pg);
    const m = new THREE.Mesh(pg, scr);
    m.position.set(x, 0.99, mz + side * 0.0165); m.rotation.y = side < 0 ? Math.PI : 0; this.group.add(m);
    B.box(0.2, 0.45, 0.42, x + 0.36, 0.0, z, 'paint', '#e4e0d9', { r: 0.012 });
    B.box(0.004, 0.3, 0.3, x + 0.258, 0.07, z, 'rgb2', '#ffffff');
    if (rnd() < 0.4) B.cyl(0.033, 0.033, 0.11, x - 0.42, 0.643, z + side * 0.1, 'ceramic', ['#f4f1ea', '#d98a92', '#7fc6d0', '#d4a446'][Math.floor(rnd() * 4)], 12);
    this._aoq(x, z, 1.1, 0.7);
    return scr;
  }

  // ------------------------------------------------------------ dải phía nam + tường tây (kanban, đồng hồ)
  _south() {
    const g = this.group, W = this.W, zs = this.zs, grid = this.grid, B = this.batch;
    // biển đứng (giá đỡ 2 cột) quay ra camera, đỉnh ~1,8 m
    const sign = (x, text, color) => {
      const z = zs + 0.35;
      for (const sx of [-0.62, 0.62]) B.box(0.035, 1.6, 0.035, x + sx, 0, z, 'metal', BLACK);
      for (const sx of [-0.62, 0.62]) B.box(0.16, 0.02, 0.16, x + sx, 0, z, 'metal', BLACK);
      B.box(1.44, 0.38, 0.03, x, 1.43, z - 0.02, 'paint', CUT, { r: 0.01 });
      this._plate(this._signTex(text, color), 1.4, 0.35, x, 1.62, z + 0.0, 0);
    };
    // pantry (sàn terrazzo)
    B.box(7.6, 0.012, 5.8, 3.8, 0, zs + 3, 'terrazzo', '#ffffff');
    B.box(0.75, 0.86, 3.8, 0.45, 0, zs + 2.8, 'walnut', WALNUT, { r: 0.02 });
    for (let k = 0; k < 4; k++) B.box(0.01, 0.8, 0.9, 0.83, 0.03, zs + 1.38 + k * 0.95, 'walnut', darker(WALNUT, 0.85));
    B.box(0.82, 0.04, 3.85, 0.45, 0.86, zs + 2.8, 'ceramic', '#f1ede6', { r: 0.01, seg: 2 });
    this._aoq(0.45, zs + 2.8, 0.75, 3.8);
    grid.rect(0, zs + 0.9, 0.85, zs + 4.7, 0.1, 1, 2.0); // quầy + tủ treo
    B.box(0.42, 0.48, 0.38, 0.42, 0.9, zs + 1.6, 'paint', '#3a3a40', { r: 0.03 });
    B.box(0.3, 0.06, 0.2, 0.5, 1.2, zs + 1.6, 'chrome', '#c9ccd0');
    B.sphere(0.03, 0.64, 1.33, zs + 1.5, 'emit', '#ff5a4a', 8);
    B.cyl(0.045, 0.04, 0.09, 0.55, 0.9, zs + 1.6, 'ceramic', '#f4f1ea', 14);
    B.cyl(0.12, 0.12, 0.16, 0.45, 0.9, zs + 3.2, 'paint', '#c8413a', 20); // hộp bánh
    for (let i = 0; i < 3; i++) B.cyl(0.045, 0.04, 0.1, 0.4, 0.9, zs + 3.7 + i * 0.15, 'ceramic', ['#f4f1ea', '#d98a92', '#4f7fa8'][i], 14);
    B.box(0.85, 1.9, 0.8, 0.45, 0, zs + 5.15, 'paint', '#e9eef2', { r: 0.03 });
    B.box(0.02, 0.012, 0.78, 0.875, 1.25, zs + 5.15, 'metal', '#c9ccd0');
    B.box(0.03, 0.5, 0.04, 0.9, 1.0, zs + 4.9, 'chrome', '#c9ccd0');
    this._aoq(0.45, zs + 5.15, 0.85, 0.8);
    grid.rect(0, zs + 4.75, 0.9, zs + 5.55, 0.1);
    for (const dz of [1.6, 2.6, 3.6]) this._spot('pantry', 1.45, zs + dz, -Math.PI / 2, 'drink');
    // bàn cao + 2 ghế quầy
    const hx = 4.0, hz = zs + 3.0;
    B.cyl(0.45, 0.45, 0.04, hx, 0.95, hz, 'wood', WOOD, 32); B.cyl(0.035, 0.035, 0.95, hx, 0, hz, 'metal', BLACK, 10); B.cyl(0.24, 0.26, 0.02, hx, 0, hz, 'metal', BLACK, 24);
    this._aoq(hx, hz, 0.6, 0.6, true);
    grid.rect(hx - 0.45, hz - 0.45, hx + 0.45, hz + 0.45, 0.05, 1, 1.0);
    for (const s of [-1, 1]) {
      const sx = hx + s * 0.75;
      B.cyl(0.19, 0.18, 0.06, sx, 0.56, hz, 'fabric', '#d4a446', 20); B.cyl(0.02, 0.02, 0.56, sx, 0, hz, 'chrome', '#c9ccd0', 8);
      B.cyl(0.16, 0.18, 0.015, sx, 0, hz, 'chrome', '#9da2a8', 20);
      this._aoq(sx, hz, 0.36, 0.36, true);
      this._spot('pantry', sx, hz, s < 0 ? Math.PI / 2 : -Math.PI / 2, 'sitDrink', 0.62);
    }
    this._plant(7.2, zs + 5.4, 1.1);
    sign(3.8, 'Pantry ☕', '#d98c3f');
    // bình nước
    B.box(0.4, 0.9, 0.4, 6.8, 0, zs + 0.7, 'paint', '#dfe6ea', { r: 0.03 }); B.cyl(0.15, 0.15, 0.42, 6.8, 0.9, zs + 0.7, 'plastic', '#9fd3f0', 18);
    this._aoq(6.8, zs + 0.7, 0.4, 0.4);
    grid.rect(6.6, zs + 0.5, 7.0, zs + 0.9, 0.08, 1, 1.35);

    // phòng nghỉ (sofa)
    const lx0 = 8, lx1 = W - 8, lcx = (lx0 + lx1) / 2, lcz = zs + 3, rw2 = Math.min(6, lx1 - lx0 - 0.5);
    B.box(rw2, 0.012, 4.2, lcx, 0, lcz, 'carpet', '#cfa48e');
    B.box(rw2 - 0.3, 0.016, 3.9, lcx, 0, lcz, 'carpet', '#eedccd');
    const sofa = (z, side, color) => {
      // side = -1: lưng ở phía bắc, người ngồi nhìn về +z; khung vải sẫm, 3 đệm ngồi + 3 đệm lưng bo, chân gỗ, gối nhấn
      const dark = darker(color, 0.78);
      B.box(2.5, 0.22, 0.85, lcx, 0.08, z, 'fabric', dark, { r: 0.04, seg: 2 });
      B.box(2.5, 0.5, 0.2, lcx, 0.28, z + side * 0.33, 'fabric', dark, { r: 0.07, seg: 2 });
      for (const sx of [-1.16, 1.16]) B.box(0.2, 0.42, 0.85, lcx + sx, 0.08, z, 'fabric', dark, { r: 0.07, seg: 2 });
      for (const sx of [-0.71, 0, 0.71]) {
        B.box(0.69, 0.12, 0.62, lcx + sx, 0.29, z - side * 0.06, 'fabric', color, { r: 0.06, seg: 2 });
        B.box(0.69, 0.36, 0.14, lcx + sx, 0.4, z + side * 0.2, 'fabric', color, { r: 0.07, seg: 2, rx: side * 0.15 });
      }
      B.box(0.36, 0.3, 0.12, lcx - 0.95, 0.42, z + side * 0.12, 'fabric', '#e9c46a', { r: 0.06, seg: 2, rx: side * 0.2, rotY: 0.2 });
      for (const sx of [-1.15, 1.15]) for (const oz of [-0.34, 0.34]) B.cyl(0.025, 0.018, 0.08, lcx + sx, 0, z + oz, 'walnut', WALNUT, 8);
      this._aoq(lcx, z, 2.5, 0.85);
      grid.rect(lcx - 1.26, z - 0.43, lcx + 1.26, z + 0.43, 0.05, 1, 0.9);
      for (const sx of [-0.72, 0, 0.72]) this._spot('lounge', lcx + sx, z - side * 0.05, side < 0 ? 0 : Math.PI, 'relax', 0.4);
    };
    sofa(zs + 1.4, -1, '#6f97b0');
    sofa(zs + 4.6, 1, '#c98a6e');
    B.box(1.5, 0.04, 0.8, lcx, 0.32, lcz, 'wood', '#d9bf98', { r: 0.015, seg: 2 });
    B.box(1.36, 0.02, 0.66, lcx, 0.1, lcz, 'wood', '#d9bf98');
    for (const sx of [-0.68, 0.68]) for (const oz of [-0.33, 0.33]) B.box(0.04, 0.32, 0.04, lcx + sx, 0, lcz + oz, 'metal', BLACK);
    B.cyl(0.06, 0.05, 0.12, lcx - 0.3, 0.36, lcz, 'ceramic', '#f4f1ea', 14);
    B.cyl(0.16, 0.08, 0.08, lcx + 0.35, 0.36, lcz + 0.1, 'ceramic', '#5b8a8c', 18);
    for (const [ox, oz, cc] of [[0.3, 0.08, '#e9b44c'], [0.4, 0.14, '#c8413a'], [0.36, 0.04, '#7fb069']]) B.sphere(0.05, lcx + ox, 0.46, lcz + oz, 'plastic', cc, 10);
    this._aoq(lcx, lcz, 1.5, 0.8);
    grid.rect(lcx - 0.75, lcz - 0.4, lcx + 0.75, lcz + 0.4, 0.05, 1, 0.55);
    this._plant(lcx - 2.6, zs + 0.7, 1.2); this._plant(lcx + 2.6, zs + 5.3, 1.2);
    // đèn cây (chân đen, chao vải sáng) + vũng sáng ban đêm
    B.cyl(0.015, 0.015, 1.55, lcx + 2.7, 0.02, zs + 1.3, 'metal', BLACK, 8); B.cyl(0.14, 0.16, 0.02, lcx + 2.7, 0, zs + 1.3, 'metal', BLACK, 20);
    B.cyl(0.13, 0.22, 0.28, lcx + 2.7, 1.5, zs + 1.3, 'emit', '#fff1c7', 20);
    this._pools.push([lcx + 2.7, zs + 1.3, 2.4, 2.4]);
    this._aoq(lcx + 2.7, zs + 1.3, 0.3, 0.3, true);
    sign(lcx, 'Phòng nghỉ', '#7aa6c2');

    // hàng ghế chờ
    const bx = W - 4.3;
    B.box(6.8, 0.012, 5.8, W - 3.9, 0, zs + 3, 'carpet', '#d9d3c7');
    for (const bz of [zs + 1.3, zs + 2.8]) {
      B.box(2.6, 0.07, 0.5, bx, 0.28, bz, 'fabric', muted('#4f8fc0'), { r: 0.03, seg: 2 });
      B.box(2.6, 0.4, 0.06, bx, 0.35, bz - 0.24, 'fabric', darker(muted('#4f8fc0'), 0.85), { r: 0.03, seg: 2, rx: -0.1 });
      for (const sx of [-1.2, 1.2]) { B.box(0.05, 0.28, 0.4, bx + sx, 0, bz, 'metal', BLACK); B.box(0.05, 0.4, 0.04, bx + sx, 0.3, bz - 0.24, 'metal', BLACK); }
      B.box(2.5, 0.03, 0.04, bx, 0.24, bz - 0.1, 'metal', BLACK);
      this._aoq(bx, bz, 2.6, 0.5);
      grid.rect(bx - 1.3, bz - 0.27, bx + 1.3, bz + 0.25, 0.04, 1, 0.75);
      for (const sx of [-0.85, 0, 0.85]) this._spot('bench', bx + sx, bz, 0, 'sit', 0.35);
    }
    // thảm lối vào + bảng "Cần bạn" (đứng nghiêng như giá vẽ, quay về phía camera)
    B.box(1.8, 0.012, 1.0, W - 1.4, 0, this.D - 0.7, 'carpet', '#8f5a45');
    // cửa ra vào "Thoát" (bấm = đăng xuất, có xác nhận 3D)
    const door = new THREE.Group(); door.position.set(W - 1.4, 0, this.D - 0.08); g.add(door);
    const dx = W - 1.4, dz = this.D - 0.08;
    for (const sx of [-0.62, 0.62]) B.box(0.14, 2.3, 0.16, dx + sx, 0, dz, 'walnut', WALNUT, { r: 0.01 });
    B.box(1.38, 0.16, 0.16, dx, 2.3, dz, 'walnut', WALNUT, { r: 0.01 });
    const leaf = this._solid(roundedBoxGeo(1.06, 2.2, 0.06, 0.01, 1), 'wood', '#c08a5c', 0, 0.02 + 1.1, 0.02, door);
    B.sphere(0.04, dx + 0.4, 1.05, dz + 0.07, 'chrome', '#d8d2c0', 10);
    B.box(0.03, 0.02, 0.12, dx + 0.4, 1.04, dz + 0.1, 'chrome', '#d8d2c0');
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
    const tm = new THREE.Matrix4().makeTranslation(bx, 0.3, this.D - 0.45).multiply(new THREE.Matrix4().makeRotationX(-0.22));
    B.box(3.78, 2.42, 0.08, 0, -0.08, -0.06, 'walnut', WALNUT, { r: 0.015, m: tm });
    needs.mesh.position.set(0, 2.25 / 2, 0.0); tilt.add(needs.mesh);
    for (const sx of [-1.6, 1.6]) B.box(0.07, 0.75, 0.07, bx + sx, 0, this.D - 0.65, 'metal', BLACK);
    this._aoq(bx, this.D - 0.6, 3.8, 0.4, true);
    grid.rect(bx - 1.9, this.D - 0.7, bx + 1.9, this.D - 0.2, 0.05);
    this._plant(W - 0.6, zs + 0.7, 1.1);
    sign(bx, 'Hàng ghế chờ ⏳', '#4f8fc0');
    this.rooms.push({ key: 'pantry', name: ZONE_NAMES.pantry, x0: 0, z0: zs, x1: 7.6, z1: this.D, p: { x: 2.6, z: zs + 2.1 } });
    this.rooms.push({ key: 'lounge', name: ZONE_NAMES.lounge, x0: lx0, z0: zs, x1: lx1, z1: this.D, p: { x: lcx - 2.0, z: lcz } });
    this.rooms.push({ key: 'bench', name: ZONE_NAMES.bench, x0: W - 7.3, z0: zs, x1: W, z1: this.D, p: { x: bx, z: zs + 4.0 } });

    // tường tây: bảng kanban + đồng hồ
    const kz = Math.max(ROOM_D + 2.2, (this.openZ0 + this.openZ1) / 2);
    const kanban = new Board(3.6, 2.25, 1024, 'kanban', 'bảng công việc');
    B.box(0.06, 2.4, 3.76, 0.04, 0.22, kz, 'walnut', WALNUT, { r: 0.012 });
    B.box(0.1, 0.03, 3.0, 0.07, 0.25, kz, 'metal', ALU);
    kanban.mesh.rotation.y = Math.PI / 2; kanban.mesh.position.set(0.08, 1.42, kz); g.add(kanban.mesh);
    // màn hình phòng server (tường tây trong phòng server): run đang chạy
    const runs = new Board(2.6, 1.6, 900, 'runs', 'màn hình phòng server');
    B.box(0.06, 1.74, 2.74, 0.04, 0.58, 3.4, 'metal', '#18191c', { r: 0.015 });
    runs.mesh.rotation.y = Math.PI / 2; runs.mesh.position.set(0.08, 1.45, 3.4); g.add(runs.mesh);
    // màn hình lớn trên tường bắc: HUD của hub
    const hud = new Board(9, 2.25, 1600, 'hud', 'màn hình trạng thái hub');
    B.box(9.3, 2.5, 0.14, W / 2, 2.72, -0.05, 'metal', '#1d2026', { r: 0.03 });
    for (const sx of [-3.5, 3.5]) B.box(0.18, 0.5, 0.3, W / 2 + sx, 2.4, -0.1, 'metal', BLACK);
    hud.mesh.position.set(W / 2, 3.97, 0.03); g.add(hud.mesh);
    // "Bảng quản lý agent" KHÔNG còn trong cảnh: điều khiển agent là bảng 2D (DOM) ở góc màn hình — office/dock.js, office-agent-board.md
    this.boards = { kanban, runs, hud, needs };
    this.focusables = [kanban.mesh, runs.mesh, hud.mesh, needs.mesh];
    const cz = kz + 2.6;
    const face = new THREE.Mesh(new THREE.CylinderGeometry(0.34, 0.34, 0.06, 40), new THREE.MeshBasicMaterial({ color: '#fffdf6', toneMapped: false }));
    face.rotation.z = Math.PI / 2; face.position.set(0.06, 1.95, cz); g.add(face);
    this.ownGeos.push(face.geometry); this.ownMats.push(face.material);
    const rimGeo = new THREE.TorusGeometry(0.35, 0.04, 10, 40); this.ownGeos.push(rimGeo);
    B.add(rimGeo, new THREE.Matrix4().makeTranslation(0.09, 1.95, cz).multiply(new THREE.Matrix4().makeRotationY(Math.PI / 2)), 'walnut', WALNUT);
    this.clockFace = face;
    for (let i = 0; i < 12; i++) {
      const a = (i / 12) * Math.PI * 2;
      B.box(0.01, i % 3 ? 0.03 : 0.06, 0.02, 0.1, 1.95 + Math.cos(a) * 0.27 - (i % 3 ? 0.015 : 0.03), cz - Math.sin(a) * 0.27, 'metal', '#3d3929');
    }
    const hand = (len, w, color) => {
      const p = new THREE.Group(); p.position.set(0.11, 1.95, cz);
      const m = this._solid(new THREE.BoxGeometry(0.012, len, w), 'metal', color, 0, len / 2 - 0.03, 0, p);
      m.castShadow = false;
      g.add(p); return p;
    };
    this.hHour = hand(0.17, 0.035, '#2a2723'); this.hMin = hand(0.25, 0.022, '#2a2723'); this.hSec = hand(0.27, 0.01, '#c96442');
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
  release(spot, id) { if (spot && this.zones[spot.zone] && spot.owner === id) { spot.owner = null; spot._play = false; } }
  // màn hình máy cafe sáng khi có người đang ngồi chơi (actor gọi mỗi khung; nhả chỗ → tắt)
  playCafe(spot, on) { if (spot && spot.zone === 'cafe') spot._play = !!on; }

  setScreen(id, on) {
    const s = this.seats.get(id);
    if (s && s.screen && s._on !== on) { s._on = on; s.screen.color.set(on ? SCREEN_ON : SCREEN_OFF); }
  }
  setEstop(on) {
    this.estop = !!on;
    if (!on) this._lightBase();
  }
  setTeamEstop(id, on) {
    const fx = this.teamFx.get(id || '');
    if (fx) { fx.on = !!on; fx.ov.visible = fx.lamp.visible = !!on; }
  }
  setLabelScale(f) { for (const s of this.signs) scaleLabel(s, f); }
  // ẩn biển phòng/team khi camera đang nhìn một bảng (biển che chữ trên bảng)
  setSignsVisible(on) { for (const s of this.signs) s.visible = on; }
  flashClock() { this.clockFlash = 2.5; }

  update(dt, t, reduced) {
    let ledDirty = false;
    this.leds.forEach((l, i) => {
      const on = reduced ? true : Math.sin(t * l.rate * 4 + l.ph) > -0.2;
      if (l.on !== on) { l.on = on; this.ledMesh.setColorAt(i, on ? this.ledCol[l.kind] : this.ledCol.off); ledDirty = true; }
    });
    if (ledDirty) this.ledMesh.instanceColor.needsUpdate = true;
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
      this.fill.color.set('#ffb0a8');
      this.hemi.intensity = 1.0 + p * 0.4;
      this.envHemi.color.copy(this.hemi.color); // ESTOP: phần bù IBL cũng đỏ (không pha loãng màu cảnh báo)
    }
    for (const fx of this.teamFx.values()) {
      if (!fx.on) continue;
      const p = reduced ? 0.6 : 0.5 + 0.5 * Math.sin(t * 4);
      fx.ov.material.opacity = 0.18 + p * 0.22; fx.lamp.material.opacity = 0.06 + p * 0.1;
    }
    // cafe: viền neon đổi màu (quầng sáng theo cùng màu), màn hình người đang chơi nhấp nháy; reduced-motion / ESTOP → màu tĩnh
    const still = reduced || this.estop;
    // accent dịu: trôi chậm trong dải lạnh (lục lam → chàm), bão hoà vừa; reduced/ESTOP → tĩnh
    if (!still) this.rgbMats.forEach((m, i) => m.color.setHSL(0.5 + 0.07 * Math.sin(t * 0.25 + i * 0.6), 0.55, 0.62 + i * 0.05));
    this.glowMats.forEach((m, i) => m.color.copy(this.rgbMats[i].color));
    for (const sp of this.zones.cafe) {
      if (sp._play) {
        if (still && sp._on === 'still') continue;
        sp._on = still ? 'still' : 'anim';
        const hue = ((still ? 0 : t * 0.15) + sp.ph * 0.13) % 1;
        sp.screen.color.setHSL(hue, 0.6, still ? 0.75 : 0.72 + 0.08 * Math.sin(t * 7 + sp.ph));
      } else if (sp._on) { sp._on = false; sp.screen.color.set(CAFE_SCREEN_OFF); }
    }
  }

  dispose() {
    for (const t of this.textures) t.dispose();
    for (const m of this.cafeMats) m.dispose();
    this.fade(new Set());
    for (const m of this.ownMats) m.dispose();
    for (const gg of this.ownGeos) gg.dispose();
    for (const b of Object.values(this.boards || {})) b.dispose();
    if (this.exitBoard) this.exitBoard.dispose();
    this.batch.dispose();
    if (this.lampBatch) this.lampBatch.dispose();
    for (const im of [this.ledMesh, this.books]) if (im) { im.geometry.dispose(); im.material.dispose(); im.dispose(); }
    // shadow map 1024² của đèn nắng (WebGLRenderTarget) — không dispose thì mỗi lần dựng lại World rò một render target
    for (const l of [this.dir, this.fill, this.hemi]) if (l) l.dispose();
    this.group.removeFromParent();
  }
}
