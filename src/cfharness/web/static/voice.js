/* Giọng nói trên web (docs/dev-notes/voice.md) — giống luồng của Claude:
 *  1) Đọc chép (nút micro cạnh ô nhập): nói → chữ hiện dần vào ô nhập → người xem/sửa rồi TỰ gửi. Bấm = bật/tắt; giữ = nói-thả
 *     (push-to-talk); ở ô nhập Điều phối: giữ phím Space khi ô trống.
 *  2) Hội thoại giọng nói (nút sóng âm, chỉ trang Điều phối): nghe → im ~1,3 s thì tự gửi → chờ điều phối viên → đọc to câu trả lời
 *     (tiếng Việt) → nghe tiếp. Chạm quả cầu để ngắt lời / tạm dừng; nói "dừng lại" để dừng lượt đang chạy, "kết thúc" để thoát.
 * Nhận dạng + đọc dùng Web Speech API của trình duyệt (không thêm thư viện/CDN, CSP không đổi). Chrome/Edge gửi âm thanh tới dịch vụ
 * nhận dạng của nhà cung cấp trình duyệt — ghi rõ trong bảng. Tin gửi bằng giọng mang `voice=dictation|conversation`: daemon KHÔNG
 * duyệt thao tác/chốt xin phép bằng phím tắt từ tin giọng nói. Chữ của agent chỉ đi qua textContent / speechSynthesis. */
(function () {
  "use strict";
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  var TTS = window.speechSynthesis || null;
  var CAN_LISTEN = !!SR && window.isSecureContext !== false;
  var PREF_KEY = "cfh_voice";
  var SILENCE_MS = 1300, INTERIM_SILENCE_MS = 2400, IDLE_PAUSE_MS = 60000, HOLD_MS = 350;

  function prefs() {
    var d = { lang: "vi-VN", speak: true, rate: 1.05 };
    try { var j = JSON.parse(localStorage.getItem(PREF_KEY) || "{}"); for (var k in j) if (k in d) d[k] = j[k]; } catch (_) {}
    if (d.lang !== "vi-VN" && d.lang !== "en-US") d.lang = "vi-VN";
    d.rate = Math.min(1.6, Math.max(0.7, Number(d.rate) || 1));
    return d;
  }
  function savePrefs(p) { try { localStorage.setItem(PREF_KEY, JSON.stringify(p)); } catch (_) {} }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function svgIcon(name) {
    var ns = "http://www.w3.org/2000/svg", s = document.createElementNS(ns, "svg"), u = document.createElementNS(ns, "use");
    s.setAttribute("class", "ic"); s.setAttribute("aria-hidden", "true"); s.setAttribute("focusable", "false");
    var src = document.querySelector('svg use[href*="icons.svg"]');
    var base = src ? src.getAttribute("href").split("#")[0] : "/static/icons.svg";
    u.setAttribute("href", base + "#i-" + name);
    s.appendChild(u);
    return s;
  }
  function errText(code) {
    switch (code) {
      case "not-allowed": case "service-not-allowed":
        return "Trình duyệt đang chặn micro — bấm biểu tượng ổ khoá trên thanh địa chỉ, cho phép Micro rồi thử lại.";
      case "audio-capture": return "Không tìm thấy micro trên máy.";
      case "network": return "Nhận dạng giọng nói cần kết nối mạng (trình duyệt gửi âm thanh tới dịch vụ nhận dạng).";
      case "language-not-supported": return "Trình duyệt chưa hỗ trợ nhận dạng ngôn ngữ này.";
      default: return "Lỗi nhận dạng giọng nói (" + code + ").";
    }
  }
  function norm(t) { return String(t || "").toLowerCase().replace(/[.,!?…;:"'“”]+/g, " ").replace(/\s+/g, " ").trim(); }
  function fire(ta) { try { ta.dispatchEvent(new Event("input", { bubbles: true })); } catch (_) {} }

  // ------------------------------------------------------------------ nhận dạng: một phiên dùng chung (chỉ một micro bật một lúc)
  var active = null;
  function Listener(opts) {
    // opts: lang, onText(final, interim), onEnd(reason), onSpeech()
    var rec = new SR(), stopped = false, finals = "", self = this;
    rec.lang = opts.lang; rec.continuous = true; rec.interimResults = true; rec.maxAlternatives = 1;
    rec.onresult = function (e) {
      var interim = "";
      for (var i = e.resultIndex; i < e.results.length; i++) {
        var t = e.results[i][0] ? e.results[i][0].transcript : "";
        if (e.results[i].isFinal) finals += (finals && !/\s$/.test(finals) && !/^\s/.test(t) ? " " : "") + t;
        else interim += t;
      }
      opts.onText(finals.trim(), interim.trim());
    };
    rec.onspeechstart = function () { if (opts.onSpeech) opts.onSpeech(); };
    rec.onerror = function (e) {
      if (e.error === "no-speech" || e.error === "aborted") return;  // im lặng / tự dừng: onend xử lý
      stopped = true;
      opts.onEnd("error", e.error);
    };
    rec.onend = function () {
      if (stopped) { if (active === self) active = null; opts.onEnd("stopped"); return; }
      try { rec.start(); } catch (_) { stopped = true; if (active === self) active = null; opts.onEnd("stopped"); }  // Chrome tự kết thúc sau một lúc
    };
    this.stop = function () { if (stopped) return; stopped = true; try { rec.stop(); } catch (_) { opts.onEnd("stopped"); } };
    this.abort = function () { stopped = true; try { rec.abort(); } catch (_) {} };
    this.reset = function () { finals = ""; };
    if (active && active !== this) active.stop();
    active = this;
    try { rec.start(); } catch (err) { stopped = true; active = null; setTimeout(function () { opts.onEnd("error", "start"); }, 0); }
  }

  // ------------------------------------------------------------------ đọc to (speechSynthesis)
  var voiceCache = null;
  function pickVoice(lang) {
    if (!TTS) return null;
    var want = lang.slice(0, 2).toLowerCase();
    var vs = (TTS.getVoices() || []).filter(function (v) { return (v.lang || "").toLowerCase().replace("_", "-").indexOf(want) === 0; });
    vs.sort(function (a, b) { return score(b) - score(a); });
    return vs[0] || null;
    function score(v) { return (/natural|online|neural/i.test(v.name) ? 4 : 0) + (/google|hoaimy|namminh|linh|an\b/i.test(v.name) ? 2 : 0) + (v.localService ? 0 : 1); }
  }
  if (TTS && TTS.addEventListener) TTS.addEventListener("voiceschanged", function () { voiceCache = null; });
  function hasVoice(lang) { return !!pickVoice(lang); }
  // markdown của trả lời → câu nói được: bỏ khối mã, link, bảng, ký hiệu; quá dài → cắt ở câu + nhắc xem màn hình
  function speakable(md, lang) {
    var vi = lang !== "en-US";
    var t = String(md || "")
      .replace(/```[\s\S]*?```/g, vi ? " (có đoạn mã, xem trên màn hình). " : " (code block on screen). ")
      .replace(/\[\[file:[^\]]*\]\]/g, "")
      .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
      .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
      .replace(/https?:\/\/[^\s)]+/g, vi ? "đường link" : "a link")
      .replace(/`([^`]*)`/g, "$1")
      .replace(/^[ \t]*\|?[ \t:|-]+\|[ \t:|-]*$/gm, "")
      .replace(/^[ \t]*\|(.*)\|[ \t]*$/gm, function (_, row) { return row.split("|").map(function (c) { return c.trim(); }).filter(Boolean).join(", ") + "."; })
      .replace(/^[ \t]{0,3}#{1,6}[ \t]*/gm, "")
      .replace(/^[ \t]*(?:[-*+•]|\d+[.)])[ \t]+/gm, "")
      .replace(/[*_~>#]+/g, "")
      .replace(/\s*\n+\s*/g, function (m) { return /\n\s*\n/.test(m) ? ". " : ", "; })
      .replace(/([.!?…])\s*[,.]\s*/g, "$1 ")
      .replace(/\s{2,}/g, " ").trim();
    var MAX = 1400;
    if (t.length > MAX) {
      var cut = t.lastIndexOf(". ", MAX);
      t = t.slice(0, cut > 400 ? cut + 1 : MAX) + (vi ? " Phần còn lại xem trên màn hình." : " The rest is on screen.");
    }
    return t;
  }
  function chunks(t) {
    var parts = t.match(/[^.!?…]+[.!?…]*\s*/g) || [t], out = [], cur = "";
    parts.forEach(function (p) {
      if ((cur + p).length > 180 && cur) { out.push(cur.trim()); cur = ""; }
      cur += p;
      while (cur.length > 220) { var i = cur.lastIndexOf(", ", 200); i = i > 60 ? i + 1 : 200; out.push(cur.slice(0, i).trim()); cur = cur.slice(i); }
    });
    if (cur.trim()) out.push(cur.trim());
    return out;
  }
  function speak(text, lang, rate, done) {
    if (!TTS || !text) { if (done) setTimeout(done, 0); return function () {}; }
    TTS.cancel();
    var v = voiceCache || (voiceCache = pickVoice(lang)), list = chunks(text), cancelled = false, i = 0;
    function next() {
      if (cancelled) return;
      if (i >= list.length) { if (done) done(); return; }
      var u = new SpeechSynthesisUtterance(list[i++]);
      u.lang = v ? v.lang : lang; if (v) u.voice = v; u.rate = rate;
      u.onend = next; u.onerror = function () { if (!cancelled) next(); };
      TTS.speak(u);
    }
    next();
    return function cancel() { cancelled = true; try { TTS.cancel(); } catch (_) {} };
  }

  // ------------------------------------------------------------------ 1) đọc chép vào ô nhập
  function markVoice(ta) {
    var form = ta.form;
    if (!form || (form.getAttribute("action") || "") !== "/manager/send") return;
    var inp = form.querySelector('input[name="voice"]');
    if (!inp) { inp = document.createElement("input"); inp.type = "hidden"; inp.name = "voice"; form.appendChild(inp); }
    if (inp.value !== "conversation") inp.value = "dictation";
  }
  function clearVoice(form) { var inp = form && form.querySelector('input[name="voice"]'); if (inp) inp.value = ""; }
  function toast(anchor, msg) {
    var mgrErr = document.getElementById("mgr-err");
    if (mgrErr && anchor.closest && anchor.closest("#mgr-form")) { mgrErr.textContent = msg; mgrErr.hidden = false; return; }
    var t = anchor.parentNode && anchor.parentNode.querySelector(".voice-toast");
    if (!t) { t = el("p", "voice-toast"); t.setAttribute("role", "alert"); anchor.parentNode.insertBefore(t, anchor.nextSibling); }
    t.textContent = msg; t.hidden = false;
    clearTimeout(t._h); t._h = setTimeout(function () { t.hidden = true; }, 7000);
  }
  var dict = null;  // {ta, btn, listener}
  function dictStart(ta, btn) {
    if (dict) dictStop();
    var p = prefs(), base = ta.value;
    if (base && !/\s$/.test(base)) base += " ";
    var state = { ta: ta, btn: btn, listener: null };
    dict = state;
    btn.classList.add("on"); btn.setAttribute("aria-pressed", "true"); btn.title = "Đang nghe — bấm để dừng";
    state.listener = new Listener({
      lang: p.lang,
      onText: function (fin, interim) {
        ta.value = base + fin + (interim ? (fin ? " " : "") + interim : "");
        if (fin || interim) markVoice(ta);
        fire(ta);
        try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch (_) {}
      },
      onEnd: function (why, code) {
        if (dict === state) dict = null;
        btn.classList.remove("on"); btn.setAttribute("aria-pressed", "false"); btn.title = btn.getAttribute("data-title") || "";
        if (why === "error") toast(btn, errText(code));
        ta.value = ta.value.replace(/\s+$/, "");
        fire(ta);
        try { ta.focus(); } catch (_) {}
      },
    });
  }
  function dictStop() { if (dict && dict.listener) dict.listener.stop(); }
  function micButton(ta) {
    var b = el("button", "btn ghost sm icon voice-mic");
    b.type = "button";
    var title = "Nói để nhập (" + (prefs().lang === "en-US" ? "English" : "tiếng Việt") + ") — bấm để bật/tắt, giữ để nói-thả";
    b.title = title; b.setAttribute("data-title", title);
    b.setAttribute("aria-label", "Nhập bằng giọng nói"); b.setAttribute("aria-pressed", "false");
    b.appendChild(svgIcon("mic"));
    bindMic(b, ta);
    return b;
  }
  function bindMic(b, ta) {
    if (b._voice) return;
    b._voice = true;
    var downAt = 0, held = false, holdT = 0;
    b.addEventListener("pointerdown", function (e) {
      if (e.button !== 0) return;
      downAt = Date.now(); held = false;
      clearTimeout(holdT);
      holdT = setTimeout(function () { if (!dict) { held = true; dictStart(ta, b); } }, HOLD_MS);
    });
    function up() {
      clearTimeout(holdT);
      if (held) { held = false; dictStop(); downAt = -1; }
    }
    b.addEventListener("pointerup", up);
    b.addEventListener("pointerleave", up);
    b.addEventListener("click", function (e) {
      e.preventDefault();
      if (downAt === -1) { downAt = 0; return; }  // vừa kết thúc nói-thả
      if (dict && dict.ta === ta) dictStop(); else dictStart(ta, b);
    });
  }
  function attachAll(root) {
    if (!CAN_LISTEN) return;
    var mic = (root.querySelector ? root : document).querySelectorAll("[data-voice-for]");
    for (var i = 0; i < mic.length; i++) {
      var t = document.getElementById(mic[i].getAttribute("data-voice-for"));
      if (t) { mic[i].hidden = false; bindMic(mic[i], t); }
    }
    var list = (root.querySelectorAll ? root : document).querySelectorAll("textarea[data-voice]");
    for (var j = 0; j < list.length; j++) {
      var ta = list[j];
      if (ta._voiceMic) continue;
      ta._voiceMic = true;
      var b = micButton(ta), host = ta.parentNode;
      if (host.classList && host.classList.contains("composer")) {
        host.classList.add("has-voice");
        host.insertBefore(b, ta.nextSibling);
      } else {
        var row = el("span", "voice-row"); row.appendChild(b);
        host.insertBefore(row, ta.nextSibling);
      }
    }
    var wave = document.getElementById("mgr-voice");
    if (wave) wave.hidden = false;
  }
  document.addEventListener("input", function (e) {
    var t = e.target;
    if (t && t.tagName === "TEXTAREA" && !t.value.trim() && !(dict && dict.ta === t)) clearVoice(t.form);
  });
  // ô nhập Điều phối: giữ Space khi ô trống = nói-thả (như Claude Code)
  var spaceHold = false;
  document.addEventListener("keydown", function (e) {
    var t = e.target;
    if (!CAN_LISTEN || e.key !== " " || !t || t.id !== "mgr-text" || e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return;
    if (!e.repeat || t.value.trim()) return;
    e.preventDefault();
    if (!spaceHold && !dict) {
      spaceHold = true;
      t.value = "";
      var b = document.getElementById("mgr-mic");
      if (b) dictStart(t, b);
    }
  });
  document.addEventListener("keyup", function (e) {
    if (e.key === " " && spaceHold) { spaceHold = false; dictStop(); }
  });

  // ------------------------------------------------------------------ 2) hội thoại giọng nói với điều phối viên
  var EXIT = ["kết thúc", "thoát", "tạm biệt", "thoát chế độ giọng nói", "kết thúc hội thoại", "stop voice", "goodbye", "exit"];
  var STOP = ["dừng", "dừng lại", "thôi dừng lại", "dừng đi", "stop", "ngừng lại"];
  var vm = null;
  function VoiceMode() {
    var p = prefs(), self = this, listener = null, silenceT = 0, idleT = 0, cancelSpeak = null;
    vm = this;  // listen() kiểm `vm` — gán trước khi bắt đầu nghe
    var state = "idle", awaitRun = "", heard = "", pendingSince = 0;
    var ov = el("div", "voice-ov");
    ov.setAttribute("role", "dialog"); ov.setAttribute("aria-label", "Trò chuyện bằng giọng nói với điều phối viên");
    var top = el("div", "voice-top");
    var title = el("span", "voice-title", "Giọng nói · Điều phối viên");
    var langB = el("button", "btn ghost sm voice-lang"); langB.type = "button";
    var spkB = el("button", "btn ghost sm voice-spk"); spkB.type = "button";
    var closeB = el("button", "btn ghost sm icon"); closeB.type = "button"; closeB.title = "Kết thúc (Esc)"; closeB.setAttribute("aria-label", "Kết thúc trò chuyện bằng giọng nói");
    closeB.appendChild(svgIcon("x"));
    top.appendChild(title); top.appendChild(langB); top.appendChild(spkB); top.appendChild(closeB);
    var stage = el("div", "voice-stage");
    var orb = el("button", "voice-orb"); orb.type = "button";
    orb.appendChild(el("span", "voice-ring")); orb.appendChild(el("span", "voice-core"));
    var col = el("div", "voice-col");
    var stateP = el("p", "voice-state"); stateP.setAttribute("role", "status"); stateP.setAttribute("aria-live", "polite");
    var heardP = el("p", "voice-heard");
    var replyP = el("div", "voice-reply");
    col.appendChild(stateP); col.appendChild(heardP); col.appendChild(replyP);
    stage.appendChild(orb); stage.appendChild(col);
    var ctl = el("div", "voice-ctl");
    var stopB = el("button", "btn sm voice-stoprun", "Dừng lượt này"); stopB.type = "button"; stopB.hidden = true;
    var endB = el("button", "btn sm", "Kết thúc"); endB.type = "button";
    ctl.appendChild(stopB); ctl.appendChild(endB);
    var note = el("p", "voice-note", "Nhận dạng và đọc giọng do trình duyệt thực hiện (Chrome/Edge gửi âm thanh tới dịch vụ nhận dạng của hãng). " +
      "Nói “dừng lại” để dừng lượt đang chạy, “kết thúc” để thoát. Hub không bao giờ duyệt thao tác bằng giọng nói — duyệt bằng nút.");
    ov.appendChild(top); ov.appendChild(stage); ov.appendChild(ctl); ov.appendChild(note);
    document.body.appendChild(ov);
    document.documentElement.classList.add("voice-open");

    function paintPrefs() {
      langB.textContent = p.lang === "en-US" ? "EN" : "VI";
      langB.title = "Ngôn ngữ nhận dạng: " + (p.lang === "en-US" ? "English" : "Tiếng Việt") + " — bấm để đổi";
      spkB.textContent = p.speak ? "🔊 Đọc trả lời" : "🔇 Không đọc";
      spkB.setAttribute("aria-pressed", p.speak ? "true" : "false");
      spkB.title = p.speak ? (hasVoice(p.lang) || !TTS ? "Đang đọc to câu trả lời — bấm để tắt" : "Máy chưa có giọng đọc " + (p.lang === "en-US" ? "tiếng Anh" : "tiếng Việt") + " — trình duyệt dùng giọng mặc định")
                         : "Bấm để đọc to câu trả lời";
    }
    function setState(s, text) {
      state = s;
      ov.setAttribute("data-state", s);
      stateP.textContent = text;
      orb.setAttribute("aria-label", { listening: "Đang nghe — chạm để tạm dừng", paused: "Đã tạm dừng — chạm để nói", speaking: "Đang đọc — chạm để ngắt lời",
                                       thinking: "Đang chờ trả lời — chạm để nói thêm", sending: "Đang gửi" }[s] || "Chạm để nói");
      stopB.hidden = !(s === "thinking" && awaitRun);
    }
    function listen() {
      if (!vm) return;
      if (cancelSpeak) { cancelSpeak(); cancelSpeak = null; }
      if (listener) listener.abort();
      heard = ""; heardP.textContent = "";
      setState("listening", "Đang nghe…");
      clearTimeout(idleT);
      idleT = setTimeout(function () { if (state === "listening" && !heard) pause("Đã tạm dừng nghe — chạm quả cầu để nói tiếp."); }, IDLE_PAUSE_MS);
      listener = new Listener({
        lang: p.lang,
        onSpeech: function () { ov.classList.add("hearing"); },
        onText: function (fin, interim) {
          heard = (fin + (interim ? " " + interim : "")).trim();
          heardP.textContent = heard;
          clearTimeout(silenceT);
          if (heard) silenceT = setTimeout(utteranceDone, interim ? INTERIM_SILENCE_MS : SILENCE_MS);
        },
        onEnd: function (why, code) {
          ov.classList.remove("hearing");
          if (why === "error") { listener = null; pause(errText(code)); }
        },
      });
    }
    function pause(msg) {
      clearTimeout(silenceT); clearTimeout(idleT);
      if (listener) { listener.abort(); listener = null; }
      ov.classList.remove("hearing");
      setState("paused", msg || "Đã tạm dừng — chạm quả cầu để nói.");
    }
    function utteranceDone() {
      var text = heard.trim(), n = norm(text);
      if (!text || state !== "listening") return;
      clearTimeout(idleT);
      if (listener) { listener.abort(); listener = null; }
      ov.classList.remove("hearing");
      if (EXIT.indexOf(n) >= 0) { self.close(); return; }
      if (STOP.indexOf(n) >= 0) {
        if (awaitRun) { document.dispatchEvent(new CustomEvent("mgr:stop")); setState("thinking", "Đang dừng lượt trả lời…"); }
        else listen();
        return;
      }
      send(text);
    }
    function send(text) {
      var form = document.getElementById("mgr-form"), ta = document.getElementById("mgr-text");
      if (!form || !ta) { pause("Không thấy ô nhập của trang Điều phối."); return; }
      ta.value = text;
      var inp = form.querySelector('input[name="voice"]');
      if (!inp) { inp = document.createElement("input"); inp.type = "hidden"; inp.name = "voice"; form.appendChild(inp); }
      inp.value = "conversation";
      fire(ta);
      setState("sending", "Đang gửi…");
      replyP.textContent = "";
      pendingSince = Date.now();
      if (form.requestSubmit) form.requestSubmit(); else form.dispatchEvent(new Event("submit", { cancelable: true }));
    }
    function onSent(e) {
      if (!vm || state !== "sending") return;
      clearVoice(document.getElementById("mgr-form"));
      if (e.detail && e.detail.run) awaitRun = e.detail.run;
      setState("thinking", "Điều phối viên đang suy nghĩ…");
    }
    function onSendError(e) {
      if (!vm || state !== "sending") return;
      clearVoice(document.getElementById("mgr-form"));
      var msg = (e.detail && e.detail.error) || "Không gửi được.";
      replyP.textContent = msg;
      setState("speaking", "Không gửi được");
      cancelSpeak = speak(p.lang === "en-US" ? "Could not send." : "Không gửi được. " + msg, p.lang, p.rate, function () { cancelSpeak = null; if (state === "speaking") listen(); });
    }
    function onTool(e) {
      var d = e.detail || {};
      if (!vm || state !== "thinking" || d.run !== awaitRun) return;
      stateP.textContent = d.label ? "Đang dùng " + d.label + "…" : "Điều phối viên đang suy nghĩ…";
    }
    function onReply(e) {
      var d = e.detail || {};
      if (!vm || !awaitRun || d.run !== awaitRun) return;
      awaitRun = "";
      var src = d.node && d.node.querySelector("template.mm-src");
      var md = src ? src.content.textContent : (d.node ? d.node.textContent : "");
      var say = speakable(md, p.lang);
      replyP.textContent = say;
      if (state === "listening" && heard) return;  // người đang nói thêm — không cắt ngang
      if (!p.speak || !TTS || !say) { listen(); return; }
      if (listener) { listener.abort(); listener = null; }
      setState("speaking", "Đang đọc trả lời…");
      cancelSpeak = speak(say, p.lang, p.rate, function () { cancelSpeak = null; if (state === "speaking") listen(); });
    }
    function onRunEnd(e) {
      var d = e.detail || {};
      if (!vm || !awaitRun || d.run !== awaitRun || d.ok) return;
      setTimeout(function () {
        if (!vm || awaitRun !== d.run) return;  // trả lời vẫn tới
        awaitRun = "";
        var msg = p.lang === "en-US" ? "The turn ended with an error. See the screen." : "Lượt trả lời bị dừng hoặc lỗi. Xem trên màn hình.";
        replyP.textContent = msg;
        if (!p.speak || !TTS) { listen(); return; }
        setState("speaking", "Lượt trả lời bị dừng");
        cancelSpeak = speak(msg, p.lang, p.rate, function () { cancelSpeak = null; if (state === "speaking") listen(); });
      }, 2800);
    }
    function onKey(e) { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); self.close(); } }
    orb.addEventListener("click", function () {
      if (state === "listening") pause();
      else if (state !== "sending") listen();  // paused / speaking (ngắt lời) / thinking (nói thêm = steer lượt đang chạy)
    });
    stopB.addEventListener("click", function () { if (awaitRun) document.dispatchEvent(new CustomEvent("mgr:stop")); });
    endB.addEventListener("click", function () { self.close(); });
    closeB.addEventListener("click", function () { self.close(); });
    langB.addEventListener("click", function () { p.lang = p.lang === "en-US" ? "vi-VN" : "en-US"; voiceCache = null; savePrefs(p); paintPrefs(); if (state === "listening") listen(); });
    spkB.addEventListener("click", function () {
      p.speak = !p.speak; savePrefs(p); paintPrefs();
      if (!p.speak && cancelSpeak) { cancelSpeak(); cancelSpeak = null; listen(); }
    });
    document.addEventListener("mgr:sent", onSent);
    document.addEventListener("mgr:send_error", onSendError);
    document.addEventListener("mgr:tool", onTool);
    document.addEventListener("mgr:reply", onReply);
    document.addEventListener("mgr:run_end", onRunEnd);
    document.addEventListener("keydown", onKey, true);
    this.close = function () {
      if (vm !== self) return;
      vm = null;
      clearTimeout(silenceT); clearTimeout(idleT);
      if (listener) listener.abort();
      if (cancelSpeak) cancelSpeak();
      document.removeEventListener("mgr:sent", onSent);
      document.removeEventListener("mgr:send_error", onSendError);
      document.removeEventListener("mgr:tool", onTool);
      document.removeEventListener("mgr:reply", onReply);
      document.removeEventListener("mgr:run_end", onRunEnd);
      document.removeEventListener("keydown", onKey, true);
      document.documentElement.classList.remove("voice-open");
      ov.remove();
      var wave = document.getElementById("mgr-voice"); if (wave) { wave.setAttribute("aria-pressed", "false"); try { wave.focus(); } catch (_) {} }
    };
    paintPrefs();
    if (TTS) { try { TTS.getVoices(); TTS.speak(new SpeechSynthesisUtterance(" ")); } catch (_) {} }  // iOS: mở khoá đọc trong cú chạm của người
    listen();
    orb.focus();
  }
  document.addEventListener("click", function (e) {
    var b = e.target && e.target.closest && e.target.closest("#mgr-voice");
    if (!b) return;
    e.preventDefault();
    if (!CAN_LISTEN) return;
    if (vm) { vm.close(); return; }
    if (dict) dictStop();
    b.setAttribute("aria-pressed", "true");
    new VoiceMode();  // tự gán `vm`
  });

  // ------------------------------------------------------------------ khởi động + nội dung htmx / chuyển tab Điều phối
  function boot(root) {
    if (!CAN_LISTEN) {
      var why = !SR ? "Trình duyệt này chưa hỗ trợ nhận dạng giọng nói — dùng Chrome, Edge hoặc Safari."
                    : "Nhập bằng giọng nói cần kết nối an toàn (https hoặc localhost).";
      var ms = document.querySelectorAll("[data-voice-for], #mgr-voice");
      for (var i = 0; i < ms.length; i++) { ms[i].hidden = true; ms[i].title = why; }
      return;
    }
    attachAll(root || document);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { boot(document); });
  else boot(document);
  document.addEventListener("htmx:load", function (e) { boot(e.target || document); });
  // trang Điều phối thay #mgr-grid khi đổi hội thoại (app.js mgrGo) → gắn lại nút
  if (window.MutationObserver) {
    var mo = new MutationObserver(function (recs) {
      for (var i = 0; i < recs.length; i++) for (var j = 0; j < recs[i].addedNodes.length; j++) {
        var n = recs[i].addedNodes[j];
        if (n.nodeType === 1 && (n.id === "mgr-grid" || (n.querySelector && n.querySelector("textarea[data-voice], [data-voice-for]")))) { boot(n); return; }
      }
    });
    var start = function () { mo.observe(document.body, { childList: true, subtree: true }); };
    if (document.body) start(); else document.addEventListener("DOMContentLoaded", start);
  }
  window.cfhVoice = { speakable: speakable, chunks: chunks };  // cho test trình duyệt
})();
