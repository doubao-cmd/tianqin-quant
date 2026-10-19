// 时间 / 数据源相关纯函数。统一口径（P0-D 的前置，本步只收敛取值来源）：
// ① 判断"是否已发生" → 用 barEnd(bar) <= 当前bar收盘
// ② 分时 / 昨收      → 用 day
// ③ 显示给用户的时刻 → 用 x（bar 标签）
import { state } from './state.js';
import { DB } from '../data/index.js';

// ★ 2026-10-18：兼容 Date 对象。原因：clampViewLeft() 会返回 new Date(...)，
//   而下游到处都 `toDate(x)` —— 旧实现直接 `s.replace(...)`，遇到 Date 就
//   `TypeError: s.replace is not a function`。加一层归一化，所有调用点都不用改。
export const toDate = s => {
  if (s instanceof Date) return s;
  const t = String(s == null ? '' : s);
  return new Date(t.length <= 10 ? t + 'T00:00:00' : t.replace(' ', 'T'));
};

export function isDaily() { return state.period === '1440'; }

// pd = 当前品种+周期的数据块（periods[period]）
// ★ P2-3：sym/period 可显式传入。收尾"会话台账"时 state.sym 可能已被切走
//   （切品种即退出回放），不传就会拿到新品种的数据块 → 索引/乘数全错。
export function pd(sym, period) {
  return DB.data[sym || state.sym].periods[period || state.period];
}

// tmap：日线只比日期，分钟比到分钟（统一比较键）
export function tmap(s) { return isDaily() ? String(s).slice(0,10) : s; }

export function tradeStart(t) { return isDaily() ? t.day : t.t; }
export function tradeEnd(t)   { return isDaily() ? (t.xday || t.day) : (t.x || t.t); }
export function evTime(e)     { return isDaily() ? e.day : e.t; }

// bar 结束时刻（收盘）：日线无"盘中收盘"，period 视作 0 分钟
export function barEnd(x, period) {
  return new Date(toDate(x).getTime() + (period === '1440' ? 0 : +period) * 60000);
}
// 交易日归属（夜盘归下一交易日），统一走 day 字段，不再用 last_day 推断
export function tradingDayOf(d, i) { return d.day[i]; }

// 区间起点（绝对索引）：日线一律全显示；非日线按"近N日"截断
export function rangeStart(xs) {
  if (isDaily() || state.range === 'ALL') return 0;
  const days = {'1D':1,'3D':3,'ALL':null}[state.range];
  if (days == null) return 0;
  const last = toDate(xs[xs.length-1]).getTime();
  const cut = last - days * 86400000;
  const i = xs.findIndex(s => toDate(s).getTime() >= cut);
  return i < 0 ? 0 : i;
}

// ---------------------------------------------------------------------------
// ★★ 视图左界锁死（2026-10-18 用户要求）
//
// 用户原话意思：「十月8日之前的K线不能拖动，不然往前拖就没有图，不好看，直接锁死就行，
//                然后其他品种也这样」
//
// 为什么"往前拖没图"：bundle 的显示窗口被**台账绑死**（start_i = 最早 FILL − 1 天），
// 所以 5min/15min 只从 2026-09-15 起、1440min 只有 18 根。而**成交/决策标记全在
// 2026-10-08 之后**（台账首条 10-08 08:55）。往前拖看到的是"有K线但没有任何交易"的
// 区域，既不好看也没信息量 ⇒ 直接把可视左界钉在 10-08。
//
// 实现要点：
//   · 按 sym|period 缓存（同 breaks() 的做法，回放播放时每根 bar 都会调，不能每次 findIndex）
//   · 返回 {i, t}：i = 第一根 >= 该日的索引，t = 它的毫秒时间戳
//   · 找不到（数据全在该日之前）时返回 i=0 ⇒ 不锁（不能把视图锁成空的）
// ---------------------------------------------------------------------------
export const VIEW_MIN_DATE = '2026-10-08';
const _VDAY_MS = Date.parse(VIEW_MIN_DATE + 'T00:00:00');
let _vminKey = null, _vminVal = null;

export function viewMin() {
  const key = state.sym + '|' + state.period;
  if (_vminKey === key && _vminVal) return _vminVal;
  const d = DB.data[state.sym];
  const xs = (d && d.periods && d.periods[state.period] && d.periods[state.period].x) || [];
  let i = xs.findIndex(s => toDate(s).getTime() >= _VDAY_MS);
  if (i < 0) i = 0;                 // 数据全在 10-08 之前 ⇒ 不锁（否则视图为空）
  _vminKey = key;
  _vminVal = { i, t: xs.length ? toDate(xs[i]).getTime() : 0 };
  return _vminVal;
}

// 把 [a,b] 的左端钳到视图左界。a/b 可以是字符串或 Date（Plotly 两种都给过）。
// 返回 [a2, b2]；若无需钳制则原样返回，便于调用方判断"有没有改动"。
// ★ 保底跨度必须和 main.js 的初始视图一致（3 天）——
//   第一版这里写成 1 分钟，导致"整个视野被压成一条线"，实测跨度 0.00 天才发现。
export const VIEW_MIN_SPAN_MS = 3 * 86400000;   // 3 天

// ★★ 关键：返回**字符串**而不是 Date 对象。
//   第一版返回 new Date(...)，结果 _t_p45.js 里 `layout.xaxis.range[0].replace(...)`
//   直接 TypeError。Plotly 虽然能吃 Date，但项目里到处按字符串处理 range
//   （_vdMs / 各处 slice），一旦类型变了就是隐性雷。
//   ⇒ 统一输出与 `x` 同格式的 'YYYY-MM-DD HH:MM'。
function _fmtDt(ms) {
  const d = new Date(ms);
  const p2 = n => (n < 10 ? '0' : '') + n;
  return d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate()) +
         ' ' + p2(d.getHours()) + ':' + p2(d.getMinutes());
}

export function clampViewLeft(a, b) {
  const vm = viewMin();
  if (!vm || !vm.t) return [a, b];
  const ta = toDate(a).getTime();
  if (!(ta < vm.t)) return [a, b];              // 已在界内 ⇒ 原样返回（引用不变）
  const tb = toDate(b).getTime();
  // 右端还够宽就保留原右端，否则给保底跨度（绝不允许 0 宽/极窄，那 Plotly 会当无效范围）
  const b2 = (tb > vm.t + VIEW_MIN_SPAN_MS) ? b : _fmtDt(vm.t + VIEW_MIN_SPAN_MS);
  return [_fmtDt(vm.t), b2];
}

// rangebreaks：周末 + 节假日 + 午休/夜盘缺口（来自 meta）
// ★ P5-3 方案A：结果按 sym|period 缓存。原来每次 renderMain 都重算一遍
//   （构造数组 + 查节假日），回放播放时每根 bar 都算一次，纯浪费。
//   只依赖 品种/周期，不依赖 range 或回放位置 ⇒ 可以安全缓存。
let _brkKey = null, _brkVal = null;
export function breaks() {
  const key = state.sym + '|' + state.period;
  if (_brkKey === key && _brkVal) return _brkVal;
  const d = DB.data[state.sym];
  const hols = isDaily() ? (d.holidays_d || []) : (d.holidays || []);
  const out = [{bounds:['sat','mon']}];
  if (hols.length) out.push({values: hols, pattern:'day'});
  if (isDaily()) { _brkKey = key; _brkVal = out; return out; }
  const meta = d.meta || {has_1015_break:true, night_end:23};
  out.push({bounds:[11.5,13.5], pattern:'hour'});
  if (meta.has_1015_break !== false) out.push({bounds:[10.25,10.5], pattern:'hour'});
  if (!meta.night_end) {
    out.push({bounds:[15,9], pattern:'hour'});
  } else {
    out.push({bounds:[15,21], pattern:'hour'});
    out.push({bounds:[meta.night_end, 9], pattern:'hour'});
  }
  _brkKey = key; _brkVal = out;
  return out;
}
