// 会话台账（P2-3）—— 每次回放会话记一行，回答"我这段时间到底练了什么"。
//
// 规格（路线图 §7 P2-3）：
//   会话ID,开始,结束,数据源,品种,周期,起播时间,结束时间,推进根数,模拟笔数,模拟盈亏,标记数,模式
//   写 运行记录/复盘会话/会话_YYYYMMDD.csv（追加，不覆盖）
//
// ⚠️ 浏览器 file:// 下无法直接追加写文件 ⇒ 路线图自己给的两条路里选 A：
//   **每次导出都写"累计全部会话"**。用户第二次存到同一路径时，新文件是旧文件的超集
//   （含此前每一行）⇒ "追加而不是覆盖"由导出内容保证，不依赖用户手动合并。
//   （B 方案 = 给 HTTP 版 serve.py 加 do_POST 的 /api/save，只有走 启动复盘终端.bat 时才有效。）
//
// 红线自查：
//   1. 会话边界 = state.replay 从"有"变"无"。四种结束方式：exit（点退出回放）/ switch（切品种周期区间）
//      / mode（切训练复看）/ restart（没退出就重新起播）/ unload（关页面）。
//   2. **落账永远不等用户操作**：updateReplayUI 轮询 + beforeunload 双保险，任何路径都不丢会话。
//      只有"点退出回放"才自动弹下载（避免每次切品种都弹一个文件）。
//   3. 收尾时 state.sym/state.replay 可能已经变了 ⇒ 会话开始就把 sym/period/回放对象引用快照进 _cur，
//      并用 bookState(R, sym) 显式传参算盈亏（否则新品种的乘数会算错旧会话）。
//   4. 台账是用户自产数据，不是 trades（答案）⇒ 训练/复看模式都记，模式本身作为一列写进去。
//   5. localStorage 上限 ~5MB，软封顶 1000 条（超出砍最旧并 toast），实测单条约 200 字节。
import { state } from '../core/state.js';
import { pd } from '../core/time.js';
import { $, toast } from '../core/fmt.js';
import { lsStore } from '../core/store.js';
import { bookState } from './book.js';
import { marksList } from './marks.js';
import { scoreSession } from './score.js';

// ★ 同 marks.js：key 常量/句柄/读写封装一律 lg 前缀，防拼接后顶层撞名
const LEDGER_KEY = 'rt:sessions:v1';
const DATA_SRC = 'tqsdk';      // data.py:406 hl_source="tqsdk"（--fetch2026 实取路径；CSV 直读路径同源）
const CAP = 1000;              // 软封顶：超出砍最旧

const _lgStore = lsStore(LEDGER_KEY, {
  cap: CAP,
  clean: r => (r && r.id) ? r : null,
  onCap: () => toast(`台账已满 ${CAP} 条，最早记录被截掉（建议先导出）`),
  msg: 'localStorage 不可用：台账只在本次会话有效',
});
function _lgLoad() { return _lgStore.load(); }
function _lgSave(list) { return _lgStore.save(list); }

// ── 进行中的会话 ─────────────────────────────────────────────────────
let _cur = null;

function _pad(n) { return String(n).padStart(2, '0'); }
function _hm(t) { const d = new Date(t); return `${_pad(d.getHours())}:${_pad(d.getMinutes())}`; }
function _hms(t) { const d = new Date(t); return `${_hm(t)}:${_pad(d.getSeconds())}`; }
function _ymd(t) { const d = new Date(t); return `${d.getFullYear()}${_pad(d.getMonth() + 1)}${_pad(d.getDate())}`; }
function _ymdFull(t) { const d = new Date(t); return `${d.getFullYear()}-${_pad(d.getMonth() + 1)}-${_pad(d.getDate())}`; }

// 会话ID：S + 起播日期 + _ + 起播时刻（路线图示例 S20261018_1530 到分钟；
// ★ P2-3 实测：同一分钟内连续两次会话会撞号 ⇒ 加到秒；再加"撞号自增后缀"
//   （同秒连开两次会话时给 _2/_3，保证 CSV 里一行一个 ID，人对得上）。
function _sessId(t) {
  const d = new Date(t);
  const base = `S${_ymd(t)}_${_pad(d.getHours())}${_pad(d.getMinutes())}${_pad(d.getSeconds())}`;
  const list = _lgLoad();
  let id = base, n = 1;
  while (list.some(r => r.id === id) || (_cur && _cur.id === id)) { n++; id = `${base}_${n}`; }
  return id;
}

// 开一次会话（startReplay 里调）。若上一次还没收尾 → 先按 restart 落一条，不重复计数
export function beginSession() {
  if (_cur) _flush('restart');
  const R = state.replay;
  if (!R) return null;
  const t0 = Date.now();
  const d = pd(state.sym, state.period);
  _cur = {
    id: _sessId(t0),
    t0, t1: t0,
    src: DATA_SRC,
    sym: state.sym, period: state.period, mode: state.mode,
    startBarX: (d.x[R.startAbs] != null ? String(d.x[R.startAbs]) : ''),
    endBarX: (d.x[R.abs] != null ? String(d.x[R.abs]) : ''),
    startAbs: R.startAbs,                // ★ P2-4：评分要用「区间起点」筛策略成交
    nStep: 0,
    R,                                   // ★ 持引用：state.replay 被清后仍读得到 book
  };
  return _cur;
}

// ★ P2-4：最近一次会话的完整评分（含逐笔/逐标记明细），供 report.js 渲染。
//   不写 localStorage（明细可重放派生，存两份必然漂移），只在内存里留最后一份。
let _lastScore = null;
export function lastScore() { return _lastScore; }

// 会话是否进行中（updateReplayUI 轮询用）
export function sessionActive() { return !!_cur; }

// 推进根数计数（engine._setAbs 调；只数向前，往回跳不计）
export function noteStep(n) {
  if (_cur && n > 0) _cur.nStep += n;
}

// 收尾：把 _cur 写成一条台账（不弹下载）。返回记录或 null
function _flush(reason) {
  const c = _cur;
  _cur = null;
  if (!c) return null;
  const R = c.R;
  // ★ 用会话开始时的 sym/period 显式算，不能用"当前"的（可能已被切走）
  const st = bookState(R, c.sym);
  const d = pd(c.sym, c.period);
  const nOpen = (R.book || []).filter(a => a.type === 'open').length;
  const nMarks = marksList(c.sym, c.period).filter(m => (m.ts || 0) >= c.t0).length;

  // ★ P2-4：评分（纯函数派生，不落盘明细）。失败不能让台账丢行 ⇒ try/catch 兜住
  let sc = null;
  try {
    sc = scoreSession({
      sym: c.sym, period: c.period,
      actions: (R.book || []),
      startAbs: (c.startAbs == null ? 0 : c.startAbs),
      endAbs: R.abs,
    });
    _lastScore = sc;
  } catch (e) {
    if (window.console) console.warn('[P2-4] 评分失败：', e);
  }

  const rec = {
    id: c.id, t0: c.t0, t1: c.t1, src: c.src,
    sym: c.sym, period: c.period, mode: c.mode,
    startBarX: c.startBarX,
    endBarX: (d.x[R.abs] != null ? String(d.x[R.abs]) : c.endBarX),
    nStep: c.nStep,
    nSim: nOpen,
    realized: Math.round(st.realized),
    nMarks,
    openFlag: st.pos ? 1 : 0,
    reason: reason || 'auto',
    // ── P2-4 新增列（★ 追加在末尾，不改已有列顺序，旧 CSV 仍可读）──
    nSimClosed: sc ? sc.nSim : null,
    stratPnl: sc ? Math.round(sc.stratPnl) : null,
    stratN: sc ? sc.stratN : null,
    overTrade: (sc && sc.overTrade != null && isFinite(sc.overTrade))
      ? Math.round(sc.overTrade * 100) / 100 : null,
    hitRate: (sc && sc.hitRate != null) ? Math.round(sc.hitRate * 1000) / 1000 : null,
    avoidQ: (sc && sc.avoidQ != null) ? Math.round(sc.avoidQ * 1000) / 1000 : null,
    // ── 五期 T2.1 新增（趋势面板要"样本数"才能决定实心/空心，要 manualShare 才画得出手操占比）──
    manualShare: (sc && sc.manualShare != null) ? Math.round(sc.manualShare * 1000) / 1000 : null,
    nHitSample: sc ? sc.nHitSample : null,
    nAvoidSample: sc ? sc.nAvoidSample : null,
  };
  const list = _lgLoad().slice();
  // 同 id 覆盖（restart 之外不会重号；防 beforeunload 与轮询双写）
  const i = list.findIndex(r => r.id === rec.id && r.t0 === rec.t0);
  if (i >= 0) list[i] = rec; else list.push(rec);
  _lgSave(list);
  return rec;
}

// 显式结束会话（点"退出回放"）：落账 + 有实质内容才弹下载。
// ★ 下载内容 = 累计全部会话（不是只有这一条）—— 路线图验收"连续两次会话第二行追加而不是
//   覆盖"靠这个保证：用户每次把弹窗存到同一路径，文件里始终含此前每一行。
//   （若只弹当前会话，第二次存盘就会把第一次覆盖掉，反而丢数据。）
export function endSession(reason) {
  const c = _cur;
  if (!c) return null;
  const rec = _flush(reason);
  if (rec && (rec.nStep > 0 || rec.nSim > 0 || rec.nMarks > 0)) _download(null);
  return rec;
}

// 只收尾不下载（轮询 / beforeunload / restart 用）
export function flushSession(reason) { return _flush(reason); }

// ── 导出 CSV（utf-8-sig，累计全部会话） ──────────────────────────────
const HEAD = ['会话ID', '开始', '结束', '数据源', '品种', '周期', '起播时间', '结束时间',
  '推进根数', '模拟笔数', '模拟盈亏', '标记数', '模式', '未平', '结束方式',
  // ── P2-4 新增（★ 一律追加在末尾，旧 CSV 前 15 列语义不变）──
  '已平笔数', '策略同区间盈亏', '策略笔数', '过度交易指数', '方向命中率', '规避质量',
  // ── 五期 T2.1 新增（同样追加在末尾）──
  '手动平仓占比', '命中样本数', '规避样本数'];
const REASON_CN = { exit: '退出回放', switch: '切换上下文', mode: '切换模式', restart: '重新起播', unload: '关闭页面', auto: '自动' };

function _cell(s) {
  const t = String(s == null ? '' : s);
  return /[",\r\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
}
function _row(r) {
  return [r.id, _hms(r.t0), _hms(r.t1), r.src, r.sym, r.period, r.startBarX, r.endBarX,
    r.nStep, r.nSim, r.realized, r.nMarks, r.mode === 'train' ? 'train' : 'review',
    r.openFlag, REASON_CN[r.reason] || r.reason || '',
    r.nSimClosed, r.stratPnl, r.stratN, r.overTrade, r.hitRate, r.avoidQ,
    r.manualShare, r.nHitSample, r.nAvoidSample];
}

function _download(only) {
  const list = _lgLoad().slice().sort((a, b) => a.t0 - b.t0);
  const rows = only ? [only] : list;
  if (!rows.length) { toast('暂无会话记录'); return; }
  const csv = '\ufeff' + [HEAD].concat(rows.map(_row)).map(r => r.map(_cell).join(',')).join('\r\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `会话_${_ymd(Date.now())}.csv`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { try { URL.revokeObjectURL(a.href); } catch (e) {} a.remove(); }, 0);
  return rows.length;
}

// 手动导出：累计全部会话（路线图验收"第二次追加不覆盖"靠这个保证）
export function exportSessionsCsv() {
  const n = _download(null);
  if (n) toast(`已导出 ${n} 条会话记录（建议存到 运行记录/复盘会话/，重复存同一路径即为追加）`);
}

// 供 UI 显示：本次会话的实时统计（不落盘）
export function sessStats() {
  if (!_cur) return null;
  const c = _cur, R = c.R;
  const st = bookState(R, c.sym);
  const nOpen = (R.book || []).filter(a => a.type === 'open').length;
  const nMarks = marksList(c.sym, c.period).filter(m => (m.ts || 0) >= c.t0).length;
  return { id: c.id, sym: c.sym, period: c.period, mode: c.mode, nStep: c.nStep,
    nSim: nOpen, nClosed: st.closed.length, realized: Math.round(st.realized),
    nMarks, openFlag: st.pos ? 1 : 0, elapsed: Date.now() - c.t0 };
}

export function sessionsCount() { return _lgLoad().length; }

// ★ 五期 T2.1：只读全部会话（按时间升序）。趋势面板用；不暴露 _lgStore 句柄。
export function lgSessions() {
  return _lgLoad().slice().sort((a, b) => (a.t0 || 0) - (b.t0 || 0));
}
