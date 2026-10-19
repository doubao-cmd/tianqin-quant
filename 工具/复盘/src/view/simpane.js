// 模拟下单面板（右侧「模拟」tab，P2-1）：会话内的挂单/持仓/已平单与已实现盈亏
import { state } from '../core/state.js';
import { pd } from '../core/time.js';
import { $, esc, sgn, money } from '../core/fmt.js';
import { DB } from '../data/index.js';
import { bookState, simCfg } from '../replay/book.js';
import { sessStats } from '../replay/ledger.js';

export function renderSim() {
  const title = $('simtitle'), body = $('simbody');
  if (!title || !body) return;
  const dd = DB.data[state.sym];
  if (!state.replay) {
    title.textContent = `${dd.name} ｜ 模拟下单`;
    body.innerHTML = '<div class="muted" style="padding:8px">进入回放后，用回放条上的 买 / 卖 / 平 按钮模拟下单（下一根开盘成交，撮合口径与 06 回测冻结一致）。</div>';
    return;
  }
  const cfg = simCfg(state.sym);
  const st = bookState();
  const d = pd();
  let html = '';
  if (st.pending) {
    const p = st.pending;
    html += `<div class="note">⏳ 待成交：${p.type === 'open' ? `${esc(p.dir)} ${p.qty} 手（止损 ${p.stopPx}${p.targetPx != null ? ' / 止盈 ' + p.targetPx : ''}）` : '市价平仓'} → 下一根开盘成交。点击报价条上的「待成交」字样可撤销。</div>`;
  }
  if (st.pos) {
    const p = st.pos, cur = d.c[state.replay.abs];
    const dirSign = p.dir === '多' ? 1 : -1;
    const fl = (cur - p.entryPx) * p.qty * cfg.mult * dirSign;
    html += `<div class="sim-pos"><b class="${p.dir === '多' ? 'up' : 'down'}">${esc(p.dir)} ${p.qty} 手</b> @${p.entryPx}` +
      ` ｜ 浮盈 <b class="${fl >= 0 ? 'up' : 'down'}">${sgn(fl)}</b>` +
      ` ｜ 已持 ${state.replay.abs - p.entryBar} 根 ｜ 止损 ${p.stopPx}${p.targetPx != null ? ' ｜ 止盈 ' + p.targetPx : ''}</div>`;
  }
  let rows = '';
  st.closed.forEach(t => {
    rows += `<tr><td class="${t.dir === '多' ? 'up' : 'down'}">${t.dir}</td><td>${t.qty}</td>` +
      `<td>${d.x[t.entryBar] ? d.x[t.entryBar].slice(5, 16) : t.entryBar} @${t.entryPx}</td>` +
      `<td>${d.x[t.exitBar] ? d.x[t.exitBar].slice(5, 16) : t.exitBar} @${t.exitPx}</td>` +
      `<td>${t.reason}</td><td class="${t.pnl >= 0 ? 'up' : 'down'}">${money(t.pnl)}</td></tr>`;
  });
  html += '<table><thead><tr><th>方向</th><th>手数</th><th>开仓</th><th>平仓</th><th>原因</th><th>盈亏</th></tr></thead><tbody>' +
    (rows || '') + '</tbody></table>';
  if (!st.closed.length && !st.pos && !st.pending) {
    html += '<div class="muted" style="padding:4px">本会话还没有模拟单。按 买/卖 弹出下单面板。</div>';
  }
  if (st.closed.length) {
    const n = st.closed.length, wins = st.closed.filter(t => t.pnl > 0).length;
    html += `<div class="stat-line" style="margin-top:4px">已平 <b>${n}</b> 笔 ｜ 已实现 <b class="${st.realized >= 0 ? 'up' : 'down'}">${money(st.realized)}</b> 元 ｜ 胜 ${wins}/亏 ${n - wins}</div>`;
  }
  // ★ P2-3：本次会话的台账预览（退出回放时会自动弹下载；这里先让用户看见在记什么）
  {
    const ss = sessStats();
    if (ss) {
      const mm = Math.floor(ss.elapsed / 60000), sss = Math.floor(ss.elapsed / 1000) % 60;
      html += `<div class="stat-line" style="margin-top:5px;border-top:1px solid var(--border);padding-top:4px">` +
        `本次会话 <b>${ss.id}</b> ｜ 已练 <b>${mm}</b> 分 ${sss} 秒 ｜ 推进 <b>${ss.nStep}</b> 根` +
        ` ｜ 模拟 <b>${ss.nSim}</b> 笔${ss.openFlag ? '（含未平 1 笔）' : ''}` +
        ` ｜ 标记 <b>${ss.nMarks}</b> 个 ｜ 模式 <b>${ss.mode === 'train' ? '训练' : '复看'}</b>` +
        `<br><span class="stat-note">退出回放时自动导出「会话_日期.csv」；标记面板里也可手动「导出台账」。</span></div>`;
    }
  }
  title.textContent = `${dd.name} ｜ 模拟下单 · bar ${state.replay.abs + 1}`;
  body.innerHTML = html;
}
