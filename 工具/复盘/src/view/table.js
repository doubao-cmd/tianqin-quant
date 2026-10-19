// 成交列表（右侧"成交"tab）：回放时只显示已发生的笔
import { state, canSeeAnswers } from '../core/state.js';
import { pd, tmap } from '../core/time.js';
import { $, esc, sgn, toast } from '../core/fmt.js';
import { DB } from '../data/index.js';
import { renderAll } from './index.js';

// ★ P1-3 增量刷新：记住上次渲染的行数与上下文签名，播放时只追加"新成交"那一行，不整表重建
let _renderedCount = -1;
let _renderedSig = '';

// ═══════════════════════════════════════════════════════════════════════
// ★ T2.2 排序 + 筛选（五期）
//   排序三态：点一次升 → 再点降 → 第三次取消（回到台账原序 = 时间序）。
//   筛选 chip：全部 / 多 / 空 / 价格止损 / 时间止损 / 接管。
//   ★ 训练模式不生效：按"价格止损"筛 = 提前知道这批单是止损出场的，等于开卷。
//     所以训练模式下 chip 全部置灰并给提示，而不是"点了没反应"（那会让人以为坏了）。
// ═══════════════════════════════════════════════════════════════════════
const _TB_CHIPS = [
  ['all', '全部'], ['long', '多'], ['short', '空'],
  ['stop', '价格止损'], ['time', '时间止损'], ['take', '接管'],
];
let _tbFilter = 'all';
let _tbSort = { key: '', dir: 0 };      // dir: 1 = 升序, -1 = 降序, 0 = 原序

function _tbMatch(t) {
  if (_tbFilter === 'long') return t.d === '多';
  if (_tbFilter === 'short') return t.d === '空';
  if (_tbFilter === 'stop') return t.r === '价格止损' || t.r === '止损';
  if (_tbFilter === 'time') return t.r === '时间止损';
  if (_tbFilter === 'take') return t.e == null;      // 接管（开仓价未知）
  return true;
}

// 排序键取值：null 一律排最后（无论升降序）——接管笔没开仓价，浮到顶端会挡视线
function _tbVal(t, key) {
  if (key === 'd') return t.d === '多' ? 1 : (t.d === '空' ? -1 : 0);
  if (key === 'q') return t.q;
  if (key === 't') return t.t || '';
  if (key === 'pnl') return (t.pnl == null) ? null : t.pnl;
  return null;
}

function _tbSortRows(rows) {
  if (!_tbSort.key || !_tbSort.dir) return rows;
  const k = _tbSort.key, dir = _tbSort.dir;
  return rows.slice().sort((a, b) => {
    const va = _tbVal(a, k), vb = _tbVal(b, k);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (va === vb) return 0;
    return (va > vb ? 1 : -1) * dir;
  });
}

function _tbRenderChips() {
  const el = $('tchips');
  if (!el) return;
  const on = canSeeAnswers();
  el.innerHTML = _TB_CHIPS.map(([k, label]) => {
    const sel = _tbFilter === k;
    return `<button class="chip${sel ? ' on' : ''}${on ? '' : ' dis'}" data-f="${k}"`
      + `${on ? '' : ' title="训练模式不生效：按出场原因筛选等于提前知道答案"'}>${label}</button>`;
  }).join('');
  el.querySelectorAll('.chip').forEach(b => {
    b.onclick = () => tbSetFilter(b.dataset.f);
  });
}

// 筛选入口（chip 用；点当前项 = 取消回到"全部"）
export function tbSetFilter(f) {
  if (!canSeeAnswers()) { toast('训练模式不筛成交（会泄露答案）；切复看模式再用'); return false; }
  _tbFilter = (_tbFilter === f && f !== 'all') ? 'all' : f;
  renderTable();
  return true;
}

// 排序入口：升 → 降 → 取消（三态）
export function tbToggleSort(k) {
  if (!k) return;
  if (_tbSort.key !== k) _tbSort = { key: k, dir: 1 };
  else if (_tbSort.dir === 1) _tbSort = { key: k, dir: -1 };
  else _tbSort = { key: '', dir: 0 };
  renderTable();
}

function _tbHeaderHtml() {
  const cell = (k, label) => {
    const s = (_tbSort.key === k && _tbSort.dir) ? (_tbSort.dir > 0 ? '↑' : '↓') : '↕';
    return `<th class="sortable" data-k="${k}">${label}<span class="arr">${s}</span></th>`;
  };
  return `<tr>${cell('d', '方向')}${cell('q', '手数')}${cell('t', '开仓')}${cell('pnl', '盈亏')}</tr>`;
}

function _tbBindHead() {
  document.querySelectorAll('#tbody th.sortable').forEach(th => {
    th.onclick = () => tbToggleSort(th.dataset.k);
  });
}

function _rowHtml(t, isNew) {
  const cls = t.d === '多' ? 'up' : 'down';
  const pc = t.pnl == null ? '' : (t.pnl > 0 ? 'up' : (t.pnl < 0 ? 'down' : ''));
  return `<tr data-i="${t.i}" class="${isNew ? 'new ' : ''}${state.sel === t.i ? 'on' : ''}">` +
    `<td class="${cls}">${t.d}</td><td class="num">${t.q}</td>` +
    `<td class="num">${t.t ? t.t.slice(5, 16) : '—'}</td>` +
    `<td class="num ${pc}">${t.e == null ? '<span class="muted">接管</span>' : (t.pnl == null ? '—' : sgn(t.pnl))}</td></tr>`;
}

function _bindRow(tr) {
  if (!tr) return;
  tr.onclick = () => {
    // ★ 点成交 = "我要看这一笔"：若当前是"近1日/近3日"，该笔可能在区间外，
    //   强行按区间定位会算出 -1 索引 → 视图塌成一团。切 ALL 再定位。
    state.sel = +tr.dataset.i;
    state.range = 'ALL';
    state._lastViewKey = null;
    renderAll();
  };
}

export function renderTable(opts = {}) {
  const appendOnly = !!opts.appendOnly;   // ★ P1-3：播放中传 true，走增量路径
  const d = DB.data[state.sym];
  const R = state.replay;
  // ★ P1-5：训练模式隐藏成交列表（防止开卷考试）
  if (!canSeeAnswers()) {
    $('ttitle').textContent = `${d.name} ｜ 训练模式：不显示成交`;
    $('tbody').innerHTML = '<div class="muted" style="padding:8px">训练模式：不显示成交（防止开卷考试）</div>';
    _tbRenderChips();
    _renderedCount = -1; _renderedSig = '';
    return;
  }
  let rows = d.trades;
  if (R) {
    const p = pd();
    const hi = p.x[Math.min(p.x.length - 1, R.abs)];
    rows = rows.filter(t => t.t && t.x && tmap(t.x) <= tmap(hi));
  }
  const totalN = rows.length;
  // ★ T2.2：先筛后排。有筛选/排序时禁用增量追加（追加只在"原序+无筛选"下成立）
  const active = (_tbFilter !== 'all') || (_tbSort.dir !== 0);
  if (_tbFilter !== 'all') rows = rows.filter(_tbMatch);
  const filteredN = rows.length;
  rows = _tbSortRows(rows);

  const sig = `${state.sym}|${state.period}|${R ? R.startAbs : 'static'}|${_tbFilter}|${_tbSort.key}${_tbSort.dir}`;
  const openN = d.trades.filter(t => !t.x).length;
  const tail = active ? `（筛后 ${filteredN}/${totalN}）` : '';
  $('ttitle').textContent = R
    ? `${d.name} ｜ 回放中 · 已发生 ${filteredN} 笔${active ? tail : ''}`
    : `${d.name} ｜ ${filteredN} 笔${active ? tail : ''}` + (openN ? ` ｜ 未平 ${openN}` : '');

  _tbRenderChips();

  // ★ P1-3 增量路径（仅播放中 + 无筛选排序）：
  if (appendOnly && !active && sig === _renderedSig) {
    if (rows.length === _renderedCount) return;                 // 无新成交 → 不碰 DOM（避免每根整表闪烁）
    if (rows.length === _renderedCount + 1) {                   // 刚多 1 行 → 只追加末行 + flash 高亮
      // ★ 注意：#tbody 是外层 div，真正的 <tbody> 嵌在里面的 <table>，必须追加到内层 tbody
      const innerTb = $('tbody').querySelector('tbody');
      if (innerTb) innerTb.insertAdjacentHTML('beforeend', _rowHtml(rows[rows.length - 1], true));
      _renderedCount = rows.length;
      _bindRow($('tbody').querySelector('tbody tr:last-child'));
      return;
    }
    // 行数非 +1（罕见：同根多笔平仓 / 上下文微变）→ 落到下面的全量重建
  }
  let html = (d.note ? `<div class="note">${esc(d.note)}</div>` : '') +
    `<table><thead>${_tbHeaderHtml()}</thead><tbody>`;
  if (!rows.length) {
    html += `<tr><td colspan="4" class="muted" style="padding:8px">没有符合条件的成交（当前筛选：${esc((_TB_CHIPS.find(c => c[0] === _tbFilter) || ['', '全部'])[1])}）</td></tr>`;
  } else {
    rows.forEach(t => { html += _rowHtml(t, false); });
  }
  html += '</tbody></table>';
  $('tbody').innerHTML = html;
  _renderedCount = rows.length;
  _renderedSig = sig;
  document.querySelectorAll('#tbody tbody tr[data-i]').forEach(_bindRow);
  _tbBindHead();
}
