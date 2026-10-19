// 成交详情（右侧"成交"tab 下半）：选中一笔显示开平/止损/MFE/MAE 等
import { state } from '../core/state.js';
import { pd, tmap } from '../core/time.js';
import { $, esc, f1, f2, num2, sgn, money } from '../core/fmt.js';
import { DB } from '../data/index.js';
import { renderAll } from './index.js';
import { afterExit } from '../replay/segstats.js';

// ★ P3-4：出场后 N 根（5/10/20）继续走了多少。
// ⚠️ 这个数字**极度危险**：它会诱导「我出早了，应该拿住」——正是"提前出场家族"5 次失败的那条路。
//    所以每次显示都强制带警告，且只给"错过多少"，不给"应该拿住"的建议。
function _dtAfterHtml(t, d, mult) {
  const Ns = [5, 10, 20];
  const parts = Ns.map(N => {
    const r = afterExit(t, d, N, mult);
    if (!r) return null;
    if (!r.n) return `<div class="muted">${N} 根：平仓后已无数据</div>`;
    const cls = r.gain > 0 ? 'up' : 'down';
    return `<div>${N} 根内最有利 <b>${f1(r.px)}</b>　` +
      `<span class="${cls}">${sgn(r.yuan)} 元</span>` +
      `<span class="muted">（${(r.pct * 100).toFixed(2)}%，看了 ${r.n} 根）</span></div>`;
  }).filter(Boolean);
  return `<div class="stat-h" style="margin-top:4px">出场后路径</div>${parts.join('')}
    <div class="warn" style="margin-top:3px">⚠️ 单笔的"错过"是<b>事后视角</b>。改变出场规则会同时改变
    所有其它笔的结果，而 06 毛利仅成本的 4.5% ⇒ 改出场在数学上不可能改变期望符号。<br>
    把它当"我是不是砍太早"的自省材料，<b>不要当改进方案</b>。</div>`;
}

export function renderDetail() {
  if (state.sel == null) {
    $('detail').innerHTML = '<div class="muted">点「成交」里一行 → 图缩放到那笔并亮红</div>';
    return;
  }
  const t = DB.data[state.sym].trades[state.sel];
  const d = pd();
  let slope = null, dist = null;
  if (t.t) {
    let bi = 0;
    for (let k = d.x.length - 1; k >= 0; k--) {
      if (tmap(d.x[k]) <= tmap(t.t)) { bi = k; break; }
    }
    slope = d.slope ? d.slope[bi] : null;
    dist = d.atr_dist ? d.atr_dist[bi] : null;
  }
  const openLine = t.e == null
    ? `<div>开 ${t.t ? t.t.slice(5,16) : '—'} @ <b>接管笔 · 开仓价未知</b></div>`
    : `<div>开 ${t.t ? t.t.slice(5,16) : '—'} @ ${f1(t.e)}</div>`;
  $('detail').innerHTML =
    `<div><b>${t.d} ${t.q} 手</b>　<span class="muted">${esc(t.k)}</span></div>` +
    openLine +
    `<div>平 ${t.x ? t.x.slice(5,16) : '—'} @ ${f1(t.p)}</div>` +
    `<div>止损 ${f2(t.s)}　计划 ${f2(t.ps)}</div>` +
    `<div>开仓时 MA13 斜率 ${num2(slope)} ｜ 距MA ${num2(dist)} ATR</div>` +
    `<div>MFE <span class="up">${sgn(t.mfe)}</span>　MAE <span class="down">${sgn(t.mae)}</span>　盈亏 <b>${sgn(t.pnl)}</b></div>` +
    `<div class="muted">${esc(t.r)}　持仓 ${t.h==null?'—':t.h+' 分'}</div>` +
    (t.x && t.p != null ? _dtAfterHtml(t, d, DB.data[state.sym].mult || 1) : '') +
    `<button id="clr" style="margin-top:4px;width:100%">取消选中</button>`;
  const b = $('clr'); if (b) b.onclick = () => { state.sel = null; renderAll(); };
}
