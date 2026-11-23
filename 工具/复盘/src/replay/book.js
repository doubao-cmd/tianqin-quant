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
// R9  时间止损（2026-11-07 用户要求，**用户自己设、不是策略强制**）：持仓满 N 根 → 下一根 bar
//     按 open 平仓，reason='时间'。默认 N=null（不设）。★ 优先级高于同根的止损/止盈：
//     「b-entryBar>=N」在 b-1 收盘时就已知，属开盘即执行的定时指令，用 open 成交不引入未来信息。
//     （06 的 MAX_HOLD=15 是策略写死的，这里刻意不照搬 —— R7 的初衷不变，只是把选择权给用户。）
// R10 限价挂单（2026-11-07 用户要求）：bar i 收盘挂单，从 i+1 起逐根检查
//     多单 low<=limit → 成交 min(limit, open)；空单 high>=limit → 成交 max(limit, open)
//     （跳空穿越按开盘价，与 R2 保守口径同源）。同时只允许 1 张挂单，可撤销；未成交不占用持仓。
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

// ★ R8 动作重放（纯函数）：
// actions=[{type:'open'|'close', bar, dir?, qty?, stopPx?, targetPx?, limitPx?, timeStopBars?, ts?}]
// 返回 {pos, closed[], realized, pending}；pos/closed 的 bar 均为绝对索引。
export function simulateBook(actions, d, cfg, abs) {
  const pos = { open: false, dir: null, qty: 0, entryBar: null, entryPx: null,
    stopPx: null, targetPx: null, timeStopBars: null, limitPx: null };
  const closed = [];
  let realized = 0;
  let pending = null;                       // 已下未成交的委托（市价 = 下一根开盘；限价 = 到价）
  const acts = (actions || []).slice().sort((a, b) => a.bar - b.bar);
  let ai = 0;
  const _exit = (b, px, reason) => {
    const fee = _costRT(pos.qty, cfg);
    const pnl = _dirSign(pos.dir) * (px - pos.entryPx) * pos.qty * cfg.mult - fee;
    closed.push({ dir: pos.dir, qty: pos.qty, entryBar: pos.entryBar, entryPx: pos.entryPx,
      exitBar: b, exitPx: px, reason, pnl, fee, holdBars: b - pos.entryBar,
      stopPx: pos.stopPx, targetPx: pos.targetPx, timeStopBars: pos.timeStopBars });
    realized += pnl;
    pos.open = false;
  };
  for (let b = 0; b <= abs; b++) {
    const o = d.o[b], h = d.h[b], l = d.l[b];

    // 1) 委托成交（R1 市价 / R10 限价）。限价单可存活多根，市价单只在下一根。
    if (pending && !pos.open) {
      const isLimit = pending.limitPx != null && pending.limitPx !== '';
      if (isLimit || pending.bar + 1 === b) {
        let fillPx = null;
        if (pending.type === 'open') {
          if (isLimit) {
            const lim = +pending.limitPx;
            if (pending.dir === '多' && l <= lim) fillPx = Math.min(lim, o);
            else if (pending.dir === '空' && h >= lim) fillPx = Math.max(lim, o);
          } else {
            fillPx = o;                              // 市价：下一根开盘
          }
        } else if (pending.type === 'close') {
          fillPx = o;
        }
        if (fillPx != null) {
          if (pending.type === 'open') {
            pos.open = true; pos.dir = pending.dir; pos.qty = pending.qty;
            pos.entryBar = b; pos.entryPx = fillPx;
            pos.stopPx = +pending.stopPx;
            pos.targetPx = (pending.targetPx == null || pending.targetPx === '') ? null : +pending.targetPx;
            pos.timeStopBars = (pending.timeStopBars > 0) ? Math.round(+pending.timeStopBars) : null;
            pos.limitPx = isLimit ? +pending.limitPx : null;
            pending = null;
          } else {
            _exit(b, fillPx, '手动');
            pending = null;
          }
        } else if (!isLimit) {
          pending = null;                            // 市价单过了成交根 ⇒ 作废（防御）
        }
      } else if (pending.bar + 1 > b) {
        // 还没到成交根（不应发生：动作只在 bar<=abs 记录）
      }
    }

    // 2) ★ R9 时间止损：开盘即判定（b-entryBar>=N 在上一根收盘已确定）⇒ 用 open 成交
    if (pos.open && pos.timeStopBars > 0 && (b - pos.entryBar) >= pos.timeStopBars) {
      _exit(b, o, '时间');
    }

    // 3) 止损/止盈自动检查（R2/R2b/R3/R4：止损优先）
    if (pos.open) {
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

    // 4) bar b 收盘时用户的动作 → 挂单待下一根开盘（R1）
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
  if (st.pending) { toast('已有待成交委托（在「模拟」页或报价条上撤销）'); return false; }
  if (type === 'open') {
    if (st.pos) { toast('已有模拟持仓，请先平仓'); return false; }
    const lim = (opts.limitPx == null || opts.limitPx === '') ? null : +opts.limitPx;
    const tsb = (opts.timeStopBars == null || opts.timeStopBars === '') ? null : Math.round(+opts.timeStopBars);
    const a = { type: 'open', bar: R.abs, dir: opts.dir,
      qty: Math.max(1, Math.round(+opts.qty || 0)),
      stopPx: +opts.stopPx,
      targetPx: (opts.targetPx != null && opts.targetPx !== '') ? +opts.targetPx : null,
      limitPx: (lim > 0 ? lim : null),
      timeStopBars: (tsb > 0 ? tsb : null),
      ts: Date.now() };
    if ((a.dir !== '多' && a.dir !== '空') || !(a.stopPx > 0) || !(a.qty > 0)) { toast('方向/手数/止损价无效'); return false; }
    R.book.push(a);
    toast(a.limitPx != null
      ? `${a.dir} ${a.qty} 手限价 ${a.limitPx} 已挂单 → 到价成交`
      : `${a.dir} ${a.qty} 手已挂单 → 下一根开盘成交`);
  } else if (type === 'close') {
    if (!st.pos) { toast('无模拟持仓'); return false; }
    R.book.push({ type: 'close', bar: R.abs, ts: Date.now() });
    toast('平仓已挂单 → 下一根开盘成交');
  } else if (type === 'modify') {
    // ★ 改止损/止盈/时间止损：不新增动作，直接改「产生当前持仓的那条 open」（R8 重放仍然确定）
    if (!st.pos) { toast('无持仓'); return false; }
    const bk = R.book || [];
    for (let k = bk.length - 1; k >= 0; k--) {
      const a = bk[k];
      if (a.type === 'open' && a.bar <= st.pos.entryBar) {
        if (opts.stopPx != null && +opts.stopPx > 0) a.stopPx = +opts.stopPx;
        if (opts.targetPx !== undefined) a.targetPx = (opts.targetPx === '' || opts.targetPx == null) ? null : +opts.targetPx;
        if (opts.timeStopBars !== undefined) a.timeStopBars = (+opts.timeStopBars > 0) ? Math.round(+opts.timeStopBars) : null;
        break;
      }
    }
    toast('持仓参数已更新');
  } else return false;
  renderAll();
  return true;
}

// 撤销待成交委托（市价单只在下一根有效；限价单可能已挂多根 ⇒ 按 ts 精确定位）
export function bookCancelPending() {
  const R = state.replay;
  if (!R) return;
  const st = bookState();
  if (!st.pending) { toast('当前没有待成交委托'); return; }
  const bk = R.book || [];
  let hit = -1;
  const ts = st.pending.ts;
  for (let k = bk.length - 1; k >= 0; k--) {
    const a = bk[k];
    if ((a.type === 'open' || a.type === 'close') && a.bar <= R.abs) {
      if (ts != null && a.ts === ts) { hit = k; break; }
    }
  }
  if (hit < 0) {
    for (let k = bk.length - 1; k >= 0; k--) {
      if (bk[k].type === 'open' || bk[k].type === 'close') { hit = k; break; }
    }
  }
  if (hit >= 0) bk.splice(hit, 1);
  toast('已撤销待成交委托');
  renderAll();
}

// 下单面板默认值（R6）：止损 = 当前收盘 ∓ k×ATR14（吸附最小变动价位）+ 手数建议
// ★ 2026-11-07：k 改为可传（面板上的 0.5/1/1.5/2 快捷按钮），默认仍 1.5 —— 行为与旧版一致。
export function simSuggest(dir, atrMult) {
  const R = state.replay;
  if (!R) return null;
  const d = pd(), cfg = simCfg(state.sym);
  const equity = cfg.principal + bookState().realized;
  const ref = d.c[R.abs], atr = d.atr[R.abs] || 0;
  const k = (atrMult > 0) ? atrMult : 1.5;
  const stopPx = _roundTick(dir === '多' ? ref - k * atr : ref + k * atr, cfg.tick);
  let q = Math.floor(equity * 0.01 / (Math.max(cfg.tick, Math.abs(ref - stopPx)) * cfg.mult));
  q = Math.min(q, Math.floor(equity * 2 / (ref * cfg.mult)));       // 名义敞口 ≤ 权益×2
  return { qty: Math.max(1, Math.min(8, q)), stopPx, ref, atr, equity, atrMult: k,
    costRT: _costRT(Math.max(1, Math.min(8, q)), cfg) };
}

// 快捷价：按 ATR 倍数算止损价（吸附最小变动价位）
// ★ 2026-11-07：basePx 可传 —— 限价挂单时基准应是**挂单价**（挂在 20030 成交，止损就该在 20030 下方），
//   不传则用当前 bar 收盘价（市价单口径）。
export function stopByAtr(dir, mult, basePx) {
  const R = state.replay;
  if (!R) return null;
  const d = pd(), cfg = simCfg(state.sym);
  const ref = (basePx > 0) ? +basePx : d.c[R.abs];
  const atr = d.atr[R.abs] || 0;
  if (!(atr > 0)) return null;
  return _roundTick(dir === '多' ? ref - mult * atr : ref + mult * atr, cfg.tick);
}

// 快捷价：按 R 倍数算止盈价（R = |基准价 − 止损价|，吸附最小变动价位）
export function targetByR(dir, rMult, stopPx, basePx) {
  const R = state.replay;
  if (!R) return null;
  const d = pd(), cfg = simCfg(state.sym);
  const ref = (basePx > 0) ? +basePx : d.c[R.abs];
  const s = +stopPx;
  if (!(s > 0)) return null;
  const risk = Math.abs(ref - s);
  if (!(risk > 0)) return null;
  return _roundTick(dir === '多' ? ref + rMult * risk : ref - rMult * risk, cfg.tick);
}

export function roundToTick(v) {
  const cfg = simCfg(state.sym);
  return _roundTick(+v, cfg.tick);
}
