/* Văn phòng 3D — máy trạng thái của từng nhân vật: trạng thái agent + event → (khu vực, tư thế, bong bóng) → đi tới chỗ, làm động tác.
 * Chống giật khi event dồn dập: chuyến đi theo tool chỉ bắt đầu sau 1,5 s (tool xong sớm hơn → không đi), về bàn trễ 1,5 s
 * (tool kế tiếp cùng khu vực thì ở lại), bong bóng ngắn chỉ hiện 2,5 s. */
import { Chibi, SIT_POSES } from './chibi.js';
import { lerpAngle, hashStr, seeded } from './util.js';
import { ZONE_NAMES } from './world.js';

export const STATUS_VI = {
  working: 'Đang làm', waiting: 'Chờ duyệt', waiting_human: 'Chờ bạn duyệt', asking: 'Chờ bạn trả lời',
  idle: 'Rảnh', error: 'Lỗi', paused: 'Tạm dừng', queued: 'Đang xếp hàng',
};
const VALID = new Set(['working', 'waiting', 'waiting_human', 'asking', 'idle', 'error', 'paused']);
const TRIP = { Bash: 'server', Read: 'library', Grep: 'library', Glob: 'library', WebFetch: 'library', WebSearch: 'library' };
const FAST = new Set(['Edit', 'Write', 'NotebookEdit', 'MultiEdit']);
const ERR_OUT = new Set(['failed', 'error', 'crashed', 'protocol_violation', 'blocked', 'timeout']);
const SCREEN_ON = new Set(['working', 'waiting', 'waiting_human', 'asking', 'error']);
const TRIP_DELAY = 1.5, RETURN_DELAY = 1.5, TRIP_MAX = 45, POP_S = 2.5;
const SPEED = 1.7;

export class Actor {
  constructor(agent, color, opts) {
    this.id = agent.id;
    this.agent = Object.assign({}, agent);
    this.isManager = !!agent.is_manager;
    this.chibi = new Chibi({ id: agent.id, name: agent.name || agent.id, color, isManager: this.isManager });
    this.color = color;
    this.rnd = seeded(hashStr(agent.id) ^ 0x9e3779b9);
    this.pos = null; this.yaw = 0;
    this.spot = null; this.zone = ''; this.want = ''; this.path = [];
    this.trip = null; this.pendingTrip = null;
    this.rateUntil = 0; this.reviewUntil = 0; this.visitUntil = 0; this.queuedUntil = 0;
    this.pop = null; this.wander = null; this.fallback = false; this.retryAt = 0;
    this.spawnAtEntrance = !!(opts && opts.entrance);
    this.selected = false;
  }

  attach(world) {
    if (this.spot && this.world) this.world.release(this.spot, this.id);
    this.world = world; this.spot = null; this.zone = ''; this.want = ''; this.path = [];
  }

  setData(agent, color, now) {
    const prev = this.agent.status, prevTool = this.agent.tool, first = !this._seen;
    this._seen = true;
    this.agent = Object.assign({}, agent);
    this.isManager = !!agent.is_manager;
    if (color !== this.color || (agent.name || agent.id) !== this._name) {
      this.color = color; this._name = agent.name || agent.id;
      this.chibi.setLabel(this._name, color);
    }
    if (prev !== agent.status) this.queuedUntil = 0;
    // tool trong state khác lần trước (hoặc lần đầu) → coi như tool.start để bắt đầu/kết thúc chuyến đi
    if ((first || agent.tool !== prevTool) && agent.status === 'working') {
      this.event({ type: 'tool.start', tool: agent.tool || null }, now || 0);
      if (first && this.pendingTrip) this.pendingTrip.at = 0;
    }
  }

  status(now) {
    let st = this.agent.status || 'idle';
    if (this.queuedUntil > now && (st === 'idle' || st === 'paused')) st = 'queued';
    return st;
  }

  // ------------------------------------------------------------ event (khung SSE đã lọc: chỉ id/tool/trạng thái)
  event(ev, now) {
    const a = this.agent;
    const setSt = (s) => { if (VALID.has(s)) a.status = s; };
    const popE = (e) => { this.pop = { emoji: e, until: now + POP_S }; };
    switch (ev.type) {
      case 'agent.status': setSt(ev.status === 'running' ? 'working' : ev.status); break;
      case 'run.queued': this.queuedUntil = now + 20; break;
      case 'run.started': setSt('working'); break;
      case 'run.waiting_human': setSt('waiting_human'); break;
      case 'run.completed': case 'run.failed': case 'run.cancelled': case 'run.interrupted':
        setSt(ERR_OUT.has(ev.outcome) || ev.type === 'run.failed' ? 'error' : 'idle');
        a.tool = null; this.trip = this.pendingTrip = null; break;
      case 'tool.start': {
        a.tool = ev.tool || null;
        if (a.status !== 'waiting' && a.status !== 'waiting_human') setSt('working');
        const z = TRIP[a.tool];
        if (z && !this.isManager) {
          if (this.trip && this.trip.zone === z) { this.trip.end = null; this.trip.hard = now + TRIP_MAX; }
          else if (!this.pendingTrip || this.pendingTrip.zone !== z) this.pendingTrip = { zone: z, at: now + TRIP_DELAY };
        } else {
          this.pendingTrip = null;
          if (this.trip && !this.trip.end) this.trip.end = now + RETURN_DELAY;
        }
        break;
      }
      case 'tool.complete':
        this.pendingTrip = null; // chuyến quá ngắn → bỏ
        if (this.trip && !this.trip.end) this.trip.end = now + RETURN_DELAY;
        if (ev.status === 'error') popE('⚠️');
        break;
      case 'subagent.spawn': popE('👥'); break;
      case 'approval.open': setSt('waiting'); break;
      case 'approval.waiting_human': case 'approval.escalated': setSt('waiting_human'); break;
      case 'approval.resolved': if (a.status === 'waiting' || a.status === 'waiting_human') setSt('working'); break;
      case 'approval.delegated': this.visitUntil = now + 15; break;
      case 'question.open': case 'question.escalated': setSt('asking'); break;
      case 'question.answered': case 'question.cancelled': if (a.status === 'asking') setSt('working'); break;
      case 'task.status': if (ev.status === 'review') this.reviewUntil = now + 20; break;
      case 'schedule.fired': popE('⏰'); break;
      case 'manager.action': popE('📋'); break;
      case 'ratelimit.throttle': this.rateUntil = now + 30; break;
      case 'learning.applied': case 'learning.rolled_back': popE('💡'); break;
      case 'guard.blocked': popE('⛔'); break;
      case 'steer.ack': popE('💬'); break;
      default: break;
    }
  }

  // ------------------------------------------------------------ mục tiêu hiện tại
  goal(now) {
    const st = this.status(now), a = this.agent;
    if (st !== 'working') { this.trip = null; this.pendingTrip = null; }
    if (this.pendingTrip && now >= this.pendingTrip.at) { this.trip = { zone: this.pendingTrip.zone, end: null, hard: now + TRIP_MAX }; this.pendingTrip = null; }
    if (this.trip && ((this.trip.end && now >= this.trip.end) || now >= this.trip.hard)) this.trip = null;
    if (st !== 'idle') this.wander = null;
    const home = this.isManager ? 'mgr' : 'desk';
    switch (st) {
      case 'waiting_human': return { zone: home, pose: 'raise', bubble: '✋', tone: 'danger', ring: true };
      case 'waiting': return { zone: home, pose: 'stand', bubble: '🛡️' };
      case 'asking': return { zone: home, pose: 'sitThink', bubble: '❓', tone: 'ask' };
      case 'error': return { zone: home, pose: 'headInHands', bubble: '⚠️', tone: 'danger' };
      default: break;
    }
    if (!this.isManager) {
      if (this.rateUntil > now && st !== 'paused') return { zone: 'bench', pose: 'sit', bubble: '⏳' };
      if (this.visitUntil > now && st !== 'paused') return { zone: 'mgrVisit', pose: 'spot', bubble: '🗂️' };
      if (this.reviewUntil > now && st !== 'paused') return { zone: 'meeting', pose: 'sit', bubble: '🔍' };
    }
    switch (st) {
      case 'working':
        if (this.trip) return { zone: this.trip.zone, pose: 'spot', bubble: this.trip.zone === 'server' ? '🖥️' : '📚' };
        return { zone: home, pose: FAST.has(a.tool) ? 'typeFast' : 'type', bubble: String(a.tool || '').startsWith('mcp__') ? '🔌' : null };
      case 'queued': return { zone: home, pose: 'sit' };
      case 'paused': return { zone: this.isManager ? home : 'lounge', pose: 'sleep', bubble: '💤' };
      default: break;
    }
    // rảnh: điều phối viên ngả lưng ở phòng riêng; agent đi lang thang pantry ↔ sofa (thỉnh thoảng ngồi bàn)
    if (this.isManager) return { zone: home, pose: 'relax' };
    if (!this.wander || now >= this.wander.until) {
      const r = this.rnd();
      let z = r < 0.45 ? 'pantry' : r < 0.85 ? 'lounge' : 'desk';
      if (this.wander && z === this.wander.zone) z = z === 'pantry' ? 'lounge' : 'pantry';
      const first = !this.wander;
      this.wander = { zone: z, until: now + (first ? 4 + this.rnd() * 20 : 12 + this.rnd() * 16) };
    }
    const wz = this.wander.zone;
    return { zone: wz, pose: wz === 'desk' ? 'relax' : wz === 'lounge' ? 'relax' : 'spot', bubble: wz === 'pantry' ? '☕' : null };
  }

  _target(pose) {
    const s = this.spot;
    if (!s) return null;
    return !SIT_POSES.has(pose) && s.stand ? s.stand : s;
  }

  _resolvePose(g) {
    let pose = g.pose === 'spot' ? ((this.spot && this.spot.pose) || 'stand') : g.pose;
    if (this.fallback && g.pose === 'spot') pose = this.agent.status === 'working' ? 'type' : 'sit';
    if (this.fallback && pose === 'standType') pose = 'type';
    return pose;
  }

  _replan(g, now, teleport) {
    const w = this.world;
    let spot = g.zone === 'desk' || g.zone === 'mgr' ? w.seatFor(this.id) : w.reserve(g.zone, this.id, this.pos);
    this.fallback = false;
    if (!spot) { spot = w.seatFor(this.id) || w.reserve('lounge', this.id, this.pos); this.fallback = true; this.retryAt = now + 4; }
    if (this.spot && this.spot !== spot) w.release(this.spot, this.id);
    this.spot = spot;
    this.zone = this.fallback ? (spot && spot.zone) || '' : g.zone;
    this.path = [];
    if (!spot) return;
    const tp = this._target(this._resolvePose(g));
    if (!this.pos || teleport) { this.pos = { x: tp.x, z: tp.z }; this.yaw = spot.rot; return; }
    this.path = w.grid.findPath(this.pos.x, this.pos.z, tp.x, tp.z);
  }

  update(dt, now, t, reduced, frozen) {
    const c = this.chibi, w = this.world;
    if (!w) return;
    if (!this.pos && this.spawnAtEntrance) { this.pos = { x: w.W - 1.4, z: w.D - 0.6 }; this.yaw = Math.PI; }
    const g = this.goal(now);
    if (frozen && this.pos && this.spot) {
      // ESTOP: đứng im tại chỗ
      c.walking = false; c.setBubble('🛑', 'danger'); c.setRing(false);
      c.animate(0, t, true);
      this._place();
      return;
    }
    if (g.zone !== this.want || !this.spot) { this.want = g.zone; this._replan(g, now, !this.pos || reduced || frozen); }
    else if (this.fallback && now >= this.retryAt) this._replan(g, now, reduced);
    const pose = this._resolvePose(g);
    const tp = this._target(pose);
    if (reduced && this.path.length) { const last = this.path[this.path.length - 1]; this.pos = { x: last.x, z: last.z }; this.path = []; }
    let walking = false;
    if (this.path.length) {
      let step = SPEED * dt;
      walking = true;
      while (step > 0 && this.path.length) {
        const p = this.path[0], dx = p.x - this.pos.x, dz = p.z - this.pos.z, d = Math.hypot(dx, dz);
        if (d > 1e-4) this.yaw = lerpAngle(this.yaw, Math.atan2(dx, dz), Math.min(1, dt * 12));
        if (d <= step) { this.pos = { x: p.x, z: p.z }; this.path.shift(); step -= d; }
        else { this.pos.x += (dx / d) * step; this.pos.z += (dz / d) * step; step = 0; }
      }
    } else if (tp) {
      const dx = tp.x - this.pos.x, dz = tp.z - this.pos.z, d = Math.hypot(dx, dz);
      if (d > 0.01) {
        const step = reduced ? d : Math.min(d, 1.2 * dt);
        this.pos.x += (dx / d) * step; this.pos.z += (dz / d) * step;
      }
      if (this.spot) this.yaw = reduced ? this.spot.rot : lerpAngle(this.yaw, this.spot.rot, Math.min(1, dt * 8));
    }
    c.walking = walking;
    c.pose = pose;
    c.seatY = (this.spot && this.spot.seatY) || 0.33;
    const popOn = this.pop && this.pop.until > now;
    if (!popOn) this.pop = null;
    c.setBubble(popOn ? this.pop.emoji : g.bubble, popOn ? '' : g.tone);
    c.setRing(!!g.ring);
    c.setSelected(this.selected);
    c.animate(dt, t, reduced);
    this._place();
    w.setScreen(this.id, SCREEN_ON.has(this.status(now)) && (this.zone === 'desk' || this.zone === 'mgr') && !walking);
  }

  _place() {
    if (!this.pos) return;
    this.chibi.group.position.set(this.pos.x, 0, this.pos.z);
    this.chibi.group.rotation.y = this.yaw;
  }

  where() {
    const z = ZONE_NAMES[this.zone] || '—';
    return this.path.length ? 'Đang đi tới ' + z.toLowerCase() : z;
  }

  dispose() {
    if (this.world && this.spot) this.world.release(this.spot, this.id);
    this.chibi.group.removeFromParent();
    this.chibi.dispose();
  }
}
