// 会话评分报告（P2-4）—— 一页纸，回答"这次练到了什么"
//
// 设计约束：
// 1. **不新增 index.html 的 DOM**：面板动态创建（P2-4 只加 JS，避免动 HTML 影响 P2-1/P2-3 的验收）
// 2. 数值全部来自 score.js，本文件只负责排版与文案 ⇒ 口径与判据不在这里
// 3. 每个数字必须带「怎么读」的一句话，否则用户会把它当 KPI 而不是诊断
import { $, esc, money, num0, num2, f1 } from '../core/fmt.js';

const _rpCss = `
#rpMask{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9990;display:none}
#rpBox{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);z-index:9991;
  width:min(880px,94vw);max-height:88vh;overflow:auto;display:none;
  background:#12151c;border:1px solid #2a2f3a;border-radius:10px;
  box-shadow:0 16px 48px rgba(0,0,0,.6);color:#e6e6e6;font-size:12px}
#rpBox h3{margin:0;padding:12px 16px;border-bottom:1px solid #2a2f3a;font-size:13px;font-weight:600;
  display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;background:#12151c}
#rpBox .bd{padding:12px 16px 16px}
#rpBox .grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
#rpBox .card{background:#1a1d26;border:1px solid #2a2f3a;border-radius:6px;padding:8px 10px}
#rpBox .k{color:#9aa3af;font-size:11px}
#rpBox .v{font-size:18px;font-weight:600;margin:2px 0 2px;font-variant-numeric:tabular-nums}
#rpBox .h{color:#7b8494;font-size:10.5px;line-height:1.45}
#rpBox .sec{margin:14px 0 6px;color:#9aa3af;font-size:11px;border-bottom:1px dashed #2a2f3a;padding-bottom:4px}
#rpBox table{width:100%;border-collapse:collapse;font-size:11px}
#rpBox th,#rpBox td{padding:3px 6px;border-bottom:1px solid #23272f;text-align:right;white-space:nowrap}
#rpBox th{color:#9aa3af;font-weight:500}
#rpBox td:first-child,#rpBox th:first-child{text-align:left}
#rpBox .up{color:#ef4444}#rpBox .dn{color:#22c55e}#rpBox .mu{color:#9aa3af}
#rpBox .warn{background:#2a1f1a;border:1px solid #7a4a2a;border-radius:6px;padding:8px 10px;
  color:#f0b080;font-size:11px;line-height:1.5;margin-top:10px}
#rpClose{background:#23272f;border:1px solid #333a46;color:#c8ced8;border-radius:5px;
  padding:3px 10px;cursor:pointer;font-size:11px}
#rpClose:hover{background:#2d323c}
`;

let _rpBuilt = false;
function _rpEnsure() {
  if (_rpBuilt) return;
  const st = document.createElement('style');
  st.textContent = _rpCss;
  document.head.appendChild(st);
  const mask = document.createElement('div'); mask.id = 'rpMask';
  const box = document.createElement('div'); box.id = 'rpBox';
  box.innerHTML = '<h3><span id="rpTitle">会话评分</span><button id="rpClose">关闭</button></h3>'
    + '<div class="bd" id="rpBody"></div>';
  document.body.appendChild(mask); document.body.appendChild(box);
  mask.addEventListener('click', closeReport);
  box.querySelector('#rpClose').addEventListener('click', closeReport);
  _rpBuilt = true;
}

export function closeReport() {
  if (!_rpBuilt) return;
  $('rpMask').style.display = 'none';
  $('rpBox').style.display = 'none';
}

const _rpCls = v => v == null ? 'mu' : (v > 0 ? 'up' : (v < 0 ? 'dn' : 'mu'));

function _rpCard(k, v, cls, hint) {
  return `<div class="card"><div class="k">${esc(k)}</div>`
    + `<div class="v ${cls || ''}">${v}</div><div class="h">${hint}</div></div>`;
}

export function showReport(s) {
  if (!s) return;
  _rpEnsure();
  $('rpTitle').textContent = `会话评分 · ${s.sym} · ${s.period === '1440' ? '日线' : s.period + 'min'}`
    + ` · 区间 bar ${s.startAbs} → ${s.endAbs}`;

  // ── 基础对照 ────────────────────────────────────────────────────────
  const overTxt = s.overTrade == null ? '—'
    : (s.overTrade === Infinity ? '∞（策略 0 笔）' : num2(s.overTrade));
  const overCls = (s.overTrade != null && s.overTrade > 2) ? 'dn' : '';
  const holdTxt = s.avgHoldBars == null ? '—' : f1(s.avgHoldBars) + ' 根';
  const sHoldTxt = s.stratHoldBars == null ? '—' : f1(s.stratHoldBars) + ' 根';

  let html = '<div class="grid">'
    + _rpCard('我的模拟盈亏', money(s.realized), _rpCls(s.realized),
        `共 ${s.nSim} 笔（已扣双边成本）`)
    + _rpCard('策略同区间盈亏', money(s.stratPnl), _rpCls(s.stratPnl),
        `同区间真实成交 ${s.stratN} 笔，合计。方案 A 口径：abs∈[起播, 结束]`)
    + _rpCard('我的平均持仓', holdTxt, '', `策略中位 ${sHoldTxt} —— 明显更短 = 拿不住`)
    + _rpCard('过度交易指数', overTxt, overCls,
        '我的笔数 ÷ 策略笔数。<b>&gt;2 说明你在乱开</b>')
    + _rpCard('手动平仓占比', s.manualShare == null ? '—' : Math.round(s.manualShare * 100) + '%',
        '', '其余为自动止损/止盈触发')
    + _rpCard('手动平仓的止损进度',
        s.avgStopProgress == null ? '—' : Math.round(s.avgStopProgress * 100) + '%', '',
        '平仓时价格已走完「开仓→止损」的百分之几。<b>接近 100% = 总在贴边平；很小 = 爱提前砍</b>')
    + '</div>';

  // ── 标记（最值钱的一栏）──────────────────────────────────────────────
  html += '<div class="sec">标记判断力（唯一能回答「我有没有进步」的地方）</div>';
  if (!s.nMark) {
    html += '<div class="h">本次会话没有标记。回放中按 <b>M</b> 可以标记「会进多 / 会进空 / 不进 / 不确定」。</div>';
  } else {
    html += '<div class="grid">'
      + _rpCard('方向命中率',
          s.hitRate == null ? '—' : Math.round(s.hitRate * 100) + '%',
          s.hitRate == null ? 'mu' : (s.hitRate >= 0.5 ? 'up' : 'dn'),
          `「会进多/会进空」后续 ${s.N} 根方向对的比例（样本 ${s.nHitSample} 个）。<b>50% = 抛硬币</b>`)
      + _rpCard('「不进」的规避质量',
          s.avoidQ == null ? '—' : num2(s.avoidQ),
          s.avoidQ == null ? 'mu' : (s.avoidQ > 1 ? 'up' : 'dn'),
          `你规避的 bar 后续 ${s.N} 根波动 ÷ 全样本平均（样本 ${s.nAvoidSample} 个）。<b>&gt;1 = 确实躲开了大波动；≈1 = 和随机没差别</b>`)
      + '</div>';
    html += '<table><thead><tr><th>bar</th><th>判断</th><th>后续 ' + s.N + ' 根</th><th>对错</th><th>理由</th></tr></thead><tbody>';
    s.marks.forEach(m => {
      const r = m.fwdRet == null ? '—'
        : ((m.fwdRet > 0 ? '+' : '') + (m.fwdRet * 100).toFixed(2) + '%');
      const hit = m.hit == null ? (m.verdict === '不进' ? '—' : '样本不足')
        : (m.hit ? '<span class="up">✓</span>' : '<span class="dn">✗</span>');
      html += `<tr><td>${esc(String(m.barX).slice(0, 16))}</td><td>${esc(m.verdict)}</td>`
        + `<td class="${m.fwdRet == null ? 'mu' : (m.fwdRet > 0 ? 'up' : 'dn')}">${r}</td>`
        + `<td>${hit}</td><td style="text-align:left;color:#9aa3af">${esc(m.note || '')}</td></tr>`;
    });
    html += '</tbody></table>';
  }

  // ── 逐笔 ────────────────────────────────────────────────────────────
  html += '<div class="sec">我的模拟单（逐笔）</div>';
  if (!s.nSim) {
    html += '<div class="h">本次会话没有成交。回放中按 <b>B</b> 开多 / <b>S</b> 开空 / <b>F</b> 平仓。</div>';
  } else {
    html += '<table><thead><tr><th>#</th><th>方向</th><th>手数</th><th>开仓 bar</th><th>平仓 bar</th>'
      + '<th>持根</th><th>盈亏</th><th>出场</th><th>MFE</th><th>MAE</th></tr></thead><tbody>';
    s.trades.forEach((t, i) => {
      html += `<tr><td>${i + 1}</td><td>${esc(t.dir)}</td><td>${t.qty}</td>`
        + `<td>${t.entryBar}</td><td>${t.exitBar}</td><td>${t.holdBars}</td>`
        + `<td class="${_rpCls(t.pnl)}">${money(t.pnl)}</td><td>${esc(t.reason)}</td>`
        + `<td class="mu">${num0(t.mfe)}</td><td class="mu">${num0(t.mae)}</td></tr>`;
    });
    html += '</tbody></table>';
    html += '<div class="h" style="margin-top:4px">MFE/MAE 为 <b>K 线口径</b>（逐 bar 取高低价），'
      + '与台账 EXCURSION（逐 tick）不同，会系统性低估 —— 两者不混用统计。</div>';
  }

  // ── 强制警告 ────────────────────────────────────────────────────────
  html += '<div class="warn">⚠️ 这些数字<b>只能用来诊断自己的行为</b>，不能用来评价策略：'
    + '①「策略同区间盈亏」是<b>事后区间求和</b>，不是可交易收益；'
    + '②06 毛利只有成本的 4.5%，任何「我比策略好」都不构成 alpha 证据；'
    + '③「规避质量 &gt;1」也不等于你会择时 —— 它只说明你标记的位置波动更大，样本量通常远不足 30。</div>';

  $('rpBody').innerHTML = html;
  $('rpMask').style.display = 'block';
  $('rpBox').style.display = 'block';
}
