/* Văn phòng 3D — bộ điều nhịp khung vẽ (docs/dev-notes/office-3d-perf.md, phase 2). Module thuần: không import three, không DOM,
 * test được bằng Node (tests/office_pacer_check.mjs).
 *
 * Vì sao có module này: phase 1 dùng EMA khoảng rAF (`gapEst`) + giữ nhịp 60 thêm 400 ms sau tương tác. Khi CPU bị tranh (máy thật có
 * ~19 tiến trình claude), rAF đến muộn xen kẽ → khoảng khung 16,7/33,4 ms lẫn lộn (đổi nhịp 60–108 lần / 5 s, judder 8–15 ms) —
 * đúng kiểu "giật" mắt thấy, dù FPS trung bình vẫn ~50. Đều 30 FPS nhìn mượt hơn hẳn 60/30 xen kẽ, nên pacer:
 *  1. ước lượng chu kỳ màn hình `vsync` = phân vị 20 của 60 khoảng rAF gần nhất (bỏ khoảng > 100 ms) — không trôi lên khi máy bận;
 *  2. chọn bội số d: động = base (màn 60 Hz → mọi vsync; 120/144 Hz → cách 1), yên = 2·base; rAF dưới 50 Hz (máy đã không theo kịp)
 *     → luôn d = 1, không bỏ rAF nào;
 *  3. bộ điều tiết quá tải: ≥ 3/30 khung động đến trễ (> 1,5·d·vsync) → khoá d = 2·base trong lockMs (2 s, khoá lại trong 1,5 s sau
 *     khi nhả thì nhân đôi tới 8 s; sạch 10 s về 2 s); nhả khoá = thử lại 60. Gai đơn lẻ (> 4 vsync), cửa sổ hitch() (việc nặng biết
 *     trước: vào đi bộ, dựng lại World…) và lúc đứng yên không tính;
 *  4. dt lượng tử theo vsync → khung 16,7/33,4 cho dt đúng bội số, chuyển động không lệch theo độ trễ rAF.
 * Việc phụ (vẽ lại thẻ…) đặt qua onSkip(): chạy trong rAF bị bỏ qua (khung không vẽ), hoặc trên khung vẽ nếu đã chờ > 250 ms. */
import { frameGate } from './util.js';

const RING = 60, LATE_N = 30, LATE_MAX = 3;
const LOCK_MIN = 2000, LOCK_MAX = 8000, RELOCK_WIN = 1500, CLEAN_MS = 10000;
const MOTION_HOLD = 150, JOB_WAIT = 250;

export function createPacer(opts) {
  const o = opts || {};
  const clock = o.now || (() => performance.now());
  const iv = new Float64Array(RING), tmp = new Float64Array(RING);
  const late = new Uint8Array(LATE_N);
  let ivN = 0, ivI = 0, vsync = 1000 / 60, prevTs = 0, lastDraw = 0;
  let motionUntil = -1, moving = false, dEff = 2;
  let lateN = 0, lateI = 0, lateSum = 0;
  let cap = 0, capUntil = 0, lockMs = LOCK_MIN, locks = 0, lastRelease = -1e12, lastLockEnd = -1e12, misses = 0;
  let hitchUntil = -1;
  const jobs = new Map(); // key → { fn, at }

  function clearLate() { late.fill(0); lateN = 0; lateI = 0; lateSum = 0; }
  function base() { return vsync < 11 ? 2 : 1; }
  function runJob(ts, onlyOld) {
    for (const [k, j] of jobs) {
      if (onlyOld && ts - j.at < JOB_WAIT) continue;
      jobs.delete(k);
      try { j.fn(); } catch (_) { /* việc phụ hỏng không được làm hỏng vòng vẽ */ }
      return;
    }
  }

  return {
    // gọi MỖI rAF; trả true khi khung này nên vẽ
    shouldDraw(ts, motion) {
      if (prevTs) {
        const g = ts - prevTs;
        if (g > 0 && g <= 100) {
          iv[ivI] = g; ivI = (ivI + 1) % RING; if (ivN < RING) ivN++;
          if (ivN >= 10) {
            const s = tmp.subarray(0, ivN);
            s.set(ivN === RING ? iv : iv.subarray(0, ivN));
            s.sort();
            vsync = s[Math.floor(ivN * 0.2)];
          }
        }
      }
      prevTs = ts;
      if (motion) motionUntil = ts + MOTION_HOLD;
      moving = !!motion || ts < motionUntil;
      if (cap && ts >= capUntil) { cap = 0; lastRelease = ts; lastLockEnd = ts; clearLate(); }
      if (!cap && lockMs > LOCK_MIN && ts - lastLockEnd >= CLEAN_MS) lockMs = LOCK_MIN;
      const b = base();
      dEff = vsync > 20 ? 1 : Math.max(moving ? b : 2 * b, cap);
      if (!lastDraw) return true;
      const go = frameGate(ts, lastDraw, dEff * vsync, vsync);
      if (!go && jobs.size) runJob(ts, false);
      return go;
    },
    // gọi khi THỰC SỰ vẽ; trả dt (giây) đã lượng tử theo vsync
    drew(ts) {
      let dt = vsync / 1000;
      if (lastDraw) {
        const g = ts - lastDraw;
        dt = Math.min(0.1, Math.max(vsync / 1000, (Math.round(g / vsync) * vsync) / 1000));
        const b = base();
        if (moving && vsync <= 20 && dEff === b && ts >= hitchUntil && g <= 4 * vsync) {
          const isLate = g > 1.5 * dEff * vsync ? 1 : 0;
          if (isLate) misses++;
          lateSum += isLate - late[lateI]; late[lateI] = isLate; lateI = (lateI + 1) % LATE_N; if (lateN < LATE_N) lateN++;
          if (lateSum >= LATE_MAX) {
            if (ts - lastRelease < RELOCK_WIN) lockMs = Math.min(LOCK_MAX, lockMs * 2);
            cap = 2 * b; capUntil = ts + lockMs; locks++; clearLate();
          }
        }
      }
      lastDraw = ts;
      if (jobs.size) runJob(ts, true);
      return dt;
    },
    // việc nặng biết trước trong ms tới (vào đi bộ, dựng lại World, đổi chất lượng…): khung trễ không tính là quá tải
    hitch(ms, ts) { hitchUntil = Math.max(hitchUntil, (ts == null ? clock() : ts) + (ms || 500)); },
    // việc phụ: chạy trong rAF không vẽ, hoặc trên khung vẽ khi đã chờ > 250 ms. Cùng key → thay việc cũ (giữ thời điểm chờ).
    onSkip(fn, key) {
      const k = key == null ? fn : key, j = jobs.get(k);
      jobs.set(k, { fn, at: j ? j.at : (prevTs || clock()) });
    },
    reset() {
      ivN = 0; ivI = 0; prevTs = 0; lastDraw = 0; motionUntil = -1; moving = false; cap = 0; capUntil = 0; clearLate();
    },
    state() { return { vsync, d: dEff, cap, lockMs, locks, misses, moving }; },
  };
}

// Watchdog chất lượng (đo theo nhịp rAF, đặt TRƯỚC cổng vẽ): trả 'ok' | 'low' (hạ tier) | 'slow' (tắt 3D, fallback).
// Phase 1 coi "gap > 1 s sau khung bình thường" là khựng — nhưng sau khung đứng đầu tiên điều kiện đó sai, nên GPU/driver đứng
// 27 s + 72 s rồi rAF 1 Hz (đo được ở n=19 ×2) bị tính là "máy chậm" → tier low vĩnh viễn → fallback "slow". Ở đây:
//  - khoảng > 1000 ms = ĐỨNG (stall): bỏ cửa sổ đo, không đếm cho tới khi có 30 khoảng liên tiếp < 100 ms;
//  - chỉ cửa sổ rAF ĐỀU mà chậm (phân vị 80 ≤ 1,5 × trung vị) mới được tính vào lowFps / slowFps;
//  - không có focus: không đếm.
// st = {} giữ trạng thái giữa các lần gọi; gap = khoảng rAF (ms, 0 ở khung đầu).
export function watchdogStep(st, ts, gap, focused, lowFps, slowFps, tierHigh) {
  if (!st.init) { st.init = true; st.start = ts + 2000; st.frames = 0; st.gaps = []; st.okRun = 30; st.low = 0; st.slow = 0; }
  if (!focused || gap > 1000) {
    st.start = ts + 1000; st.frames = 0; st.gaps.length = 0;
    if (gap > 1000) { st.okRun = 0; st.low = 0; st.slow = 0; }
    if (!focused) { st.low = 0; st.slow = 0; }
    return 'ok';
  }
  if (st.okRun < 30) {
    st.okRun = gap > 0 && gap < 100 ? st.okRun + 1 : 0;
    if (st.okRun < 30) return 'ok';
    st.start = ts; st.frames = 0; st.gaps.length = 0;
  }
  if (ts < st.start) return 'ok';
  st.frames++;
  if (gap > 0) st.gaps.push(gap);
  if (ts - st.start < 5000) return 'ok';
  const fps = st.frames / ((ts - st.start) / 1000);
  const g = st.gaps.slice().sort((a, b) => a - b), med = g[g.length >> 1] || 0, p80 = g[Math.floor(g.length * 0.8)] || 0;
  st.start = ts; st.frames = 0; st.gaps.length = 0;
  if (!(med > 0 && p80 <= 1.5 * med)) return 'ok'; // không đều (giật/đứng xen kẽ) → không kết luận "chậm"
  if (tierHigh) {
    st.low = fps < lowFps ? st.low + 1 : 0;
    if (st.low >= 2) { st.low = 0; st.slow = 0; return 'low'; }
  }
  st.slow = fps < slowFps ? st.slow + 1 : 0;
  if (st.slow >= 3) { st.slow = 0; return 'slow'; }
  return 'ok';
}
