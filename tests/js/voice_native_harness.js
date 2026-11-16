// Harness node cho static/voice.js (tests/test_desktop_hub.py): DOM giả tối thiểu + cầu nối window.cfhNative.speech giả.
// argv: <đường dẫn voice.js> <kịch bản: native|unavailable|none|error|websr>. In một dòng JSON kết quả.
"use strict";
const fs = require("fs");
const [, , voicePath, scenario] = process.argv;

function makeEl(tag, attrs) {
  const el = {
    nodeType: 1, tagName: tag.toUpperCase(), children: [], parentNode: null, attrs: Object.assign({}, attrs || {}), listeners: {},
    hidden: false, value: "", title: "", type: "", name: "",
    get id() { return this.attrs.id || ""; },
    set id(v) { this.attrs.id = v; },
    get className() { return [...this._cls].join(" "); },
    set className(v) { this._cls = new Set(String(v).split(/\s+/).filter(Boolean)); },
    _cls: new Set(),
    textContent: "",
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = String(v); if (k === "name") this.name = String(v); },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    removeEventListener(t, f) { this.listeners[t] = (this.listeners[t] || []).filter((x) => x !== f); },
    dispatchEvent(e) { (this.listeners[e.type] || []).slice().forEach((f) => f(e)); return true; },
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    insertBefore(c, ref) { c.parentNode = this; const i = ref ? this.children.indexOf(ref) : -1; if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; },
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((x) => x !== this); },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    querySelectorAll(sel) { const out = []; walk(this, (n) => { if (n !== this && matches(n, sel)) out.push(n); }); return out; },
    closest(sel) { let n = this; while (n && n.nodeType === 1) { if (matches(n, sel)) return n; n = n.parentNode; } return null; },
    focus() {}, setSelectionRange() {},
    get form() { let n = this.parentNode; while (n && n.nodeType === 1) { if (n.tagName === "FORM") return n; n = n.parentNode; } return null; },
  };
  el.classList = { add: (c) => el._cls.add(c), remove: (c) => el._cls.delete(c), contains: (c) => el._cls.has(c) };
  if (attrs && attrs.class) el.className = attrs.class;
  return el;
}
function walk(n, f) { f(n); (n.children || []).forEach((c) => walk(c, f)); }
function matches(n, sel) {
  return sel.split(",").some((part) => {
    const s = part.trim();
    if (s.includes(" ")) return false;  // 'svg use[...]' — không cần
    const m = s.match(/^([a-z]*)(#[\w-]+)?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/);
    if (!m) return false;
    const [, tag, id, attr, val] = m;
    if (tag && n.tagName !== tag.toUpperCase()) return false;
    if (id && n.id !== id.slice(1)) return false;
    if (attr) {
      const v = attr === "name" ? (n.name || n.getAttribute("name")) : n.getAttribute(attr);
      if (v === null || v === undefined || v === "") { if (!(attr in n.attrs)) return false; }
      if (val !== undefined && v !== val) return false;
    }
    return !!(tag || id || attr);
  });
}

// --------------------------------------------------------------- trang Điều phối giả
const body = makeEl("body");
const form = makeEl("form", { id: "mgr-form", action: "/manager/send" });
const ta = makeEl("textarea", { id: "mgr-text" });
const mic = makeEl("button", { id: "mgr-mic", "data-voice-for": "mgr-text" });
mic.hidden = true;
const wave = makeEl("button", { id: "mgr-voice" });
wave.hidden = true;
const err = makeEl("p", { id: "mgr-err" });
err.hidden = true;
form.appendChild(ta); form.appendChild(mic); form.appendChild(err); body.appendChild(form); body.appendChild(wave);
const docListeners = {};
global.document = {
  readyState: "complete", body,
  documentElement: makeEl("html"),
  addEventListener(t, f) { (docListeners[t] = docListeners[t] || []).push(f); },
  removeEventListener() {},
  dispatchEvent(e) { (docListeners[e.type] || []).forEach((f) => f(e)); return true; },
  getElementById(id) { let r = null; walk(body, (n) => { if (!r && n.id === id) r = n; }); return r; },
  querySelector(sel) { return body.querySelector(sel); },
  querySelectorAll(sel) { return body.querySelectorAll(sel); },
  createElement: (t) => makeEl(t),
  createElementNS: (_ns, t) => makeEl(t),
};
global.window = global;

// --------------------------------------------------------------- cầu nối native giả
const calls = [];
const handlers = {};
if (scenario !== "none" && scenario !== "websr") {
  window.cfhNative = {
    speech: {
      available: () => Promise.resolve(scenario !== "unavailable"),
      start: (o) => { calls.push(["start", o]); return Promise.resolve(); },
      stop: () => { calls.push(["stop"]); return Promise.resolve(); },
      on: (ev, cb) => { calls.push(["on", ev]); handlers[ev] = cb; },
    },
  };
}
let srStarted = 0;
if (scenario === "websr") {
  window.isSecureContext = true;
  window.SpeechRecognition = function () { this.start = () => { srStarted++; }; this.stop = () => {}; this.abort = () => {}; };
}
const tick = () => new Promise((r) => setTimeout(r, 0));
const click = (el) => el.dispatchEvent({ type: "click", preventDefault() {}, target: el, button: 0 });

(async () => {
  eval(fs.readFileSync(voicePath, "utf8"));  // nạp như <script> (không module) — chỉ trong harness test
  await tick(); await tick();
  const out = { engine: window.cfhVoice.engine(), micHidden: mic.hidden, waveHidden: wave.hidden, srStarted };
  if (scenario === "native" || scenario === "error") {
    click(mic);
    await tick(); await tick();
    out.startArg = (calls.find((c) => c[0] === "start") || [])[1] || null;
    out.registered = calls.filter((c) => c[0] === "on").map((c) => c[1]).sort();
    out.micOn = mic.classList.contains("on");
    if (scenario === "native") {
      handlers.result({ text: "xin chào", final: false });
      out.interimValue = ta.value;
      handlers.result({ text: "xin chào bạn", final: true });
      handlers.result({ text: "ơi", final: false });
      out.mixedValue = ta.value;
      const v = form.querySelector('input[name="voice"]');
      out.voiceField = v ? v.value : null;
      click(mic);  // bấm lần nữa = dừng
      await tick(); await tick(); await tick();
      out.stopCalled = calls.some((c) => c[0] === "stop");
      out.micOnAfterStop = mic.classList.contains("on");
      out.finalValue = ta.value;
      handlers.result({ text: "muộn", final: true });  // sự kiện muộn sau khi dừng → bỏ
      out.afterLate = ta.value;
    } else {
      handlers.error({ code: "not-allowed" });
      await tick(); await tick();
      out.errShown = !err.hidden;
      out.errText = err.textContent;
      out.micOnAfterError = mic.classList.contains("on");
    }
  }
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stdout.write(JSON.stringify({ crash: String(e && e.stack || e) }) + "\n"); process.exit(1); });
