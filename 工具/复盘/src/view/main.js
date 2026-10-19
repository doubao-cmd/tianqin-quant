// 主图（K线 + MA13 + 副图 + 成交标记 + 选中/画线）
import { state, UP, DOWN, canSeeAnswers } from '../core/state.js';
import { pd, isDaily, tmap, tradeStart, tradeEnd, evTime, breaks, rangeStart, toDate,
         viewMin, clampViewLeft, VIEW_MIN_SPAN_MS } from '../core/time.js';
import { $, esc, f1, f2, sgn } from '../core/fmt.js';
import { DB } from '../data/index.js';
import { bookState, SIM_COLOR } from '../replay/book.js';
import { markAt, marksList, markColor } from '../replay/marks.js';

function renderQuote(q) {
  const chg = q.c - q.prev, pct = q.prev ? chg/q.prev*100 : 0;
  const col = chg >= 0 ? UP : DOWN;
  const d = DB.data[state.sym];
  const tag = q.sel ? '选中笔 · ' : '';
  $('qstrip').innerHTML =
    `<span class="name">${esc(d.name)} ${state.sym}</span>` +
    `<span class="ohlc">${tag}${isDaily()?'日线':state.period+' 分钟'}</span>` +
    `<span class="px" style="color:${col}">${q.c.toFixed(1)}</span>` +
    `<span class="chg" style="color:${col}">${(chg>=0?'+':'')}${chg.toFixed(1)}  ${(pct>=0?'+':'')}${pct.toFixed(2)}%</span>` +
    `<span class="ohlc">开 ${q.o.toFixed(1)} · 高 ${q.h.toFixed(1)} · 低 ${q.l.toFixed(1)} · 昨收 ${q.prev.toFixed(1)}</span>` +
    `<span class="seg" style="color:${q.sel?'#ffb3b3':'inherit'}">${String(q.t).slice(5)}</span>`;
  const amp = q.prev ? (q.h-q.l)/q.prev*100 : null;
  $('qcard').innerHTML =
    `<div>今开 <b>${q.o.toFixed(1)}</b>　最高 <b>${q.h.toFixed(1)}</b></div>` +
    `<div>最低 <b>${q.l.toFixed(1)}</b>　收盘 <b>${q.c.toFixed(1)}</b></div>` +
    `<div>昨收 <b>${q.prev.toFixed(1)}</b>　振幅 <b>${amp==null?'—':amp.toFixed(1)+'%'}</b></div>`;
}

// ★ P1-4 持仓状态：回放到某根时，你当时有没有仓、成本多少、浮盈多少。
//   口径（写死）：开仓 bar 收盘 <= 当前 bar 收盘 且 (未平 或 平仓 bar 收盘 > 当前 bar 收盘)。
//   浮盈只用"当前 bar 收盘价"算，禁碰未来 bar；接管笔(开仓价未知)显示 @未知、浮盈 —。
//   仅在"回放中 + 复看模式"显示（训练模式隐藏，防泄露答案——持仓本身也是答案）。
function renderPosition(pdata, sdata, abs) {
  const cur = pdata.x[abs];
  const curClose = pdata.c[abs];
  const poss = (sdata.trades || []).filter(t =>
    t.t && tmap(t.t) <= tmap(cur) && (!t.x || tmap(t.x) > tmap(cur)));
  if (!poss.length) return '';
  const dir = poss[0].d;
  const totalQ = poss.reduce((s, t) => s + t.q, 0);
  const allKnown = poss.every(t => t.e != null);
  const avgE = allKnown ? poss.reduce((s, t) => s + t.e * t.q, 0) / totalQ : null;   // 多笔→加权均价
  const mult = sdata.mult || 1;
  const dirSign = dir === '多' ? 1 : -1;
  const pnl = (avgE != null && curClose != null) ? (curClose - avgE) * totalQ * mult * dirSign : null;
  let minEntry = Infinity;                                                            // 已持 N 根 = 当前 - 最早开仓 bar
  poss.forEach(t => { const k = pdata.x.findIndex(s => tmap(s) >= tmap(t.t)); if (k >= 0 && k < minEntry) minEntry = k; });
  const bars = isFinite(minEntry) ? Math.max(0, abs - minEntry) : 0;
  const col = dir === '多' ? UP : DOWN;
  const pcol = pnl == null ? 'inherit' : (pnl > 0 ? UP : (pnl < 0 ? DOWN : 'inherit'));
  return `<span class="posgrp">` +
    `<span class="pl" style="color:${col}">持仓 ${dir} ${totalQ} 手 @${avgE == null ? '未知' : avgE.toFixed(1)}</span>` +
    `<span class="pl" style="color:${pcol}">浮盈 ${pnl == null ? '—' : sgn(pnl)}</span>` +
    `<span class="pl">已持 ${bars} 根</span></span>`;
}

export function renderMain() {
  const d = pd(), n = d.c.length;
  const R = state.replay;

  // —— P0-C 固定窗口（★ 一条修两个病：单调变宽 + 每帧切片变长）——
  let iStart, iEnd;
  if (R) {
    iEnd = R.abs;                                  // 窗口右沿 = 当前 bar（绝对索引）
    iStart = Math.max(0, iEnd - R.win + 1);        // 长度恒 = R.win（起点不足时自然变短）
  } else {
    const i0 = rangeStart(d.x);                    // 非回放：整段显示
    iStart = i0;
    iEnd = n - 1;
  }
  const xs = d.x.slice(iStart, iEnd + 1),
        o = d.o.slice(iStart, iEnd + 1),
        h = d.h.slice(iStart, iEnd + 1),
        l = d.l.slice(iStart, iEnd + 1),
        c = d.c.slice(iStart, iEnd + 1),
        v = d.v.slice(iStart, iEnd + 1),
        ma = d.ma.slice(iStart, iEnd + 1),
        atr = d.atr.slice(iStart, iEnd + 1);
  const dd = DB.data[state.sym];

  // 昨收：回放/选中都用"该 bar 所属交易日"的昨收，不能拿上一根 bar 收盘冒充
  let prevBar;
  if (R) {
    const dp = (!isDaily() && d.day) ? dd.day_prev[d.day[iEnd]] : null;
    prevBar = (dp != null) ? dp : (iEnd > 0 ? d.c[iEnd - 1] : d.c[iEnd]);
  } else if (isDaily()) {
    prevBar = n > 1 ? d.c[n-2] : d.c[n-1];
  } else {
    prevBar = dd.day_prev[dd.last_day] != null
      ? dd.day_prev[dd.last_day]
      : (n > 1 ? d.c[n-2] : d.c[n-1]);
  }

  // ★ 选中某笔时，报价条改成"该笔平仓那根"；取消选中恢复最新
  let q = {o:o[o.length-1], h:h[h.length-1], l:l[l.length-1], c:c[c.length-1],
           prev:prevBar, t:xs[xs.length-1], sel:false};
  if (state.sel != null) {
    const st = dd.trades[state.sel];
    const et = tradeEnd(st) || st.t;
    let idx = d.x.length - 1;
    for (let k = d.x.length - 1; k >= 0; k--) {
      if (tmap(d.x[k]) <= tmap(et)) { idx = k; break; }
    }
    if (idx >= 0) {
      const qp = isDaily()
        ? (idx > 0 ? d.c[idx-1] : d.c[idx])
        : ((dd.day_prev[st.xday || st.day] != null)
            ? dd.day_prev[st.xday || st.day]
            : (idx > 0 ? d.c[idx-1] : d.c[idx]));
      q = {o:d.o[idx], h:d.h[idx], l:d.l[idx], c:d.c[idx], prev:qp, t:d.x[idx], sel:true};
    }
  }
  renderQuote(q);

  // ★ P1-4：回放中追加"持仓状态"到报价条右侧（复看模式才显示；训练模式隐藏防泄露）
  if (R && canSeeAnswers()) {
    const posHtml = renderPosition(d, dd, R.abs);
    if (posHtml) $('qstrip').insertAdjacentHTML('beforeend', posHtml);
  }

  // ★ P2-1：模拟持仓/待成交段（用户自己的单子，训练/复看都显示；蓝 = 模拟，区别真实红绿）
  if (R) {
    const bs = bookState();
    if (bs.pending) {
      const p = bs.pending;
      $('qstrip').insertAdjacentHTML('beforeend',
        `<span class="posgrp simgrp pend" data-act="cancel" title="点击撤销待成交委托">⏳ 待成交 ${p.type === 'open' ? esc(p.dir) + ' ' + p.qty + ' 手' : '平仓'} → 下根开盘（点此撤销）</span>`);
    } else if (bs.pos) {
      const p = bs.pos, cur = d.c[R.abs], dirSign = p.dir === '多' ? 1 : -1;
      const fl = (cur - p.entryPx) * p.qty * (dd.mult || 1) * dirSign;
      const flCol = fl > 0 ? UP : (fl < 0 ? DOWN : 'inherit');
      $('qstrip').insertAdjacentHTML('beforeend',
        `<span class="posgrp simgrp">` +
        `<span class="pl" style="color:${SIM_COLOR}">模拟 ${esc(p.dir)} ${p.qty} 手 @${p.entryPx}</span>` +
        `<span class="pl" style="color:${flCol}">浮盈 ${sgn(fl)}</span>` +
        `<span class="pl">已持 ${R.abs - p.entryBar} 根</span>` +
        `<span class="pl">止损 ${p.stopPx}</span></span>`);
    }
  }

  // ★ P2-2：当前 bar 的决策标记回显（用户自己的判断，训练/复看都显——不是答案）
  if (R) {
    const mk = markAt(d.x[R.abs]);
    if (mk) {
      $('qstrip').insertAdjacentHTML('beforeend',
        `<span class="posgrp" title="${esc(mk.note || '（无理由）')}">` +
        `<span class="pl" style="color:${markColor(mk.verdict)}">我的标记 ${esc(mk.verdict)}</span>` +
        (mk.note ? `<span class="pl">${esc(mk.note)}</span>` : '') + `</span>`);
    }
  }

  const cd = c.map((cc,i) => [v[i], ma[i], atr[i],
    i>0 ? +(cc-c[i-1]).toFixed(1) : 0,
    i>0 && c[i-1] ? +((cc-c[i-1])/c[i-1]*100).toFixed(2) : 0]);
  const fmt = isDaily() ? '%Y-%m-%d' : '%m-%d %H:%M';
  const traces = [{
    type:'candlestick', x:xs, open:o, high:h, low:l, close:c, name:'K线',
    increasing:{line:{color:UP,width:1}, fillcolor:UP},
    decreasing:{line:{color:DOWN,width:1}, fillcolor:DOWN},
    customdata:cd,
    hovertemplate:'<b>%{x|'+fmt+'}</b><br>开 %{open:.1f}　高 %{high:.1f}<br>低 %{low:.1f}　收 %{close:.1f}<br>'+
      '涨跌 %{customdata[3]:+.1f}（%{customdata[4]:+.2f}%）<br>量 %{customdata[0]:,.0f}<br>'+
      'MA13 %{customdata[1]:.1f}　ATR14 %{customdata[2]:.2f}<extra></extra>',
  },{
    type:'scatter', x:xs, y:ma, mode:'lines', name:'MA13',
    line:{color:'#f2a900', width:1.6}, hoverinfo:'skip',
  }];
  const ind = state.ind;
  if (ind === 'vol') {
    traces.push({type:'bar', x:xs, y:v, yaxis:'y2', opacity:.55, hoverinfo:'skip',
      marker:{color:c.map((cc,i)=>cc>=o[i]?UP:DOWN)}});
  } else if (ind === 'atr') {
    traces.push({type:'scatter', x:xs, y:atr, yaxis:'y2', mode:'lines',
      line:{color:'#f2a900', width:1.2}, hoverinfo:'skip'});
  } else if (ind === 'macd') {
    const hh = d.hist.slice(iStart, iEnd + 1);
    traces.push({type:'bar', x:xs, y:hh, yaxis:'y2', opacity:.6, hoverinfo:'skip',
      marker:{color:hh.map(v=>v>=0?UP:DOWN)}});
    traces.push({type:'scatter', x:xs, y:d.dif.slice(iStart, iEnd + 1), yaxis:'y2', mode:'lines',
      line:{color:'#f2a900', width:1.1}, hoverinfo:'skip'});
    traces.push({type:'scatter', x:xs, y:d.dea.slice(iStart, iEnd + 1), yaxis:'y2', mode:'lines',
      line:{color:'#60a5fa', width:1.1}, hoverinfo:'skip'});
  } else if (ind === 'rsi') {
    traces.push({type:'scatter', x:xs, y:d.rsi.slice(iStart, iEnd + 1), yaxis:'y2', mode:'lines',
      line:{color:'#a78bfa', width:1.3}, hoverinfo:'skip'});
  } else if (ind === 'kdj') {
    traces.push({type:'scatter', x:xs, y:d.k.slice(iStart, iEnd + 1), yaxis:'y2', mode:'lines',
      line:{color:'#f2a900', width:1.1}, hoverinfo:'skip'});
    traces.push({type:'scatter', x:xs, y:d.d.slice(iStart, iEnd + 1), yaxis:'y2', mode:'lines',
      line:{color:'#60a5fa', width:1.1}, hoverinfo:'skip'});
    traces.push({type:'scatter', x:xs, y:d.j.slice(iStart, iEnd + 1), yaxis:'y2', mode:'lines',
      line:{color:'#c084fc', width:1.1}, hoverinfo:'skip'});
  }

  const lo = tmap(xs[0]), hi = tmap(xs[xs.length-1]);
  // ★ P1-5：训练模式隐藏所有成交标记（防止开卷考试）
  // ★ P5-3 方案A：8 个 trace 合并成 2 个（光晕层 + 实心层）。原来「类型×方向」4 组各出 2 个 trace，
  //   最多 8 个 ⇒ 每个都是独立 SVG group，拖动时浏览器要重排的节点翻倍。
  //   Plotly 的 marker.symbol / size / color 都接受**数组** ⇒ 一个 trace 就能画全部四种标记。
  if (canSeeAnswers()) {
    const evs = dd.events.filter(e => { const q = tmap(evTime(e)); return q >= lo && q <= hi; });
    const SPEC = {
      '开|多': [UP, 'triangle-up', 16, '多'], '开|空': [DOWN, 'triangle-down', 16, '空'],
      '平|多': ['#ff9aa2', 'x', 12, ''], '平|空': ['#86efac', 'x', 12, ''],
    };
    const ex = [], ey = [], mk = [], tx = [], cd = [];
    evs.forEach(e => {
      const sp = SPEC[e.y + '|' + e.d];
      if (!sp) return;
      ex.push(evTime(e)); ey.push(e.p);
      mk.push({ symbol: sp[1], size: sp[2], color: sp[0] });
      tx.push(sp[3]);
      cd.push([e.q, e.p, e.y + e.d]);
    });
    if (ex.length) {
      traces.push({type:'scatter', x:ex, y:ey, mode:'markers',
        marker:{symbol:mk.map(m=>m.symbol), size:mk.map(m=>m.size+10),
                color:mk.map(m=>m.color), opacity:.22, line:{width:0}},
        hoverinfo:'skip', showlegend:false});
      traces.push({type:'scatter', x:ex, y:ey, mode:'markers+text',
        marker:{symbol:mk.map(m=>m.symbol), size:mk.map(m=>m.size),
                color:mk.map(m=>m.color), line:{width:1.6, color:'#fff'}},
        text:tx, textposition:'top center',
        textfont:{size:10, color:'#fff'}, showlegend:false,
        customdata:cd,
        hovertemplate:'%{customdata[2]} %{customdata[0]}手 @%{customdata[1]}<extra></extra>'});
    }
  }

  const shapes = [];
  if (ind === 'rsi') {
    shapes.push({type:'line', x0:xs[0], x1:xs[xs.length-1], y0:30, y1:30, yref:'y2',
      line:{color:'#555', width:1, dash:'dot'}});
    shapes.push({type:'line', x0:xs[0], x1:xs[xs.length-1], y0:70, y1:70, yref:'y2',
      line:{color:'#555', width:1, dash:'dot'}});
  } else if (ind === 'macd') {
    shapes.push({type:'line', x0:xs[0], x1:xs[xs.length-1], y0:0, y1:0, yref:'y2',
      line:{color:'#555', width:1}});
  }
  // 不再画止损线：它会把价格 Y 轴撑宽，导致 K 线显矮

  // 默认视图 = 当前窗口（回放：整窗；非回放：复用用户缩放）
  let xr = [xs[0], xs[xs.length-1]];
  if (!R) {
    const viewKey = `${state.sym}|${state.period}|${state.range}|${state.sel}`;
    const mEl = $('main');
    if (viewKey === state._lastViewKey && mEl && mEl.layout &&
        mEl.layout.xaxis && mEl.layout.xaxis.range) {
      xr = mEl.layout.xaxis.range.slice();
    } else {
      state._lastViewKey = viewKey;
    }
  }

  // ★ P4 多周期联动（降级方案）：切周期后保持「时间中心 + 时间跨度」，而不是跳回全图。
  //   由 state.setContext 在切周期前捕获（state._keepCenter），这里消费一次即清。
  //   ⚠️ 降级方案不做副图：跨周期的"同时看"没实现，只做"视野不丢"。
  if (!R && state._keepCenter) {
    const kc = state._keepCenter;
    state._keepCenter = null;
    const t0 = kc.c - kc.w / 2, t1 = kc.c + kc.w / 2;
    let j0 = -1, j1 = -1;
    for (let k = 0; k < xs.length; k++) {
      const tk = toDate(xs[k]).getTime();
      if (j0 < 0 && tk >= t0) j0 = k;
      if (tk <= t1) j1 = k;
    }
    if (j0 >= 0 && j1 > j0) {
      xr = [xs[j0], xs[j1]];
      // 记下 viewKey：下一次同上下文的 render 会从 layout 里读回这个范围，不会又跳回全图
      state._lastViewKey = `${state.sym}|${state.period}|${state.range}|${state.sel}`;
    }
  }

  // ★★ 视图左界锁死（2026-10-18 用户要求）：不许拖到 2026-10-08 之前。
  //   原因见 core/time.js 的 viewMin() 注释（10-08 之前有K线但没任何成交/决策标记）。
  //   这里在「所有」xr 计算完之后统一钳一次 —— 无论是默认全段、用户缩放复用、
  //   还是 P4 切周期的 _keepCenter，都不能突破左界。
  {
    // clampViewLeft 已处理「界内原样返回 / 越界顶回 + 保底跨度」，这里直接复用，
    // 不要在 main.js 里再写一份保底逻辑（曾各写一份且值不一致 ⇒ 视野被压成一条线）
    const [ca, cb] = clampViewLeft(xr[0], xr[1]);
    if (ca !== xr[0]) xr = [ca, cb];
  }

  // ★ 选中笔高亮（仅在非回放下缩放视图；回放保持固定窗口）
  // ★ P1-5：训练模式不显示选中笔高亮（会泄露答案）
  if (state.sel != null && !R && canSeeAnswers()) {
    const t = dd.trades[state.sel];
    const isLong = t.d === '多', col = isLong ? UP : DOWN;
    const x0 = tradeStart(t), x1 = tradeEnd(t);
    if (x0 != null) {
      const vx0 = x0 < xs[0] ? xs[0] : x0;
      const vx1 = (!x1 || x1 > xs[xs.length-1]) ? xs[xs.length-1] : x1;
      if (vx1 > vx0) shapes.push({type:'rect', x0:vx0, x1:vx1, y0:0, y1:1, yref:'paper',
        fillcolor:isLong?'rgba(239,68,68,0.18)':'rgba(34,197,94,0.18)', line:{width:0}, layer:'below'});
      const sVal = t.ps != null ? t.ps : t.s;
      if (sVal != null && tmap(vx1) > tmap(vx0)) {
        shapes.push({type:'line', x0:vx0, x1:vx1, y0:sVal, y1:sVal,
          line:{color:col, width:1.6, dash:'dot'}});
      }
      if (t.e != null && x0 >= xs[0] && x0 <= xs[xs.length-1]) {
        traces.push({type:'scatter', x:[x0], y:[t.e], mode:'markers',
          marker:{symbol:isLong?'triangle-up':'triangle-down', size:26, color:col,
                  line:{width:3, color:'#ffe082'}},
          hovertemplate:'选中 · '+t.d+' '+t.q+'手<extra></extra>', showlegend:false});
      }
      const k0 = xs.findIndex(s => tmap(s) >= tmap(x0));
      const k1 = x1 ? xs.findIndex(s => tmap(s) >= tmap(x1)) : xs.length - 1;
      if (k0 >= 0) {
        const j0 = Math.max(0, k0 - 40);
        const j1 = Math.min(xs.length - 1, (k1 >= 0 ? k1 : xs.length - 1) + 20);
        if (j1 > j0) xr = [xs[j0], xs[j1]];
      }
    }
  }

  // ★ P2-1：图上画模拟单 —— 开仓三角 / 平仓 x / 止损虚线 / 持仓区间底色（蓝 = 模拟）。
  //   放在 _progShapes 快照之前 ⇒ 归入程序化 shapes，不会混进用户画线。
  if (R) {
    const bs = bookState();
    const simT = bs.closed.slice();
    if (bs.pos) simT.push(bs.pos);
    simT.forEach(t => {
      if (t.exitBar != null && t.exitBar < iStart) return;
      if (t.entryBar > iEnd) return;
      const isL = t.dir === '多';
      const eIdx = Math.max(0, t.entryBar - iStart);
      const xIdx = (t.exitBar != null ? t.exitBar : R.abs) - iStart;
      if (xIdx < 0) return;
      traces.push({ type: 'scatter', x: [xs[eIdx]], y: [t.entryPx], mode: 'markers',
        marker: { symbol: isL ? 'triangle-up' : 'triangle-down', size: 13, color: SIM_COLOR, line: { width: 1.5, color: '#fff' } },
        customdata: [[t.qty, t.entryPx]],
        hovertemplate: '模拟开仓 ' + t.dir + ' %{customdata[0]}手 @%{customdata[1]}<extra>模拟</extra>', showlegend: false });
      if (t.exitPx != null && t.exitBar >= iStart) {
        traces.push({ type: 'scatter', x: [xs[xIdx]], y: [t.exitPx], mode: 'markers',
          marker: { symbol: 'x', size: 11, color: '#93c5fd' },
          customdata: [[t.reason, t.pnl == null ? '—' : sgn(t.pnl)]],
          hovertemplate: '模拟平仓（' + '%{customdata[0]}）@%{y}  盈亏 %{customdata[1]}<extra>模拟</extra>', showlegend: false });
      }
      const xe0 = xs[eIdx], xe1 = xs[Math.min(xs.length - 1, xIdx)];
      shapes.push({ type: 'rect', x0: xe0, x1: xe1, y0: 0, y1: 1, yref: 'paper',
        fillcolor: 'rgba(96,165,250,0.10)', line: { width: 0 }, layer: 'below' });
      if (t.stopPx != null) {
        shapes.push({ type: 'line', x0: xe0, x1: xe1, y0: t.stopPx, y1: t.stopPx,
          line: { color: SIM_COLOR, width: 1.2, dash: 'dot' } });
      }
    });
  }

  // ★ P2-2：图上画决策标记圆点（蓝=模拟单之后、成交标记之后，互不遮挡）。
  //   吸附口径：xs.findIndex(s => s === m.barX) —— barX 就是 d.x[abs] 原字符串，
  //   逐个相等才命中 ⇒ 圆点永远落在 bar 正上方，不可能落在两根 bar 之间。
  //   用户自己的判断 ≠ trades（答案）⇒ 不加 canSeeAnswers 守卫，训练模式也画。
  {
    const mks = marksList(state.sym, state.period);
    mks.forEach(m => {
      const k = xs.findIndex(s => s === m.barX);
      if (k < 0) return;                       // 该 bar 不在当前窗口（换了区间/周期）→ 不画
      const col = markColor(m.verdict);
      traces.push({ type: 'scatter', x: [xs[k]], y: [c[k]], mode: 'markers',
        marker: { symbol: 'circle', size: 9, color: col, line: { width: 1.4, color: '#0f1117' } },
        customdata: [[m.verdict, m.note ? '理由：' + m.note : '（未写理由）']],
        hovertemplate: '我的标记：%{customdata[0]}<br>%{customdata[1]}<extra>P2-2</extra>',
        showlegend: false });
    });
  }

  // ★ 用户画线存 state.shapes，重绘时合并；回放时裁到当前 bar（hiBar）
  state._progShapes = shapes.slice();
  (state.shapes || []).forEach(s => {
    let s2 = s;
    if (R) {
      const hiBar = xs[xs.length-1];
      if (s2.x0 && tmap(s2.x0) > tmap(hiBar)) return;
      if (s2.x1 && tmap(s2.x1) > tmap(hiBar)) {
        s2 = Object.assign({}, s2, {x1: hiBar});
      }
    }
    shapes.push(s2);
  });

  // ★ 价格 Y 轴只按"当前可见窗口"取高低，K 线才会填满价格区
  const ai = Math.max(0, xs.findIndex(s => s >= xr[0]));
  const bi = Math.max(ai, Math.min(xs.length-1, xs.findIndex(s => s >= xr[1])));
  let plo = Infinity, phi = -Infinity;
  for (let i = ai; i <= bi; i++) {
    if (l[i] != null && l[i] < plo) plo = l[i];
    if (h[i] != null && h[i] > phi) phi = h[i];
  }
  if (!isFinite(plo) || !isFinite(phi)) { plo = 0; phi = 1; }
  const ppad = Math.max((phi - plo) * 0.06, 0.5);

  const layout = {
    margin:{l:8, r:8, t:8, b:8},
    paper_bgcolor:'#0f1117', plot_bgcolor:'#0f1117',
    font:{family:'Microsoft YaHei', color:'#e6e6e6', size:11},
    hovermode:'closest', dragmode:'pan',
    hoverlabel:{bgcolor:'#151d2e', bordercolor:'#3a4350', font:{color:'#e6e6e6', size:11}, align:'left'},
    showlegend:false, shapes:shapes,
    uirevision: state.sel != null
      ? `${state.sym}|${state.period}|${state.sel}`
      : `${state.sym}|${state.period}`,
    xaxis:{type:'date', range:xr, rangeslider:{visible:false},
      gridcolor:'#232733', tickfont:{color:'#9aa3af'}, linecolor:'#2a2f3a',
      rangebreaks:breaks()},
    yaxis:{domain:[0.30,1], range:[plo-ppad, phi+ppad],
      gridcolor:'#232733', tickfont:{color:'#9aa3af'},
      linecolor:'#2a2f3a', side:'right'},
    yaxis2:{domain:[0,0.22], gridcolor:'rgba(255,255,255,0.04)',
      tickfont:{color:'#9aa3af'}, showticklabels: state.ind !== 'vol',
      linecolor:'#2a2f3a', side:'right'},
  };
  Plotly.react('main', traces, layout, {
    // ★ P5-2：responsive 关掉，改由 topbar.safeResize() 统一管（避免 ResizeObserver 与手动 resize 打架）
    displayModeBar:true, scrollZoom:true, responsive:false, displaylogo:false,
    modeBarButtonsToRemove:['select2d','lasso2d','toggleSpikelines'],
    modeBarButtonsToAdd:['drawline','drawrect','eraseshape'],
  });

  // ★ 捕获用户画的线 + 回放时把缩放根数换算回 R.win（P0-C）
  if (!state._shapeBound) {
    $('main').on('plotly_relayout', e => {
      if (e && e.shapes) {
        const nProg = (state._progShapes || []).length;
        state.shapes = e.shapes.slice(nProg);
      }
      // ★★ 左界锁死：用户往左拖/缩小到 10-08 之前时，把它顶回去。
      //   Plotly 没有「cartesian axis minallowed」这种东西（那是 mapbox/geo 才有），
      //   所以只能靠 relayout 后修正。修正后再 relayout 一次，且
      //   因为修正后 range[0] == 左界（不再 < 左界），**不会无限递归**。
      if (e && e['xaxis.range[0]'] != null && e['xaxis.range[1]'] != null) {
        const a2 = e['xaxis.range[0]'], b2 = e['xaxis.range[1]'];
        const [ca, cb] = clampViewLeft(a2, b2);
        if (ca !== a2) {
          state._leftClampAt = Date.now();
          Plotly.relayout($('main'), { 'xaxis.range': [ca, cb] });
        }
      }
      if (state.replay && e['xaxis.range[0]'] && e['xaxis.range[1]']) {
        const a = e['xaxis.range[0]'], b = e['xaxis.range[1]'];
        const x = DB.data[state.sym].periods[state.period].x;
        const n = x.filter(s => tmap(s) >= tmap(a) && tmap(s) <= tmap(b)).length;
        if (n >= 20) state.replay.win = Math.min(600, n);   // 记住用户偏好
      }
    });
    state._shapeBound = true;
  }

  const seg = isDaily() ? '日线' : (() => {
    const hh = parseInt(xs[xs.length-1].slice(11,13) || 0, 10);
    return hh >= 21 ? '夜盘' : (hh < 12 ? '早盘' : '午盘');
  })();
  // ★ T1.4：#cap 拆两行。原来 100+ 字符挤一行 11px（末尾必被省略号吃掉）。
  //   第一行 = 价格/指标（复盘时要反复对照的数字）；第二行 = 上下文（时段/根数/操作提示）。
  const capL1 = `${xs[xs.length-1]} ｜ 开 ${o[o.length-1].toFixed(1)}  高 ${h[h.length-1].toFixed(1)}  低 ${l[l.length-1].toFixed(1)}  收 ${c[c.length-1].toFixed(1)} ｜ MA13 ${f2(ma[ma.length-1])} ｜ ATR14 ${f2(atr[atr.length-1])}`;
  const capL2 = `${seg} ｜ 加载 ${xs.length} 根 ｜ 拖动/滚轮缩放 · 悬停看该根详情`;
  $('cap').innerHTML = `<div class="cap1">${esc(capL1)}</div><div class="cap2">${esc(capL2)}</div>`;
}
