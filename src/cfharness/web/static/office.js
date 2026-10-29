/* cfharness web — chế độ "Văn phòng 3D" (/office). Chế độ 3D = TOÀN TRANG 3D: trang độc lập, thấy được chỉ có canvas + nút "2D".
 * Mọi thông tin vẽ TRONG cảnh: màn hình HUD trên tường, kanban, màn hình phòng server, bảng "Cần bạn", thẻ nhân vật, thẻ đỏ cần duyệt,
 * biển hết phiên/mất kết nối. Danh sách chữ chỉ cho trình đọc màn hình (sr-only). Cảnh 3D không có link sang trang 2D.
 * Dữ liệu: GET data-state (JSON, chữ tự do đã cắt ở server) + SSE data-sse (khung mỏng: chỉ id/tên tool/trạng thái).
 * Chữ của agent là DỮ LIỆU: trong DOM chỉ textContent, trên cảnh chỉ fillText. Không innerHTML. Hợp đồng: docs/dev-notes/office-3d.md.
 * Giai đoạn 2: duyệt / trả lời câu hỏi / chat với điều phối viên / đăng xuất NGAY TRONG CẢNH (office/flows.js, office/chat.js, office/ui.js),
 * cùng cổng ghi với web 2D (CSRF từ data-csrf + khoá phiên `k` trong localStorage — không bao giờ vẽ/in/log).
 * Hiệu năng: ≤ 30 FPS, dừng khi tab ẩn, pixelRatio ≤ 1,5; không có WebGL hoặc FPS TB < 10 trong 5 s → #office-fallback. */
import * as THREE from './vendor/three-0.169.0/three.module.min.js';
import { OrbitControls } from './vendor/three-0.169.0/OrbitControls.js';
import { World, layoutKey } from './office/world.js';
import { Actor, STATUS_VI } from './office/actors.js';
import { NO_TEAM_COLOR, scaleLabel } from './office/util.js';
import { drawHud, drawKanban, drawRuns, drawNeeds, drawCard, drawApprovalTag, drawSign, TextSprite, fmtUsd } from './office/boards.js';
import { makeApi, Modals, createFlows } from './office/flows.js';
import { createChat } from './office/chat.js';
import { FONT, roundRect } from './office/util.js';

const $ = (id) => document.getElementById(id);
const KCOL_VI = { ready: 'Sẵn sàng', running: 'Đang làm', review: 'Review', blocked: 'Kẹt' };

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
  if (renderer) {
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    host.appendChild(cvs);
    cvs.setAttribute('aria-hidden', 'true');
  }

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(36, 1, 0.3, 400);
  camera.position.set(0, 4, 16);
  scene.add(camera); // bảng modal + thông báo gắn vào camera
  const controls = new OrbitControls(camera, cvs);
  controls.enableDamping = true; controls.dampingFactor = 0.08;
  controls.minPolarAngle = 0.2; controls.maxPolarAngle = 1.4;
  controls.minDistance = 3; controls.maxDistance = 70;
  controls.screenSpacePanning = false;

  function applyTheme() {
    const dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    scene.background = new THREE.Color(dark ? '#262624' : '#faf9f5');
  }
  applyTheme();
  const mqDark = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  if (mqDark && mqDark.addEventListener) mqDark.addEventListener('change', applyTheme);

  let world = null, wkey = '', state = null, stopped = false;
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
      } } else if (overview) { flyTo(overview.pos, overview.target); }
      renderSrPanel();
    },
  });
  function syncControls() { controls.enabled = !modals.open && !chat.isOpen; }
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
    if (overview) flyTo(overview.pos, overview.target);
    renderSrPanel();
  }

  // ------------------------------------------------------------ áp dữ liệu /office/state
  function teamColorOf(a) {
    if (a.is_manager) return '#c8413a';
    return (world && world.teamColor.get(a.team && world.teamColor.has(a.team) ? a.team : '')) || NO_TEAM_COLOR;
  }
  const teamEstop = new Map();

  function applyState(s) {
    state = s;
    const key = layoutKey(s);
    if (key !== wkey || !world) {
      const first = !world;
      if (world) world.dispose();
      world = new World(s);
      scene.add(world.group);
      wkey = key;
      for (const a of actors.values()) a.attach(world);
      chat.mount(world);
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
  let down = null;
  function pick(ev) {
    const r = cvs.getBoundingClientRect();
    ndc.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, camera);
    const objs = [];
    if (card.sprite.visible) objs.push(card.sprite);
    for (const ts of tags.values()) if (ts.sprite.visible) objs.push(ts.sprite);
    objs.push(...[...actors.values()].map((a) => a.chibi.group));
    if (world) objs.push(...world.focusables, ...(world.exitMeshes || []), chat.panel.mesh);
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
    cvs.addEventListener('pointerdown', (e) => { down = { x: e.clientX, y: e.clientY }; });
    cvs.addEventListener('pointerup', (e) => {
      if (!down || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) { down = null; return; }
      down = null;
      // bảng modal đang mở: chỉ bảng trên cùng nhận chuột (bấm ra ngoài không làm gì — tránh bấm nhầm)
      if (modals.open) { const uv = panelUV(e, modals.top); if (uv) modals.top.click(uv); return; }
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
      else if (p && p.actor) select(p.actor);
      else if (p && p.board) showBoard(p.board);
      else if (selected || focusBoard) toOverview(); // bấm sàn trống → về toàn cảnh
    });
    // con lăn trên bảng: cuộn bảng, không phóng to cảnh (bắt ở pha capture trước OrbitControls)
    host.addEventListener('wheel', (e) => {
      const pnl = modals.open ? modals.top : chat.isOpen ? chat.panel : null;
      if (!pnl) return;
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
      const pnl = modals.open ? modals.top : chat.isOpen ? chat.panel : null;
      if (pnl) { const uv = panelUV(e, pnl); cvs.style.cursor = pnl.hover(uv) ? 'pointer' : ''; return; }
      cvs.style.cursor = pick(e) ? 'pointer' : '';
    });
  }
  function panelUV(ev, pnl) {
    const r = cvs.getBoundingClientRect();
    ndc.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, camera);
    const h = ray.intersectObject(pnl.mesh, false)[0];
    return h && h.uv ? h.uv : null;
  }
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || e.isComposing) return;
    if (modals.open) { e.preventDefault(); const t = modals.top; if (t.opts.onEscape) t.opts.onEscape(); return; }
    if (chat.isOpen) { e.preventDefault(); chat.close(); return; }
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    toOverview();
  });

  // ------------------------------------------------------------ kích thước
  function resize() {
    if (!renderer) return;
    const w = Math.max(1, host.clientWidth), h = Math.max(1, host.clientHeight);
    renderer.setSize(w, h, false);
    camera.aspect = w / h; camera.updateProjectionMatrix();
    modals.fit(); fitToast(); chat.fitOverlay();
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

  // ------------------------------------------------------------ vòng vẽ (≤ 30 FPS, dừng khi ẩn)
  let raf = 0, last = 0, lastDraw = 0, fpsFrames = 0, fpsStart = 0, uiAt = 0;
  const tmp = new THREE.Vector3();
  function frame(ts) {
    raf = requestAnimationFrame(frame);
    if (ts - lastDraw < 1000 / 30 - 3) return;
    const dt = Math.min(0.1, last ? (ts - last) / 1000 : 0.016);
    last = ts; lastDraw = ts;
    const now = perfNow();
    const lf = Math.min(2.6, Math.max(1, camera.position.distanceTo(controls.target) / 20));
    if (world) {
      world.setLabelScale(lf);
      const quiet = !!focusBoard || chat.isOpen || modals.open;
      world.setSignsVisible(!quiet);
      for (const a of actors.values()) a.chibi.label.visible = !quiet;
      world.update(dt, now, reduced);
      for (const a of actors.values()) { a.chibi.labelScale = lf; a.update(dt, now, now, reduced, world.estop); }
    }
    // camera bay
    if (tween) {
      tween.k = Math.min(1, tween.k + dt / 1.0);
      const e = tween.k < 0.5 ? 4 * tween.k ** 3 : 1 - (-2 * tween.k + 2) ** 3 / 2;
      camera.position.lerpVectors(tween.p0, tween.p1, e);
      controls.target.lerpVectors(tween.q0, tween.q1, e);
      if (tween.k >= 1) tween = null;
    } else if (selected && actors.get(selected) && actors.get(selected).pos) {
      // bám theo nhân vật đang đi
      tmp.copy(charTarget(actors.get(selected))).sub(controls.target).multiplyScalar(reduced ? 1 : Math.min(1, dt * 2.5));
      controls.target.add(tmp); camera.position.add(tmp);
    }
    clampTarget();
    controls.update();
    // thẻ nhân vật cạnh nhân vật (phía phải theo camera)
    const act = selected && actors.get(selected);
    if (act && act.pos) {
      drawCard(card, cardInfo(act));
      card.sprite.position.copy(cardPos(act));
    }
    for (const [id, ts2] of tags) {
      const a = actors.get(id);
      if (!a || !a.pos) { ts2.sprite.visible = false; continue; }
      ts2.sprite.visible = true;
      scaleLabel(ts2.sprite, Math.min(lf, 2));
      ts2.sprite.position.set(a.pos.x, 2.55 + (lf - 1) * 0.5, a.pos.z);
    }
    if (now - uiAt > 1) {
      uiAt = now;
      if (world && state) drawRuns(world.boards.runs, state.runs || [], names, Date.now());
      renderList(); renderSrPanel(); syncSign();
    }
    modals.update();
    chat.update();
    if (toastTs.sprite.visible && performance.now() > toastUntil) toastTs.sprite.visible = false;
    renderer.render(scene, camera);
    // đo FPS: bỏ 2 s đầu, cửa sổ 5 s
    if (!fpsStart) fpsStart = ts + 2000;
    if (ts >= fpsStart) {
      fpsFrames++;
      if (ts - fpsStart >= 5000) {
        const fps = fpsFrames / ((ts - fpsStart) / 1000);
        fpsFrames = 0; fpsStart = ts;
        if (fps < 10) disableRender();
      }
    }
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
  function start() { if (!raf && !stopped && !noRender && renderer) { last = 0; lastDraw = 0; fpsStart = 0; fpsFrames = 0; raf = requestAnimationFrame(frame); } }
  function pause() { if (raf) cancelAnimationFrame(raf); raf = 0; }
  document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); else start(); });

  // Quá chậm / không WebGL: ngừng vẽ (thông báo DOM tối thiểu) nhưng vẫn cập nhật danh sách sr-only.
  let slowT = 0;
  function disableRender() {
    noRender = true; pause();
    if (fallback) fallback.hidden = false;
    host.hidden = true;
    if (!slowT) slowT = setInterval(() => { if (!stopped) { renderList(); renderSrPanel(); } }, 1000);
    if (renderer) { renderer.dispose(); renderer.forceContextLoss(); }
  }
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
    if (renderer) start(); else disableRender();
    const r = await fetchState();
    if (r === 'ok') connect();
    else if (r === 'error') { setConn('lost'); reconnect(2000); }
  })();
}

main();
