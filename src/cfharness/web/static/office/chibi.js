/* Văn phòng 3D — nhân vật chibi dựng thủ tục từ hình khối (đầu to ~ bằng thân, tay chân ngắn có khớp để chuyển động).
 * Mô hình nhìn về +z khi rotation.y = 0. Tư thế = góc đích cho từng khớp, nội suy mượt + dao động cộng thêm (đi, gõ phím…). */
import * as THREE from '../vendor/three-0.169.0/three.module.min.js';
import { hashStr, seeded, mat, basic, geo, labelSprite, bubbleTexture, scaleLabel } from './util.js';

const SKIN = ['#f7d7c4', '#f1c6a6', '#e0ac87', '#c98f68', '#a8714f', '#fbe1cf'];
const HAIR = ['#3b2a20', '#5a3b28', '#1f1b1a', '#8a5a3b', '#d8a657', '#b4513a', '#6b4f8a', '#2f4858', '#e7c9a0'];
const PANTS = ['#4a4e69', '#3d405b', '#5c4d3c', '#2f3e46', '#6d597a'];
const STYLES = ['short', 'bob', 'pony', 'buns', 'spiky', 'ahoge'];

export const SIT_POSES = new Set(['sit', 'type', 'typeFast', 'headInHands', 'sleep', 'relax', 'sitDrink', 'sitThink', 'sitRead']);
const HIP_Y = 0.26;

function shade(hex, k) {
  const c = new THREE.Color(hex);
  const hsl = {}; c.getHSL(hsl);
  c.setHSL(hsl.h, Math.min(1, hsl.s * (0.85 + 0.3 * k)), Math.max(0.18, Math.min(0.8, hsl.l + (k - 0.5) * 0.18)));
  return '#' + c.getHexString();
}

function capsule(r, len) { return geo('cap' + r + ':' + len, () => new THREE.CapsuleGeometry(r, len, 4, 10)); }
function hairCap(r, theta) { return geo('hair' + r + ':' + theta, () => new THREE.SphereGeometry(r, 18, 12, 0, Math.PI * 2, 0, theta)); }

export class Chibi {
  constructor(opts) {
    const h = hashStr(opts.id), rnd = seeded(h);
    this.isManager = !!opts.isManager;
    const skin = SKIN[h % SKIN.length];
    const hairC = HAIR[(h >>> 3) % HAIR.length];
    const shirt = shade(opts.color, rnd());
    const pants = PANTS[(h >>> 7) % PANTS.length];
    const style = STYLES[(h >>> 11) % STYLES.length];

    const g = this.group = new THREE.Group();
    g.userData.actorId = opts.id;
    const body = this.body = new THREE.Group();
    g.add(body);

    // bóng giả dưới chân (không dùng shadow map)
    const sh = new THREE.Mesh(geo('shadow', () => new THREE.CircleGeometry(0.32, 20)), basic('#000000', { transparent: true, opacity: 0.16, depthWrite: false }));
    sh.rotation.x = -Math.PI / 2; sh.position.y = 0.012; g.add(sh);
    this.shadow = sh;

    // chân
    const mkLeg = (x) => {
      const p = new THREE.Group(); p.position.set(x, HIP_Y, 0);
      const leg = new THREE.Mesh(capsule(0.075, 0.1), mat(pants)); leg.position.y = -0.11; p.add(leg);
      const shoe = new THREE.Mesh(geo('shoe', () => new THREE.SphereGeometry(0.08, 10, 8)), mat('#3a302a'));
      shoe.scale.set(1, 0.65, 1.35); shoe.position.set(0, -0.22, 0.03); p.add(shoe);
      body.add(p); return p;
    };
    this.legL = mkLeg(-0.09); this.legR = mkLeg(0.09);

    // thân
    const torso = new THREE.Mesh(capsule(0.17, 0.14), mat(shirt));
    torso.position.y = 0.43; torso.scale.set(1, 1, 0.85); body.add(torso);
    if (this.isManager) {
      // áo vest tối + cà vạt đỏ
      torso.material = mat('#3a3f58');
      const shirtV = new THREE.Mesh(geo('vee', () => new THREE.ConeGeometry(0.07, 0.16, 3)), mat('#ffffff'));
      shirtV.rotation.x = Math.PI; shirtV.position.set(0, 0.52, 0.135); body.add(shirtV);
      const tie = new THREE.Mesh(geo('tie', () => new THREE.ConeGeometry(0.035, 0.2, 4)), mat('#c8413a'));
      tie.rotation.x = Math.PI; tie.position.set(0, 0.45, 0.15); body.add(tie);
    }

    // tay
    const mkArm = (x) => {
      const p = new THREE.Group(); p.position.set(x, 0.53, 0);
      const arm = new THREE.Mesh(capsule(0.055, 0.1), mat(this.isManager ? '#3a3f58' : shirt)); arm.position.y = -0.09; p.add(arm);
      const hand = new THREE.Mesh(geo('hand', () => new THREE.SphereGeometry(0.06, 10, 8)), mat(skin)); hand.position.y = -0.2; p.add(hand);
      body.add(p); return p;
    };
    this.armL = mkArm(-0.2); this.armR = mkArm(0.2);
    // cốc cà phê và quyển sách gắn vào tay phải (ẩn mặc định)
    this.cup = new THREE.Group();
    const cupM = new THREE.Mesh(geo('cup', () => new THREE.CylinderGeometry(0.045, 0.038, 0.09, 10)), mat('#ffffff'));
    const coffee = new THREE.Mesh(geo('coffee', () => new THREE.CircleGeometry(0.04, 10)), mat('#6b4226'));
    coffee.rotation.x = -Math.PI / 2; coffee.position.y = 0.046;
    this.cup.add(cupM, coffee); this.cup.position.set(0, -0.24, 0.05); this.cup.visible = false; this.armR.add(this.cup);
    this.book = new THREE.Mesh(UNIT(), mat(['#c8413a', '#4f8fc0', '#7fb069', '#e9b44c'][h % 4]));
    this.book.scale.set(0.2, 0.03, 0.15); this.book.position.set(-0.08, -0.25, 0.06); this.book.visible = false; this.armR.add(this.book);

    // đầu
    const head = this.head = new THREE.Group(); head.position.set(0, 0.6, 0); body.add(head);
    const skull = new THREE.Mesh(geo('skull', () => new THREE.SphereGeometry(0.3, 22, 16)), mat(skin));
    skull.position.y = 0.29; skull.scale.set(1.04, 0.96, 1); head.add(skull);
    const eyeG = geo('eye', () => new THREE.SphereGeometry(0.045, 12, 10));
    this.eyes = [];
    for (const sx of [-0.105, 0.105]) {
      const eye = new THREE.Group(); eye.position.set(sx, 0.25, 0.265);
      const e = new THREE.Mesh(eyeG, basic('#2a211c')); e.scale.set(0.85, 1.15, 0.5); eye.add(e);
      const hl = new THREE.Mesh(geo('hl', () => new THREE.SphereGeometry(0.015, 6, 6)), basic('#ffffff')); hl.position.set(0.015, 0.02, 0.022); eye.add(hl);
      head.add(eye); this.eyes.push(eye);
      const blush = new THREE.Mesh(geo('blush', () => new THREE.CircleGeometry(0.045, 14)), basic('#f39a9a', { transparent: true, opacity: 0.55 }));
      blush.position.set(sx * 1.6, 0.17, 0.262 - Math.abs(sx) * 0.18); blush.rotation.y = sx > 0 ? 0.55 : -0.55; head.add(blush);
    }
    const mouth = new THREE.Mesh(geo('mouth', () => new THREE.TorusGeometry(0.03, 0.008, 6, 12, Math.PI)), basic('#7a3b2e'));
    mouth.rotation.z = Math.PI; mouth.position.set(0, 0.16, 0.285); head.add(mouth);
    this.mouth = mouth;

    // tóc (chỏm cầu nghiêng ra sau: che trán + gáy, chừa mặt)
    const hm = mat(hairC);
    const cap = new THREE.Mesh(hairCap(0.318, style === 'bob' ? 2.05 : 1.8), hm);
    cap.position.y = 0.3; cap.rotation.x = -0.6; head.add(cap);
    const fringe = new THREE.Mesh(hairCap(0.322, 0.75), hm); // mái trước
    fringe.position.y = 0.3; fringe.rotation.x = 0.35; head.add(fringe);
    if (style === 'pony') {
      const tail = new THREE.Mesh(capsule(0.08, 0.2), hm); tail.position.set(0, 0.32, -0.36); tail.rotation.x = 0.55; head.add(tail);
      this.tail = tail;
    } else if (style === 'buns') {
      for (const sx of [-0.21, 0.21]) { const b = new THREE.Mesh(geo('bun', () => new THREE.SphereGeometry(0.1, 12, 10)), hm); b.position.set(sx, 0.52, -0.05); head.add(b); }
    } else if (style === 'spiky') {
      const cg = geo('spike', () => new THREE.ConeGeometry(0.07, 0.17, 6));
      for (let i = 0; i < 5; i++) {
        const s = new THREE.Mesh(cg, hm), a = (i - 2) * 0.35;
        s.position.set(Math.sin(a) * 0.2, 0.55 + Math.cos(a) * 0.04, -0.04 - Math.abs(a) * 0.05); s.rotation.z = -a * 0.9; s.rotation.x = -0.25; head.add(s);
      }
    } else if (style === 'ahoge') {
      const s = new THREE.Mesh(geo('ahoge', () => new THREE.TorusGeometry(0.07, 0.016, 6, 12, Math.PI * 1.2)), hm);
      s.position.set(0.02, 0.66, 0); s.rotation.y = Math.PI / 2; head.add(s);
    }
    if (this.isManager) {
      // tai nghe: vòng qua đầu + chụp tai + mic
      const band = new THREE.Mesh(geo('band', () => new THREE.TorusGeometry(0.33, 0.022, 8, 24, Math.PI)), mat('#2d2d33'));
      band.position.y = 0.3; head.add(band);
      for (const sx of [-0.32, 0.32]) { const cupE = new THREE.Mesh(geo('ear', () => new THREE.CylinderGeometry(0.075, 0.075, 0.05, 14)), mat('#2d2d33')); cupE.rotation.z = Math.PI / 2; cupE.position.set(sx, 0.28, 0); head.add(cupE); }
      const mic = new THREE.Mesh(capsule(0.012, 0.2), mat('#2d2d33')); mic.position.set(0.25, 0.2, 0.14); mic.rotation.set(1.3, 0, 0.6); head.add(mic);
      const tip = new THREE.Mesh(geo('mictip', () => new THREE.SphereGeometry(0.025, 8, 6)), mat('#c8413a')); tip.position.set(0.17, 0.15, 0.24); head.add(tip);
    }

    // vòng dưới chân: đỏ = chờ bạn duyệt; cam = đang chọn
    this.ringRed = new THREE.Mesh(geo('ringR', () => new THREE.RingGeometry(0.36, 0.46, 32)), basic('#e0443a', { transparent: true, opacity: 0.85, depthWrite: false, side: THREE.DoubleSide }));
    this.ringRed.rotation.x = -Math.PI / 2; this.ringRed.position.y = 0.02; this.ringRed.visible = false; g.add(this.ringRed);
    this.ringSel = new THREE.Mesh(geo('ringS', () => new THREE.RingGeometry(0.5, 0.58, 40)), basic('#d97757', { transparent: true, opacity: 0.95, depthWrite: false, side: THREE.DoubleSide }));
    this.ringSel.rotation.x = -Math.PI / 2; this.ringSel.position.y = 0.025; this.ringSel.visible = false; g.add(this.ringSel);

    // nhãn tên + bong bóng
    this.label = null;
    this.setLabel(opts.name, opts.color);
    this.bubble = new THREE.Sprite(new THREE.SpriteMaterial({ transparent: true, depthWrite: false }));
    this.bubble.scale.set(0.55, 0.55, 1); this.bubble.position.y = 2.0; this.bubble.visible = false; this.bubble.renderOrder = 6;
    g.add(this.bubble);
    this._bubbleKey = '';

    if (this.isManager) g.scale.setScalar(1.08);
    this.pose = 'stand';
    this.cur = { lL: 0, lR: 0, aLx: 0, aRx: 0, aLz: 0.12, aRz: -0.12, hx: 0, hz: 0, hy: 0, by: 0 };
    this.phase = rnd() * 10; this.walkPhase = 0; this.blinkAt = 1 + rnd() * 4; this.blink = 0;
    this.walking = false; this.seatY = 0.3;
  }

  setLabel(name, color) {
    if (this._labelText === name + '|' + color) return;
    this._labelText = name + '|' + color;
    if (this.label) { this.group.remove(this.label); this.label.material.map.dispose(); this.label.material.dispose(); }
    this.label = labelSprite(name, color, { scale: this.isManager ? 1.35 : 1.2 });
    this.label.position.y = 1.62;
    this.group.add(this.label);
  }

  setBubble(emoji, tone) {
    const k = emoji ? emoji + '|' + (tone || '') : '';
    if (k === this._bubbleKey) return;
    this._bubbleKey = k;
    if (!emoji) { this.bubble.visible = false; return; }
    this.bubble.material.map = bubbleTexture(emoji, tone);
    this.bubble.material.needsUpdate = true;
    this.bubble.visible = true;
    this._pop = 0; // nảy nhẹ khi đổi bong bóng
  }

  setRing(red) { this.ringRed.visible = !!red; }
  setSelected(on) { this.ringSel.visible = !!on; }

  // Cập nhật khớp theo tư thế. t = thời gian (giây), reduced = giảm chuyển động.
  animate(dt, t, reduced) {
    const p = this.pose, c = this.cur, w = this.walking;
    const sit = SIT_POSES.has(p) && !w;
    // góc đích
    let lL = 0, lR = 0, aLx = 0, aRx = 0, aLz = 0.14, aRz = -0.14, hx = 0, hz = 0, hy = 0, by = 0;
    let eyesClosed = false;
    this.cup.visible = (p === 'drink' || p === 'sitDrink') && !w;
    this.book.visible = (p === 'read' || p === 'sitRead') && !w;
    if (sit) { lL = lR = -1.45; by = this.seatY - HIP_Y + 0.02; }
    switch (w ? 'walk' : p) {
      case 'type': case 'typeFast': case 'standType': aLx = aRx = -1.15; aLz = 0.25; aRz = -0.25; hx = 0.12; break;
      case 'raise': aRx = -2.95; aRz = 0.4; hx = -0.12; hz = -0.1; break;
      case 'headInHands': aLx = aRx = -2.55; aLz = -0.35; aRz = 0.35; hx = 0.38; break;
      case 'sleep': aLx = aRx = -0.35; hx = 0.45; hz = 0.35; eyesClosed = true; break;
      case 'relax': aLz = 0.6; aRz = -0.6; hx = -0.15; break;
      case 'drink': case 'sitDrink': aRx = -1.9; aRz = 0.55; aLx = -0.2; break;
      case 'read': case 'sitRead': aLx = aRx = -1.0; aLz = -0.25; aRz = 0.25; hx = 0.3; break;
      case 'think': case 'sitThink': aRx = -2.3; aRz = 0.6; hz = 0.18; hy = 0.2; break;
      case 'freeze': aLz = 0.05; aRz = -0.05; break;
      default: break;
    }
    // dao động cộng thêm
    this.phase += dt;
    if (!reduced) {
      if (w) {
        this.walkPhase += dt * 9;
        const s = Math.sin(this.walkPhase);
        lL = s * 0.65; lR = -s * 0.65; aLx = -s * 0.55; aRx = s * 0.55;
        by += Math.abs(Math.cos(this.walkPhase)) * 0.05;
      } else if (p === 'type' || p === 'typeFast' || p === 'standType') {
        const f = p === 'typeFast' ? 24 : 13;
        aLx += Math.sin(this.phase * f) * 0.12; aRx += Math.sin(this.phase * f + 1.7) * 0.12;
        hy = Math.sin(this.phase * 0.7) * 0.08;
      } else if (p === 'raise') {
        aRz += Math.sin(this.phase * 7) * 0.3; by += Math.abs(Math.sin(this.phase * 3.5)) * 0.04;
      } else if (p === 'drink' || p === 'sitDrink') {
        const k = (Math.sin(this.phase * 1.2) + 1) / 2;
        aRx = -1.3 - k * 1.0; hx = -k * 0.15;
      } else if (p === 'sleep') {
        hx += Math.sin(this.phase * 1.3) * 0.05; by += Math.sin(this.phase * 1.3) * 0.008;
      } else if (p === 'headInHands') {
        hz = Math.sin(this.phase * 2.2) * 0.12;
      } else if (p === 'read' || p === 'sitRead') {
        hy = Math.sin(this.phase * 0.9) * 0.15;
      } else if (p !== 'freeze') {
        by += Math.sin(this.phase * 2) * 0.006; // thở
        hy = Math.sin(this.phase * 0.5) * 0.12;
      }
    }
    const k = reduced ? 1 : 1 - Math.exp(-dt * 12);
    c.lL += (lL - c.lL) * k; c.lR += (lR - c.lR) * k;
    c.aLx += (aLx - c.aLx) * k; c.aRx += (aRx - c.aRx) * k; c.aLz += (aLz - c.aLz) * k; c.aRz += (aRz - c.aRz) * k;
    c.hx += (hx - c.hx) * k; c.hz += (hz - c.hz) * k; c.hy += (hy - c.hy) * k; c.by += (by - c.by) * k;
    this.legL.rotation.x = c.lL; this.legR.rotation.x = c.lR;
    this.armL.rotation.set(c.aLx, 0, c.aLz); this.armR.rotation.set(c.aRx, 0, c.aRz);
    // tay giơ cao dài ra một chút để thấy rõ trên đầu to
    const ry = p === 'raise' && !w ? 1.55 : 1;
    this.armR.scale.y += (ry - this.armR.scale.y) * k;
    this.head.rotation.set(c.hx, c.hy, c.hz);
    this.body.position.y = c.by;
    this.shadow.scale.setScalar(sit ? 1.15 : 1);

    // chớp mắt
    let ey = 1;
    if (eyesClosed) ey = 0.12;
    else if (!reduced) {
      this.blinkAt -= dt;
      if (this.blinkAt < 0) { this.blink = 0.14; this.blinkAt = 2.5 + Math.random() * 4; }
      if (this.blink > 0) { this.blink -= dt; ey = 0.15; }
    }
    this.eyes[0].scale.y = this.eyes[1].scale.y = ey;

    // bong bóng nảy + lắc nhẹ; vòng đỏ nhấp nháy
    if (this.bubble.visible) {
      this._pop = Math.min(1, (this._pop || 0) + dt * 4);
      const s = (reduced ? 0.55 : 0.55 * (0.6 + 0.4 * easeOutBack(this._pop))) * Math.min(1.8, this.labelScale || 1);
      this.bubble.scale.set(s, s, 1);
      this.bubble.position.y = 2.0 + ((this.labelScale || 1) - 1) * 0.35 + (reduced ? 0 : Math.sin(this.phase * 2.4) * 0.04) + (sit ? -0.12 : 0);
    }
    scaleLabel(this.label, this.labelScale || 1);
    this.label.position.y = (sit ? 1.48 : 1.62) + ((this.labelScale || 1) - 1) * 0.12;
    if (this.ringRed.visible) {
      const s = reduced ? 1 : 1 + Math.sin(t * 5) * 0.08;
      this.ringRed.scale.set(s, s, s);
    }
    if (this.ringSel.visible && !reduced) this.ringSel.rotation.z = t * 0.6;
  }

  dispose() {
    if (this.label) { this.label.material.map.dispose(); this.label.material.dispose(); }
    this.bubble.material.dispose();
  }
}

function easeOutBack(x) { const c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * Math.pow(x - 1, 3) + c1 * Math.pow(x - 1, 2); }
let _unit = null;
function UNIT() { if (!_unit) _unit = new THREE.BoxGeometry(1, 1, 1); return _unit; }
