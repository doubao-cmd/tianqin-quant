// 与实盘对照（P2-5）—— "我和系统的分歧在哪、谁对"
//
// 规格（路线图 §7 P2-5）：同一根 bar 上并排显示
//   策略实际：开多 3 手 @3268 → 止损 @3240（−840 元）
//   我 的 判 断：不进（理由：缩量）
// 只在 live 源 + 复看模式可用。
//
// ══ 三条口径（★ 写死，改必须注明日期）══
// C1  策略「决策 bar」= 成交 bar − 1。台账 `开仓时间` 是**成交时刻**（trades.py 从 FILL 取，
//     build_data 的 _snap5 向下吸附到 5min 网格），而 06 是「信号 bar 收盘下单 → 下一根开盘成交」
//     ⇒ 信号 bar = 成交 bar − 1。与 P1-5 逐笔跳转的落点（开仓 bar − 1）**必须同一定义**，
//     否则同一笔在「下一笔」和「对照」里会指向两根不同的 bar，用户会以为是两个机会。
// C2  平仓不设「决策 bar」，直接落在**成交 bar** 上 —— 06 的出场多是盘中触价（STOPLOSS 行），
//     没有「上一根收盘做决定」这回事，硬减 1 根就是编造。
// C3  ★ 本模块**故意允许看未来**（后续 N 根收益）。这是复看模式专用工具：不给未来就无法回答
//     「谁对」。因此 canSeeAnswers() 是硬门槛，训练模式下整块禁用（不是靠自觉）。
//
// ⚠️ 三条自审（写码前对过）：
//   1. 「我」的判断来自 marks（用户自产数据，训练模式也存），「策略」来自 trades（答案）
//      ⇒ 二者在训练模式下不能同屏，否则「策略实际」这一列就是答案本身。故整块 gate 在复看模式。
//   2. 同一根 bar 可能同时是「平旧仓」和「开新仓」的决策点（反手）⇒ 三个数组都保留，不合并成单值。
//   3. 逐条「谁对」不是证据：单笔盈亏是噪声。汇总区只报**计数**与**合计**，并强制写
//      「样本 <30 不做结论」。这与 P2-4 报告底部的警告同一套纪律。
import { state, canSeeAnswers } from '../core/state.js';
import { pd, tmap } from '../core/time.js';
import { DB } from '../data/index.js';
import { marksList, markAt } from './marks.js';

export const CMP_N = 10;      // 后续 N 根（与 P2-4 评分同口径，改这里两处一起变）

// ── 时间 → bar 索引：**包含该时刻的那根 bar**（最后一个 x <= t）──────────────
// ★ 2026-10-18 修正（真实数据暴露）：原来用「第一个 x >= t」在 15min / 日线上是错的。
//   台账时间被 _snap5 吸附到 5min 网格，15min 网格是 :00/:15/:30/:45 ⇒ 成交 21:35 的
//   「第一个 >= 21:35」= 21:45（**后一根**），而 21:35 实际落在 21:30 那根里。
//   5min 上看不出差别（吸附后正好落在网格点，两规则同解），所以只有换周期才暴露。
//   ⇒ 统一为「包含」口径，与 score.js 的 _scAbsOf 一致。
function _cmpAbsOf(xs, s) {
  if (!s) return null;
  const key = tmap(s);
  let lo = 0, hi = xs.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (tmap(xs[mid]) <= key) { ans = mid; lo = mid + 1; } else { hi = mid - 1; }
  }
  return ans < 0 ? null : ans;
}

// ── 数据源门槛：live 源才有 trades ────────────────────────────────────────
export function cmpHasTrades() {
  const s = DB.data && DB.data[state.sym];
  return !!(s && (s.trades || []).length);
}
// 唯一可用判据（路线图：只在 live 源 + 复看模式可用）
export function cmpEligible() { return canSeeAnswers() && cmpHasTrades(); }

// ── 台账索引（按 品种|周期 缓存；数据是静态的，切回来不必重建）──────────────
let _cmpCache = { key: null, rows: null, byOpen: null, byClose: null };
function _cmpIdx() {
  const key = state.sym + '|' + state.period;
  if (_cmpCache.key === key) return _cmpCache;
  const d = pd();
  const src = (DB.data[state.sym] || {}).trades || [];
  const rows = src.map(t => ({
    i: t.i, d: t.d, q: t.q, e: t.e, p: t.p, s: t.s, ps: t.ps,
    pnl: t.pnl, r: t.r, k: t.k, h: t.h,
    openAbs: _cmpAbsOf(d.x, t.t),
    closeAbs: t.x ? _cmpAbsOf(d.x, t.x) : null,
  }));
  const byOpen = new Map(), byClose = new Map();
  const _push = (m, k, v) => { if (!m.has(k)) m.set(k, []); m.get(k).push(v); };
  rows.forEach(r => {
    if (r.openAbs != null) _push(byOpen, r.openAbs, r);
    if (r.closeAbs != null) _push(byClose, r.closeAbs, r);
  });
  _cmpCache = { key, rows, byOpen, byClose };
  return _cmpCache;
}

// 某一根 bar 上策略的三类事实（C2：平仓落成交 bar；C1：开仓落成交 bar − 1）
function _cmpFacts(abs) {
  const C = _cmpIdx();
  const opens = C.byOpen.get(abs + 1) || [];          // 本 bar 收盘决定 → 下一根开盘成交
  const closes = C.byClose.get(abs) || [];            // 本 bar 内成交的平仓
  const holds = C.rows.filter(r => r.openAbs != null && r.openAbs <= abs &&
    (r.closeAbs == null || r.closeAbs > abs));         // 本 bar 收盘时仍在场内
  return { opens, closes, holds };
}

// ── 分歧判定（唯一分类表；改口径只改这里）─────────────────────────────────
const C_NONE = '#6b7280', C_OK = '#22c55e', C_BAD = '#ef4444',
      C_WARN = '#f59e0b', C_INFO = '#60a5fa', C_GREY = '#9aa3af';

export function cmpDivergence(opens, holds, mark) {
  if (!mark) return { k: 'unmarked', label: '未表态', c: C_NONE };
  const v = mark.verdict;
  if (opens.length) {
    const sd = opens[0].d;                              // 多 / 空
    if (v === '会进多' || v === '会进空') {
      const md = (v === '会进多') ? '多' : '空';
      return md === sd ? { k: 'same', label: '同向一致', c: C_OK }
                       : { k: 'opposite', label: '反向分歧', c: C_BAD };
    }
    if (v === '不进') return { k: 'avoid', label: '我规避', c: C_WARN };
    return { k: 'unsure', label: '我犹豫', c: C_GREY };
  }
  if (holds.length) {
    if (v === '会进多' || v === '会进空') return { k: 'late', label: '我迟到（策略已在场内）', c: C_INFO };
    if (v === '不进') return { k: 'inpos', label: '策略持仓中，我标不进', c: C_GREY };
    return { k: 'unsure', label: '我犹豫', c: C_GREY };
  }
  if (v === '会进多' || v === '会进空') return { k: 'frontrun', label: '我抢跑（策略无信号）', c: C_INFO };
  if (v === '不进') return { k: 'agree', label: '一致（都不做）', c: C_OK };
  return { k: 'unsure', label: '我犹豫', c: C_GREY };
}

// ── 后续 N 根收益（C3：允许看未来，仅复看模式）──────────────────────────────
function _cmpFwd(d, a, N) {
  const hi = Math.min(a + N, d.x.length - 1);
  if (hi <= a) return null;
  const c0 = d.c[a], c1 = d.c[hi];
  if (!c0) return null;
  return { n: hi - a, ret: (c1 - c0) / c0 };
}

// ── 当前 bar 的对照（回放中用）────────────────────────────────────────────
export function cmpAt(abs) {
  if (!cmpEligible()) return null;
  const d = pd();
  if (!d || !d.x.length) return null;
  const a = Math.max(0, Math.min(d.x.length - 1, abs == null ? 0 : abs));
  const barX = d.x[a];
  const mark = markAt(barX);
  const f = _cmpFacts(a);
  return { abs: a, barX, mark, div: cmpDivergence(f.opens, f.holds, mark),
           opens: f.opens, closes: f.closes, holds: f.holds };
}

// ── 分歧台账（所有带标记的 bar，不看回放位置）───────────────────────────────
export function cmpRows() {
  if (!cmpEligible()) return [];
  const d = pd();
  const out = [];
  marksList(state.sym, state.period).forEach(m => {
    // 身份一律认 barX（marks 的既有纪律：数据重建后 abs 会漂，时间戳不会）
    const a = (m.abs != null && d.x[m.abs] === m.barX) ? m.abs : _cmpAbsOf(d.x, m.barX);
    if (a == null) return;
    const f = _cmpFacts(a);
    const fw = _cmpFwd(d, a, CMP_N);
    const div = cmpDivergence(f.opens, f.holds, m);
    // 我方向的收益：会进多 → +ret；会进空 → −ret；不进 → |ret|（幅度，不是方向，见 P2-4 修订 1）
    let myRet = null;
    if (fw) {
      if (m.verdict === '会进多') myRet = fw.ret;
      else if (m.verdict === '会进空') myRet = -fw.ret;
      else if (m.verdict === '不进') myRet = Math.abs(fw.ret);
    }
    const sp = f.opens.filter(t => t.pnl != null);
    out.push({
      abs: a, barX: m.barX, verdict: m.verdict, note: m.note || '',
      opens: f.opens, closes: f.closes, holds: f.holds, div,
      fwdN: fw ? fw.n : 0, fwdRet: fw ? fw.ret : null, myRet,
      stratPnl: sp.length ? sp.reduce((s, t) => s + t.pnl, 0) : null,
      stratQty: sp.reduce((s, t) => s + (t.q || 0), 0),
    });
  });
  return out;
}

// ── 汇总（只报计数与合计，不做单条判决）────────────────────────────────────
export function cmpSummary(rows) {
  const byKind = {};
  rows.forEach(r => { byKind[r.div.k] = (byKind[r.div.k] || 0) + 1; });
  // 有方向的标记（会进多/会进空）：我方向后续 N 根为正 = 命中
  const dir = rows.filter(r => r.verdict === '会进多' || r.verdict === '会进空');
  const dirHit = dir.filter(r => r.myRet != null && r.myRet > 0).length;
  const dirN = dir.filter(r => r.myRet != null).length;
  // 被规避的交易：合计盈亏（负 = 你躲对了；正 = 你漏掉了）
  const avoid = rows.filter(r => r.div.k === 'avoid' && r.stratPnl != null);
  const avoidPnl = avoid.reduce((s, r) => s + r.stratPnl, 0);
  const same = rows.filter(r => r.div.k === 'same' && r.stratPnl != null);
  const samePnl = same.reduce((s, r) => s + r.stratPnl, 0);
  return {
    n: rows.length, byKind,
    dirN, dirHit, hitRate: dirN ? dirHit / dirN : null,
    avoidN: avoid.length, avoidPnl,
    sameN: same.length, samePnl,
  };
}
