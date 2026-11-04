/* cfharness web — chế độ "Văn phòng 3D" (/office). Chế độ 3D = TOÀN TRANG 3D: trang độc lập, thấy được chỉ có canvas + nút "2D".
 * Mọi thông tin vẽ TRONG cảnh: màn hình HUD trên tường, kanban, màn hình phòng server, bảng "Cần bạn", thẻ nhân vật, thẻ đỏ cần duyệt,
 * biển hết phiên/mất kết nối. Danh sách chữ chỉ cho trình đọc màn hình (sr-only). Cảnh 3D không có link sang trang 2D.
 * Dữ liệu: GET data-state (JSON, chữ tự do đã cắt ở server) + SSE data-sse (khung mỏng: chỉ id/tên tool/trạng thái).
 * Chữ của agent là DỮ LIỆU: trong DOM chỉ textContent, trên cảnh chỉ fillText. Không innerHTML. Hợp đồng: docs/dev-notes/office-3d.md.
 * Giai đoạn 2: duyệt / trả lời câu hỏi / chat với điều phối viên / đăng xuất NGAY TRONG CẢNH (office/flows.js, office/chat.js, office/ui.js),
 * cùng cổng ghi với web 2D (CSRF từ data-csrf + khoá phiên `k` trong localStorage — không bao giờ vẽ/in/log).
 * Hiệu năng: chuyển động → vẽ theo màn hình (≈ 60 FPS), đứng yên → 30 FPS (docs/dev-notes/office-3d-perf.md), dừng khi tab ẩn, pixelRatio ≤ 1,5; không có WebGL (hoặc mất context không khôi phục trong 5 s) hoặc FPS TB < 10 trong 3 cửa sổ 5 s liền → #office-fallback (chậm: giữ renderer, nút "Tiếp tục 3D"). */
import * as THREE from './vendor/three-0.169.0/three.module.min.js';
import { OrbitControls } from './vendor/three-0.169.0/OrbitControls.js';
import { World, layoutKey } from './office/world.js';
import { Actor, STATUS_VI } from './office/actors.js';
import { setChibiQuality, updateChibiLod } from './office/chibi.js';
import { NO_TEAM_COLOR, scaleLabel, declutter, frameGate } from './office/util.js';
import { drawHud, drawKanban, drawRuns, drawNeeds, drawCard, drawApprovalTag, drawSign, TextSprite, fmtUsd } from './office/boards.js';
import { makeApi, Modals, createFlows } from './office/flows.js';
import { createChat } from './office/chat.js';
import { FONT, roundRect } from './office/util.js';
import { Walker, walkBusy, hintShown, hintLayout, coordinatorOf, nearDesk } from './office/walk.js';
import { createTalk } from './office/talk.js';
import { setTier, makeEnvironment } from './office/materials.js';
import { createPerf, perfFlags } from './office/perf.js';

const $ = (id) => document.getElementById(id);
const KCOL_VI = { ready: 'Sẵn sàng', running: 'Đang làm', review: 'Review', blocked: 'Kẹt' };
// giờ địa phương (số thực) cho ánh sáng theo giờ thật
const hourNow = () => { const d = new Date(); return d.getHours() + d.getMinutes() / 60; };
// Nhịp vẽ (frameGate, util.js): camera/cảnh đang chuyển động → theo màn hình (≈ 60 FPS); đứng yên → 30 FPS. Giữ nhịp nhanh thêm ACTIVE_HOLD ms.
const ACTIVE_HOLD = 400;
// OrbitControls: hệ số tắt dần theo THỜI GIAN (0,08 mỗi 1/60 s) — không phụ thuộc số khung/giây
const DAMP_60 = 0.08;

function el(tag, text) {
  const e = document.createElement(tag);
  if (text != null) e.textContent = String(text);
  return e;
}

// Nút "2D": xoá lựa chọn 3D rồi về trang tổng quan.
function bindTo2D() {
  const b = $('office-to-2d');
  if (b) b.addEventListener('click', (e) => {
    e.preventDefault();
    try { localStorage.removeItem('cfh_view'); } catch (_) { /* bỏ qua */ }
    location.href = '/';
  });
}

function main() {
  const root = $('office');
  if (!root) return;
  bindTo2D();
  const stateUrl = root.getAttribute('data-state') || '/office/state';
  const sseUrl = root.getAttribute('data-sse') || '/sse/office';
  const host = $('office-canvas'), srPanel = $('office-panel'), list = $('office-list'), srHud = $('office-hud');
  const fallback = $('office-fallback'), expiredEl = $('office-expired');
  const mqReduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  let reduced = !!(mqReduce && mqReduce.matches);
  if (mqReduce && mqReduce.addEventListener) mqReduce.addEventListener('change', (e) => { reduced = e.matches; });

  // ------------------------------------------------------------ WebGL (không có → chỉ chạy phần dữ liệu cho danh sách sr-only)
  let renderer = null, noRender = false;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'low-power' });
    if (!renderer.getContext()) throw new Error('no ctx');
  } catch (_) {
    renderer = null;
  }
  const cvs = renderer ? renderer.domElement : document.createElement('canvas');
  // Chất lượng (docs/dev-notes/office-3d-visual.md §3): 'high' = PBR + env map + bóng tĩnh; 'low' = Lambert, không bóng/env, pixelRatio 1.
  // Máy yếu (≤ 4 nhân + màn dày) hoặc đã tự hạ trong tab này (sessionStorage) → low; FPS < 24 hai cửa sổ đo liền → tự hạ (không tự nâng).
  let tier = 'high';
  try { if (sessionStorage.getItem('cfh_office_q') === 'low') tier = 'low'; } catch (_) { /* bỏ qua */ }
  if ((navigator.hardwareConcurrency || 8) <= 4 && (window.devicePixelRatio || 1) >= 2) tier = 'low';
  const pixelRatio = () => (tier === 'high' ? Math.min(window.devicePixelRatio || 1, 1.5) : 1);
  setTier(tier, { aniso: renderer ? renderer.capabilities.getMaxAnisotropy() : 4 });
  setChibiQuality(tier); // nhân vật theo cùng tier (vinyl PBR ↔ Lambert)
  if (renderer) {
    renderer.setPixelRatio(pixelRatio());
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.0;
    renderer.shadowMap.enabled = tier === 'high';
    // PCFSoft (lọc song tuyến): PCF thường + radius cho mép bóng bậc thang khi nhìn gần (đi bộ) — đo trên Iris Xe không tụt FPS
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.shadowMap.autoUpdate = false;          // bóng TĨNH: chỉ vẽ lại khi world.shadowDirty
    host.appendChild(cvs);
    cvs.setAttribute('aria-hidden', 'true');
  }

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(36, 1, 0.3, 400);
  camera.position.set(0, 4, 16);
  scene.add(camera); // bảng modal + thông báo gắn vào camera
  const controls = new OrbitControls(camera, cvs);
  controls.enableDamping = true; controls.dampingFactor = DAMP_60;
  controls.minPolarAngle = 0.2; controls.maxPolarAngle = 1.4;
  controls.minDistance = 3; controls.maxDistance = 70;
  controls.screenSpacePanning = false;

  // env map tự dựng (PMREM) — chỉ tier high; mất context → dựng lại (webglcontextrestored)
  let envTex = null;
  function applyEnv() {
    if (!renderer) return;
    if (tier === 'high') { if (!envTex) envTex = makeEnvironment(renderer); scene.environment = envTex; }
    else { scene.environment = null; if (envTex) { envTex.dispose(); envTex = null; } } // low: không giữ PMREM (render target) trên GPU
  }
  applyEnv();
  scene.fog = new THREE.Fog('#faf9f5', 60, 140);
  let dark = false;
  function applyTheme() {
    dark = !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    scene.background = new THREE.Color(dark ? '#262624' : '#faf9f5');
    scene.fog.color.copy(scene.background);
    if (renderer) renderer.toneMappingExposure = dark ? 0.92 : 1.0;
    if (world) world.setTheme(dark);
  }
  let world = null, wkey = '', state = null, stopped = false;
  applyTheme();
  const mqDark = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  if (mqDark && mqDark.addEventListener) mqDark.addEventListener('change', applyTheme);

  const actors = new Map();
  let selected = null, focusBoard = null;
  let conn = 'connecting', lostAt = 0;
  const t0 = performance.now();
  const perfNow = () => (performance.now() - t0) / 1000;
  const names = (id) => { const a = id && actors.get(id); return a ? a.agent.name || id : id; };

  // ------------------------------------------------------------ vật thể chữ trong cảnh
  const card = new TextSprite(640, 560, 2.7, { depthTest: false, order: 30 });
  card.sprite.visible = false; scene.add(card.sprite);
  const sign = new TextSprite(1200, 320, 10, { depthTest: false, order: 40 });
  sign.sprite.visible = false; scene.add(sign.sprite);
  const tags = new Map(); // agentId → TextSprite thẻ đỏ "cần duyệt"

  // ------------------------------------------------------------ giai đoạn 2: bảng tương tác (duyệt, câu hỏi, chat, thoát)
  const srBox = (list && list.parentNode) || root;
  const srDialogs = document.createElement('div'); srBox.appendChild(srDialogs);
  const srToast = document.createElement('p'); srToast.setAttribute('role', 'alert'); srBox.appendChild(srToast);
  const api = makeApi(root);
  const toastTs = new TextSprite(1100, 120, 1, { depthTest: false, order: 2000 });
  toastTs.sprite.visible = false; camera.add(toastTs.sprite);
  let toastUntil = 0;
  function toast(msg, ok) {
    srToast.textContent = msg;
    toastTs.sig = null;
    toastTs.draw(msg + ok, (ctx, W, H) => {
      ctx.fillStyle = ok ? '#2f6b3d' : '#b3402f'; roundRect(ctx, 6, 6, W - 12, H - 12, 40); ctx.fill();
      ctx.fillStyle = '#ffffff'; ctx.font = '700 44px ' + FONT; ctx.textBaseline = 'middle'; ctx.textAlign = 'center';
      let t = (ok ? '✓ ' : '⚠ ') + String(msg || '');
      while (t.length > 2 && ctx.measureText(t).width > W - 60) t = t.slice(0, -2);
      ctx.fillText(t, W / 2, H / 2 + 2); ctx.textAlign = 'left';
    });
    toastUntil = performance.now() + 4000;
    fitToast();
    toastTs.sprite.visible = !!renderer;
  }
  function fitToast() {
    const d = 1.5, vh = 2 * d * Math.tan((camera.fov * Math.PI) / 360), vw = vh * camera.aspect;
    const w = Math.min(vw * 0.8, vh * 0.75), h = (w * 120) / 1100;
    toastTs.sprite.scale.set(w, h, 1);
    // có bảng modal: sát mép trên (không che hàng nút); không có: đáy khung nhìn
    toastTs.sprite.position.set(0, modals.open ? vh / 2 - h * 0.62 : -vh / 2 + h * 0.9, -d);
  }
  const modals = new Modals({ camera, srRoot: srDialogs, onChange: () => { syncControls(); } });
  const flows = createFlows({
    api, modals, names, toast,
    refresh: () => { clearTimeout(refetchT); refetchT = setTimeout(() => fetchState(), 300); },
    pop: (id, emoji) => { const a = actors.get(id); if (a) a.pop = { emoji, until: perfNow() + 3 }; },
    onExpired: () => onExpired(),
  });
  const chat = createChat({
    api, srRoot: srDialogs, toast, onExpired: () => onExpired(), openQuestion: (id) => flows.openQuestion(id), camera,
    managerActor: () => (state && state.manager_id && actors.get(state.manager_id)) || [...actors.values()].find((a) => a.isManager) || null,
    onOpenChange: (open) => {
      syncControls();
      if (open) { select(null); focusBoard = null; if (!chat.overlay) {
        // nhìn thẳng màn hình chat (đọc rõ); bong bóng lời nói của điều phối viên nằm trên mép màn hình phía điều phối viên
        const f = chat.frame(), m = chat.managerPos && chat.managerPos();
        const side = m ? new THREE.Vector3(m.x - f.center.x, 0, m.z - f.center.z) : new THREE.Vector3();
        side.addScaledVector(f.normal, -side.dot(f.normal));
        if (side.lengthSq() > 1e-4) { side.normalize(); f.center.addScaledVector(side, 0.25); }
        f.center.y += 0.4; f.h += 1.0; // chừa phía trên màn hình cho bong bóng lời nói
        flyToFrame(f, 1.1);
      } } else if (walk.active) { tween = null; if (walk.stand()) walk.resume(reduced); } else if (overview) { flyTo(overview.pos, overview.target); }
      renderSrPanel();
    },
  });
  // Nói chuyện với agent (chế độ đi bộ, [E] khi đứng gần): office/talk.js — action steer / run_continue / run_start qua /office/do
  const talk = createTalk({
    api, srRoot: srDialogs, camera, toast, onExpired: () => onExpired(), ask: (d) => flows.ask(d),
    openApproval: (id) => flows.openApproval(id), openQuestion: (id) => flows.openQuestion(id),
    agent: (id) => {
      const act = actors.get(id);
      if (!act) return null;
      const ap = ((state && state.approvals) || []).find((x) => x.agent === id), q = ((state && state.questions) || []).find((x) => x.agent === id);
      return { id, name: act.agent.name || id, status: act.status(perfNow()), statusText: statusText(act), run_id: act.agent.run_id || null,
        approval: ap ? ap.id : null, question: q ? q.id : null };
    },
    onOpenChange: (open, id) => {
      const act = actors.get(id);
      // agent đứng/ngồi yên, quay mặt về người dùng trong lúc nói chuyện
      if (act) act.talking = open && walk.pos ? { x: walk.pos.x, z: walk.pos.z } : null;
      if (open) { select(null); focusBoard = null; }
      syncControls(); renderSrPanel();
    },
  });
  function syncControls() { controls.enabled = !walk.active && !modals.open && !chat.isOpen && !talk.isOpen; }
  // khi chưa có văn phòng (vd. hết phiên ngay lần tải đầu): biển dựng trên một khoảnh sàn nhỏ cho ra cảnh 3D
  const signStand = new THREE.Group();
  {
    const wood = new THREE.MeshLambertMaterial({ color: '#e6cfab' }), dark = new THREE.MeshLambertMaterial({ color: '#6a432b' });
    const floor = new THREE.Mesh(new THREE.CylinderGeometry(6, 6, 0.2, 40), wood); floor.position.y = -3.1; signStand.add(floor);
    for (const x of [-3.2, 3.2]) { const p = new THREE.Mesh(new THREE.BoxGeometry(0.25, 2.2, 0.25), dark); p.position.set(x, -2.0, -0.2); signStand.add(p); }
    signStand.add(new THREE.HemisphereLight('#fff6e8', '#cdb497', 2.2));
    signStand.visible = false; scene.add(signStand);
  }

  // ------------------------------------------------------------ camera: toàn cảnh / bay tới nhân vật / bay tới bảng
  let overview = null, tween = null;
  function fitCamera() {
    const W = world.W, D = world.D;
    const c = new THREE.Vector3(W / 2, 0, D / 2);
    const half = Math.tan((camera.fov * Math.PI) / 360);
    if (camera.aspect < 0.85) {
      // màn dọc (điện thoại): nhìn từ phía đông để chiều dài văn phòng chạy dọc màn hình
      const dist = (D * 0.62) / (half * camera.aspect);
      overview = { pos: new THREE.Vector3(c.x + dist * 0.62, dist * 0.78, c.z + dist * 0.06), target: c };
      controls.maxDistance = Math.max(30, dist * 1.4);
      return;
    }
    const span = Math.max(W, D * 1.2);
    const dist = (span * 0.6) / half / Math.max(0.42, Math.min(1.5, camera.aspect * 0.8));
    overview = { pos: new THREE.Vector3(c.x + dist * 0.3, dist * 0.72, c.z + dist * 0.63), target: c };
    controls.maxDistance = Math.max(30, dist * 1.6);
  }
  function flyTo(pos, target) {
    if (reduced || !renderer) { camera.position.copy(pos); controls.target.copy(target); controls.update(); tween = null; return; }
    tween = { p0: camera.position.clone(), q0: controls.target.clone(), p1: pos.clone(), q1: target.clone(), k: 0 };
  }
  function camRight() {
    const r = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 0);
    r.y = 0;
    return r.lengthSq() < 1e-6 ? new THREE.Vector3(1, 0, 0) : r.normalize();
  }
  // màn dọc: thẻ nằm TRÊN nhân vật; màn ngang: bên phải nhân vật
  const portrait = () => camera.aspect < 0.85;
  function charTarget(act) {
    if (portrait()) return new THREE.Vector3(act.pos.x, 1.9, act.pos.z);
    return new THREE.Vector3(act.pos.x, 1.0, act.pos.z).addScaledVector(camRight(), 0.8);
  }
  function cardPos(act) {
    if (portrait()) return new THREE.Vector3(act.pos.x, 3.35, act.pos.z);
    return new THREE.Vector3(act.pos.x, 1.4, act.pos.z).addScaledVector(camRight(), 1.95);
  }
  function flyToActor(act) {
    if (!act.pos) return;
    const tgt = charTarget(act);
    const dir = camera.position.clone().sub(controls.target); dir.y = 0;
    if (dir.lengthSq() < 1e-4) dir.set(0.4, 0, 1);
    dir.normalize();
    const k = portrait() ? Math.min(2.2, 0.8 / Math.max(0.3, camera.aspect)) : 1;
    flyTo(tgt.clone().addScaledVector(dir, 7.6 * k).add(new THREE.Vector3(0, 4.6 * k, 0)), tgt);
  }
  function flyToBoard(b) { flyToFrame(b.frame(), 1.32); }
  function flyToFrame(f, margin) {
    const half = Math.tan((camera.fov * Math.PI) / 360);
    const dist = Math.max((f.h / 2) / half, (f.w / 2) / (half * camera.aspect)) * (margin || 1.32);
    flyTo(f.center.clone().addScaledVector(f.normal, dist).add(new THREE.Vector3(0, dist * 0.22, 0)), f.center);
  }
  function toOverview() {
    if (chat.isOpen) { chat.close(); return; }
    select(null);
    focusBoard = null;
    if (walk.active) tween = null; // chế độ đi bộ: camera trở lại sau lưng avatar
    else if (overview) flyTo(overview.pos, overview.target);
    renderSrPanel();
  }

  // ------------------------------------------------------------ chế độ "Vào văn phòng" (đi bộ) — chỉ là cách XEM, không mở thêm quyền
  // Avatar chủ hub + camera góc thứ ba/thứ nhất (office/walk.js). Đứng gần agent → thẻ thông tin + agent chào; đứng gần màn hình chat /
  // bảng / cửa ra → [E] mở đúng luồng sẵn có (chat.open, showBoard, flows.*) — cùng endpoint, CSRF, khoá phiên như chế độ toàn cảnh.
  const walk = new Walker({ camera, scene });
  const walkBtn = $('office-walk'), povBtn = $('office-pov');
  const hint = new TextSprite(1100, 150, 1, { depthTest: false, order: 1900 });
  hint.sprite.visible = false; camera.add(hint.sprite);
  let near = null, autoSel = false, walkHere = null, walkYield = true, nearAt = 0;
  // trạng thái giao diện cho chính sách đi bộ (walk.js): có bảng modal / chat / bảng đang xem → ẩn gợi ý, không tự chọn agent
  const ui = () => ({ walk: walk.active, modal: modals.open, chat: chat.isOpen || talk.isOpen, board: !!focusBoard });
  // nhân vật người dùng điều khiển = điều phối viên (nhãn + màu của agent is_manager); không có → "Bạn · chủ hub"
  const coordinator = () => coordinatorOf(actors, state && state.manager_id);
  function syncIdentity() { const c = coordinator(); walk.setIdentity(c ? { id: c.id, name: c.agent.name || c.id, color: c.color } : null); }
  // dòng gợi ý: gọn, sát mép dưới khung nhìn (không đè biển tên khu vực / đầu hộp thoại)
  function fitHint() {
    const L = hintLayout(camera.fov, camera.aspect);
    hint.sprite.scale.set(L.w, L.h, 1);
    hint.sprite.position.set(0, L.y, -L.d);
  }
  function setWalk(on) {
    if (!world || !renderer || on === walk.active) return;
    select(null); focusBoard = null; tween = null; near = null; autoSel = false;
    if (on) { syncIdentity(); walk.enter(world); walkYield = true; }
    else {
      talk.close(); chat.close();
      walk.exit();
      for (const a of actors.values()) { a.puppet = false; a.talking = null; } // actor điều phối viên AI hiện lại
      // OrbitControls tiếp quản từ chỗ camera đang đứng rồi bay về toàn cảnh
      const f = new THREE.Vector3(); camera.getWorldDirection(f);
      controls.target.copy(camera.position).addScaledVector(f, 4);
      if (overview) flyTo(overview.pos, overview.target);
    }
    hint.sprite.visible = hintShown(ui());
    if (walkBtn) { walkBtn.setAttribute('aria-pressed', on ? 'true' : 'false'); walkBtn.title = on ? 'Ra toàn cảnh' : 'Vào văn phòng (đi bộ)'; walkBtn.setAttribute('aria-label', walkBtn.title); }
    if (povBtn) povBtn.hidden = !on;
    syncControls(); renderSrPanel();
  }
  if (walkBtn) walkBtn.addEventListener('click', () => setWalk(!walk.active));
  if (povBtn) povBtn.addEventListener('click', () => { walk.togglePov(); renderSrPanel(); });
  // thứ ở gần avatar có thể tương tác bằng [E]: màn hình chat, bảng, cửa ra, agent
  function walkTarget() {
    const p = walk.pos;
    if (!p || !world) return null;
    const a = world.chatAnchor, room = world.roomAt(p.x, p.z);
    if (walk.seated) return { kind: 'stand', label: 'Đứng dậy (Esc)' };
    // điều phối viên đứng cạnh bàn của mình → ngồi xuống ra lệnh (bàn điều khiển = chat với điều phối viên AI)
    if (walk.isCoordinator && room && room.key === 'mgr' && nearDesk(world, p)) return { kind: 'sit', label: 'Ngồi vào bàn (ra lệnh)' };
    if (room && room.key === 'mgr' && Math.hypot(a.x - p.x, a.z - p.z) < 2.8) return { kind: 'chat', label: 'Chat với điều phối viên' };
    const v = new THREE.Vector3();
    for (const [k, label] of [['kanban', 'Xem bảng công việc'], ['runs', 'Xem màn hình phòng server'], ['needs', 'Xem bảng Cần bạn'], ['hud', 'Xem màn hình trạng thái']]) {
      const b = world.boards[k];
      b.mesh.getWorldPosition(v);
      if (Math.hypot(v.x - p.x, v.z - p.z) < (k === 'hud' ? 3.2 : 2.6)) return { kind: 'board', board: b, label };
    }
    if (Math.hypot(world.W - 1.4 - p.x, world.D - p.z) < 1.4) return { kind: 'exit', label: 'Thoát (đăng xuất)' };
    const act = near && actors.get(near);
    if (act) {
      const ap = ((state && state.approvals) || []).find((x) => x.agent === act.id);
      const q = ((state && state.questions) || []).find((x) => x.agent === act.id);
      // agent thường: [E] mở hội thoại (bảng hội thoại có nút Duyệt / Trả lời khi agent đang chờ)
      if (!act.isManager && !act.puppet) return { kind: 'talk', id: act.id, label: 'Nói chuyện với ' + (act.agent.name || act.id) };
      if (ap) return { kind: 'ap', id: ap.id, label: 'Duyệt yêu cầu của ' + (act.agent.name || act.id) };
      if (q) return { kind: 'q', id: q.id, label: 'Trả lời câu hỏi của ' + (act.agent.name || act.id) };
      if (act.isManager) return { kind: 'chat', label: 'Chat với điều phối viên' };
    }
    return null;
  }
  function walkInteract() {
    const t = walkTarget();
    if (!t) return;
    if (t.kind === 'chat') chat.open();
    else if (t.kind === 'sit') { if (walk.sit(world.mgrSpot)) chat.open({ desk: true }); }
    else if (t.kind === 'stand') { if (chat.isOpen) chat.close(); else if (walk.stand()) walk.resume(reduced); }
    else if (t.kind === 'talk') talk.open(t.id);
    else if (t.kind === 'board') showBoard(t.board);
    else if (t.kind === 'exit') flows.openLogout();
    else doAct(t.kind, t.id);
  }
  // mỗi 0,2 s: agent gần nhất (≤ 1,8 m) → tự mở thẻ + agent chào; đi xa (> 2,6 m) → đóng thẻ tự mở; cập nhật dòng gợi ý
  function walkProximity(now) {
    const p = walk.pos;
    let best = null, bd = 1.8;
    for (const a of actors.values()) {
      if (!a.pos || a.puppet) continue;
      const d = Math.hypot(a.pos.x - p.x, a.pos.z - p.z);
      if (d < bd || (a.id === near && d < 2.6 && !best)) { bd = Math.min(bd, d); best = a; }
    }
    const id = best ? best.id : null;
    // đang mở bảng modal / chat / bảng: không tự chọn/bỏ chọn (select đóng bảng đang xem) — xét lại khi đóng
    if (id !== near && !walkBusy(ui())) {
      near = id;
      if (best) { best.greet(p.x, p.z, now); if (!selected || autoSel) { select(id, false); autoSel = true; } }
      else if (autoSel) { select(null); autoSel = false; }
    }
    const room = world.roomAt(p.x, p.z), t = walkTarget();
    walkHere = { room: room ? room.name : 'Khu làm việc', action: t ? t.label : '' };
    hint.draw(JSON.stringify(walkHere) + walk.pov, (ctx, W, H) => {
      ctx.fillStyle = 'rgba(31,30,29,0.78)'; roundRect(ctx, 6, 6, W - 12, H - 12, 36); ctx.fill();
      ctx.textBaseline = 'middle'; ctx.fillStyle = '#ffffff'; ctx.font = '700 40px ' + FONT;
      let top = (walk.seated ? '🪑 ' : '📍 ') + walkHere.room + (walkHere.action ? '   ·   [E] ' + walkHere.action : '');
      while (top.length > 4 && ctx.measureText(top).width > W - 70) top = top.slice(0, -2) + '…';
      ctx.fillText(top, 36, 56);
      ctx.fillStyle = '#d6d3c8'; ctx.font = '500 28px ' + FONT;
      ctx.fillText('WASD/mũi tên: đi · Shift: chạy · kéo: xoay · bấm sàn: tự đi · V: ' + (walk.pov === 'third' ? 'góc thứ nhất' : 'góc thứ ba') + ' · Esc: ra', 36, 112);
    });
  }

  // ------------------------------------------------------------ áp dữ liệu /office/state
  function teamColorOf(a) {
    if (a.is_manager) return '#c8413a';
    return (world && world.teamColor.get(a.team && world.teamColor.has(a.team) ? a.team : '')) || NO_TEAM_COLOR;
  }
  const teamEstop = new Map();

  function applyState(s) {
    state = s;
    const key = layoutKey(s), rebuilt = key !== wkey || !world;
    if (rebuilt) {
      const first = !world;
      if (world) world.dispose();
      world = new World(s);
      scene.add(world.group);
      wkey = key;
      for (const a of actors.values()) a.attach(world);
      chat.mount(world);
      if (walk.active) syncIdentity();
      world.setDaylight(hourNow()); world.setTheme(dark);
      if (walk.active) { walk.setWorld(world); near = null; }
      if (walkBtn && renderer && !noRender) walkBtn.hidden = false;
      fitCamera();
      if (first) { camera.position.copy(overview.pos); controls.target.copy(overview.target); controls.update(); }
    }
    const seen = new Set(), now = perfNow(), initial = actors.size === 0;
    for (const ag of s.agents || []) {
      const a = Object.assign({}, ag, { is_manager: !!ag.is_manager || ag.id === s.manager_id });
      seen.add(a.id);
      let act = actors.get(a.id);
      if (!act) {
        act = new Actor(a, teamColorOf(a), { entrance: !initial });
        actors.set(a.id, act);
        act.attach(world);
        scene.add(act.chibi.group);
      }
      act.setData(a, teamColorOf(a), now);
    }
    for (const [id, act] of actors) {
      if (!seen.has(id)) { act.dispose(); actors.delete(id); if (selected === id) select(null); }
    }
    world.setEstop(!!(s.hub && s.hub.estop));
    const est = new Set((s.teams || []).filter((t) => t.estop).map((t) => t.id));
    for (const c of world.clusters) world.setTeamEstop(c.id, est.has(c.id) || teamEstop.get(c.id) === true);
    syncTags();
    drawBoards(true);
    renderList(); renderSrHud(); renderSrPanel(); renderActions();
    if (rebuilt) warmShaders();
  }

  // Biên dịch trước MỌI shader của cảnh (kể cả vật đang ẩn: đèn trần chế độ đi bộ, vật liệu mờ tường, thẻ, vòng chọn, đạo cụ, ESTOP…)
  // ngay sau khi dựng World — trước đây shader biên dịch lần đầu vật hiện ra → khựng 50–170 ms khi vào đi bộ / bay tới agent / đêm.
  // compileAsync dùng KHR_parallel_shader_compile nếu có (không chặn luồng chính). Không bật vật chứa đèn (đổi số đèn → sai biến thể).
  function warmShaders() {
    if (!renderer || !world || noRender) return;
    const shown = [];
    scene.traverse((o) => {
      if (o.visible || o.isLight) return;
      let light = false;
      o.traverse((c) => { if (c.isLight) light = true; });
      if (!light) { o.visible = true; shown.push(o); }
    });
    const occ = world.occluders && world.occluders[0];
    if (occ) world.fade(new Set([occ])); // biến thể vật liệu mờ tường (chế độ đi bộ)
    try { renderer.compileAsync(scene, camera).catch(() => {}); } catch (_) { /* bỏ qua: sẽ biên dịch khi vẽ như cũ */ }
    if (occ) world.fade(new Set());
    for (const o of shown) o.visible = false;
  }

  function drawBoards() {
    if (!world || !state) return;
    const b = world.boards;
    drawHud(b.hud, { hub: state.hub, task_counts: state.task_counts, conn });
    drawKanban(b.kanban, state.task_counts, state.tasks, names);
    drawRuns(b.runs, state.runs || [], names, Date.now());
    drawNeeds(b.needs, state.approvals || [], state.questions || [], names);
  }

  // thẻ đỏ trên đầu agent có approval đang chờ (approval đầu tiên của agent)
  function syncTags() {
    const by = new Map();
    for (const a of (state && state.approvals) || []) if (a.agent && !by.has(a.agent)) by.set(a.agent, a);
    for (const [id, ts] of tags) if (!by.has(id) || !actors.has(id)) { ts.dispose(); tags.delete(id); }
    for (const [id, a] of by) {
      if (!actors.has(id)) continue;
      let ts = tags.get(id);
      if (!ts) { ts = new TextSprite(620, 128, 2.3, { depthTest: false, order: 25 }); ts.sprite.userData.base = [2.3, 2.3 * 128 / 620]; scene.add(ts.sprite); tags.set(id, ts); }
      ts.sprite.userData.approvalId = a.id; // bấm thẻ đỏ → bảng duyệt 3D
      drawApprovalTag(ts, a, a.status === 'waiting_human');
    }
  }

  // ------------------------------------------------------------ chữ cho trình đọc màn hình (sr-only — không hiển thị)
  const items = new Map();
  const statusKey = (act) => act.status(perfNow());
  const statusText = (act) => STATUS_VI[statusKey(act)] || act.agent.status || '—';
  // nút xem các bảng (bàn phím: Tab → Enter → camera bay tới)
  if (list && list.parentNode) {
    const views = el('ul');
    for (const [k, label] of [['overview', 'Toàn cảnh văn phòng'], ['hud', 'Xem màn hình trạng thái hub'], ['kanban', 'Xem bảng công việc'],
      ['runs', 'Xem màn hình phòng server'], ['needs', 'Xem bảng Cần bạn']]) {
      const li = el('li'), btn = el('button', label);
      btn.type = 'button';
      btn.addEventListener('click', () => { if (k === 'overview') toOverview(); else if (world) showBoard(world.boards[k]); });
      li.appendChild(btn); views.appendChild(li);
    }
    list.parentNode.insertBefore(views, list.previousElementSibling || list);
  }
  // nút sr-only cho mọi thao tác 3D: mở bảng duyệt / câu hỏi, chat, thoát
  const actList = el('ul');
  if (list && list.parentNode) list.parentNode.insertBefore(actList, list.previousElementSibling || list);
  let actSig = '';
  function renderActions() {
    const rows = [];
    for (const a of (state && state.approvals) || []) rows.push(['ap', a.id, 'Mở bảng duyệt: ' + (names(a.agent) || a.agent || '?') + ' · ' + (a.tool || '?') + (a.preview ? ' — ' + a.preview : '')]);
    for (const q of (state && state.questions) || []) rows.push(['q', q.id, 'Trả lời câu hỏi của ' + (names(q.agent) || q.agent || '?') + (q.text ? ': ' + q.text : '')]);
    rows.push(['chat', '', 'Mở chat với điều phối viên']);
    rows.push(['logout', '', 'Thoát (đăng xuất)']);
    const sig = JSON.stringify(rows);
    if (sig === actSig) return;
    actSig = sig;
    actList.replaceChildren(...rows.map(([k, id, label]) => {
      const li = el('li'), b = el('button', label);
      b.type = 'button';
      b.addEventListener('click', () => doAct(k, id));
      li.appendChild(b);
      return li;
    }));
  }
  function doAct(k, id) {
    if (k === 'ap') flows.openApproval(id);
    else if (k === 'q') flows.openQuestion(id);
    else if (k === 'chat') chat.open();
    else if (k === 'logout') flows.openLogout();
  }

  function renderList() {
    if (!list) return;
    for (const [id, it] of items) if (!actors.has(id)) { it.li.remove(); items.delete(id); }
    for (const [id, act] of actors) {
      let it = items.get(id);
      if (!it) {
        const li = el('li'), btn = el('button');
        btn.type = 'button';
        btn.addEventListener('click', () => select(selected === id ? null : id, true));
        li.appendChild(btn); list.appendChild(li);
        it = { li, btn };
        items.set(id, it);
      }
      const txt = (act.isManager ? 'Điều phối viên ' : '') + (act.agent.name || id) + ': ' + statusText(act) + (act.agent.task ? ' — ' + act.agent.task : '');
      if (it.btn.textContent !== txt) it.btn.textContent = txt;
      it.btn.setAttribute('aria-pressed', selected === id ? 'true' : 'false');
    }
  }
  function renderSrHud() {
    if (!srHud) return;
    const h = (state && state.hub) || {};
    const t = (conn === 'expired' ? 'Phiên đăng nhập đã hết. ' : conn === 'lost' ? 'Mất kết nối, đang thử lại. ' : '') +
      (h.running || 0) + ' đang chạy, ' + (h.queued || 0) + ' xếp hàng, ' + (h.approvals_waiting_human || 0) + ' chờ bạn duyệt' +
      (h.estop ? ', ESTOP đang bật' : '') + (h.rate_limited ? ', đang giới hạn tốc độ' : '') + ', chi phí hôm nay ' + fmtUsd(h.cost_today_usd) + '.';
    if (srHud.textContent !== t) srHud.textContent = t;
  }
  function renderSrPanel() {
    if (!srPanel) return;
    let t = '';
    const act = selected && actors.get(selected);
    if (act) {
      const a = act.agent;
      t = (a.name || a.id) + '. ' + statusText(act) + '. ' + act.where() + '.' + (a.task ? ' Việc: ' + a.task + '.' : '') + (a.tool ? ' Tool: ' + a.tool + '.' : '');
    } else if (focusBoard && state) {
      const k = focusBoard.mesh.userData.focus.kind;
      if (k === 'kanban') {
        t = 'Bảng công việc. ' + Object.keys(KCOL_VI).map((c) => KCOL_VI[c] + ' ' + ((state.task_counts || {})[c] || 0) + ': ' +
          (((state.tasks || {})[c]) || []).map((x) => x.title).join('; ')).join('. ');
      } else if (k === 'needs') {
        t = 'Cần bạn. ' + (state.approvals || []).map((a) => 'Phê duyệt ' + (names(a.agent) || '') + ' ' + (a.tool || '') + ': ' + (a.preview || '')).join('. ') +
          ' ' + (state.questions || []).map((q) => 'Câu hỏi ' + (names(q.agent) || '') + ': ' + (q.text || '')).join('. ');
      } else if (k === 'runs') {
        t = 'Lượt chạy đang chạy: ' + ((state.runs || []).map((r) => (names(r.agent) || r.agent) + ' ' + fmtUsd(r.cost_usd)).join(', ') || 'không có') + '.';
      } else t = srHud ? srHud.textContent : '';
    } else if (walk.active && walkHere) {
      t = 'Chế độ đi bộ (' + (walk.pov === 'third' ? 'góc nhìn thứ ba' : 'góc nhìn thứ nhất') + '). Bạn đang ở ' + walkHere.room + '.' +
        (walkHere.action ? ' Bấm E: ' + walkHere.action + '.' : '');
    }
    if (srPanel.textContent !== t) srPanel.textContent = t;
  }

  // ------------------------------------------------------------ chọn nhân vật / bảng
  function select(id, fly) {
    if (selected && actors.get(selected)) actors.get(selected).selected = false;
    selected = id && actors.has(id) ? id : null;
    card.sprite.visible = !!selected && !!renderer;
    if (selected) {
      focusBoard = null;
      const act = actors.get(selected);
      act.selected = true;
      if (fly !== false) flyToActor(act);
    }
    renderList(); renderSrPanel();
  }
  function showBoard(b) {
    if (!b) return;
    select(null);
    focusBoard = b;
    flyToBoard(b);
    renderSrPanel();
  }
  function cardInfo(act) {
    const a = act.agent, team = ((state && state.teams) || []).find((t) => t.id === a.team);
    const ap = ((state && state.approvals) || []).find((x) => x.agent === a.id);
    return {
      id: a.id, name: a.name || a.id, manager: act.isManager, color: act.color, status: statusText(act), statusKey: statusKey(act),
      where: act.where(), role: a.role || '—', team: team ? (team.name || team.id) + (a.team_role ? ' · ' + a.team_role : '') : 'Không team',
      task: a.task || '—', tool: a.tool || '—', run: a.run_id || '—',
      approval: ap ? (ap.tool || '?') + (ap.preview ? ' — ' + ap.preview : '') : null,
      actions: cardActions(act),
    };
  }
  function cardActions(act) {
    const out = [];
    const ap = ((state && state.approvals) || []).find((x) => x.agent === act.id);
    const q = ((state && state.questions) || []).find((x) => x.agent === act.id);
    if (ap) out.push({ key: 'ap:' + ap.id, label: 'Duyệt…', kind: 'danger' });
    if (q) out.push({ key: 'q:' + q.id, label: 'Trả lời…', kind: 'ask' });
    if (act.isManager) out.push({ key: 'chat:', label: 'Chat', kind: 'primary' });
    return out;
  }

  // ------------------------------------------------------------ chuột: nhân vật / bảng / sàn trống
  const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
  ray.camera = camera;
  let down = null, hoverId = null;
  function pick(ev) {
    const r = cvs.getBoundingClientRect();
    ndc.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, camera);
    const objs = [];
    if (card.sprite.visible) objs.push(card.sprite);
    for (const ts of tags.values()) if (ts.sprite.visible) objs.push(ts.sprite);
    objs.push(...[...actors.values()].filter((a) => !a.puppet).map((a) => a.chibi.group)); // actor bị ẩn (điều phối viên khi đi bộ): không bấm được
    // chế độ đi bộ: cửa ra KHÔNG bấm được (bấm/chạm cửa chỉ là bấm sàn) — hộp thoại thoát chỉ mở khi bấm E chủ động
    if (world) objs.push(...world.focusables, ...(walk.active ? [] : world.exitMeshes || []), chat.panel.mesh);
    for (const h of ray.intersectObjects(objs, true)) {
      let o = h.object;
      if (o === card.sprite) {
        const px = h.uv && { x: h.uv.x * card.canvas.width, y: (1 - h.uv.y) * card.canvas.height };
        const b = px && (card.hits || []).find((r) => px.x >= r.x && px.x <= r.x + r.w && px.y >= r.y && px.y <= r.y + r.h);
        if (b) return { cardAct: b.key };
        continue; // thẻ chỉ để đọc — bấm xuyên qua
      }
      if (o.userData.approvalId) return { approval: o.userData.approvalId };
      if (o.userData.exit) return { exit: true };
      if (o.userData.chat) return { chat: true };
      if (o.userData.focus) {
        const b = o.userData.focus.board;
        if (b.hits && h.uv) {
          const px = { x: h.uv.x * b.canvas.width, y: (1 - h.uv.y) * b.canvas.height };
          const it = b.hits.find((r) => px.x >= r.x && px.x <= r.x + r.w && px.y >= r.y && px.y <= r.y + r.h);
          if (it) return it.kind === 'approval' ? { approval: it.id } : { question: it.id };
        }
        return { board: b };
      }
      while (o && !o.userData.actorId) o = o.parent;
      if (o) return { actor: o.userData.actorId };
    }
    return null;
  }
  if (renderer) {
    cvs.addEventListener('pointerdown', (e) => { kick(); down = { x: e.clientX, y: e.clientY, lx: e.clientX, ly: e.clientY, drag: false }; });
    // chế độ đi bộ: kéo chuột / kéo tay = xoay hướng nhìn (OrbitControls đang tắt)
    cvs.addEventListener('pointermove', (e) => {
      if (down || e.pointerType !== 'mouse') kick(); // đang kéo / chạm: vẽ theo màn hình
      if (!down || !walk.active || modals.open || chat.isOpen || talk.isOpen) return;
      if (!down.drag && Math.hypot(e.clientX - down.x, e.clientY - down.y) <= 5) return;
      down.drag = true;
      walk.look(e.clientX - down.lx, e.clientY - down.ly);
      down.lx = e.clientX; down.ly = e.clientY;
    });
    cvs.addEventListener('pointerup', (e) => {
      if (!down || down.drag || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) { down = null; return; }
      down = null;
      // bảng modal đang mở: chỉ bảng trên cùng nhận chuột (bấm ra ngoài không làm gì — tránh bấm nhầm)
      if (modals.open) { const uv = panelUV(e, modals.top); if (uv) modals.top.click(uv); return; }
      if (talk.isOpen) { const uv = panelUV(e, talk.panel); if (uv) talk.panel.click(uv); else talk.close(); return; }
      if (chat.isOpen) {
        const uv = panelUV(e, chat.panel);
        if (uv) { chat.panel.click(uv); return; }
        chat.close(); return;
      }
      const p = pick(e);
      if (p && p.cardAct) { const [k, id] = p.cardAct.split(':'); doAct(k, id); }
      else if (p && p.approval) flows.openApproval(p.approval);
      else if (p && p.question) flows.openQuestion(p.question);
      else if (p && p.exit) flows.openLogout();
      else if (p && p.chat) chat.open();
      else if (p && p.actor) { autoSel = false; select(p.actor, !walk.active); }
      else if (p && p.board) showBoard(p.board);
      else if (walk.active && !focusBoard) {
        // chế độ đi bộ: bấm/chạm sàn → avatar tự đi tới (A*); reduced-motion → dịch chuyển tức thời
        const h = world && ray.intersectObject(world.floor, false)[0];
        if (h) walk.goTo(h.point.x, h.point.z, reduced);
      } else if (selected || focusBoard) toOverview(); // bấm sàn trống → về toàn cảnh
    });
    // con lăn trên bảng: cuộn bảng, không phóng to cảnh (bắt ở pha capture trước OrbitControls)
    host.addEventListener('wheel', (e) => {
      kick();
      const pnl = modals.open ? modals.top : talk.isOpen ? talk.panel : chat.isOpen ? chat.panel : null;
      if (!pnl) { if (walk.active) { e.preventDefault(); e.stopPropagation(); if (!focusBoard) walk.zoom(e.deltaY); } return; }
      e.preventDefault(); e.stopPropagation();
      const uv = panelUV(e, pnl);
      if (uv) pnl.wheel(uv, e.deltaY);
    }, { capture: true, passive: false });
    let hoverAt = 0;
    cvs.addEventListener('pointermove', (e) => {
      if (down || e.pointerType !== 'mouse') return;
      const n = performance.now();
      if (n - hoverAt < 120) return;
      hoverAt = n;
      const pnl = modals.open ? modals.top : talk.isOpen ? talk.panel : chat.isOpen ? chat.panel : null;
      if (pnl) { const uv = panelUV(e, pnl); cvs.style.cursor = pnl.hover(uv) ? 'pointer' : ''; return; }
      const p = pick(e);
      cvs.style.cursor = p ? 'pointer' : '';
      hoverId = (p && p.actor) || null; // rê chuột lên agent → luôn hiện tên
    });
    cvs.addEventListener('pointerleave', () => { hoverId = null; });
  }
  function panelUV(ev, pnl) {
    const r = cvs.getBoundingClientRect();
    ndc.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, camera);
    const h = ray.intersectObject(pnl.mesh, false)[0];
    return h && h.uv ? h.uv : null;
  }
  const typing = (t) => t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
  document.addEventListener('keydown', (e) => {
    kick();
    if (e.isComposing) return;
    // ngồi bàn ra lệnh: E (khi không gõ chữ trong ô chat) cũng đứng dậy — Esc đóng bàn điều khiển → đứng dậy (onOpenChange)
    if (walk.active && walk.seated && e.code === 'KeyE' && !e.repeat && !modals.open && !typing(e.target) && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault(); if (chat.isOpen) chat.close(); else if (walk.stand()) walk.resume(reduced); return;
    }
    if (walk.active && e.key !== 'Escape') {
      // phím đi bộ: chỉ khi không có bảng/chat mở và không đang gõ chữ (ô chat ẩn, ô ghi chú)
      if (modals.open || chat.isOpen || talk.isOpen || typing(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
      // preventDefault cả khi focus đang ở nút/link (vd. vừa bấm 🚶): mũi tên không cuộn trang, E/V không kích hoạt gì khác
      if (walk.key(e.code, true, e.shiftKey)) { e.preventDefault(); if (focusBoard) toOverview(); return; }
      if (e.code === 'KeyV') { e.preventDefault(); if (!e.repeat) { walk.togglePov(); renderSrPanel(); } return; }
      if (e.code === 'KeyE') { e.preventDefault(); if (!e.repeat) walkInteract(); return; }
      return;
    }
    if (e.key !== 'Escape') return;
    if (modals.open) { e.preventDefault(); const t = modals.top; if (t.opts.onEscape) t.opts.onEscape(); return; }
    if (talk.isOpen) { e.preventDefault(); talk.close(); return; }
    if (chat.isOpen) { e.preventDefault(); chat.close(); return; }
    if (typing(e.target)) return;
    if (walk.active && walk.seated) { if (walk.stand()) walk.resume(reduced); return; }
    // chế độ đi bộ: Esc đóng thẻ/bảng trước, lần sau mới ra toàn cảnh
    if (walk.active && !selected && !focusBoard) { setWalk(false); return; }
    toOverview();
  });
  document.addEventListener('keyup', (e) => { walk.key(e.code, false, e.shiftKey); });
  window.addEventListener('blur', () => { walk.keys.clear(); });

  // ------------------------------------------------------------ kích thước
  function resize() {
    if (!renderer) return;
    const w = Math.max(1, host.clientWidth), h = Math.max(1, host.clientHeight);
    renderer.setSize(w, h, false);
    camera.aspect = w / h; camera.updateProjectionMatrix();
    modals.fit(); fitToast(); fitHint(); chat.fitOverlay(); talk.fit();
    if (world) fitCamera();
  }
  if (window.ResizeObserver) new ResizeObserver(resize).observe(host); else window.addEventListener('resize', resize);
  resize();

  // ------------------------------------------------------------ biển trạng thái (hết phiên / mất kết nối) trong cảnh
  function syncSign() {
    const kind = conn === 'expired' ? 'expired' : conn === 'lost' && lostAt && performance.now() - lostAt > 3000 ? 'lost' : null;
    if (!kind) { sign.sprite.visible = false; signStand.visible = false; return; }
    drawSign(sign, kind);
    sign.sprite.visible = !!renderer;
    if (world) sign.sprite.position.set(world.W / 2, 4.2, world.D / 2);
    else {
      sign.sprite.position.set(0, 0, 0);
      signStand.visible = true;
      if (!tween) { camera.position.set(0, 3, 15); controls.target.set(0, -0.8, 0); }
    }
  }
  function setConn(c) {
    if (conn === 'expired') return;
    if (c === 'lost' && conn !== 'lost') lostAt = performance.now();
    if (c !== 'lost') lostAt = 0;
    conn = c;
    if (expiredEl) expiredEl.hidden = c !== 'expired';
    syncSign(); renderSrHud();
    if (world && state) drawHud(world.boards.hud, { hub: state.hub, task_counts: state.task_counts, conn });
  }

  // Hạ chất lượng (tự động khi FPS thấp): đổi tier vật liệu, tắt bóng/env, pixelRatio 1, dựng lại World theo đường layoutKey sẵn có.
  function setQuality(t) {
    if (!renderer || t === tier) return;
    tier = t;
    setTier(t, { aniso: renderer.capabilities.getMaxAnisotropy() });
    setChibiQuality(t);
    if (t === 'low') { try { sessionStorage.setItem('cfh_office_q', 'low'); } catch (_) { /* bỏ qua */ } }
    renderer.setPixelRatio(pixelRatio()); resize();
    renderer.shadowMap.enabled = t === 'high';
    applyEnv();
    wkey = ''; if (state) applyState(state);
    scene.traverse((o) => { if (o.material) [].concat(o.material).forEach((m) => { m.needsUpdate = true; }); });
  }
  // fog: toàn cảnh → nhà rõ, nền đất tan vào nền trang; đi bộ / nhìn gần → không mờ gì trong nhà
  function syncFog() {
    const cd = walk.active ? 0 : camera.position.distanceTo(controls.target), k = Math.min(1, Math.max(0, (cd - 15) / 25));
    scene.fog.near = 40 + (cd * 1.05 - 40) * k; scene.fog.far = 140 + (cd * 2.4 - 140) * k;
    if (world) world.setLampsVisible(walk.active || cd < 26);
  }
  let dbgAt = 0, dbgFrames = 0, shadowFrames = 0;
  // đo hiệu năng chỉ khi gỡ lỗi (#perf3d / #uncap3d — office/perf.js); không bật thì perf = null
  const pf = perfFlags(), perf = createPerf(renderer, () => ({ tier, actors: actors.size, walk: walk.active }));
  // ------------------------------------------------------------ vòng vẽ (động ≈ 60 FPS / yên 30 FPS, dừng khi ẩn)
  let raf = 0, last = 0, lastDraw = 0, fpsFrames = 0, fpsStart = 0, uiAt = 0, prevRaf = 0, prevGap = 0, slowWins = 0, lowWins = 0;
  let gapEst = 1000 / 60, activeUntil = 0;
  const tmp = new THREE.Vector3(), camWas = new THREE.Matrix4();
  // có tương tác (chuột/chạm/lăn/phím) → vẽ theo màn hình trong ACTIVE_HOLD ms tới
  function kick() { activeUntil = performance.now() + ACTIVE_HOLD; }
  // cảnh "đang động" theo nghĩa người dùng nhìn thấy: camera dời/xoay (kể cả OrbitControls còn trôi), tween, đi bộ đang bước
  const moving = () => !!tween || (walk.active && (walk.keys.size > 0 || walk.path.length > 0)) || !!down;
  function frame(ts) {
    raf = requestAnimationFrame(frame);
    // một lần khựng > 1 s sau khung hình bình thường (tab bị che/điều tiết, máy ngủ, GC, devtools…) không phải "máy chậm":
    // bỏ cửa sổ đo đang dở, đo lại. Khung nào cũng > 1 s (chậm thật) thì vẫn đo bình thường → fallback.
    const gap = prevRaf ? ts - prevRaf : 0;
    // cửa sổ không có focus (đang ở app khác): Chrome/Windows điều tiết rAF xuống ~1 FPS dù tab vẫn "visible" → không đo
    if ((gap > 1000 && prevGap < 250) || !document.hasFocus()) { fpsStart = ts + 1000; fpsFrames = 0; slowWins = 0; lowWins = 0; }
    prevRaf = ts; prevGap = gap;
    if (gap > 0 && gap < 100) gapEst += (gap - gapEst) * 0.1; // khoảng rAF (≈ tần số màn hình)
    // đo FPS theo nhịp rAF (cả khung bỏ qua vì đứng yên): máy theo kịp → rAF bằng tần số màn hình; vẽ nặng → rAF chậm theo.
    // Không phụ thuộc giới hạn khung (trước: đếm khung đã vẽ, cap 30 → mốc < 24 sát nút, rAF lệch nhịp là tụt).
    // Bỏ 2 s đầu, cửa sổ 5 s; chỉ tắt 3D khi CHẬM LIÊN TỤC 3 cửa sổ liền (15 s), một lần giật không tính
    if (!fpsStart) fpsStart = ts + 2000;
    if (ts >= fpsStart) {
      fpsFrames++;
      if (ts - fpsStart >= 5000) {
        const fps = fpsFrames / ((ts - fpsStart) / 1000);
        fpsFrames = 0; fpsStart = ts;
        if (tier === 'high') { lowWins = fps < 24 ? lowWins + 1 : 0; if (lowWins >= 2) { lowWins = 0; slowWins = 0; setQuality('low'); fpsStart = 0; fpsFrames = 0; return; } }
        slowWins = fps < 10 ? slowWins + 1 : 0;
        if (slowWins >= 3) { disableRender('slow'); return; }
      }
    }
    if (moving()) activeUntil = Math.max(activeUntil, ts + ACTIVE_HOLD);
    if (!pf.uncap && lastDraw && !frameGate(ts, lastDraw, gapEst, ts < activeUntil)) { if (perf) perf.skip(); return; }
    if (perf) perf.begin(ts);
    const dt = Math.min(0.1, last ? (ts - last) / 1000 : 0.016);
    last = ts; lastDraw = ts;
    const now = perfNow();
    const lf = walk.active ? 1 : Math.min(2.6, Math.max(1, camera.position.distanceTo(controls.target) / 20));
    if (world) {
      world.setLabelScale(lf);
      const quiet = !!focusBoard || chat.isOpen || modals.open || talk.isOpen;
      world.setSignsVisible(!quiet);
      world.update(dt, now, reduced);
      // KHÔNG có 2 điều phối viên: khi đi bộ, actor điều phối viên AI nhường chỗ cho avatar người dùng (ẩn, giữ trạng thái/bong bóng)
      const coord = walk.active && walk.isCoordinator ? coordinator() : null;
      for (const a of actors.values()) { a.puppet = a === coord; a.chibi.labelScale = lf; a.update(dt, now, now, reduced, world.estop); }
      if (walk.active) walk.setStatus(coord ? coord.bubble : null);
      syncLabels(quiet);
    }
    hint.sprite.visible = hintShown(ui());
    // camera bay
    if (tween) {
      tween.k = Math.min(1, tween.k + dt / 1.0);
      const e = tween.k < 0.5 ? 4 * tween.k ** 3 : 1 - (-2 * tween.k + 2) ** 3 / 2;
      camera.position.lerpVectors(tween.p0, tween.p1, e);
      controls.target.lerpVectors(tween.q0, tween.q1, e);
      if (tween.k >= 1) tween = null;
    } else if (!walk.active && selected && actors.get(selected) && actors.get(selected).pos) {
      // bám theo nhân vật đang đi
      tmp.copy(charTarget(actors.get(selected))).sub(controls.target).multiplyScalar(reduced ? 1 : Math.min(1, dt * 2.5));
      controls.target.add(tmp); camera.position.add(tmp);
    }
    if (walk.active && world) {
      // đi bộ: avatar luôn cập nhật; camera do walker lái trừ khi đang xem bảng / chat / bay camera (khi đó nhìn controls.target)
      walk.update(dt, now, reduced);
      const yieldCam = !!tween || !!focusBoard || (chat.isOpen && !walk.seated);
      if (yieldCam) { camera.lookAt(controls.target); world.fade(new Set()); }
      else { if (walkYield) walk.resume(reduced); walk.updateCamera(dt, reduced); }
      walkYield = yieldCam;
      if (now - nearAt > 0.2) { nearAt = now; walkProximity(now); }
    } else {
      clampTarget();
      controls.dampingFactor = 1 - Math.pow(1 - DAMP_60, dt * 60);
      controls.update();
    }
    // thẻ nhân vật cạnh nhân vật (phía phải theo camera)
    const act = selected && actors.get(selected);
    if (act && act.pos) {
      drawCard(card, cardInfo(act));
      card.sprite.position.copy(cardPos(act));
    }
    for (const [id, ts2] of tags) {
      const a = actors.get(id), ap2 = a && a.puppet && walk.active ? walk.pos : a && a.pos; // thẻ đỏ của điều phối viên theo avatar
      if (!ap2) { ts2.sprite.visible = false; continue; }
      ts2.sprite.visible = true;
      scaleLabel(ts2.sprite, Math.min(lf, 2));
      ts2.sprite.position.set(ap2.x, 2.55 + (lf - 1) * 0.5, ap2.z);
    }
    if (now - uiAt > 1) {
      uiAt = now;
      if (world && state) drawRuns(world.boards.runs, state.runs || [], names, Date.now());
      if (world) world.setDaylight(hourNow());
      renderList(); renderSrPanel(); syncSign();
    }
    modals.update();
    chat.update();
    talk.update();
    if (toastTs.sprite.visible && performance.now() > toastUntil) toastTs.sprite.visible = false;
    syncFog();
    if (world) {
      scene.environmentIntensity = world.envInt;
      // bóng tĩnh: vẽ lại shadow map vài khung sau khi World báo đổi (khung đầu sau khi dựng có thể chưa đủ — đo thực tế)
      // đi bộ (tier high): shadow camera thu nhỏ quanh avatar → bóng nét khi nhìn gần; tâm dời xa mới vẽ lại 1 khung
      if (tier === 'high') { if (walk.active && walk.pos) world.setShadowFocus(walk.pos.x, walk.pos.z, walk.view); else world.setShadowFocus(null); }
      if (world.shadowDirty) { shadowFrames = 3; world.shadowDirty = false; }
      if (world.shadowRefocus) { shadowFrames = Math.max(shadowFrames, 1); world.shadowRefocus = false; }
      if (shadowFrames > 0) { shadowFrames--; renderer.shadowMap.needsUpdate = true; }
    }
    camera.updateMatrixWorld();
    updateChibiLod(camera, Math.max(1, host.clientHeight)); // chibi nhỏ trên màn hình → hình ít tam giác
    if (perf) perf.gpuBegin();
    renderer.render(scene, camera);
    if (perf) { perf.gpuEnd(); perf.end(); }
    // camera vừa dời/xoay (OrbitControls còn trôi, bám nhân vật, walker lerp…) → giữ nhịp vẽ theo màn hình
    const cm = camera.matrixWorld.elements, cw = camWas.elements;
    for (let i = 0; i < 16; i++) if (Math.abs(cm[i] - cw[i]) > 1e-5) { activeUntil = ts + ACTIVE_HOLD; camWas.copy(camera.matrixWorld); break; }
    // gỡ lỗi cho QA: #debug3d → mỗi 2 s ghi số draw call / tam giác / FPS / tier vào data-office3d (không log, không chữ agent)
    dbgFrames++;
    if (ts - dbgAt >= 2000) {
      if (location.hash.indexOf('debug3d') >= 0) {
        const ri = renderer.info.render;
        document.documentElement.dataset.office3d = JSON.stringify({ calls: ri.calls, triangles: ri.triangles, fps: Math.round((dbgFrames * 10000) / (ts - dbgAt)) / 10, tier });
      }
      dbgAt = ts; dbgFrames = 0;
    }
  }
  // Nhãn tên agent: không để chồng thành một đám (cafe, cụm bàn đông). Luôn hiện: agent đang chọn / rê chuột; đi bộ: chỉ agent
  // trong bán kính 7 m; còn lại tránh chồng trên màn hình (declutter) — ưu tiên điều phối viên, agent có approval, gần camera,
  // nhãn đang hiện (đỡ nhấp nháy khi agent đi lại).
  const lv = new THREE.Vector3();
  function syncLabels(quiet) {
    const sh = Math.max(1, host.clientHeight), sw = Math.max(1, host.clientWidth);
    const ppu = sh / (2 * Math.tan((camera.fov * Math.PI) / 360)); // pixel trên 1 m ở khoảng cách 1 m
    const aps = new Set(((state && state.approvals) || []).map((x) => x.agent));
    const items = [];
    for (const a of actors.values()) {
      const L = a.chibi.label;
      if (!L) continue;
      if (quiet || !a.pos || a.puppet) { L.visible = false; continue; }
      const must = a.id === selected || a.id === hoverId;
      if (!must && walk.active && walk.pos && Math.hypot(a.pos.x - walk.pos.x, a.pos.z - walk.pos.z) > 7) { L.visible = false; continue; }
      L.getWorldPosition(lv);
      const dist = Math.max(0.1, lv.distanceTo(camera.position));
      lv.project(camera);
      if (lv.z > 1 || Math.abs(lv.x) > 1.2 || Math.abs(lv.y) > 1.2) { L.visible = false; continue; }
      const k = ppu / dist;
      items.push({ id: a.id, L, x: (lv.x + 1) * sw / 2, y: (1 - lv.y) * sh / 2, w: L.scale.x * k * 0.8, h: L.scale.y * k * 0.75,
        pri: (must ? 100 : 0) + (a.isManager ? 20 : 0) + (aps.has(a.id) ? 10 : 0) + (L.visible ? 2 : 0) - dist * 0.01 });
    }
    const keep = declutter(items);
    for (const it of items) it.L.visible = keep.has(it.id);
  }
  function clampTarget() {
    if (!world) return;
    const t = controls.target;
    const cx = Math.min(world.W, Math.max(0, t.x)), cz = Math.min(world.D, Math.max(0, t.z)), cy = Math.min(5, Math.max(0, t.y));
    if (cx !== t.x || cz !== t.z || cy !== t.y) {
      tmp.set(cx - t.x, cy - t.y, cz - t.z);
      t.add(tmp); camera.position.add(tmp);
    }
  }
  function start() { if (!raf && !stopped && !noRender && !ctxLost && renderer) { last = 0; lastDraw = 0; fpsStart = 0; fpsFrames = 0; prevRaf = 0; prevGap = 0; slowWins = 0; lowWins = 0; raf = requestAnimationFrame(frame); } }
  function pause() { if (raf) cancelAnimationFrame(raf); raf = 0; }
  document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); else start(); });

  // Quá chậm / không WebGL: ngừng vẽ (thông báo DOM tối thiểu) nhưng vẫn cập nhật danh sách sr-only.
  let slowT = 0, ctxLost = false, lostT = 0;
  // Mất WebGL context (GPU reset, driver, quá nhiều context…): KHÔNG tắt ngay — chờ trình duyệt khôi phục (three.js tự dựng lại),
  // chỉ khi 5 s không khôi phục mới về fallback.
  cvs.addEventListener('webglcontextlost', (e) => {
    if (noRender) return; // do chính disableRender() gọi forceContextLoss
    e.preventDefault(); // cho phép khôi phục
    ctxLost = true; pause();
    clearTimeout(lostT);
    lostT = setTimeout(() => { if (ctxLost) disableRender('nogl'); }, 5000);
  });
  cvs.addEventListener('webglcontextrestored', () => {
    ctxLost = false; clearTimeout(lostT);
    if (envTex) { envTex.dispose(); envTex = null; }
    applyEnv(); shadowFrames = 3;
    if (!document.hidden) start();
  });
  function disableRender(reason) {
    if (noRender) return;
    noRender = true; pause();
    const msg = fallback && fallback.querySelector('[data-reason="' + (reason || 'nogl') + '"]');
    if (msg) for (const p of fallback.querySelectorAll('[data-reason]')) p.hidden = p !== msg;
    if (walkBtn) walkBtn.hidden = true;
    if (povBtn) povBtn.hidden = true;
    if (fallback) fallback.hidden = false;
    host.hidden = true;
    if (!slowT) slowT = setInterval(() => { if (!stopped) { renderList(); renderSrPanel(); } }, 1000);
    // chậm (thường do CPU/GPU đang bận việc khác): GIỮ renderer để người dùng bấm "Tiếp tục 3D"; chỉ huỷ khi thật sự không có WebGL
    if (reason !== 'slow' && renderer) { renderer.dispose(); renderer.forceContextLoss(); }
  }
  function resumeRender() {
    if (!noRender || !renderer || ctxLost) return;
    noRender = false;
    clearInterval(slowT); slowT = 0;
    if (fallback) fallback.hidden = true;
    host.hidden = false;
    if (walkBtn && world) walkBtn.hidden = false;
    if (povBtn) povBtn.hidden = !walk.active;
    start();
  }
  const resumeBtn = $('office-resume');
  if (resumeBtn) resumeBtn.addEventListener('click', resumeRender);
  function stopAll() {
    stopped = true;
    if (es) { es.close(); es = null; }
    clearTimeout(refetchT); clearTimeout(retryT); clearInterval(slowT);
  }

  // ------------------------------------------------------------ dữ liệu: state + SSE
  let es = null, refetchT = 0, retryT = 0, backoff = 1000;
  async function fetchState() {
    let r;
    try {
      r = await fetch(stateUrl, { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
    } catch (_) { return 'error'; }
    if (r.status === 401) { onExpired(); return 'expired'; }
    if (!r.ok) return 'error';
    let s;
    try { s = await r.json(); } catch (_) { return 'error'; }
    if (stopped) return 'stopped';
    applyState(s);
    return 'ok';
  }
  function onExpired() {
    talk.close();
    setConn('expired');
    select(null);
    stopAll(); // vẫn vẽ cảnh (biển "hết phiên"), chỉ ngừng dữ liệu
  }
  function scheduleRefetch() {
    clearTimeout(refetchT);
    refetchT = setTimeout(() => { fetchState(); }, 500);
  }
  function connect() {
    if (stopped || !state) return;
    if (es) es.close();
    const q = 'since=' + encodeURIComponent(state.last_seq == null ? '' : state.last_seq) + '&epoch=' + encodeURIComponent(state.epoch || '');
    es = new EventSource(sseUrl + (sseUrl.indexOf('?') >= 0 ? '&' : '?') + q, { withCredentials: true });
    es.onopen = () => { setConn('live'); backoff = 1000; };
    es.onmessage = (m) => {
      let f;
      try { f = JSON.parse(m.data); } catch (_) { return; }
      if (!f || typeof f !== 'object') return;
      if (f.t === 'hello') setConn('live');
      else if (f.t === 'ev') onEvent(f);
      else if (f.t === 'refresh') scheduleRefetch();
      else if (f.t === 'end') { es.close(); es = null; reconnect(0); }
    };
    es.onerror = () => {
      if (es) { es.close(); es = null; }
      setConn('lost');
      reconnect(backoff);
      backoff = Math.min(15000, backoff * 2);
    };
  }
  // Kết nối lại: tải lại state trước (bắt 401, lấy since/epoch mới) rồi mở SSE.
  function reconnect(delay) {
    clearTimeout(retryT);
    retryT = setTimeout(async () => {
      const r = await fetchState();
      if (r === 'ok') connect();
      else if (r === 'error') { setConn('lost'); reconnect(Math.min(15000, (delay || 1000) * 2)); }
    }, delay);
  }

  function onEvent(f) {
    if (typeof f.seq === 'number' && state && (state.last_seq == null || f.seq > state.last_seq)) state.last_seq = f.seq;
    const now = perfNow();
    switch (f.type) {
      case 'hub.estop': {
        const on = f.status !== 'off';
        if (state && state.hub) state.hub.estop = on;
        if (world) { world.setEstop(on); drawBoards(); }
        renderSrHud();
        return;
      }
      case 'team.estop': {
        const on = f.status !== 'off';
        teamEstop.set(f.team || '', on);
        if (world) world.setTeamEstop(f.team || '', on);
        return;
      }
      case 'schedule.fired': if (world) world.flashClock(); break;
      default: break;
    }
    const act = f.agent && actors.get(f.agent);
    if (act) act.event(f, now);
    if (f.type === 'manager.action' && !act && state && state.manager_id && actors.get(state.manager_id)) actors.get(state.manager_id).event(f, now);
  }

  (async () => {
    if (renderer) start(); else disableRender('nogl');
    const r = await fetchState();
    if (r === 'ok') connect();
    else if (r === 'error') { setConn('lost'); reconnect(2000); }
  })();
}

main();
