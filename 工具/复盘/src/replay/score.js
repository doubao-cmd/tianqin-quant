// 事后评分（P2-4）—— 会话结束的产出，回答"我这段时间的判断到底有没有用"
//
// 规格（路线图 §7 P2-4）+ 两处修订（2026-10-18）：
//
// ★ 修订 1：路线图原文「标记『不进』的 bar 后续走势 —— 如果后续 60% 是下跌的（而他做多），
//   说明他的判断是反的」。**「不进」没有方向**，无法算对错。
//   ⇒ 拆成两个指标：
//     · 会进多 / 会进空 → 方向命中率（有方向，可算）
//     · 不进           → 规避质量 = 标记后 N 根 |收益| 均值 ÷ 全样本 |收益| 均值
//                        >1 说明你规避的确实是高波动段；≈1 说明你和随机没差别
//
// ★ 修订 2：路线图指标「我的止损执行率 = 触发止损时是否真的平了」在本模拟器里**恒为 100%**
//   —— book.js 的 R2 是引擎自动止损，用户无法"想扛"。指标是空的。
//   ⇒ 换成两个真能测的：`manualShare`（手动平仓占比）+ `avgStopProgress`（手动平仓时
//     价格已走到止损距离的百分之几；接近 100% = 你总在贴边平，很小 = 你爱提前砍）
//
// ★ 不越界：所有"后续走势"只用到会话内已发生的 bar（上限 = min(abs+N, endAbs, 数据末根)）。
// ★ 不存明细：逐笔从 R.book 纯函数重放派生（book.js R8 已保证可重放），存两份必然漂移。
import { state } from '../core/state.js';
import { pd } from '../core/time.js';
import { DB } from '../data/index.js';
import { simulateBook, simCfg } from './book.js';
import { marksList } from './marks.js';

// ── 时间 → 绝对 bar 索引（d.x 升序；取「<= 目标」的最大下标）───────────────
function _scAbsOf(xs, s) {
  if (!s) return null;
  const key = String(s).slice(0, 16);
  let lo = 0, hi = xs.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (String(xs[mid]).slice(0, 16) <= key) { ans = mid; lo = mid + 1; } else { hi = mid - 1; }
  }
  return ans < 0 ? null : ans;
}

// ── MAE / MFE（bar 口径）────────────────────────────────────────────────
// ⚠️ 与台账 EXCURSION（逐 tick/逐分钟）口径不同：K 线口径**系统性低估**（取不到盘中瞬时极值）
//    ⇒ 输出标 source:'kline'，两者不混用统计（沿用二期纪律）
function _scExc(d, t, mult) {
  const a = Math.max(0, t.entryBar), b = Math.min(t.exitBar, d.x.length - 1);
  let hi = -Infinity, lo = Infinity;
  for (let k = a; k <= b; k++) {
    if (d.h[k] > hi) hi = d.h[k];
    if (d.l[k] < lo) lo = d.l[k];
  }
  if (!isFinite(hi) || !isFinite(lo)) return { mfe: null, mae: null };
  if (t.dir === '多') return { mfe: (hi - t.entryPx) * mult, mae: (lo - t.entryPx) * mult };
  return { mfe: (t.entryPx - lo) * mult, mae: (t.entryPx - hi) * mult };
}

// ── 标记后续走势 ────────────────────────────────────────────────────────
// 只用会话内已发生 bar：hi = min(abs+N, endAbs, len-1)；不足 2 根则跳过（不给假数）
// ★ 五期 T2.3：标记 tab 复用同一函数（导出一份，避免两处各写一遍口径漂移）
export function _scFwd(d, abs, N, endAbs) {
  const hi = Math.min(abs + N, endAbs, d.x.length - 1);
  if (hi <= abs) return null;
  const c0 = d.c[abs], c1 = d.c[hi];
  if (!c0) return null;
  return { n: hi - abs, ret: (c1 - c0) / c0 };
}

const _scMed = a => {
  if (!a.length) return null;
  const s = a.slice().sort((x, y) => x - y), m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};

// ═══════════════════════════════════════════════════════════════════════
// 主入口
//   opt = { sym, period, actions, startAbs, endAbs, N=10 }
//   ★ 全部显式传入：收尾时 state.sym / state.replay 可能已被切走
// ═══════════════════════════════════════════════════════════════════════
export function scoreSession(opt) {
  const sym = opt.sym || state.sym;
  const period = opt.period || state.period;
  const actions = opt.actions || [];
  const startAbs = opt.startAbs == null ? 0 : opt.startAbs;
  const N = opt.N || 10;
  const d = pd(sym, period);
  const cfg = simCfg(sym);
  const endAbs = Math.min(
    opt.endAbs == null ? d.x.length - 1 : opt.endAbs,
    d.x.length - 1);

  // ── 1. 逐笔（从 book 纯函数重放派生）──────────────────────────────────
  const R = simulateBook(actions, d, cfg, endAbs);
  const trades = R.closed.map(t => {
    const ex = _scExc(d, t, cfg.mult);
    // 止损进度：价格从开仓朝止损方向走了「开仓→止损」全程的百分之几
    // ★ 只在「手动平仓 且 亏损方向」时有意义（盈利平仓 moved<0，进度是负的，无意义）
    const dist = (t.entryPx != null && t.stopPx != null) ? Math.abs(t.entryPx - t.stopPx) : 0;
    const moved = (t.entryPx != null) ? (t.dir === '多' ? (t.entryPx - t.exitPx) : (t.exitPx - t.entryPx)) : 0;
    return {
      dir: t.dir, qty: t.qty, reason: t.reason,
      entryBar: t.entryBar, exitBar: t.exitBar, holdBars: t.holdBars,
      entryPx: t.entryPx, exitPx: t.exitPx, pnl: t.pnl,
      mfe: ex.mfe, mae: ex.mae, excursion_source: 'kline',
      stopProgress: (t.reason === '手动' && dist > 0 && moved > 0)
        ? Math.min(2, moved / dist) : null,
    };
  });

  const nSim = trades.length;
  const realized = trades.reduce((s, t) => s + (t.pnl || 0), 0);
  const avgHold = nSim ? trades.reduce((s, t) => s + t.holdBars, 0) / nSim : null;
  const manual = trades.filter(t => t.reason === '手动');
  const manualShare = nSim ? manual.length / nSim : null;
  const sps = manual.map(t => t.stopProgress).filter(v => v != null);
  const avgStopProgress = sps.length ? sps.reduce((a, b) => a + b, 0) / sps.length : null;

  // ── 2. 策略同区间对照（★ 方案 A：abs ∈ [startAbs, endAbs] 的全部真实成交）──
  const all = (DB.data[sym] && DB.data[sym].trades) || [];
  const strat = all.filter(t => {
    const a = _scAbsOf(d.x, t.t);
    return a != null && a >= startAbs && a <= endAbs && t.pnl != null;
  });
  const stratPnl = strat.reduce((s, t) => s + t.pnl, 0);
  const stratN = strat.length;
  // 策略中位持仓根数：台账记的是「持仓分钟」⇒ 换算成根
  const pMin = period === '1440' ? 1440 : +period;
  const stratHoldBars = strat.length
    ? _scMed(strat.map(t => (t.h || 0) / pMin).filter(v => v > 0)) : null;

  // ── 3. 过度交易指数 ────────────────────────────────────────────────────
  const overTrade = (stratN > 0) ? nSim / stratN : (nSim > 0 ? Infinity : null);

  // ── 4. 标记：方向命中率 + 规避质量 ────────────────────────────────────
  const mk = marksList(sym, period).filter(m => {
    const a = (m.abs != null) ? m.abs : _scAbsOf(d.x, m.barX);
    return a != null && a >= startAbs && a <= endAbs;
  });
  const hits = [], avoids = [];
  const mkRows = mk.map(m => {
    const a = (m.abs != null) ? m.abs : _scAbsOf(d.x, m.barX);
    const fw = _scFwd(d, a, N, endAbs);
    let hit = null, ret = fw ? fw.ret : null;
    if (fw) {
      if (m.verdict === '会进多') { hit = fw.ret > 0; hits.push(hit); }
      else if (m.verdict === '会进空') { hit = fw.ret < 0; hits.push(hit); }
      else if (m.verdict === '不进') { avoids.push(Math.abs(fw.ret)); }
    }
    return { barX: m.barX, abs: a, verdict: m.verdict, note: m.note,
             fwdN: fw ? fw.n : 0, fwdRet: ret, hit };
  });
  const hitRate = hits.length ? hits.filter(Boolean).length / hits.length : null;

  // 对照：全样本（同品种同周期、与会话同长度）的 |N 根收益| 均值
  let baseAbs = null, baseN = 0, baseSum = 0;
  for (let i = Math.max(0, startAbs); i <= endAbs; i++) {
    const fw = _scFwd(d, i, N, endAbs);
    if (fw) { baseSum += Math.abs(fw.ret); baseN++; }
  }
  baseAbs = baseN ? baseSum / baseN : null;
  const avoidQ = (avoids.length && baseAbs)
    ? (avoids.reduce((a, b) => a + b, 0) / avoids.length) / baseAbs : null;

  return {
    sym, period, N,
    startAbs, endAbs,
    nSim, realized, avgHoldBars: avgHold,
    manualShare, avgStopProgress,
    stratN, stratPnl, stratHoldBars, overTrade,
    nMark: mk.length, hitRate, nHitSample: hits.length,
    avoidQ, nAvoidSample: avoids.length,
    baseAbsRet: baseAbs,
    trades, marks: mkRows,
  };
}
