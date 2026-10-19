// 唯一的状态源（P0-B：回放状态收进 state.replay，不再有散落的 i / playing 字段）
// 回放状态全部收进 state.replay，其它模块不持有回放私有字段。
//
// 为避免 state.js 反向依赖 view 模块（造成循环 import），renderAll 通过
// registerRender 注入，setContext 在运行期（用户操作）才调用，不会在模块求值期执行。

export const UP = '#ef4444', DOWN = '#22c55e';
export const PERIODS = [['5','5 分钟'],['15','15 分钟'],['1440','日线']];
export const RANGES = [['1D','近1日',1],['3D','近3日',3],['ALL','全部',null]];

export const state = {
  sym: 'M', period: '5', range: '3D', ind: 'vol',
  sel: null,                 // 选中交易索引（DATA[sym].trades 下标）
  showTrades: true,
  mode: 'review',            // 'train' 训练模式 | 'review' 复看模式（P1-5）
  shapes: [],                // 用户画线
  replay: null,              // null = 未回放；对象 = 回放中
};

let _renderAll = () => {};
export function registerRender(fn) { _renderAll = fn; }

// ★ P2-3：模式切换钩子。setMode 清 state.replay 时，updateReplayUI 的轮询只能看出
//   "replay 没了"，看不出是切品种还是切模式 ⇒ 统一记 'switch' 会丢信息。
//   由 engine 注册 flushSession，setMode 在清 replay 前先以 'mode' 收尾。
let _onModeChange = () => {};
export function registerModeChange(fn) { _onModeChange = fn; }

// 回放对象（起播时创建，冻结 symbol/period 防止中途切换导致索引错位）
export function makeReplay(abs, opts = {}) {
  return {
    abs,                     // ★ 绝对 bar 索引（相对 DATA[sym].periods[period].x）
    startAbs: abs,           // 起播位置
    endAbs: (opts.endAbs == null ? null : opts.endAbs),
    win: opts.win || 120,    // ★ 可见根数（固定窗口）
    playing: false,
    stepMs: opts.stepMs || 600,   // ★ 每根停留毫秒（P1-2 会用）
    symbol: state.sym,       // ★ 冻结
    period: state.period,    // ★ 冻结
    book: [],                // 模拟下单记录（P2）
    marks: [],               // 决策标记（P2）
  };
}

// ★ P4 多周期联动（降级方案）：切周期时保持「时间中心 + 时间跨度」。
//   不 import core/time.js 的 toDate —— time.js 反过来 import state，会形成
//   state→time→state 的环（能跑但脆弱）；这里 8 行自己解析，零依赖。
//   Plotly 的 xaxis.range 返回的是「原样字符串」（'2026-10-13 09:00' 或 ISO），
//   两种格式 new Date(replace(' ','T')) 都能吃。
const _vdMs = s => {
  const t = new Date(String(s == null ? '' : s).replace(' ', 'T'));
  return t.getTime();
};

function _captureViewCenter() {
  try {
    const el = document.getElementById('main');
    const r = el && el.layout && el.layout.xaxis && el.layout.xaxis.range;
    if (!r || r.length < 2) return null;
    const a = _vdMs(r[0]), b = _vdMs(r[1]);
    if (!isFinite(a) || !isFinite(b) || b <= a) return null;
    return { c: (a + b) / 2, w: b - a };
  } catch (e) { return null; }
}

// 唯一入口：任何上下文变化都走这里 → 统一退出回放 + 清选中
export function setContext({sym, period, range}) {
  // ★ P4：只有「切周期」才继承视野；切品种是换数据，继承过去没有意义
  const periodChanging = (period != null && period !== state.period);
  if (periodChanging) state._keepCenter = _captureViewCenter();
  else if (sym != null) state._keepCenter = null;
  if (sym != null) state.sym = sym;
  if (period != null) state.period = period;
  if (range != null) state.range = range;
  state.sel = null;
  state.replay = null;       // 自动退出回放（正在播放的定时器由 engine 守卫自停）
  _renderAll();
}

// ★ P1-5 双模式：唯一判据——是否允许"看答案"（成交标记 / 成交列表 / 统计 / 逐笔跳转）
export const canSeeAnswers = () => state.mode === 'review';

// ★ 切换训练/复看模式：清选中、退出回放、图/表/统计三处同刷
export function setMode(m) {
  state.mode = (m === 'train') ? 'train' : 'review';
  state.sel = null;
  _onModeChange('mode');           // ★ P2-3：切模式 = 会话结束，理由记 mode（不是 switch）
  state.replay = null;
  _renderAll();
}
