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
  let idx = [];
  d.m1.day.forEach((v,i) => { if (v === day) idx.push(i); });
  if (!idx.length && !R) {
    day = d.last_day;
    d.m1.day.forEach((v,i) => { if (v === day) idx.push(i); });
  }
  // ★ 回放时 1min 分时也截断到当前 bar 收盘，不能看当天未来
  if (R && idx.length) {
    const pmin = isDaily() ? 1440 : (+state.period || 5);
    const cut = new Date(toDate(hiBar).getTime() + pmin * 60000);
    idx = idx.filter(i => toDate(d.m1.x[i]) <= cut);
  }
  $('ititle').textContent = '分时 ' + day.slice(5);
  if (!idx.length) { Plotly.purge('intra'); return; }
  const x = idx.map(i => d.m1.x[i]), o = idx.map(i => d.m1.o[i]),
        c = idx.map(i => d.m1.c[i]), v = idx.map(i => d.m1.v[i]);
  let pv = 0, vv = 0;
  const avg = c.map((cc,i) => { pv += cc*v[i]; vv += v[i]; return vv ? pv/vv : cc; });
  const prev = d.day_prev[day] != null ? d.day_prev[day] : o[0];
  const col = c[c.length-1] >= prev ? UP : DOWN;
  const traces = [
    {type:'scatter', x:x, y:c, mode:'lines', name:'价格', line:{color:col, width:1.5},
     hovertemplate:'%{x|%H:%M}  %{y:.1f}<extra></extra>'},
    {type:'scatter', x:x, y:avg, mode:'lines', name:'均价', line:{color:'#f2a900', width:1}, hoverinfo:'skip'},
    {type:'bar', x:x, y:v, yaxis:'y2', showlegend:false, opacity:.6, hoverinfo:'skip',
     marker:{color:c.map((cc,i)=>cc>=o[i]?UP:DOWN)}},
  ];
  const layout = {
    height:200, margin:{l:2, r:2, t:20, b:2},
    paper_bgcolor:'#0f1117', plot_bgcolor:'#0f1117',
    font:{family:'Microsoft YaHei', color:'#e6e6e6', size:9},
    title:{text:'分时 '+day.slice(5), font:{size:11, color:'#9aa3af'}, x:0.01, y:0.99},
    showlegend:false, hovermode:'x unified',
    xaxis:{type:'date', tickformat:'%H:%M', nticks:4, showgrid:false,
      tickfont:{color:'#9aa3af'}, linecolor:'#2a2f3a', rangebreaks:breaks()},
    yaxis:{domain:[0.24,1], side:'right', nticks:4, gridcolor:'rgba(255,255,255,0.06)',
      tickfont:{color:'#9aa3af'}, linecolor:'#2a2f3a'},
    yaxis2:{domain:[0,0.2], side:'right', nticks:2, gridcolor:'rgba(0,0,0,0)',
      showticklabels:false, tickfont:{color:'#9aa3af'}, linecolor:'#2a2f3a'},
    shapes:[{type:'line', x0:x[0], x1:x[x.length-1], y0:prev, y1:prev,
      line:{color:'#888', width:1, dash:'dot'}}],
  };
  Plotly.react('intra', traces, layout, {displayModeBar:false, responsive:false, displaylogo:false});
}
