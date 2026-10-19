// 决策标记列表（五期 T2.3）——右侧第 5 个 tab
//
// 为什么要单列：P2-2 只把标记画成图上的点。想回答"我上次在 10-13 14:30 判断会进多，
// 后来到底走没走"必须一个个点上去看 → 等于没有复盘。
//
// ★ 红线：训练模式**不渲染**这个 tab（topbar.syncMarkTab 直接 display:none）。
//   不是 disabled——disabled 的 DOM 仍然能被读到，等于半个开卷。
//
// ★ 前程收益口径与 score.js 同源（`_scFwd`）：hi = min(abs+N, endAbs, len-1)，
//   不足 2 根返回 null（不给假数）。N=10 与评分卡一致。
import { state, canSeeAnswers } from '../core/state.js';
import { pd } from '../core/time.js';
import { $, esc } from '../core/fmt.js';
import { marksList, markColor } from '../replay/marks.js';
import { _scFwd } from '../replay/score.js';
import { seekTo } from '../replay/engine.js';

const _MKL_N = 10;

// 点一行 → 跳回那根 bar
function _mklJump(abs) {
  const d = pd();
  if (abs == null || !d.x[abs]) return;
  if (state.replay) { seekTo(abs); return; }      // 回放中：直接定位
  state.range = 'ALL';                            // 非回放：先把区间放开，再缩到该 bar 附近
  state._lastViewKey = null;
  const a = Math.max(0, abs - 40);
  const b = Math.min(d.x.length - 1, abs + 40);
  try {
    Plotly.relayout('main', { 'xaxis.range': [d.x[a], d.x[b]] });
    state._lastViewKey = `${state.sym}|${state.period}|${state.range}|${state.sel}`;
  } catch (e) {}
}

export function renderMkTab() {
  const el = $('mklist');
  if (!el) return;
  if (!canSeeAnswers()) { el.innerHTML = ''; return; }   // 训练模式：不渲染（红线）

  const list = marksList(state.sym, state.period);
  if (!list.length) {
    el.innerHTML = '<div class="stat-h">决策标记</div>'
      + '<div class="tr-empty">这份数据还没有标记。回放中按 <b>M</b> 记录你对当前 bar 的判断，'
      + '这里会累计成一张表（含 10 根后的实际走势）。</div>';
    return;
  }
  const d = pd();
  const endAbs = state.replay ? state.replay.abs : d.x.length - 1;

  let nHit = 0, nDir = 0;
  const rows = list.slice().sort((a, b) => (b.abs || 0) - (a.abs || 0)).map(m => {
    const abs = (m.abs != null) ? m.abs : null;
    const fw = (abs == null) ? null : _scFwd(d, abs, _MKL_N, endAbs);
    let hitTxt = '—', hitCls = 'muted';
    if (fw) {
      const ret = fw.ret;
      if (m.verdict === '会进多') { nDir++; if (ret > 0) { nHit++; hitTxt = '✓ 对'; hitCls = 'up'; } else { hitTxt = '✗ 错'; hitCls = 'down'; } }
      else if (m.verdict === '会进空') { nDir++; if (ret < 0) { nHit++; hitTxt = '✓ 对'; hitCls = 'up'; } else { hitTxt = '✗ 错'; hitCls = 'down'; } }
      else if (m.verdict === '不进') { hitTxt = '规避 · 不看对错'; hitCls = 'muted'; }
      else { hitTxt = '—'; }
    }
    const retTxt = fw ? (fw.ret * 100).toFixed(2) + '%' : '—';
    const retCls = fw ? (fw.ret >= 0 ? 'up' : 'down') : 'muted';
    return `<tr data-abs="${abs == null ? '' : abs}">`
      + `<td class="num">${esc(String(m.barX || '').slice(5, 16))}</td>`
      + `<td style="color:${markColor(m.verdict)}">${esc(m.verdict)}</td>`
      + `<td class="num ${retCls}">${retTxt}</td>`
      + `<td class="${hitCls}">${hitTxt}</td>`
      + `<td class="mknote">${m.note ? esc(m.note) : '<span class="muted">—</span>'}</td></tr>`;
  }).join('');

  const rate = nDir ? Math.round(nHit / nDir * 100) + '%' : '—';
  el.innerHTML = `
    <div class="stat-h">决策标记 · ${list.length} 条</div>
    <div class="stat-line">方向判断（会进多/会进空）<b>${nDir}</b> 条，其中 <b>${nHit}</b> 条与后 ${_MKL_N} 根走势一致 = <b>${rate}</b></div>
    <table><thead><tr><th>时间</th><th>判断</th><th>后 ${_MKL_N} 根</th><th>对错</th><th>理由</th></tr></thead>
      <tbody>${rows}</tbody></table>
    <div class="stat-note">点一行跳回该 bar。<br>
    「后 ${_MKL_N} 根」= 标记后第 ${_MKL_N} 根收盘相对标记根收盘的涨跌；不足 2 根时显示 —（不给假数）。<br>
    「不进」没有方向，<b>不判对错</b>——它的价值是规避质量（见统计页训练曲线 avoidQ）。</div>`;

  el.querySelectorAll('tr[data-abs]').forEach(tr => {
    tr.onclick = () => _mklJump(tr.dataset.abs === '' ? null : +tr.dataset.abs);
  });
}
