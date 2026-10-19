// 模拟下单账本（P2-1）—— 从"看"到"练"的核心引擎
//
// ══ 撮合规则（★ 冻结，任何一条改动都必须在此注明理由与日期）══
// R1  开仓/市价平仓：用户在回放位置 bar i 按键 = 信号在 bar i 收盘确认 → bar i+1 的 open 成交
//     （四铁律 1；与 06 回测「信号用已收盘 bar、下一根开盘成交」同口径）
// R2  止损触发（自动）：多 low<=stop → 成交 min(stop, open)；空 high>=stop → max(stop, open)
//     （GAP_FILL 保守口径，06 回测同款：跳空穿价按开盘价成交，用 stop 会高估）
// R2b 开仓当根也参与止损检查（含"开盘即穿价"的跳空止损）。理由（2026-10-18）：路线图验收用例 2
//     要求；且物理上成交即在场。06 回测因循环结构（先查出场后开仓）开仓当根不查止损——
//     两者只在「开仓根即触发」的路径上有差异，06 属乐观偏差，模拟器取保守侧。
// R3  止盈触发（自动，仅当 targetPx 非空）：多 high>=target → max(target, open)；空 low<=target → min(target, open)
// R4  同根内止损与止盈都触及 → 止损优先（5min bar 内无法分辨先后，宁可低估）
// R5  成本（双边）：每边 (COMM×手数 + SLIP×TICK×MULT×手数)；
//     COMM/TICK/MULT 来自 bundle（build_data 写入 tick/comm/mult，权威来源 config_symbols.py），
//     SLIP=1 跳/边 —— 与 06 回测成本模型逐项一致（06: SLIP*TICK*MULT*hands + COMM*hands）
// R6  手数建议：floor(权益×1% ÷ (|参考价−止损|×乘数))，夹 [1,8]，再夹名义敞口 ≤ 权益×2（06 保护同款）；
//     权益 = 20万 + 本次会话已实现盈亏（06 PRINCIPAL=20万同款）
// R7  ★ 与 06 的唯一行为偏离：不做自动时间止损（06 MAX_HOLD=15 根）。
//     理由（2026-10-18）：训练器的目的恰是练「拿住」，自动时间止损会替用户做决策；
//     reason 枚举保留 '时间' 备用。拿没拿住由 P2-4 评分的「平均持仓根数 vs 策略中位」对照。
// R8  账本 = 有序动作列表 R.book（append-only，动作含触发 bar）。任意跳转（进度条/逐笔/Home/End）
//     后按动作重放至当前 bar 派生全部状态 —— 纯函数、确定性的迷你回测，与推进方式无关，
//     跳转不会漏掉任何止损触发。
import { state } from '../core/state.js';
import { pd } from '../core/time.js';
import { $, toast } from '../core/fmt.js';
import { DB } from '../data/index.js';
import { renderAll } from '../view/index.js';

export const SIM_COLOR = '#60a5fa';      // 模拟单专属蓝（区别真实成交的红/绿）

export function simCfg(sym) {
  const s = DB.data[sym || state.sym] || {};
  return {
    mult: s.mult || 1,
    tick: s.tick || 1,
    comm: (s.comm != null) ? s.comm : 0,
    slip: 1,             // ★ R5：1 跳/边（冻结）
    principal: 200000,   // ★ R6：20 万（06 INIT 同款，冻结）
  };
}

const _dirSign = dir => (dir === '多' ? 1 : -1);
const _costSide = (q, c) => (c.comm + c.slip * c.tick * c.mult) * q;   // 单边成本（元）
const _costRT = (q, c) => 2 * _costSide(q, c);                          // 双边成本（元）
const _roundTick = (v, t) => +(Math.round(v / t) * t).toFixed(6);

// ★ R8 动作重放（纯函数）：actions=[{type:'open'|'close', bar, dir?, qty?, stopPx?, targetPx?}]
// 返回 {pos, closed[], realized, pending}；pos/closed 的 bar 均为绝对索引。
export function simulateBook(actions, d, cfg, abs) {
  const pos = { open: false, dir: null, qty: 0, entryBar: null, entryPx: null, stopPx: null, targetPx: null };
  const closed = [];
  let realized = 0;
  let pending = null;                       // 待成交（fillBar = bar+1）
  const acts = (actions || []).slice().sort((a, b) => a.bar - b.bar);
  let ai = 0;
  const _exit = (b, px, reason) => {
    const pnl = _dirSign(pos.dir) * (px - pos.entryPx) * pos.qty * cfg.mult - _costRT(pos.qty, cfg);
    closed.push({ dir: pos.dir, qty: pos.qty, entryBar: pos.entryBar, entryPx: pos.entryPx,
      exitBar: b, exitPx: px, reason, pnl, holdBars: b - pos.entryBar });
    realized += pnl;
    pos.open = false;
  };
  for (let b = 0; b <= abs; b++) {
    // 1) 待成交委托在 bar b 开盘成交（R1）
    if (pending && pending.bar + 1 === b) {
      if (pending.type === 'open' && !pos.open) {
        pos.open = true; pos.dir = pending.dir; pos.qty = pending.qty;
        pos.entryBar = b; pos.entryPx = d.o[b]; pos.stopPx = pending.stopPx; pos.targetPx = pending.targetPx;
      } else if (pending.type === 'close' && pos.open) {
        _exit(b, d.o[b], '手动');
      }
      pending = null;
    }
    // 2) 止损/止盈自动检查（R2/R2b/R3/R4：止损优先）
    if (pos.open) {
      const o = d.o[b], h = d.h[b], l = d.l[b];
      let px = null, reason = null;
      if (pos.dir === '多') {
        if (l <= pos.stopPx) { px = Math.min(pos.stopPx, o); reason = '止损'; }
        else if (pos.targetPx != null && h >= pos.targetPx) { px = Math.max(pos.targetPx, o); reason = '止盈'; }
      } else {
        if (h >= pos.stopPx) { px = Math.max(pos.stopPx, o); reason = '止损'; }
        else if (pos.targetPx != null && l <= pos.targetPx) { px = Math.min(pos.targetPx, o); reason = '止盈'; }
      }
      if (px != null) _exit(b, px, reason);
    }
    // 3) bar b 收盘时用户的动作 → 挂单待下一根开盘（R1）
    while (ai < acts.length && acts[ai].bar === b) {
      const a = acts[ai]; ai++;
      if (a.type === 'open' && !pos.open && !pending) pending = a;
      else if (a.type === 'close' && pos.open && !pending) pending = a;
    }
  }
  return { pos: pos.open ? pos : null, closed, realized, pending };
}

// 当前回放位置的账本状态（便捷封装）
// ★ P2-3：R/sym 可显式传入 —— 收尾会话台账时 state.replay 可能已被清、state.sym 可能已被切走，
//   不传就会用新品种的 tick/comm/mult 重算旧会话的盈亏（错）。默认值保持原有调用方不变。
export function bookState(R, sym) {
  const r = R || state.replay;
  const s = sym || state.sym;
  if (!r) return { pos: null, closed: [], realized: 0, pending: null };
  return simulateBook(r.book || [], pd(s, r.period), simCfg(s), r.abs);
}

// 下单/平仓入口（boot 的按钮调它）。返回 true = 动作已记录
export function bookAct(type, opts = {}) {
  const R = state.replay;
  if (!R) { toast('请先进入回放'); return false; }
  const st = bookState();
  if (st.pending) { toast('已有待成交委托（点击报价条上的待成交字样可撤销）'); return false; }
  if (type === 'open') {
    if (st.pos) { toast('已有模拟持仓，请先平仓'); return false; }
    const a = { type: 'open', bar: R.abs, dir: opts.dir,
      qty: Math.max(1, Math.round(+opts.qty || 0)),
      stopPx: +opts.stopPx,
      targetPx: (opts.targetPx != null && opts.targetPx !== '') ? +opts.targetPx : null,
      ts: Date.now() };
    if ((a.dir !== '多' && a.dir !== '空') || !(a.stopPx > 0) || !(a.qty > 0)) { toast('方向/手数/止损价无效'); return false; }
    R.book.push(a);
    toast(`${a.dir} ${a.qty} 手已挂单 → 下一根开盘成交`);
  } else if (type === 'close') {
    if (!st.pos) { toast('无模拟持仓'); return false; }
    R.book.push({ type: 'close', bar: R.abs, ts: Date.now() });
    toast('平仓已挂单 → 下一根开盘成交');
  } else return false;
  renderAll();
  return true;
}

// 撤销待成交委托（只删"挂了还没成交"的那条：bar === 当前 abs）
export function bookCancelPending() {
  const R = state.replay;
  if (!R) return;
  const bk = R.book || [];
  for (let k = bk.length - 1; k >= 0; k--) {
    if (bk[k].bar === R.abs && (bk[k].type === 'open' || bk[k].type === 'close')) { bk.splice(k, 1); break; }
  }
  toast('已撤销待成交委托');
  renderAll();
}

// 下单面板默认值（R6）：止损 = 当前收盘 ∓ 1.5×ATR14（吸附最小变动价位）+ 手数建议
export function simSuggest(dir) {
  const R = state.replay;
  if (!R) return null;
  const d = pd(), cfg = simCfg(state.sym);
  const equity = cfg.principal + bookState().realized;
  const ref = d.c[R.abs], atr = d.atr[R.abs] || 0;
  const stopPx = _roundTick(dir === '多' ? ref - 1.5 * atr : ref + 1.5 * atr, cfg.tick);
  let q = Math.floor(equity * 0.01 / (Math.max(cfg.tick, Math.abs(ref - stopPx)) * cfg.mult));
  q = Math.min(q, Math.floor(equity * 2 / (ref * cfg.mult)));       // 名义敞口 ≤ 权益×2
  return { qty: Math.max(1, Math.min(8, q)), stopPx, ref, atr, equity, costRT: _costRT(Math.max(1, Math.min(8, q)), cfg) };
}
