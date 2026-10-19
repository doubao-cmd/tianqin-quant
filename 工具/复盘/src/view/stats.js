// 统计（右侧"统计"tab）：回放中显示「回放内统计 + 全样本对照(灰,含未来)」
import { state, canSeeAnswers } from '../core/state.js';
import { pd, tmap } from '../core/time.js';
import { $, esc, num0, num2, money, pct, f2 } from '../core/fmt.js';
import { DB } from '../data/index.js';
import { segStats, truncation, poolCycles, versionSplit, takeoverBlock, holidayCheck } from '../replay/segstats.js';
import { trendSeries, trendHover, TREND_MIN_SAMPLE } from '../replay/trend.js';
import { lgSessions } from '../replay/ledger.js';

// ── P3-1：时段 / 交易日 / 持有根数桶（全样本，跨品种聚合）────────────────────
// ⚠️ 事后分组警告写在每块底部，不是集中写在页面末尾——用户是分段看的。
function _stSegHtml() {
  const s = segStats(state.period);
  if (!s.n) return '';
  const seg = s.seg.map(r =>
    `<tr><td>${esc(r.name)}</td><td>${r.n}</td>` +
    `<td class="${r.sum >= 0 ? 'up' : 'down'}">${money(r.sum)}</td>` +
    `<td>${r.wr == null ? '—' : r.wr + '%'}</td></tr>`).join('');
  const hold = s.hold.map(r =>
    `<tr><td>${esc(r.bucket)} 根</td><td>${r.n}</td>` +
    `<td class="${r.sum >= 0 ? 'up' : 'down'}">${money(r.sum)}</td>` +
    `<td>${r.wr == null ? '—' : r.wr + '%'}</td></tr>`).join('');
  return `
    <div class="stat-h">按时段（${s.n} 笔已结算，跨品种）</div>
    <table><thead><tr><th>时段</th><th>n</th><th>合计</th><th>胜率</th></tr></thead><tbody>${seg}</tbody></table>
    <div class="stat-h">按持有根数（${state.period === '1440' ? '日线' : state.period + 'min'}）</div>
    <table><thead><tr><th>持有</th><th>n</th><th>合计</th><th>胜率</th></tr></thead><tbody>${hold}</tbody></table>
    <div class="warn">⚠️ 时段与持有期都是<b>事后分组</b>：同一批交易按结果切开，不是随机分组。<br>
    「夜盘亏得多」<b>不等于</b>「别做夜盘」——夜盘本来波动就大、成本/波动比更差。<br>
    要判断"封某个时段"是否有效，只能靠前瞻实验（四期封盘模块 + 敞口混杂检查），不能靠这张表。</div>`;
}

// ── P3-1：按交易日（含累计）────────────────────────────────────────────────
function _stDayHtml() {
  const s = segStats(state.period);
  if (!s.days.length) return '';
  const rows = s.days.slice(-30);
  let cum = s.days.slice(0, Math.max(0, s.days.length - rows.length))
    .reduce((a, r) => a + r.sum, 0);
  const body = rows.map(r => {
    cum += r.sum;
    return `<tr><td>${esc(r.day.slice(5))}</td><td>${r.n}</td>` +
      `<td class="${r.sum >= 0 ? 'up' : 'down'}">${money(r.sum)}</td>` +
      `<td class="${cum >= 0 ? 'up' : 'down'}">${money(cum)}</td></tr>`;
  }).join('');
  return `
    <div class="stat-h">按交易日（近 ${rows.length} 日）</div>
    <table><thead><tr><th>交易日</th><th>n</th><th>当日</th><th>累计</th></tr></thead><tbody>${body}</tbody></table>
    <div class="stat-note">夜盘归下一交易日（sessions.py §6·9 同口径）；累计从最早一天起算</div>`;
}

// ── A19 池子周期分组（5 个交易日 = 池子资格期，不是持仓期）──────────────────
// 每行 = 一个池子纪元切出来的 5 交易日段。见 segstats.js poolCycles() 的口径注释。
function _stCycleHtml() {
  const c = poolCycles();
  if (!c.ok) {
    return `<div class="stat-h">按池子周期（${c.cycle_days} 交易日）</div>`
         + `<div class="muted">${esc(c.why)}</div>`;
  }
  if (!c.rows.length) return '';

  const tag = r => {
    const t = [];
    if (r.trend) t.push('进行中');
    if (r.short && !r.trend) t.push('尾段 ' + r.nDays + ' 日');
    if (r.partial) t.push('首日不完整');
    if (r.extra.length) t.push('越池 ' + r.extra.join('/'));
    if (r.absent.length) t.push('缺 ' + r.absent.join('/') + '（无台账）');
    return t.join(' ｜ ');
  };
  const body = c.rows.map(r => {
    const legTxt = r.legs.length
      ? r.legs.map(x => `${x.k} ${x.n}笔/${money(x.sum)}`).join(' ')
      : '<span class="muted">无成交</span>';
    const poolTxt = r.pool.join('·') || '—';
    const span = r.nDays === 1
      ? esc(r.dayList[0].slice(5))
      : esc(r.dayList[0].slice(5)) + '~' + esc(r.dayList[r.dayList.length - 1].slice(5));
    const tg = tag(r);
    return `<tr>
      <td>#${r.ep}-${r.ci}<br><span class="muted">${span}</span></td>
      <td>${esc(poolTxt)}</td>
      <td>${r.nDays}</td>
      <td>${r.n}</td>
      <td class="${r.sum >= 0 ? 'up' : 'down'}">${r.n ? money(r.sum) : '—'}</td>
      <td>${r.wr == null ? '—' : r.wr + '%'}</td>
      <td>${legTxt}</td>
      <td class="muted">${esc(tg) || '—'}</td></tr>`;
  }).join('');

  const skipNote = c.skipped
    ? `<div class="stat-note">另有 ${c.skipped} 笔成交无法归属到任何池子周期（无开仓时刻的接管笔 / 台账起点之前），未计入。</div>`
    : '';

  return `
    <div class="stat-h">按池子周期（${c.cycle_days} 个交易日／段，共 ${c.rows.length} 段，${c.n} 笔已结算）</div>
    <div class="stat-line">合计 <b class="${c.total >= 0 ? 'up' : 'down'}">${money(c.total)}</b> 元
      ｜ 未平仓 ${c.open} 笔（不计入）</div>
    <table><thead><tr><th>周期</th><th>池子</th><th>交易日</th><th>n</th>
      <th>合计</th><th>胜率</th><th>成交腿</th><th>标记</th></tr></thead><tbody>${body}</tbody></table>
    ${skipNote}
    <div class="stat-note">池子来源：${esc(c.source)}（${c.restarts} 条 START 记录）。<br>
    周期 = <b>池子资格期</b>（5 个交易日的交易许可），不是持仓期——MA13 在这些天里自己进出十几次。<br>
    归属按<b>开仓时刻</b>：换池后被接管的持仓仍记在「开仓时那个池子」里，不在新池子下。</div>
    <div class="warn">⚠️ 每段样本只有 ~20 笔，<b>不足以判断「这个池子好不好」</b>。<br>
    池子变更是「选品种」的输入，不是结果；拿 5 天盈亏反推选品种对错 = 用结果改规则（四铁律 2）。<br>
    「越池」= 换池时该品种还有持仓，按 §3.10 强制保留到自然出场，<b>不是 bug</b>。<br>
    「缺 X（无台账）」= 该腿在池子里但没有 K 线缓存 ⇒ 它的成交这张表<b>看不到</b>，不是没成交。</div>`;
}

// ── A10/A14 接管单列（RECOVER：开仓价未知，盈亏不可知，不进已结算分母）──────
function _stTakeoverHtml() {
  const b = takeoverBlock();
  if (!b.n_takeover && !b.n_open) return '';
  const tk = b.takeovers.map(r =>
    `<tr><td>${esc(r.sym)}</td><td>${esc((r.t || '').slice(5))}</td><td>${esc((r.x || '').slice(5))}</td>`
    + `<td>${esc(r.d)}</td><td>${r.q}</td><td>${r.p == null ? '—' : f2(r.p)}</td>`
    + `<td>${esc(r.r || '—')}</td></tr>`).join('');
  const op = b.opens.map(r =>
    `<tr><td>${esc(r.sym)}</td><td>${esc((r.t || '').slice(5))}</td><td>${esc(r.d)}</td><td>${r.q}</td>`
    + `<td>${r.e == null ? '<span class="down">未知</span>' : f2(r.e)}</td>`
    + `<td>${r.s == null ? '—' : f2(r.s)}</td></tr>`).join('');
  return `
    <div class="stat-h">接管（RECOVER）单列 · ${b.n_takeover} 笔已平 ｜ ${b.n_open} 笔未平</div>
    <table><thead><tr><th>品种</th><th>开仓</th><th>平仓</th><th>方向</th><th>手数</th><th>平仓价</th><th>出场</th></tr></thead>
      <tbody>${tk || '<tr><td colspan="7" class="muted">无</td></tr>'}</tbody></table>
    <div class="stat-h">未平仓（当前持仓）· ${b.n_open} 笔</div>
    <table><thead><tr><th>品种</th><th>开仓</th><th>方向</th><th>手数</th><th>开仓价</th><th>止损</th></tr></thead>
      <tbody>${op || '<tr><td colspan="6" class="muted">无</td></tr>'}</tbody></table>
    <div class="warn">⚠️ 接管笔<b>开仓价未知（e=null）</b>，盈亏不可算 ⇒ 已排除出「已结算」分母，单列于此（A10/A14）。<br>
    与「未平仓」是两类：接管=进程重启后云端有仓、本地丢开仓价；未平仓=还没出场。<br>
    ★ 别把它们和 90 笔已结算混算期望——分母不同，结论会偏。</div>`;
}

// ── A12/A13 长假清仓检查（判定只用交易日历，不涉及行情）────────────────────
function _stHolidayHtml() {
  const h = holidayCheck();
  if (!h.ok) {
    return `<div class="stat-h">长假清仓检查（休市 ≥5 天）</div>`
         + `<div class="muted">${esc(h.note)}</div>`;
  }
  const recent = h.long.slice(-8).reverse();
  const rows = recent.map(g =>
    `<tr><td>${esc(g.prev)}</td><td>${esc(g.next)}</td><td>${g.gap}</td><td>${esc(g.clearBy)}</td></tr>`).join('');
  const vio = h.violations.length
    ? `<div class="warn">⚠️ <b>${h.violations.length}</b> 笔持仓跨过 ≥5 天休市（应清仓未清）：<br>`
      + h.violations.map(v => `${esc(v.sym)} ${esc(v.d)}${v.q}手 ${esc((v.t || '').slice(5))}→${esc((v.x || '').slice(5))}（休市 ${v.gap} 天）`).join('；')
      + `</div>`
    : `<div class="stat-line">跨 ≥5 天休市持仓：<b>0</b> 笔 ✓（窗口内无长假跨越）</div>`;
  return `
    <div class="stat-h">长假清仓检查（休市 ≥5 天，共 ${h.long.length} 次；日历 ${h.cal_n} 日）</div>
    <table><thead><tr><th>最后交易日</th><th>复市日</th><th>休市天数</th><th>清仓日</th></tr></thead><tbody>${rows}</tbody></table>
    ${vio}
    <div class="stat-line muted">小长假（休市 3~4 天）：${h.small.length} 次，跨仓 ${h.small_cross} 笔 —— <b>不清仓</b>（A13）</div>
    <div class="stat-note">判定只用交易日历，不涉及任何行情。<br>
    规则：休市 ≥5 天 → 最后交易日无条件清仓；3~4 天 → 不动（跳空 0.23% ≈ 普通隔夜，不值得付清仓成本）。</div>
    <div class="warn">⚠️ 这是<b>规则检查</b>，不是收益结论。清仓动作须写进预注册卡，不能临场判断。</div>`;
}

// ── A11 版本分段（INTRABAR_STOP 切换点，不跨版本求和）────────────────────
// 每行 = 一个成交机制版本。跨版本合计**故意不显示**（两种机制混算污染期望）。
// 未标记时给出明确提示，不假装"全样本"能代表一个版本。
function _stVersionHtml() {
  const v = versionSplit();
  if (!v.ok) {
    return `<div class="stat-h">按版本（INTRABAR_STOP 切换）</div>`
      + `<div class="warn">⚠️ 台账无版本切换标记（方案 §1.4 后果 2）。<br>`
      + `${esc(v.note || '当前全部按同一版本统计')}。<br>`
      + `旧「收盘才查」与新「盘中触价即平」是两种成交机制，混算会污染期望；`
      + `标记后本区块按切换点分段并禁用跨版本合计。</div>`;
  }
  if (!v.rows.length) return '';
  const body = v.rows.map(r =>
    `<tr><td>${esc(r.name)}</td><td>${r.n}</td>`
    + `<td class="${r.sum >= 0 ? 'up' : 'down'}">${money(r.sum)}</td>`
    + `<td>${r.wr == null ? '—' : r.wr + '%'}</td></tr>`).join('');
  const swTxt = v.switches.map(s => (s.on ? '新' : '旧') + '@' + esc(s.t.slice(5))
    + (s.src ? '·' + (String(s.src).indexOf('自') >= 0 ? '自记录' : '手工') : '')).join(' ｜ ');
  // ★ 文案必须由数据算，不能写死：一旦进程重启加载新码并成交，写死的「新版 = 0」立刻说谎
  const nNew = (v.rows.find(r => r.ver === 'new') || {}).n || 0;
  const tail = nNew === 0
    ? '新版<b>尚无已结算成交</b>（切换点之后还没平过仓）⇒ 新版那一行空着是正常的，不是没记上。'
    : `新版已有 <b>${nNew}</b> 笔已结算；样本小，只作观察，别据此下结论。`;
  const lastOn = v.switches.length ? v.switches[v.switches.length - 1].on : null;
  const cvTxt = (v.code_value == null) ? '未知（未读到源码）' : (v.code_value ? 'True' : 'False');
  const same = (v.code_value == null || lastOn == null) ? null : (lastOn === v.code_value);
  const codeLine = `代码当前 <b>INTRABAR_STOP=${cvTxt}</b> ｜ 台账最后记录 <b>${lastOn == null ? '—' : (lastOn ? 'True' : 'False')}</b>`
    + (same === null ? '' : (same
      ? ' ｜ <span class="muted">一致</span>'
      : ' ｜ <span class="down">不一致：代码改完未重启，进程仍在跑旧口径</span>'));
  return `
    <div class="stat-h">按版本（INTRABAR_STOP 切换，共 ${v.rows.length} 段，${v.n} 笔已结算）</div>
    <div class="stat-line">切换点：${swTxt}</div>
    <div class="stat-line">${codeLine}</div>
    <table><thead><tr><th>版本</th><th>n</th><th>合计</th><th>胜率</th></tr></thead><tbody>${body}</tbody></table>
    <div class="warn">⚠️ <b>跨版本合计已禁用</b>：旧版「收盘才查」与新版「盘中触价即平」是两种成交机制，<br>
    混在一起求和会让期望失真（四铁律：口径一致）。本区块只给各版本<b>独立</b>分布，不求和。<br>
    ${tail}<br>
    <span class="muted">切换点由策略启动时自记录（口径变了才写一行）；手工行只代表代码落地，不代表已生效。</span></div>`;
}

// ── P3-3：截断盘分位 ───────────────────────────────────────────────────────
function _stTruncHtml() {
  const t = truncation(state.period);
  if (!t.ok) {
    return `<div class="stat-h">截断盘分位</div><div class="muted">${esc(t.why)}</div>`;
  }
  const q = t.pct == null ? '—' : Math.round(t.pct * 100) + '%';
  const cont = t.cont
    ? `<div class="stat-line">历史延续率（n=${t.cont.nHist} 日）：
         低振幅组 ${t.cont.low ? Math.round(t.cont.low.r * 100) + '%（' + t.cont.low.n + '）' : '—'}
         ｜ 高振幅组 ${t.cont.high ? Math.round(t.cont.high.r * 100) + '%（' + t.cont.high.n + '）' : '—'}</div>`
    : `<div class="stat-line muted">历史延续率：<b>样本不足</b>（${t.nHist} 日 &lt; ${t.need} 日）⇒ 不作结论，等 hist 源接入</div>`;
  return `
    <div class="stat-h">截断盘分位（${esc(t.day)} 早盘 09:00–10:15）</div>
    <div class="stat-line">今日早盘振幅 <b>${f2(t.amp)}</b> ｜ 近 ${t.nHist} 日中位 ${f2(t.med)} ｜ 分位 <b>${q}</b></div>
    ${cont}
    <div class="stat-note">分位 = 今日振幅在近 ${t.nHist} 个交易日同期振幅中的位置；100% = 今天最猛</div>
    <div class="warn">⚠️ 这是<b>当日状态的描述</b>，不是交易信号。<br>
    「高振幅下午延续」即便成立，也要先扣成本再谈——06 毛利仅成本的 4.5%，<br>
    任何"看状态决定做不做"的规则都必须换干净样本单独验证（四铁律 3）。</div>`;
}

// ★ P1-6：回放内统计——只统计"当前 bar 及之前已平仓"的笔（随进度单调递增，无未来泄露）
function replayStatsHtml() {
  const R = state.replay;
  const d = pd();
  if (!d.x.length) return '<div class="muted">无数据</div>';
  const cur = d.x[Math.min(d.x.length - 1, R.abs)];
  const done = DB.data[state.sym].trades
    .filter(t => t.x && t.pnl != null && tmap(t.x) <= tmap(cur));
  const n = done.length;
  const sum = done.reduce((a, t) => a + t.pnl, 0);
  const wins = done.filter(t => t.pnl > 0), losses = done.filter(t => t.pnl < 0);
  const wr = n ? Math.round(wins.length / n * 100) + '%（' + wins.length + '/' + n + '）' : '—';
  const aw = wins.length ? money(wins.reduce((a, t) => a + t.pnl, 0) / wins.length) : '—';
  const al = losses.length ? money(losses.reduce((a, t) => a + t.pnl, 0) / losses.length) : '—';
  const s = DB.data[state.sym].stats_all || {};
  const fs = s.n_settled == null ? '' :
    `<div class="stat-h">全样本对照<span class="future-tag">含未来</span></div>
     <div class="stat-line muted">${s.n_settled} 笔 ｜ ${money(s.pnl_sum)} 元 ｜ 胜率 ${pct(s.win, s.n_settled)}</div>
     <div class="stat-note">灰色对照含尚未回放到的未来数据，仅作最终校验，勿用于途中决策</div>`;
  return `
    <div class="stat-h">回放内 · 截至 ${String(cur).slice(0, 16)}</div>
    <div class="stat-line">已平 <b>${n}</b> 笔 ｜ 合计 <b class="${sum >= 0 ? 'up' : 'down'}">${money(sum)}</b> 元 ｜ 胜率 <b>${wr}</b></div>
    <div class="stat-line">均盈 ${aw} ｜ 均亏 ${al}</div>
    <div class="stat-note">仅计当前 bar 之前已平仓且有盈亏的笔；未平 / 接管不计，随播放进度单调递增</div>
    ${fs}`;
}

// ── P3-2：逐笔 MFE/MAE 覆盖（台账 + K 线补算）────────────────────────────
// 台账口径的 coverage 仍按台账自己算（stats_all.excursion），这里是"补算后"的逐笔覆盖。
function _stCovHtml() {
  let n = 0, lg = 0, kl = 0, miss = 0;
  Object.keys(DB.data).forEach(k => (DB.data[k].trades || []).forEach(t => {
    if (t.pnl == null) return;                  // 未平 / 接管不进分母
    n++;
    if (t.mfe == null && t.mae == null) miss++;
    else if (t.mfe_src === 'kline' || t.mae_src === 'kline') kl++;
    else lg++;
  }));
  if (!n) return '';
  return `<div class="stat-line">逐笔 MFE/MAE 覆盖 <b>${n - miss}/${n}</b>` +
    `（${Math.round((n - miss) / n * 100)}%）｜ 台账 <b>${lg}</b> ｜ K 线补算 <b>${kl}</b></div>
    <div class="stat-note">K 线口径<b>系统性低估</b>（bar 内极值取不到盘中瞬时极值）⇒ 两个口径不混用统计</div>`;
}

// ═══════════════════════════════════════════════════════════════════════
// ★★ T2.1 训练曲线（跨会话趋势）—— 五期价值最高的一块
//   数据全在 localStorage 的会话台账里（ledger.js / rt:sessions:v1），
//   这里只做"把它画出来"，不新增任何依赖（Plotly 已内嵌）。
//
//   ★ 术语红线：标题与说明只许写「方向一致率」，**不许写"胜率"**。
//     hitRate = 标记方向 与 后 10 根实际走势 的一致率；胜率 = 已平仓笔里赚钱的占比。
//     分母不同，混称会让人误以为"命中率高=在赚钱"。
//   ★ 会话 < 2 不画图（单点连线是假趋势）；样本 < 5 的点用空心，不隐藏。
// ═══════════════════════════════════════════════════════════════════════
const _TR_N = 10;
let _trSig = '';

function _stTrendHtml() {
  const s = trendSeries(lgSessions());
  if (!s.ok) {
    return `<div class="stat-h">训练曲线 · 跨会话</div>
      <div class="tr-empty">${esc(s.why)}<br>
      ${s.missing ? `${s.missing} 条旧会话没有指标（早期版本没存评分）` : '每次退出回放会自动记一条会话'}。</div>`;
  }
  const missNote = s.missing ? `<br>另有 <b>${s.missing}</b> 条会话缺指标未计入（早期版本没存评分）。` : '';
  return `<div class="stat-h">训练曲线 · 跨会话（${s.n} 次复盘）</div>
    <div id="trChart"></div>
    <div class="stat-note">主图 = <b>方向一致率</b>（标记「会进多/会进空」后 ${_TR_N} 根实际走势与判断一致的比例，
      基准线 50% = 瞎猜）；副图 1 = 规避质量 <b>avoidQ</b>（「不进」后的波动 ÷ 全样本，1.0 = 与随机无异）；
      副图 2 = 手操占比 / 过度交易指数（越低 = 越少靠手操、越贴策略）。<br>
      空心点 = 该会话样本 &lt; ${TREND_MIN_SAMPLE}，仅供参考。${missNote}</div>
    <div class="warn">⚠️ 这不是<b>胜率</b>。方向一致率只说明"你看图的方向感"，不含手续费、不含手数、
      也不等于赚钱——策略整体期望仍由已结算笔的合计决定（见上方全样本）。</div>`;
}

function _trDraw(hasBox) {
  // 用「刚写进去的 html 里有没有 trChart」判断，而不是 getElementById ——
  //   后者在容器没进 DOM 时仍可能返回节点（测试桩/旧 React 残留），Plotly 会白跑一次。
  if (!hasBox) { _trSig = ''; return; }
  const s = trendSeries(lgSessions());
  if (!s.ok) return;
  const sig = JSON.stringify([s.n, s.hit, s.avoid, s.over, s.manual, s.hitHollow]);
  if (sig === _trSig) return;                 // renderStats 每根都会被调，同数据不重画
  _trSig = sig;

  const symOf = (arr, hollow) => arr.map((_, i) => (hollow[i] ? 'circle-open' : 'circle'));
  const base = { mode: 'lines+markers', line: { width: 1.6 }, marker: { size: 7 } };
  const traces = [
    Object.assign({}, base, {
      x: s.xs, y: s.hit, yaxis: 'y', name: '方向一致率',
      line: { color: '#60a5fa', width: 1.8 },
      marker: { size: 7, color: '#60a5fa', symbol: symOf(s.hit, s.hitHollow),
                line: { width: 1.4, color: '#93c5fd' } },
      customdata: s.labels.map(l => trendHover(l)),
      hovertemplate: '%{customdata}<extra>方向一致率</extra>',
    }),
    Object.assign({}, base, {
      x: s.xs, y: s.avoid, yaxis: 'y2', name: 'avoidQ',
      line: { color: '#f59e0b', width: 1.6 },
      marker: { size: 6, color: '#f59e0b', symbol: symOf(s.avoid, s.avoidHollow) },
      hovertemplate: '会话 #%{x}<br>规避质量 %{y}<extra></extra>',
    }),
    Object.assign({}, base, {
      x: s.xs, y: s.manual, yaxis: 'y3', name: '手操占比 %',
      line: { color: '#a78bfa', width: 1.4, dash: 'dot' },
      marker: { size: 5, color: '#a78bfa' },
      hovertemplate: '会话 #%{x}<br>手操占比 %{y}%<extra></extra>',
    }),
    Object.assign({}, base, {
      x: s.xs, y: s.over, yaxis: 'y3', name: '过度交易',
      line: { color: '#22c55e', width: 1.4 },
      marker: { size: 5, color: '#22c55e' },
      hovertemplate: '会话 #%{x}<br>过度交易 %{y}<extra></extra>',
    }),
  ];
  const layout = {
    margin: { l: 30, r: 30, t: 6, b: 16 },
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    showlegend: false, hovermode: 'closest',
    font: { family: 'Microsoft YaHei', color: '#9aa3af', size: 10 },
    hoverlabel: { bgcolor: '#151d2e', bordercolor: '#3a4350', font: { color: '#e6e6e6', size: 11 } },
    xaxis: { type: 'linear', dtick: 1, gridcolor: '#232733', zeroline: false,
             tickfont: { size: 10 }, title: { text: '会话序号', font: { size: 10 } } },
    yaxis: { domain: [0.64, 1], range: [0, 100], gridcolor: '#232733', zeroline: false,
             ticksuffix: '%', tickfont: { size: 10 } },
    yaxis2: { domain: [0.33, 0.56], gridcolor: '#232733', zeroline: false, tickfont: { size: 10 } },
    yaxis3: { domain: [0, 0.25], gridcolor: '#232733', zeroline: false, tickfont: { size: 10 } },
    shapes: [
      { type: 'line', xref: 'paper', x0: 0, x1: 1, yref: 'y', y0: 50, y1: 50,
        line: { color: '#475569', width: 1, dash: 'dot' } },
      { type: 'line', xref: 'paper', x0: 0, x1: 1, yref: 'y2', y0: 1, y1: 1,
        line: { color: '#475569', width: 1, dash: 'dot' } },
    ],
  };
  try {
    Plotly.react('trChart', traces, layout, {
      displayModeBar: false, responsive: false, staticPlot: false,
    });
  } catch (e) { /* Plotly 不可用（测试桩）⇒ 静默 */ }
}

// 统一出口：写 innerHTML 后立刻画趋势图（否则容器还没进 DOM，Plotly 找不到节点）
function _stSet(html) {
  $('stats').innerHTML = html;
  _trDraw(html.indexOf('id="trChart"') >= 0);
}

export function renderStats() {
  // ★ P1-5：训练模式隐藏全样本统计（防止开卷考试）
  if (!canSeeAnswers()) {
    // ★ T2.1：训练模式仍显示「训练曲线」——那是**你自己的历史**，不是本次的答案。
    //   全样本统计（含未来）照旧隐藏。
    _stSet('<div class="warn">训练模式：隐藏全样本统计（防止开卷考试）。切到「复看模式」后查看。</div>'
      + _stTrendHtml());
    return;
  }
  // ★ P1-6：回放中改显「回放内统计 + 全样本对照(灰,含未来)」，不再整块隐藏
  if (state.replay) {
    _stSet(replayStatsHtml());
    return;
  }
  const s = DB.data[state.sym].stats_all;
  if (!s) { _stSet('<div class="muted">统计未生成</div>'); return; }
  const byR = (s.by_reason || []).map(r =>
    `<tr><td>${esc(r.r)}</td><td>${r.n}</td>` +
    `<td class="${r.sum >= 0 ? 'up' : 'down'}">${money(r.sum)}</td>` +
    `<td>${num0(r.mean)}</td><td>${pct(r.win, r.n)}</td></tr>`).join('');
  const byS = (s.by_symbol || []).map(r =>
    `<tr class="${r.key === state.sym ? 'on' : ''}"><td>${esc(r.name)} ${r.key}</td>` +
    `<td>${r.n}</td><td>${r.pnl_n}</td>` +
    `<td class="${r.sum >= 0 ? 'up' : 'down'}">${money(r.sum)}</td></tr>`).join('');
  const sl = s.slippage || {};
  const ex = s.excursion || {};
  const cov = ex.coverage == null ? '—' : Math.round(ex.coverage * 100) + '%';
  const worse = sl.worse_share == null ? '—' : Math.round(sl.worse_share * 100) + '%';
  _stSet(`
    ${_stTrendHtml()}
    <div class="stat-h">全样本（6 品种）</div>
    <div class="stat-line">总交易 <b>${s.n_trades}</b> ｜ 已结算 <b>${s.n_settled}</b> ｜ 接管 <b>${s.n_takeover}</b>（未计） ｜ 未平 <b>${s.n_open}</b></div>
    <div class="stat-line">合计 <b class="${s.pnl_sum >= 0 ? 'up' : 'down'}">${money(s.pnl_sum)}</b> 元 ｜ 均值 ${num0(s.pnl_mean)} ｜ 胜率 <b>${pct(s.win, s.n_settled)}</b></div>
    <div class="stat-line">盈 ${s.win} / 亏 ${s.loss} / 平 ${s.flat} ｜ 均盈 ${money(s.avg_win)} ｜ 均亏 ${money(s.avg_loss)} ｜ 盈亏比 ${num2(s.payoff)}</div>
    <div class="stat-line">持仓中位 ${num0(s.hold_median)} 分 ｜ 均值 ${num0(s.hold_mean)} 分</div>
    <div class="stat-line muted">A2 覆盖 ${ex.n}/${ex.n_closes}（${cov}）｜ MFE 中位 ${money(ex.mfe_median)} ｜ MAE 中位 ${money(ex.mae_median)}</div>
    ${_stCovHtml()}
    <div class="stat-h">出场原因（${s.n_settled} 笔已结算，按合计升序）</div>
    <table><thead><tr><th>原因</th><th>n</th><th>合计</th><th>均值</th><th>胜率</th></tr></thead><tbody>${byR}</tbody></table>
    <div class="stat-h">超损（${sl.n_ok} 笔价格止损）</div>
    <div class="stat-line">差于止损价 <b>${sl.worse_n}/${sl.n_ok} = ${worse}</b></div>
    <div class="stat-line">中位 <b>${num2(sl.median)}</b> ｜ 均值 ${num2(sl.mean)} ｜ P10 ${num2(sl.p10)} ｜ P25 ${num2(sl.p25)} ｜ P75 ${num2(sl.p75)}</div>
    <div class="stat-line">最差 ${num2(sl.min)} ｜ 金额合计 <b class="down">${money(sl.sum_amount)}</b> 元</div>
    <div class="stat-note">止损价 vs 实际成交价；负 = 差于止损价（超损）</div>
    <div class="stat-h">分品种</div>
    <table><thead><tr><th>品种</th><th>笔数</th><th>有盈亏</th><th>合计</th></tr></thead><tbody>${byS}</tbody></table>
    <div class="warn">时间止损赚、价格止损亏 —— <b>这不等于"放宽止损就能赚"</b>。<br>
    被止损的那 55 笔本来就是走势不利的那一批（事后分组，不是随机分组）。<br>
    06 策略毛利仅为成本的 4.5%，改出场在数学上不可能改变期望符号<br>
    （"提前出场家族"已 5 次失败，同一机制）。</div>
    ${_stSegHtml()}
    ${_stDayHtml()}
    ${_stTakeoverHtml()}
    ${_stCycleHtml()}
    ${_stVersionHtml()}
    ${_stHolidayHtml()}
    ${_stTruncHtml()}
    ${_stRiskHtml()}${_stSectorHtml()}`);
}

// ★ S5：板块构成 + 集中度（结构性事实，不需要统计检验）
//   为什么这个面板必须存在（2026-10-19 教训）：
//     用户看到"化工亏 78%"就想剔除化工。但那是**粗标签**——它把建材(FG/SA)、
//     能源(FU/BU)、轻工(SP)全算进"化工"。实测拆细后：
//       建材 每笔 −455（最差）、真化工 −229（比整体还好）、能源 −244。
//     而线上 6 腿按细类其实是 5 个板块（有效腿数 4.5），一点都不集中。
//     ⇒ **用粗标签做决策会误伤**。所以这里显示细类，并把 HHI 摆出来。
//
//   ★ 本面板刻意**不给板块收益排名**：按板块比收益是数据窥探
//     （实测 tick/价 相关性 −0.738，去掉贵金属后立刻消失）。
function _stSectorHtml() {
  const s = DB.meta && DB.meta.sector;
  if (!s || !(s.legs || []).length) {
    return '<div class="stat-h">板块构成</div>'
         + '<div class="muted">未生成（bundle 缺 _meta.sector，请重跑 build_data.py）</div>';
  }
  const rows = (s.legs || []).map(x => {
    const miss = x.in_bundle ? '' : ' <span class="muted">（bundle 无此品种）</span>';
    return `<tr class="${x.key === state.sym ? 'on' : ''}">`
      + `<td>${esc(x.name)} ${esc(x.key)}</td>`
      + `<td>${esc(x.sector)}</td>`
      + `<td class="muted">${miss || '—'}</td></tr>`;
  }).join('');

  const bySec = s.by_sector || {};
  const secs = Object.keys(bySec).sort((a, b) => bySec[b] - bySec[a]);
  const bar = secs.map(k => {
    const n = bySec[k], sh = (s.shares || {})[k] || 0;
    const w = Math.max(4, Math.round(sh * 100));
    return `<div class="stat-line" style="margin:1px 0">`
      + `<span style="display:inline-block;width:${w}%;min-width:36px;`
      + `background:${n >= 3 ? '#b91c1c' : '#334155'};height:11px;border-radius:2px;`
      + `vertical-align:middle"></span> `
      + `${esc(k)} <b>${n}</b> 腿（${Math.round(sh * 100)}%）</div>`;
  }).join('');

  const eff = s.effective_legs, n = (s.pool || []).length;
  const warn = s['warn_集中']
    ? `<div class="warn">⚠️ <b>${esc(s.max_sector)}</b> 一个板块占 <b>${s.max_per_sector}</b> 腿
       —— 超过 3 腿时组合的分散度会明显下降（这几周的账户只有一个方向风险）。</div>`
    : '';

  // 有效腿数 < 名义腿数 的一半 ⇒ 假分散
  const fake = (eff != null && n > 0 && eff < n * 0.6)
    ? `<div class="warn">⚠️ 有效腿数 <b>${eff}</b> 明显低于名义 <b>${n}</b> 腿
       ⇒ 这是<b>假分散</b>（HHI 高，风险其实集中）。</div>` : '';

  return `
    <div class="stat-h">板块构成（当前池 ${n} 腿）</div>
    <div class="stat-line">覆盖 <b>${s.n_sectors}</b> 个板块 ｜
      最多一个板块 <b>${s.max_per_sector}</b> 腿（${esc(s.max_sector || '—')}）｜
      HHI <b>${num2(s.hhi)}</b> ｜ 有效腿数 <b>${num2(eff)}</b> / ${n}</div>
    ${bar}
    <table><thead><tr><th>品种</th><th>细板块</th><th>数据</th></tr></thead>
      <tbody>${rows}</tbody></table>
    ${warn}${fake}
    <div class="stat-note">${esc(s.note || '')}</div>`;
}

// ★ S5/A20：风险贡献面板（只展示，不参与下单）
//   数据来自 bundle 的 `_meta.risk`（构建期按 RiskParSizer 公式复刻算出来的参考手数）。
//   ★ 必须显示"完整性"：池子 6 腿但 bundle 只含 2 腿时，面板是残缺的，
//     不标出来会被误读成"整个组合只有 2 腿、风险可控"。
function _stRiskHtml() {
  const r = DB.meta && DB.meta.risk;
  if (!r || !(r.legs || []).length) {
    return '<div class="stat-h">风险贡献（参考值）</div>'
         + '<div class="muted">未生成（bundle 缺 _meta.risk，请重跑 build_data.py）</div>';
  }
  const t = r.totals || {};
  const p = r.params || {};
  const rows = (r.legs || []).map(x => {
    if (!x.hand) {
      return `<tr><td>${esc(x.name || '')} ${esc(x.key || '')}</td>`
           + `<td colspan="5" class="muted">${esc(x.error || '无数据')}</td></tr>`;
    }
    const rs = x.risk_share == null ? null : x.risk_share;
    const ns = x.notional_share == null ? null : x.notional_share;
    // 风险占比 > 35% 高亮（只是让人看见，不是规则）
    const cls = (rs != null && rs > 0.35) ? 'down' : '';
    return `<tr class="${x.key === state.sym ? 'on' : ''}">`
      + `<td>${esc(x.name)} ${esc(x.key)}</td>`
      + `<td>${x.hand}</td>`
      + `<td>${money(x.notional)}</td>`
      + `<td>${ns == null ? '—' : Math.round(ns * 100) + '%'}</td>`
      + `<td class="${cls}"><b>${rs == null ? '—' : Math.round(rs * 100) + '%'}</b></td>`
      + `<td class="muted">${esc(x.capped_by || '')}</td></tr>`;
  }).join('');

  const miss = (r.missing_from_bundle || []).length
    ? `<div class="warn">⚠️ 池子 ${t.pool_n} 腿，但 bundle 只含 ${t.n_legs} 腿
       （缺 ${esc((r.missing_from_bundle || []).join('、'))}，因为它们没有台账/K线缓存）。
       <br>⇒ <b>本面板不完整</b>，占比只在这 ${t.n_legs} 腿内部归一化，不能当整体风险看。</div>`
    : `<div class="stat-note">池子 ${t.pool_n} 腿在 bundle 内齐全。</div>`;

  const conc = (t.concentrated || []).length
    ? `<div class="warn">⚠️ 风险集中：<b>${esc((t.concentrated || []).join('、'))}</b>
       单腿风险占比 &gt; 35%。这是 <code>HAND_MAX=8</code> 顶格导致的
       （手数被夹上界后，ATR 小的品种风险被放大）。</div>`
    : '';

  return `
    <div class="stat-h">风险贡献（参考值，不参与下单）</div>
    <div class="stat-line">合计名义 <b>${money(t.notional_sum)}</b> 元
      ｜ 相对手数基数（${money(p.equity_base)}）= <b>${num2(t.notional_x_equity)}×</b></div>
    <div class="stat-line muted">参数：risk_pct ${(p.risk_pct * 100).toFixed(1)}%
      ｜ 手数夹 [${p.hand_min}, ${p.hand_max}] ｜ 单腿名义 ≤ ${num2(p.notional_cap)}× 基数</div>
    <table><thead><tr><th>品种</th><th>参考手数</th><th>名义</th>
      <th>名义占比</th><th>风险占比</th><th>受限原因</th></tr></thead>
      <tbody>${rows}</tbody></table>
    ${miss}
    ${conc}
    <div class="stat-note">${esc(r.note || '')}</div>`;
}
