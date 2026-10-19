// 对照面板（P2-5）：右侧第 4 个 tab —— 本根对照 + 分歧台账
//
// 两块：
//   ① #cmpNow  当前 bar 并排：「策略实际」/「我的判断」+ 分歧判定（随回放逐根刷新）
//   ② #cmpList 全部带标记的 bar 的分歧台账（不受回放位置影响，退出回放也能看）
//
// ★ 为什么整块 gate 在「live 源 + 复看模式」：这一列「策略实际」本身就是答案。
//   训练模式下显示它 = 开卷考试，故直接给原因而不是给空白（空白会被当成 bug）。
import { state, canSeeAnswers } from '../core/state.js';
import { pd } from '../core/time.js';
import { $, esc, money, sgn } from '../core/fmt.js';
import { DB } from '../data/index.js';
import { markColor } from '../replay/marks.js';
import { cmpAt, cmpRows, cmpSummary, cmpEligible, cmpHasTrades, CMP_N } from '../replay/compare.js';

const _CMP_MAXROWS = 200;
const _DIVERGE = { opposite: 1, avoid: 1, frontrun: 1, late: 1 };

const _cmpPct = v => (v == null || !isFinite(v)) ? '—'
  : (v > 0 ? '+' : '') + (v * 100).toFixed(2) + '%';

function _cmpTs(ts) {
  const d = new Date(ts || Date.now()), p = n => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 禁用原因（明确说清是哪一条不满足，不静默）
function _cmpWhy() {
  const dd = DB.data[state.sym];
  if (!cmpHasTrades()) {
    return `<div class="ct">${esc(dd ? dd.name : state.sym)} ｜ 对照</div>
      <div class="muted" style="padding:8px;line-height:1.7">
        本品种没有台账成交 ⇒ 无「策略实际」可对照。<br>
        对照需要 <b>live 源</b>（实盘台账）才有意义；hist 源（历史回放）没有 trades。</div>`;
  }
  return `<div class="ct">${esc(dd ? dd.name : state.sym)} ｜ 对照</div>
    <div class="muted" style="padding:8px;line-height:1.7">
      <b>训练模式已禁用对照</b>——「策略实际」这一列就是答案，同屏等于开卷考试。<br>
      切到 <b>复看模式</b>（顶栏右侧按钮）即可使用。</div>`;
}

// ── 策略列：本 bar 上策略做了什么 ──────────────────────────────────────────
function _cmpStratHtml(x, dd) {
  const d = pd();
  const mult = dd.mult || 1;
  const cur = d.c[x.abs];
  const lines = [];
  x.opens.forEach(t => {
    const cls = t.d === '多' ? 'up' : 'down';
    let h = `<b class="${cls}">开${t.d} ${t.q} 手</b>`;
    if (t.e != null) h += ` <span class="muted">→ 成交 @${t.e}</span>`;
    if (t.ps != null) h += `<br><span class="muted">计划止损 ${t.ps}</span>`;
    if (t.pnl != null) {
      h += `<br><span class="muted">→ 平仓 @${t.p}（${esc(t.r || '')}）</span> `
         + `<b class="${t.pnl >= 0 ? 'up' : 'down'}">${money(t.pnl)}</b> 元`;
    } else {
      h += `<br><span class="muted">→ 未平仓</span>`;
    }
    lines.push(h);
  });
  x.closes.forEach(t => {
    const cls = t.d === '多' ? 'up' : 'down';
    lines.push(`<b class="${cls}">平${t.d} ${t.q} 手</b> `
      + `<span class="muted">@${t.p}（${esc(t.r || '')}）</span> `
      + `<b class="${(t.pnl || 0) >= 0 ? 'up' : 'down'}">${money(t.pnl)}</b> 元`);
  });
  if (!lines.length && x.holds.length) {
    x.holds.forEach(t => {
      const cls = t.d === '多' ? 'up' : 'down';
      const fl = (cur - t.e) * t.q * mult * (t.d === '多' ? 1 : -1);
      lines.push(`<b class="${cls}">持有${t.d} ${t.q} 手</b> `
        + `<span class="muted">@${t.e} · 已持 ${x.abs - t.openAbs} 根</span>`
        + `<br>浮盈 <b class="${fl >= 0 ? 'up' : 'down'}">${sgn(fl)}</b> 元`);
    });
  }
  return lines.length ? lines.join('<div style="height:4px"></div>')
                      : '<span class="muted">无动作</span>';
}

function _cmpMineHtml(x) {
  if (!x.mark) return '<span class="muted">未标记（按 M 记录你的判断）</span>';
  let h = `<b style="color:${markColor(x.mark.verdict)}">${esc(x.mark.verdict)}</b>`;
  if (x.mark.note) h += `<br><span class="muted">理由：${esc(x.mark.note)}</span>`;
  h += `<br><span class="muted">记于 ${_cmpTs(x.mark.ts)}</span>`;
  return h;
}

function _cmpNowHtml(x, dd) {
  const bar = `${x.barX}`.slice(0, 16);
  return `<div class="ct">${esc(dd.name)} ｜ 对照 · bar ${x.abs + 1} ｜ ${bar}</div>
    <div class="cmp-2col">
      <div class="cmp-col"><div class="cmp-lab">策略实际</div><div class="cmp-val">${_cmpStratHtml(x, dd)}</div></div>
      <div class="cmp-col"><div class="cmp-lab">我的判断</div><div class="cmp-val">${_cmpMineHtml(x)}</div></div>
    </div>
    <div class="cmp-div"><span class="cmp-badge" style="color:${x.div.c};border-color:${x.div.c}66;background:${x.div.c}1f">${esc(x.div.label)}</span></div>`;
}

// ── 台账表 ────────────────────────────────────────────────────────────────
const _KIND_ORDER = ['opposite', 'avoid', 'frontrun', 'late', 'inpos', 'same', 'agree', 'unsure', 'unmarked'];
const _KIND_NAME = {
  same: '同向一致', opposite: '反向分歧', avoid: '我规避', frontrun: '我抢跑',
  late: '我迟到', inpos: '策略持仓中我标不进', agree: '一致不做',
  unsure: '我犹豫', unmarked: '未表态',
};

function _cmpListHtml(rows) {
  const dd = DB.data[state.sym];
  if (!rows.length) {
    return `<div class="ct">${esc(dd.name)} ｜ 分歧台账</div>
      <div class="muted" style="padding:8px;line-height:1.7">
        还没有标记。回放中按 <b>M</b> 在关键 bar 上记下你的判断（会进多 / 会进空 / 不进 / 不确定），
        这里就会列出「策略那一刻在做什么」和「你怎么看」的分歧。</div>`;
  }
  const s = cmpSummary(rows);
  const show = rows.length > _CMP_MAXROWS ? rows.slice(rows.length - _CMP_MAXROWS) : rows;
  let tr = '';
  show.forEach(r => {
    const t = `${r.barX}`.slice(5, 16);
    const st = r.opens.length
      ? `<b class="${r.opens[0].d === '多' ? 'up' : 'down'}">开${r.opens[0].d} ${r.opens[0].q} 手</b>`
      : (r.holds.length ? `<span class="muted">持${r.holds[0].d} ${r.holds[0].q}</span>`
        : (r.closes.length ? '<span class="muted">平仓</span>' : '<span class="muted">—</span>'));
    const my = `<b style="color:${markColor(r.verdict)}">${esc(r.verdict)}</b>`;
    const dv = `<span style="color:${r.div.c}">${esc(r.div.label)}</span>`;
    const fw = r.myRet == null ? '—'
      : (r.verdict === '不进'
        ? `<span class="muted">幅度 ${(r.myRet * 100).toFixed(2)}%</span>`
        : `<span class="${r.myRet >= 0 ? 'up' : 'down'}">${_cmpPct(r.myRet)}</span>`);
    const sp = r.stratPnl == null ? '—'
      : `<span class="${r.stratPnl >= 0 ? 'up' : 'down'}">${money(r.stratPnl)}</span>`;
    tr += `<tr><td>${t}</td><td>${st}</td><td>${my}</td><td>${dv}</td><td>${fw}</td><td>${sp}</td></tr>`;
  });

  // 汇总：只报计数与合计
  const cnt = _KIND_ORDER.filter(k => s.byKind[k])
    .map(k => `${_KIND_NAME[k]} <b>${s.byKind[k]}</b>`).join(' ｜ ');
  let sum = `<div class="stat-line" style="margin-top:5px;border-top:1px solid var(--border);padding-top:5px">
    标记 <b>${s.n}</b> 条 ｜ ${cnt}</div>`;
  if (s.dirN) {
    sum += `<div class="stat-line">有方向的标记 <b>${s.dirN}</b> 条，我方向后续 ${CMP_N} 根为正 <b>${s.dirHit}</b> 条`
      + `（<b>${(s.hitRate * 100).toFixed(0)}%</b>）<span class="stat-note">50% = 抛硬币</span></div>`;
  }
  if (s.avoidN) {
    sum += `<div class="stat-line">被我规避的策略交易 <b>${s.avoidN}</b> 笔，合计 <b class="${s.avoidPnl >= 0 ? 'up' : 'down'}">${money(s.avoidPnl)}</b> 元`
      + `<span class="stat-note">负 = 你躲对了；正 = 你漏掉了</span></div>`;
  }
  if (s.sameN) {
    sum += `<div class="stat-line">同向一致的策略交易 <b>${s.sameN}</b> 笔，合计 <b class="${s.samePnl >= 0 ? 'up' : 'down'}">${money(s.samePnl)}</b> 元</div>`;
  }
  sum += `<div class="warn" style="margin-top:5px">⚠️ 这张表只能诊断你自己的判断，不能当业绩：
    ①单笔「谁对」是噪声，<b>标记数 &lt; 30 条时任何比例都不构成结论</b>；
    ②后续 ${CMP_N} 根是任意选的窗口，换个 N 结论会变；
    ③「我规避的交易亏了」≠ 你会择时——06 本身负期望，随便躲掉一些也会显得躲对了。</div>`;

  return `<div class="ct">${esc(dd.name)} ｜ 分歧台账（${rows.length} 条${rows.length > _CMP_MAXROWS ? `，只显示最近 ${_CMP_MAXROWS}` : ''}）</div>
    <table><thead><tr><th>时间</th><th>策略</th><th>我</th><th>分歧</th><th>我方向+${CMP_N}根</th><th>策略该笔</th></tr></thead>
    <tbody>${tr}</tbody></table>${sum}`;
}

// ── 主入口（renderAll / renderStep 都会调）──────────────────────────────────
let _cmpListSig = '';
export function renderCompare() {
  const now = $('cmpNow'), list = $('cmpList'), tab = $('tabC');
  if (!now || !list) return;

  if (!cmpEligible()) {
    now.innerHTML = _cmpWhy();
    list.innerHTML = '';
    list.style.display = 'none';
    if (tab) tab.textContent = '对照';
    _cmpListSig = '';
    return;
  }
  list.style.display = '';

  const dd = DB.data[state.sym];
  const R = state.replay;
  let dot = false;
  if (!R) {
    now.innerHTML = `<div class="ct">${esc(dd.name)} ｜ 对照</div>
      <div class="muted" style="padding:8px;line-height:1.7">
        进入回放后，这里逐根并排显示「策略实际做了什么」和「你标了什么」。<br>
        下方台账不受回放位置影响，随时可看。</div>`;
  } else {
    const x = cmpAt(R.abs);
    if (!x) { now.innerHTML = _cmpWhy(); }
    else {
      now.innerHTML = _cmpNowHtml(x, dd);
      dot = !!_DIVERGE[x.div.k];
    }
  }
  if (tab) tab.textContent = dot ? '对照 ●' : '对照';

  // 台账只在标记集合变化时重建（避免每根 bar 都拼一遍 200 行 HTML）
  const rows = cmpRows();
  const sig = state.sym + '|' + state.period + '|' + rows.map(r => r.barX + r.verdict).join(',');
  if (sig !== _cmpListSig) {
    _cmpListSig = sig;
    list.innerHTML = _cmpListHtml(rows);
  }
}
