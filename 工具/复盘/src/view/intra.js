// 左侧分时（1min）：跟随选中 / 回放当前 bar，截断未来
import { state, UP, DOWN } from '../core/state.js';
import { pd, isDaily, toDate, tmap, breaks } from '../core/time.js';
import { $ } from '../core/fmt.js';
import { DB } from '../data/index.js';

export function renderIntraday() {
  const d = DB.data[state.sym];
  const pdat = pd();
  const R = state.replay;
  const hiIdx = R ? Math.min(pdat.x.length - 1, R.abs) : pdat.x.length - 1;
  const hiBar = pdat.x[hiIdx];
  let day = R ? (pdat.day ? pdat.day[hiIdx] : d.last_day) : d.last_day;
  if (!R && state.sel != null) {
    const t = d.trades[state.sel];
    day = t.day || day;
  }
  // 当日全部 1min（不截断）——只用来定 x 轴跨度：夜盘 21:00 → 次日 15:00
  // ★ 2026-11-07（R-A 全年数据）：1min 单次上限 10000 根 ≈ 28 个交易日，只覆盖窗口尾部。
  //   回放跑到 1~9 月时当天一根 1min 都没有 ⇒ 原来直接 Plotly.purge，左栏是一片纯黑，
  //   看着像坏了。这里退化成**用当天 5min 画分时**（粗，但有形状、有量、有昨收线），
  //   标题加「5min」标注，不假装是 1min。
  const _pick = dayName => {
    const hit = [];
    ((d.m1 && d.m1.day) || []).forEach((v, i) => { if (v === dayName) hit.push(i); });
    if (hit.length) return { ser: d.m1, idx: hit, stepMs: 60000, tag: '' };
    const b5 = (d.periods || {})['5'];
    if (b5 && b5.day) {
      b5.day.forEach((v, i) => { if (v === dayName) hit.push(i); });
      if (hit.length) return { ser: b5, idx: hit, stepMs: 300000, tag: '5min' };
    }
    return { ser: d.m1, idx: [], stepMs: 60000, tag: '' };
  };
  let pick = _pick(day);
  if (!pick.idx.length && !R) {
    day = d.last_day;
    pick = _pick(day);
  }
  const dayAll = pick.idx, ser = pick.ser, stepMs = pick.stepMs;
  // ★ 回放时 1min 分时也截断到当前 bar 收盘，不能看当天未来。
  //   m1 的 x = **bar 起点**（build_data.py:796 直接取 tqsdk 的 datetime）。
  //   5min bar [09:00,09:05) 的最后一根完整 1min 是 09:04 ⇒ 必须严格小于 cut。
  //   原来写 <= 会把 09:05 那根（覆盖 09:05–09:06）也画进来 = 偷看未来 1 分钟。
  let idx = dayAll.slice();
  let cut = null;
  if (R && dayAll.length) {
    const pmin = isDaily() ? 1440 : (+state.period || 5);
    // ★ 子步动画：engine 播放时把 _subCut 按 1min 一档往前推，这里只认它；
    //   暂停/手步进时 _subCut=null，落回"当前 bar 收盘"的整根口径。
    cut = (state._subCut != null)
      ? new Date(state._subCut)
      : new Date(toDate(hiBar).getTime() + pmin * 60000);
    idx = dayAll.filter(i => toDate(ser.x[i]) < cut);
  }
  $('ititle').textContent = '分时 ' + day.slice(5) + (pick.tag ? '（' + pick.tag + '）' : '');
  if (!idx.length) { Plotly.purge('intra'); $('ititle').textContent = '分时 ' + day.slice(5) + '（无数据）'; return; }
  const x = idx.map(i => ser.x[i]), o = idx.map(i => ser.o[i]),
        c = idx.map(i => ser.c[i]), v = idx.map(i => ser.v[i]);
  let pv = 0, vv = 0;
  const avg = c.map((cc,i) => { pv += cc*v[i]; vv += v[i]; return vv ? pv/vv : cc; });
  const prev = d.day_prev[day] != null ? d.day_prev[day] : o[0];
  const col = c[c.length-1] >= prev ? UP : DOWN;
  // ★ 实时感：x 轴钉死成「当日整段」，线只画到当前 bar，右边留白 → 看起来像在跟着走，
  //   而不是"整张图被压缩"。轴尾 = 当日最后一根 1min 的起点 + 1 分钟（= 收盘时刻）。
  const _pad = n => String(n).padStart(2, '0');
  const _fmt = t => `${t.getFullYear()}-${_pad(t.getMonth()+1)}-${_pad(t.getDate())} ` +
                    `${_pad(t.getHours())}:${_pad(t.getMinutes())}`;
  const xr = dayAll.length
    ? [ser.x[dayAll[0]],
       _fmt(new Date(toDate(ser.x[dayAll[dayAll.length-1]]).getTime() + stepMs))]
    : null;
  const lx = x[x.length-1], lc = c[c.length-1];
  const traces = [
    {type:'scatter', x:x, y:c, mode:'lines', name:'价格', line:{color:col, width:1.5},
     hovertemplate:'%{x|%H:%M}  %{y:.1f}<extra></extra>'},
    {type:'scatter', x:x, y:avg, mode:'lines', name:'均价', line:{color:'#f2a900', width:1}, hoverinfo:'skip'},
    {type:'bar', x:x, y:v, yaxis:'y2', showlegend:false, opacity:.6, hoverinfo:'skip',
     marker:{color:c.map((cc,i)=>cc>=o[i]?UP:DOWN)}},
    // ★ 线头「现价」点 + 数值：实时感的最后一脚（cliponaxis:false 防止贴边被裁）
    {type:'scatter', x:[lx], y:[lc], mode:'markers+text', showlegend:false,
     marker:{color:col, size:5, line:{color:'#0f1117', width:1}},
     text:[lc.toFixed(1)], textposition:'middle right',
     textfont:{color:col, size:9}, cliponaxis:false, hoverinfo:'skip'},
  ];
  // ★ 时间刻度：rangebreaks 会把落在休市里的 tick 全删掉，自动刻度常常只剩 1 个
  //   ⇒ 改成显式指定「夜盘开盘 / 早盘开盘 / 午盘开盘」三个真实存在的时间点。
  const _tickMarks = ['21:00', '09:00', '13:30'];
  const _tickVals = dayAll.map(i => ser.x[i]).filter(s => _tickMarks.includes(s.slice(11)));
  const layout = {
    // ★ 回退方案 B：分时回到 220px 左栏（全宽 90px 带里走势被压平、看不清），
    //   高度/字号/上下分区都按窄栏口径调回来。
    height:190, margin:{l:2, r:42, t:4, b:2},
    paper_bgcolor:'#0f1117', plot_bgcolor:'#0f1117',
    font:{family:'Microsoft YaHei', color:'#e6e6e6', size:9},
    showlegend:false, hovermode:'x unified', dragmode:false,
    xaxis:{type:'date', range:xr, fixedrange:true, tickformat:'%H:%M', showgrid:false,
      tickmode: _tickVals.length ? 'array' : 'auto',
      tickvals: _tickVals.length ? _tickVals : undefined,
      tickfont:{color:'#9aa3af', size:8}, linecolor:'#2a2f3a', rangebreaks:breaks()},
    yaxis:{domain:[0.24,1], side:'right', nticks:4, gridcolor:'rgba(255,255,255,0.06)',
      tickfont:{color:'#9aa3af', size:8}, linecolor:'#2a2f3a', fixedrange:true},
    yaxis2:{domain:[0,0.2], side:'right', nticks:2, gridcolor:'rgba(0,0,0,0)',
      showticklabels:false, tickfont:{color:'#9aa3af', size:8}, linecolor:'#2a2f3a', fixedrange:true},
    shapes:[{type:'line', x0:x[0], x1:xr ? xr[1] : x[x.length-1], y0:prev, y1:prev,
      line:{color:'#888', width:1, dash:'dot'}}],
  };
  Plotly.react('intra', traces, layout, {displayModeBar:false, responsive:false, displaylogo:false});
}
