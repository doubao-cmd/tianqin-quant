// 决策标记（P2-2）——"我当时的判断"，复盘真正沉淀下来的东西。
//
// 规格（路线图 §7 P2-2）：
//   回放到任意 bar，按 M 或点「标记」→ 面板：会进多 / 会进空 / 不进 / 不确定 + 可选理由
//   存储 localStorage['rt:marks:v1']：
//     [{sym:'M', period:'5', barX:'2026-10-13 14:30', abs:312, verdict:'不进', note:'…', ts:…}]
//   导出 CSV：标记_YYYYMMDD.csv（utf-8-sig，防 Excel 打开中文乱码）
//   图上：小圆点吸附到 bar（绿=会进，红=不进，灰=不确定）
//
// 红线自查（写码前逐条对）：
//   1. 标记是"用户自己的判断"，不是 trades（答案）⇒ 训练/复看两种模式都显示。
//      §13 红线只管 trades 的导航/显示，不覆盖用户自产数据。
//   2. 同一根 bar 重复标记 = 覆盖更新（键 = sym|period|barX），绝不堆叠成两条。
//   3. barX 是 bar 标签字符串（如 '2026-10-13 14:30'），与 periods[period].x 元素逐一相等
//      ⇒ 图上用 findIndex(s => s === m.barX) 精确吸附，不可能落在两根 bar 之间。
//   4. localStorage 不可用（隐私模式 / 配额满 / file:// 被禁）⇒ 降级为内存数组，
//      会话内照常可用，刷新即丢；导出前 toast 说明，不假装已持久化。
//   5. abs 只是记录字段（CSV 用 / 排查用），身份一律认 barX —— 数据重建后索引会漂，时间戳不会。
import { state } from '../core/state.js';
import { pd } from '../core/time.js';
import { $, toast } from '../core/fmt.js';
import { lsStore } from '../core/store.js';
import { renderAll } from '../view/index.js';

// ★ bundle 顶层作用域唯一性：store.js 的 lsStore 已把内部状态关进闭包，
//   但 key 常量/store 句柄/读写封装在本模块仍是顶层名 —— 拼接后与 ledger.js
//   等同名会 SyntaxError（P2-3 实测：一次报 4 组）。⇒ 一律加 mk 前缀。
const MARKS_KEY = 'rt:marks:v1';
const _mkStore = lsStore(MARKS_KEY, {
  clean: m => (m && m.barX != null && m.verdict) ? m : null,   // 读盘时丢弃残条
});
function _mkLoad() { return _mkStore.load(); }
function _mkSave(list) { return _mkStore.save(list); }

// 判断 → 颜色（路线图：绿=会进，红=不进，灰=不确定；会进多/会进空同色，方向看 hover）
export const VERDICTS = [
  { v: '会进多', c: '#22c55e' },
  { v: '会进空', c: '#22c55e' },
  { v: '不进',   c: '#ef4444' },
  { v: '不确定', c: '#9aa3af' },
];

// 唯一键：品种 + 周期 + bar 时间标签
export function markKey(sym, period, barX) { return `${sym}|${period}|${barX}`; }

// 某品种+周期的全部标记，按 bar 时间正序（图上一根一根画）
export function marksList(sym, period) {
  const s = sym || state.sym, p = period || state.period;
  return _mkLoad().filter(m => m.sym === s && m.period === p)
    .sort((a, b) => (a.barX < b.barX ? -1 : a.barX > b.barX ? 1 : 0));
}

// 当前品种+周期、某一根 bar 上的标记（没有返回 null）
export function markAt(barX) {
  if (barX == null) return null;
  const k = markKey(state.sym, state.period, barX);
  return _mkLoad().find(m => markKey(m.sym, m.period, m.barX) === k) || null;
}

export function markColor(v) {
  const it = VERDICTS.find(x => x.v === v);
  return it ? it.c : '#9aa3af';
}

// 落一条标记：同键覆盖（更新判断/理由/时间戳），异键追加
export function saveMark({ barX, abs, verdict, note }) {
  const sym = state.sym, period = state.period;
  const list = _mkLoad().slice();
  const k = markKey(sym, period, barX);
  const i = list.findIndex(m => markKey(m.sym, m.period, m.barX) === k);
  const rec = {
    sym, period, barX,
    abs: (abs == null ? null : abs),
    verdict,
    note: String(note || '').slice(0, 200),   // 理由限 200 字，防 localStorage 爆配额
    ts: Date.now(),
  };
  if (i >= 0) list[i] = rec; else list.push(rec);
  _mkSave(list);
  return rec;
}

// 删某一根 bar 的标记（按当前品种+周期）
export function delMark(barX) {
  const k = markKey(state.sym, state.period, barX);
  _mkSave(_mkLoad().filter(m => markKey(m.sym, m.period, m.barX) !== k));
}

// ── CSV 导出（utf-8-sig） ─────────────────────────────────────────────
function _csvCell(s) {
  const t = String(s == null ? '' : s);
  return /[",\r\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
}
function _fmtTs(ts) {
  const d = new Date(ts || Date.now()), p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function _yyyymmdd(d) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}`;
}

export function exportMarksCsv() {
  const list = _mkLoad().slice().sort((a, b) => (a.ts || 0) - (b.ts || 0));
  if (!list.length) { toast('暂无标记可导出'); return; }
  const head = ['品种', '周期', 'bar时间', '绝对索引', '判断', '理由', '记录时间'];
  const rows = list.map(m => [m.sym, m.period, m.barX, m.abs, m.verdict, m.note || '', _fmtTs(m.ts)]);
  // ★ utf-8-sig：BOM 让 Excel 双击打开时按 UTF-8 解码，中文才不乱码
  const csv = '\ufeff' + [head].concat(rows).map(r => r.map(_csvCell).join(',')).join('\r\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `标记_${_yyyymmdd(new Date())}.csv`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { try { URL.revokeObjectURL(a.href); } catch (e) {} a.remove(); }, 0);
  toast(`已导出 ${list.length} 条标记（建议存到 运行记录/复盘标记/）`);
}

// ── 标记面板 ─────────────────────────────────────────────────────────
export function openMarkPanel() {
  if (!state.replay) { toast('请先进入回放（标记要落到具体 bar）'); return; }
  const d = pd();
  const barX = d.x[state.replay.abs];
  const ex = markAt(barX);
  $('mkBar').textContent = `标记 ${String(barX).slice(0, 16)}${ex ? '（已有：' + ex.verdict + '）' : ''}`;
  $('mkNote').value = ex ? (ex.note || '') : '';
  pickVerdict(ex ? ex.verdict : '');
  $('mkDel').disabled = !ex;
  $('mkPanel').style.display = 'block';
  $('mkNote').focus();
}

// 选判断（只改面板状态，不落盘；确认时才 saveMark）
export function pickVerdict(v) {
  const p = $('mkPanel');
  if (!p) return;
  p.dataset.verdict = v || '';
  ['mkBuy', 'mkSell', 'mkNo', 'mkUnsure'].forEach(id => {
    const el = $(id);
    if (!el) return;
    if (el.dataset.v === v) el.classList.add('on'); else el.classList.remove('on');
  });
  const hint = $('mkHint');
  if (hint) {
    hint.textContent = v === '不进' ? '「不进」= 你认为这里不该动手（P2-4 会拿后续走势验证）'
      : (v === '不确定' ? '「不确定」= 看不出名堂——这也是一种判断，照实记'
        : (v ? `「${v}」= 你认为这里值得进场` : '选一个判断，理由可留空'));
  }
}

export function confirmMark() {
  if (!state.replay) { closeMarkPanel(); return; }
  const v = ($('mkPanel').dataset.verdict) || '';
  if (!v) { toast('请先选择判断（会进多 / 会进空 / 不进 / 不确定）'); return; }
  const d = pd();
  const barX = d.x[state.replay.abs];
  const note = ($('mkNote').value || '').trim().slice(0, 200);
  saveMark({ barX, abs: state.replay.abs, verdict: v, note });
  closeMarkPanel();
  renderAll();                                   // 立刻把圆点画出来
  toast(`已标记 ${String(barX).slice(0, 16)}：${v}`);
}

export function delCurrentMark() {
  if (!state.replay) { closeMarkPanel(); return; }
  const d = pd();
  const barX = d.x[state.replay.abs];
  delMark(barX);
  closeMarkPanel();
  renderAll();
  toast('已删除该 bar 的标记');
}

export function closeMarkPanel() {
  const p = $('mkPanel');
  if (p) p.style.display = 'none';
}
