// P3 指标（P3-1 / P3-3 / P3-4）—— 全部是「事后分组」的纯函数
//
// ══ 三条纪律（写进代码，不是写在文档里就完）══
// D1  **事后分组不构成因果**：时段/持有期/分位都是"看完结果再切样本"。
//     「夜盘亏得多」≠「别做夜盘」——夜盘本来就波动大，这是"提前出场家族"5 次失败的同一个陷阱。
//     ⇒ 每个面板都必须自带警告，且**只给分布与分位，不给"开/不开"的结论**（sessions.py §8 红线 5 同款）。
// D2  **样本量门槛写死在代码里**：P3-3 的历史频率要 n ≥ 60 才显示，否则只显示"样本不足"。
//     7 天数据算出来的"延续率"是噪声，展示它就是骗人。
// D3  **出场后路径从「平仓当根的下一根」起算**。平仓当根内无法确定平在什么位置，
//     把当根高低价算进去会系统性夸大"错过"。
//
// 命名前缀 _sg*：拼接后所有模块共享一个顶层作用域（build_html.py 的 strip_module）。
import { state } from '../core/state.js';
import { pd, tmap } from '../core/time.js';
import { DB } from '../data/index.js';

// 时段划分与 sessions.py:31-34 逐字一致（早盘 9.0–11.5 / 午盘 13.5–15.0 / 夜盘 21.0–23.0）
const _SG_SEGS = [['早盘', 9.0, 11.5], ['午盘', 13.5, 15.0], ['夜盘', 21.0, 23.0]];
export const SEG_NAMES = ['早盘', '午盘', '夜盘'];

// bar 标签 → 时段（标签已是北京时间，不需再加 8 小时）
export function segOf(x) {
  const s = String(x == null ? '' : x);
  if (s.length < 16) return '日线';            // 日线标签 'YYYY-MM-DD' 没有时刻
  const h = (+s.slice(11, 13)) + (+s.slice(14, 16)) / 60;
  if (!isFinite(h)) return '—';
  for (const [n, a, b] of _SG_SEGS) if (h >= a && h < b) return n;
  return '休市';
}

// 时间 → bar 索引（包含判定：最后一个 x <= t，与 compare.js/_scAbsOf 同口径）
function _sgAbsOf(xs, s) {
  if (!s) return null;
  const key = tmap(s);
  let lo = 0, hi = xs.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (tmap(xs[mid]) <= key) { ans = mid; lo = mid + 1; } else { hi = mid - 1; }
  }
  return ans < 0 ? null : ans;
}

const _sgPct = (n, d) => d ? Math.round(n / d * 100) : null;

// ═══════════════════════════════════════════════════════════════════════
// P3-1 分组统计：时段 / 交易日 / 持有根数桶
//   ★ 跨全部品种聚合（单品种只有 ~23 笔，切三组就是 8 笔，没有意义）
//   period 只用于「持有分钟 → 根数」换算
// ═══════════════════════════════════════════════════════════════════════
export function segStats(period) {
  const pMin = period === '1440' ? 1440 : +period;
  const seg = {}, days = {};
  const HOLD_KEYS = ['0-5', '6-15', '16-30', '30+'];
  const hold = {};
  HOLD_KEYS.forEach(k => { hold[k] = { n: 0, sum: 0, win: 0 }; });
  let n = 0, skipped = 0;

  Object.keys(DB.data).forEach(k => {
    const dd = DB.data[k];
    (dd.trades || []).forEach(t => {
      if (t.pnl == null) { skipped++; return; }          // 未平 / 接管：不进分母
      n++;
      const s = segOf(t.t);
      const a = seg[s] || (seg[s] = { n: 0, sum: 0, win: 0 });
      a.n++; a.sum += t.pnl; if (t.pnl > 0) a.win++;

      const dy = t.day || String(t.t || '').slice(0, 10);
      const g = days[dy] || (days[dy] = { n: 0, sum: 0 });
      g.n++; g.sum += t.pnl;

      const bars = (t.h || 0) / pMin;
      const bk = bars <= 5 ? '0-5' : bars <= 15 ? '6-15' : bars <= 30 ? '16-30' : '30+';
      hold[bk].n++; hold[bk].sum += t.pnl; if (t.pnl > 0) hold[bk].win++;
    });
  });

  const segRows = SEG_NAMES.concat(['休市']).filter(s => seg[s] && seg[s].n)
    .map(s => ({ name: s, ...seg[s], wr: _sgPct(seg[s].win, seg[s].n) }));
  const dayRows = Object.keys(days).sort().map(d => ({ day: d, ...days[d] }));
  const holdRows = HOLD_KEYS.filter(b => hold[b].n)
    .map(b => ({ bucket: b, ...hold[b], wr: _sgPct(hold[b].win, hold[b].n) }));
  return { n, skipped, seg: segRows, days: dayRows, hold: holdRows };
}

// ═══════════════════════════════════════════════════════════════════════
// A19 池子周期分组（方案 §4.8(a)）
//   「池子资格 = 5 个交易日的交易许可」，**不是持有一个仓位 5 天**。
//   MA13 在这 5 天里自己进出十几次 ⇒ 产出是「这 5 天在这些品种上做了 N 笔、盈亏 X」。
//
// ★ 池子来源 = bundle `_meta.pool_epochs`（构建期从台账 START 行解析的真实留档）。
//   刻意**不用** workbench.json 的 selected：那只有一个当前值，改一次就覆盖，
//   拿它回填历史 = 用今天的事实解释上周的交易（隐性未来函数）。
//
// ★ 切段规则（简单到能一眼验）：
//   1. 纪元 = 台账里两次「池子名单变化」之间；同名单重启不算换池
//   2. 纪元内交易日按日历顺序排，每 5 个交易日一段（C1/C2…），尾段不足 5 日标出来
//   3. 成交归属按**开仓 bar 时刻**（`t`）落在哪个纪元，不按 `day`
//      —— 周五夜盘的 `day` 是下周一，按 day 分会把它算进还没生效的新池子
//
// ★ 三件事必须说清（否则这张表会被当成答案）：
//   · 每段样本 ~20 笔 ⇒ 只能记账，不能判断「这个池子好不好」
//   · 池内品种若没有 K 线缓存（FU/BU/MA/SA），它的成交**看不到**，不是没成交
//   · 换池时有持仓 ⇒ 该段「有成交腿」可能比池子腿多（§3.10 强制保留，不是 bug）
// ═══════════════════════════════════════════════════════════════════════
export const CYCLE_DAYS = 5;

let _sgCal = null;
// 交易日历 = bundle 里各品种 5min 的 day 并集（夜盘已归下一交易日，不含周末/节假日）
function _sgCalendar() {
  if (_sgCal) return _sgCal;
  const set = Object.create(null);
  Object.keys(DB.data || {}).forEach(k => {
    const p5 = ((DB.data[k] || {}).periods || {})['5'] || {};
    (p5.day || []).forEach(v => { if (v) set[v] = 1; });
  });
  _sgCal = Object.keys(set).sort();
  return _sgCal;
}

const _sgWr = (win, n) => (n ? Math.round(win / n * 100) : null);

export function poolCycles() {
  const meta = DB.meta || {};
  const pe = meta.pool_epochs || {};
  const eps = (pe.epochs || []).slice();
  const out = {
    ok: false, cycle_days: pe.cycle_days || CYCLE_DAYS,
    source: pe.source || '', restarts: pe.restarts || 0,
    rows: [], n: 0, total: 0, open: 0, skipped: 0, why: '',
  };
  if (!eps.length) {
    out.why = 'bundle 缺 _meta.pool_epochs（重跑 build_data.py：池子纪元从台账 START 行解析）';
    return out;
  }
  const cal = _sgCalendar();
  const calSet = Object.create(null);
  cal.forEach(d => { calSet[d] = 1; });

  // 打平全部成交（带品种键）。无开仓时刻的笔无法归属纪元 ⇒ 计入 skipped。
  const all = [];
  let noT = 0;
  Object.keys(DB.data).forEach(k => {
    (DB.data[k].trades || []).forEach(t => {
      if (t.t == null) { noT++; return; }
      all.push({ k, t });
    });
  });

  const rows = [];
  const empty = [];
  let used = 0, nAll = 0, sumAll = 0, openAll = 0;
  eps.forEach((ep, i) => {
    const from = ep.t, to = (eps[i + 1] || {}).t || null;
    const d0 = String(from).slice(0, 10), d1 = to ? String(to).slice(0, 10) : null;
    const inEp = all.filter(x => x.t.t >= from && (to == null || x.t.t < to));
    const dayKeyOf = t => ((t.day && calSet[t.day]) ? t.day : String(t.t).slice(0, 10));
    // 纪元覆盖的交易日 = 有成交的日子 ∪ 被完整覆盖的日子
    //   ★ 只取「完整覆盖」（from < d < to）：纪元首/尾那两天可能被前后两个池子分掉
    //     （周五夜盘 + 周日重启都落在同一个交易日 10-19），允许两边各记一次，
    //     因为它就是两个池子各跑了一段——不是重复计入，成交按时刻只会落一边。
    const covered = cal.filter(d => d > d0 && (d1 == null || d < d1));
    const dset = Object.create(null);
    inEp.forEach(x => { dset[dayKeyOf(x.t)] = 1; });
    covered.forEach(d => { dset[d] = 1; });
    const days = Object.keys(dset).sort();
    if (!days.length) { empty.push({ ep: ep.i || (i + 1), from }); return; }
    const pool = (ep.keys || []).slice();
    const poolSet = Object.create(null);
    pool.forEach(k => { poolSet[k] = 1; });
    const absent = pool.filter(k => !DB.data[k]);          // 池内但 bundle 没有 ⇒ 看不到成交
    const firstPartial = String(from).slice(11) > '09:00'; // 纪元首日不是从开盘算起

    const nChunk = Math.max(1, Math.ceil(days.length / CYCLE_DAYS));
    for (let c = 0; c < nChunk; c++) {
      const cdays = days.slice(c * CYCLE_DAYS, (c + 1) * CYCLE_DAYS);
      const cset = Object.create(null);
      cdays.forEach(d => { cset[d] = 1; });
      const mine = inEp.filter(x => !!cset[dayKeyOf(x.t)]);
      const legs = Object.create(null);
      let n = 0, sum = 0, win = 0, nOpen = 0, nTake = 0;
      mine.forEach(x => {
        const t = x.t;
        const L = legs[x.k] || (legs[x.k] = { k: x.k, n: 0, sum: 0, open: 0 });
        if (t.e == null) nTake++;
        if (t.pnl == null) { nOpen++; L.open++; return; }   // 未平/接管不进分母
        n++; sum += t.pnl; if (t.pnl > 0) win++;
        L.n++; L.sum += t.pnl;
      });
      const legRows = Object.keys(legs).sort().map(k => legs[k]);
      const extra = legRows.filter(L => !poolSet[L.k]).map(L => L.k);
      used += mine.length; nAll += n; sumAll += sum; openAll += nOpen;
      rows.push({
        ep: ep.i || (i + 1), ci: c + 1, from, to,
        dayList: cdays, nDays: cdays.length,
        pool, poolNames: ep.names || pool, absent, extra,
        n, sum, win, wr: _sgWr(win, n), nOpen, nTake,
        legs: legRows,
        partial: (c === 0 && firstPartial && cdays[0] === d0),
        short: cdays.length < CYCLE_DAYS,
        trend: !to,                                        // 最后一个纪元仍在跑
      });
    }
  });

  out.ok = true;
  out.rows = rows;
  out.empty = empty;
  out.n = nAll;
  out.total = sumAll;
  out.open = openAll;
  out.skipped = (all.length - used) + noT;
  return out;
}

// ═══════════════════════════════════════════════════════════════════════
// A11 版本分段（方案 §1.4）：按 INTRABAR_STOP 切换点把已结算成交分成
// 「旧口径 INTRABAR_STOP=False」/「新口径 INTRABAR_STOP=True」两段，
// **不跨版本求和**（两种成交机制不同：旧「收盘才查」、新「盘中触价即平」，
// 混算会污染期望符号，属口径不一致，四铁律禁）。
//
// ★ 切分键 = 开仓时刻 t（同 A19 口径，不按 day）：
//   周五夜盘 t 在 10-17 晚、day 归 10-19；按 day 切会把旧码成交算进新版本。
// ★ 版本判定：取「最后一个切换点 t <= 成交 t」的 on；无切换点 ⇒ 旧版(False)。
//   （当前全部 90 笔已结算 t 均 < 22:06:20 ⇒ 全归旧版；新版成交 = 0，符合核证。）
// ═══════════════════════════════════════════════════════════════════════
function _sgVerOf(sw, t) {
  // sw 已按 t 升序；取最后一个 t <= 成交 t 的 on（遍历到第一个大于的即停）
  let on = false;
  const tk = tmap(t);
  for (const s of sw) {
    if (tmap(s.t) <= tk) on = s.on;
    else break;
  }
  return on ? 'new' : 'old';
}

export function versionSplit() {
  const meta = DB.meta || {};
  const vs = meta.version_switch || {};
  const sw = (vs.switches || []).slice();
  const out = {
    ok: false, source: vs.source || '', note: vs.note || '',
    switches: sw, rows: [], n: 0, total: 0,
    // ★ 五期：代码里的当前口径（build_data 从 ma13_live_multi.py 读出来）
    code_value: (vs.code_value == null ? null : !!vs.code_value),
  };
  if (!sw.length) return out;                 // 未标记 ⇒ 不切分（前端提示）

  // 逐成交判定版本，按版本聚合（未平/接管不进分母，同 A19）
  const byVer = Object.create(null);
  let n = 0, sum = 0;
  Object.keys(DB.data).forEach(k => {
    (DB.data[k].trades || []).forEach(t => {
      if (t.pnl == null) return;
      const ver = _sgVerOf(sw, t.t);
      const g = byVer[ver] || (byVer[ver] = { ver, n: 0, sum: 0, win: 0 });
      n++; sum += t.pnl; if (t.pnl > 0) g.win++;
      g.n++; g.sum += t.pnl;
    });
  });
  const rows = Object.keys(byVer).sort().map(ver => {
    const g = byVer[ver];
    return {
      ver,
      name: ver === 'new' ? '新口径 INTRABAR_STOP=True' : '旧口径 INTRABAR_STOP=False',
      n: g.n, sum: g.sum, wr: _sgWr(g.win, g.n),
    };
  });
  out.ok = true;
  out.rows = rows;
  out.n = n;
  out.total = sum;
  return out;
}

// ═══════════════════════════════════════════════════════════════════════
// A10/A14 接管单列（RECOVER）
//   ★ 字段口径（build_data.py:651-659）：`e` = **开仓价**、`p` = **平仓价**、`k` = 配对。
//     ★ 注意 `e` 不是"出场原因"！开仓价 nan ⇒ e=null ⇒ 这是接管笔。
//   ★ 两类必须分开：
//     · 接管已平：e==null 且 p!=null ⇒ 盈亏不可知（无开仓价），**排除出已结算分母**（A10）
//     · 未平仓   ：p==null          ⇒ 当前持仓，单独列（A14）
//     与 build_static.py:96 `n_takeover=(e_null & ~p_null)` 同口径。
// ═══════════════════════════════════════════════════════════════════════
export function takeoverBlock() {
  const takeovers = [], opens = [];
  Object.keys(DB.data).forEach(k => {
    (DB.data[k].trades || []).forEach(t => {
      if (t.p == null) {
        opens.push({ sym: k, t: t.t, d: t.d, q: t.q, e: t.e, s: t.s, h: t.h });
        return;
      }
      if (t.e == null) {
        takeovers.push({ sym: k, t: t.t, x: t.x, d: t.d, q: t.q, p: t.p, r: t.r, h: t.h });
      }
    });
  });
  takeovers.sort((a, b) => (a.t < b.t ? -1 : a.t > b.t ? 1 : 0));
  opens.sort((a, b) => (a.sym < b.sym ? -1 : a.sym > b.sym ? 1 : 0));
  return { n_takeover: takeovers.length, n_open: opens.length, takeovers, opens };
}

// ═══════════════════════════════════════════════════════════════════════
// A12/A13 长假清仓检查（判定只用交易日历，不涉及任何行情）
//   ★ 口径（方案 §3.7）：休市 **≥5 天** ⇒ 最后交易日无条件清仓；
//     休市 **3~4 天** ⇒ 不动（跳空 0.23% ≈ 普通隔夜，不值得付清仓成本）。
//   ★ 日历来自 bundle `_meta.calendar`（archive 日线 ∪ 5min 交易日）。
//   ★ 违规 = 持仓跨过 ≥5 天休市（开仓日 ≤ 清仓日 且 平仓日 ≥ 复市日）。
// ═══════════════════════════════════════════════════════════════════════
export function holidayCheck() {
  const meta = DB.meta || {};
  const cal = (meta.calendar || []).slice().sort();
  const out = { ok: false, cal_n: cal.length, long: [], small: [], violations: [], small_cross: 0,
                note: '' };
  if (cal.length < 2) {
    out.note = 'bundle 缺 _meta.calendar（重跑 build_data.py：交易日历来自 archive 日线 ∪ 5min）';
    return out;
  }
  const DAY = 86400000;
  const ts = d => Date.parse(d + 'T00:00:00Z');
  for (let i = 0; i < cal.length - 1; i++) {
    const gap = Math.round((ts(cal[i + 1]) - ts(cal[i])) / DAY);
    if (gap >= 5) out.long.push({ prev: cal[i], next: cal[i + 1], gap, clearBy: cal[i] });
    else if (gap >= 3) out.small.push({ prev: cal[i], next: cal[i + 1], gap });
  }
  // 跨长假 / 跨小长假 的持仓
  Object.keys(DB.data).forEach(k => {
    (DB.data[k].trades || []).forEach(t => {
      if (!t.t || !t.x) return;
      const d0 = String(t.t).slice(0, 10), d1 = String(t.x).slice(0, 10);
      for (const g of out.long) {
        if (d0 <= g.prev && d1 >= g.next) {
          out.violations.push({ sym: k, d: t.d, q: t.q, t: t.t, x: t.x,
                                gap: g.gap, prev: g.prev, next: g.next, pnl: t.pnl });
        }
      }
      if (out.small.some(g => d0 <= g.prev && d1 >= g.next)) out.small_cross++;
    });
  });
  out.ok = true;
  return out;
}

// ═══════════════════════════════════════════════════════════════════════
// P3-4 出场后路径（MFE-after-exit）
//   「我平在 3268，之后 10 根最高到 3305 ⇒ 错过 +370 元」
//   D3：从平仓当根的**下一根**起算
// ═══════════════════════════════════════════════════════════════════════
export function afterExit(t, d, N, mult) {
  if (!t || t.p == null || !t.x) return null;
  const xi = _sgAbsOf(d.x, t.x);
  if (xi == null) return null;
  const hi = Math.min(xi + N, d.x.length - 1);
  if (hi <= xi) return { n: 0 };                        // 平仓后没有 bar（最近一笔）
  let best = null;
  for (let k = xi + 1; k <= hi; k++) {
    const px = (t.d === '多') ? d.h[k] : d.l[k];
    const gain = (t.d === '多') ? (px - t.p) : (t.p - px);
    if (best == null || gain > best.gain) best = { gain, px, k };
  }
  if (!best) return { n: 0 };
  const m = mult || 1;
  return {
    n: hi - xi,                                          // 实际可看根数（夹到数据末根）
    gain: best.gain,                                     // 每单位价差（未乘乘数）
    px: best.px,                                         // 最有利价
    barX: d.x[best.k],
    yuan: best.gain * (t.q || 1) * m,                    // 按该笔手数折算
    pct: t.p ? best.gain / t.p : null,
  };
}

// ═══════════════════════════════════════════════════════════════════════
// P3-3 截断盘分位（早盘振幅 → 当日分位）
//   当日早盘振幅（09:00–10:15 的 high−low）÷ 近 lookback 日同期振幅中位数 = 分位
//   D2：历史「下午延续率」要 n ≥ 60 日才给，否则只说样本不足
// ═══════════════════════════════════════════════════════════════════════
const _sgMed = a => {
  const s = a.slice().sort((x, y) => x - y);
  if (!s.length) return null;
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};

export function truncation(period, lookback) {
  const LB = lookback || 20;
  if (period === '1440') return { ok: false, why: '日线周期不适用（截断盘是日内概念）' };
  const d = pd();
  if (!d || !d.x.length) return { ok: false, why: '无数据' };

  // 逐交易日聚合：早盘振幅 / 早盘方向 / 下午方向
  const byDay = new Map();
  for (let i = 0; i < d.x.length; i++) {
    const s = String(d.x[i]);
    if (s.length < 16) continue;
    const day = s.slice(0, 10);
    const hm = (+s.slice(11, 13)) * 60 + (+s.slice(14, 16));
    let g = byDay.get(day);
    if (!g) { g = { day, mHi: null, mLo: null, mO: null, mC: null, aO: null, aC: null }; byDay.set(day, g); }
    // 早盘窗口 09:00–10:15（含 10:15 那根之前的全部；10:15–10:30 是休市）
    if (hm >= 540 && hm < 615) {
      g.mHi = (g.mHi == null) ? d.h[i] : Math.max(g.mHi, d.h[i]);
      g.mLo = (g.mLo == null) ? d.l[i] : Math.min(g.mLo, d.l[i]);
      if (g.mO == null) g.mO = d.o[i];
      g.mC = d.c[i];
    }
    // 下午窗口 13:30–15:00
    if (hm >= 810 && hm < 900) {
      if (g.aO == null) g.aO = d.o[i];
      g.aC = d.c[i];
    }
  }
  const rows = [...byDay.values()]
    .filter(g => g.mHi != null && g.mLo != null)
    .sort((a, b) => (a.day < b.day ? -1 : 1))
    .map(g => ({
      day: g.day,
      amp: g.mHi - g.mLo,
      dir: (g.mC != null && g.mO != null) ? Math.sign(g.mC - g.mO) : 0,
      pm: (g.aC != null && g.aO != null) ? Math.sign(g.aC - g.aO) : 0,
    }));
  if (!rows.length) return { ok: false, why: '没有可用的早盘 bar（该周期数据不含 09:00–10:15）' };

  const today = rows[rows.length - 1];
  const hist = rows.slice(Math.max(0, rows.length - 1 - LB), rows.length - 1);
  const med = _sgMed(hist.map(r => r.amp));
  const pct = (med && med > 0)
    ? hist.filter(r => r.amp <= today.amp).length / hist.length : null;

  // 历史「下午延续」率：只在高/低振幅两组里比（各取 1/3），且要求 n ≥ 60 日
  let cont = null;
  if (hist.length >= 60) {
    const sorted = hist.slice().sort((a, b) => a.amp - b.amp);
    const k = Math.floor(sorted.length / 3);
    const rate = arr => {
      const ok = arr.filter(r => r.dir !== 0 && r.pm !== 0);
      return ok.length ? { n: ok.length, r: ok.filter(r => r.dir === r.pm).length / ok.length } : null;
    };
    cont = { low: rate(sorted.slice(0, k)), high: rate(sorted.slice(-k)), nHist: hist.length };
  }

  return {
    ok: true, day: today.day, amp: today.amp, med, pct,
    nHist: hist.length, need: 60, cont,
    todayDir: today.dir, todayPm: today.pm,
  };
}
