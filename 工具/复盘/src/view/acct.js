// 账户页（R5）：跨会话累计的模拟成交 → 资金曲线 + 指标卡 + 逐笔明细
//
// 与「模拟」tab 的分工：
//   「模拟」= 本会话的挂单/持仓/已平（内存，退出即清）
//   「账户」= 所有会话累计（localStorage `rt:simtrades:v1`，cap 2000）
//
// 口径（写死，与 book.js R5/R6 同源）：
//   · 初始资金 20 万（06 INIT / book.simCfg.principal 同款）
//   · 净值 = 20 万 + 逐笔已实现净盈亏（pnl 已扣双边成本）
//   · 只统计**已平仓**，未平仓不算浮动盈亏（和桌面周报同一口径）
import { state } from '../core/state.js';
import { $, esc, money, sgn, num0, num2, toast } from '../core/fmt.js';
import { lsStore } from '../core/store.js';

const ACCT_KEY = 'rt:simtrades:v1';
const ACCT_CAP = 2000;   // ★ 不能叫 CAP：ledger.js 已占用该顶层名（拼单文件后直接 SyntaxError）
export const PRINCIPAL = 200000;

const _store = lsStore(ACCT_KEY, {
  cap: ACCT_CAP,
  clean: r => (r && r.xX && isFinite(r.pnl)) ? r : null,
  onCap: () => toast(`账户明细已满 ${ACCT_CAP} 条，最早记录被截掉`),
  msg: 'localStorage 不可用：账户明细只在本次会话有效',
});

export function acctList() {
  return _store.load().slice();
}

// 并入一批已平仓明细（ledger 收尾时调）。k 去重 ⇒ 同一笔被多次 flush 也只记一次。
export function acctAppend(rows) {
  if (!rows || !rows.length) return 0;
  const list = _store.load().slice();
  const seen = new Set(list.map(r => r.k));
  let n = 0;
  rows.forEach(r => {
    if (!r || !r.k || seen.has(r.k)) return;
    list.push(r); seen.add(r.k); n++;
  });
  if (n) _store.save(list);
  return n;
}

export function acctClear() { _store.save([]); }

export function acctCount() { return _store.load().length; }

// ── 指标 ────────────────────────────────────────────────────────────
function _metrics(rows) {
  const n = rows.length;
  if (!n) return null;
  const sum = rows.reduce((s, r) => s + r.pnl, 0);
  const fee = rows.reduce((s, r) => s + (r.fee || 0), 0);
  const wins = rows.filter(r => r.pnl > 0);
  const losses = rows.filter(r => r.pnl <= 0);
  const avgW = wins.length ? wins.reduce((s, r) => s + r.pnl, 0) / wins.length : 0;
  const avgL = losses.length ? losses.reduce((s, r) => s + r.pnl, 0) / losses.length : 0;
  let eq = PRINCIPAL, peak = eq, mdd = 0, mddPct = 0;
  rows.forEach(r => {
    eq += r.pnl;
    if (eq > peak) peak = eq;
    const dd = peak - eq;
    if (dd > mdd) { mdd = dd; mddPct = peak > 0 ? dd / peak * 100 : 0; }
  });
  return {
    n, sum, fee, equity: eq,
    winRate: wins.length / n,
    payoff: (losses.length && avgL !== 0) ? Math.abs(avgW / avgL) : null,
    avg: sum / n, avgW, avgL, mdd, mddPct,
  };
}

// ── 净值曲线（手写 Canvas，不新建 Plotly 实例 —— 主图已经很重）───────────
function _drawCurve(rows) {
  const cv = $('acctCv');
  if (!cv) return;
  const w = cv.clientWidth || 320;
  const h = 150;
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(w * dpr);
  cv.height = Math.round(h * dpr);
  const ctx = cv.getContext('2d');
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const vals = [PRINCIPAL];
  let eq = PRINCIPAL;
  rows.forEach(r => { eq += r.pnl; vals.push(eq); });

  const ml = 4, mr = 46, mt = 8, mb = 14;
  const iw = Math.max(10, w - ml - mr), ih = Math.max(10, h - mt - mb);
  let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
  if (hi - lo < 1) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.12;
  lo -= pad; hi += pad;
  const X = i => ml + (vals.length <= 1 ? 0 : i / (vals.length - 1) * iw);
  const Y = v => mt + (1 - (v - lo) / (hi - lo)) * ih;

  // 基准线（20 万）
  ctx.strokeStyle = '#3a4350';
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  ctx.moveTo(ml, Y(PRINCIPAL)); ctx.lineTo(ml + iw, Y(PRINCIPAL));
  ctx.stroke();
  ctx.setLineDash([]);

  // 回撤填充（峰值到当前）
  let peak = -Infinity;
  const peaks = vals.map(v => { if (v > peak) peak = v; return peak; });
  ctx.fillStyle = 'rgba(239,68,68,0.13)';
  ctx.beginPath();
  vals.forEach((v, i) => { const x = X(i), y = Y(peaks[i]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  for (let i = vals.length - 1; i >= 0; i--) ctx.lineTo(X(i), Y(vals[i]));
  ctx.closePath();
  ctx.fill();

  // 净值线
  const up = vals[vals.length - 1] >= PRINCIPAL;
  ctx.strokeStyle = up ? '#ef4444' : '#22c55e';   // 涨红跌绿（国内惯例）
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  vals.forEach((v, i) => { const x = X(i), y = Y(v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.stroke();

  // 末值点 + 数值（标签放点的左边，避免和右侧上下界刻度叠字）
  const lx = X(vals.length - 1), ly = Y(vals[vals.length - 1]);
  ctx.fillStyle = up ? '#ef4444' : '#22c55e';
  ctx.beginPath(); ctx.arc(lx, ly, 3, 0, Math.PI * 2); ctx.fill();
  ctx.font = '10px "Microsoft YaHei",sans-serif';
  ctx.textAlign = 'right';
  ctx.fillText(num0(vals[vals.length - 1]), Math.max(ml + 30, lx - 6), ly + 3);

  // 上下界刻度
  ctx.fillStyle = '#9aa3af';
  ctx.textAlign = 'right';
  ctx.fillText(num0(hi), w - 2, mt + 8);
  ctx.fillText(num0(lo), w - 2, mt + ih);
}

// ── 渲染 ────────────────────────────────────────────────────────────
export function renderAcct(force) {
  if (!force && state._tab !== 'acct') return;
  const el = $('acct');
  if (!el) return;

  const all = acctList();
  if (!all.length) {
    el.innerHTML =
      '<div class="ct">模拟账户（跨会话累计）</div>' +
      '<div class="muted" style="padding:10px 4px;line-height:1.8">' +
      '还没有已平仓的模拟单。<br>' +
      '① 点「随机 ▶」开一局 → ② 按 <b>B</b>/<b>S</b> 下单（设好止损）→ ③ 平仓或等止损触发 → ④ 交卷。<br>' +
      '每一笔已平仓都会进这里，累计成你的练习账户曲线（初始 20 万，口径同 06：双边成本已扣）。' +
      '</div>';
    return;
  }

  // 品种筛
  const syms = Array.from(new Set(all.map(r => r.sym))).sort();
  const pick = state._acctSym && syms.includes(state._acctSym) ? state._acctSym : '';
  const rows = all
    .filter(r => !pick || r.sym === pick)
    .sort((a, b) => String(a.xX || '').localeCompare(String(b.xX || '')) || (a.t || 0) - (b.t || 0));
  const m = _metrics(rows);

  const symOpts = ['<option value="">全部品种</option>']
    .concat(syms.map(s => `<option value="${esc(s)}"${s === pick ? ' selected' : ''}>${esc(s)}</option>`))
    .join('');

  let html = '<div class="ct">模拟账户（跨会话累计）' +
    `　<span class="muted">${syms.length} 个品种 · ${all.length} 笔</span></div>`;
  html += '<canvas id="acctCv" style="width:100%;height:150px;display:block"></canvas>';

  if (m) {
    const cls = v => v >= 0 ? 'up' : 'down';
    html += '<div class="acct-kpi">' +
      `<div><span>已实现</span><b class="${cls(m.sum)}">${money(m.sum)}</b></div>` +
      `<div><span>净值</span><b class="${cls(m.equity - PRINCIPAL)}">${num0(m.equity)}</b></div>` +
      `<div><span>笔数</span><b>${m.n}</b></div>` +
      `<div><span>胜率</span><b>${(m.winRate * 100).toFixed(0)}%</b></div>` +
      `<div><span>盈亏比</span><b>${m.payoff == null ? '—' : m.payoff.toFixed(2)}</b></div>` +
      `<div><span>期望</span><b class="${cls(m.avg)}">${money(m.avg)}</b></div>` +
      `<div><span>最大回撤</span><b class="down">${num0(m.mdd)}</b><i>${num2(m.mddPct)}%</i></div>` +
      `<div><span>手续费</span><b>${num0(m.fee)}</b></div>` +
      '</div>';
  }

  html += `<div class="acct-bar"><select id="acctSym">${symOpts}</select>` +
    '<button id="acctClear" title="清空全部跨会话记录（不可撤销）">清空</button>' +
    '<span class="muted">按平仓时间排序 ｜ 最近 100 笔</span></div>';

  const show = rows.slice().reverse().slice(0, 100);
  let tr = '';
  show.forEach(r => {
    tr += `<tr><td>${esc(String(r.xX || '').slice(5, 16))}</td>` +
      `<td>${esc(r.sym)}</td>` +
      `<td class="${r.dir === '多' ? 'up' : 'down'}">${esc(r.dir)}${r.qty}</td>` +
      `<td>${r.ePx}→${r.xPx}</td>` +
      `<td>${esc(r.reason || '')}</td><td>${r.hold}</td>` +
      `<td class="${r.pnl >= 0 ? 'up' : 'down'}">${money(r.pnl)}</td></tr>`;
  });
  html += '<div class="acct-tbl"><table><thead><tr><th>平仓</th><th>品种</th><th>方向</th>' +
    '<th>开→平</th><th>原因</th><th>根</th><th>盈亏</th></tr></thead><tbody>' +
    tr + '</tbody></table></div>';
  if (rows.length > 100) {
    html += `<div class="muted" style="padding:3px">只显示最近 100 笔（共 ${rows.length} 笔，筛选内）</div>`;
  }

  el.innerHTML = html;
  _drawCurve(rows);

  const sel = $('acctSym');
  if (sel) sel.onchange = () => { state._acctSym = sel.value || ''; renderAcct(true); };
  const clr = $('acctClear');
  if (clr) clr.onclick = () => {
    if (!window.confirm('清空全部跨会话模拟成交记录？此操作不可撤销。')) return;
    acctClear();
    renderAcct(true);
  };
}
