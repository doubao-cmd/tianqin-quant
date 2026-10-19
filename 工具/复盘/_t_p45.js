// P4 + P5 批次 harness：vm 沙箱 + DOM/Plotly 桩
// [A] breaks() 缓存：同 sym|period 返回同一数组（引用相等）；换周期换对象；换回再算一次（单槽）
// [B] safeResize()：尺寸指纹没变 → 不 resize；变了 → 主图+分时各 1 次；rAF 内合并（连点两次只 1 次）
// [C] P4 视野继承：切周期前捕获中心/跨度 → 切后 xr 不是全图，且中心对齐；_keepCenter 只消费一次
// [D] P5-3 marker 合并：成交标记 trace 数 = 2（光晕+实心），且 marker.symbol 是数组、四种符号都出现
// [E] 回归：切品种不继承视野（_keepCenter 置 null）；训练模式仍不画成交标记
const fs = require('fs'), vm = require('vm');

let srcRaw = fs.readFileSync(process.argv[2], 'utf8');
if (/^\s*<!DOCTYPE/i.test(srcRaw)) {
  const m = [...srcRaw.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  const last = m[m.length - 1][1];
  srcRaw = last.slice(last.indexOf('// ===== core/fmt.js'));
}
// ★ 拼接后的 bundle 是单个 <script>：`const` 声明不会挂到 global 上（只有 function 会）
//   ⇒ 末尾补一句把需要的符号显式导出到 globalThis，供 harness 取用
const appCode = srcRaw + `
;try{globalThis.__P = {state, setContext, safeResize, breaks, setMode, canSeeAnswers,
  renderAll: (typeof renderAll === 'function' ? renderAll : null)};}catch(e){globalThis.__P = {__err: e.message};}
`;
const bundle = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

function makeEl(id) {
  const el = {
    id, _html: '', textContent: '', value: '', disabled: false,
    min: '', max: '', title: '', checked: false, className: '', tagName: 'DIV',
    style: {}, dataset: {}, children: [], clientWidth: 1200, clientHeight: 600,
    layout: { xaxis: { range: ['2026-09-15 09:00', '2026-10-16 22:55'] } },
    _ls: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    onclick: null, onchange: null,
    on() {}, appendChild(c) { this.children.push(c); return c; },
    addEventListener(t, cb) { (this._ls[t] = this._ls[t] || []).push(cb); },
    insertAdjacentHTML(pos, html) { this._html += html; },
    contains() { return false; }, closest() { return null; },
    focus() {}, blur() {}, click() {}, remove() {},
    querySelector(sel) {
      if (this.id === 'tbody' && sel === 'tbody') { if (!this._inner) this._inner = makeEl('__inner_tbody'); return this._inner; }
      return null;
    },
    querySelectorAll() { return []; },
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(v) { this._html = String(v); if (this.id === 'tbody' && this._inner) this._inner._html = ''; },
  });
  return el;
}

function mkEnv(LS) {
  const els = {};
  const document = {
    getElementById(id) { return els[id] || (els[id] = makeEl(id)); },
    createElement(tag) { const e = makeEl('_c' + Math.random()); e.tagName = String(tag || 'div').toUpperCase(); return e; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, activeElement: { tagName: 'BODY' }, hidden: false, body: makeEl('body'),
  };
  const resizeCalls = [];
  const Plotly = {
    _calls: [],
    // 真实 Plotly 会把生效的 layout 写回 gd.layout（div.layout.xaxis.range）
    // ⇒ 桩必须同步，否则"二次渲染读回范围"这条测不出来
    react(div, traces, layout) {
      this._calls.push({ div, traces, layout });
      const el = document.getElementById(div);
      if (el && layout && layout.xaxis && layout.xaxis.range) {
        el.layout = { xaxis: { range: layout.xaxis.range.slice() } };
      }
    },
    newPlot() {},
    Plots: { resize(id) { resizeCalls.push(id); } },
  };
  const Blob = function (parts, opts) { this.parts = parts; this.type = (opts || {}).type; };
  const URL = { createObjectURL() { return 'blob:fake'; }, revokeObjectURL() {} };
  const rafQ = [];
  const sandbox = {
    window: { __REVIEW_DATA__: bundle, addEventListener() {} },
    document, Plotly, localStorage: LS, Blob, URL,
    performance: { now() { return 0; } },
    requestAnimationFrame(cb) { rafQ.push(cb); return rafQ.length; },
    cancelAnimationFrame() {},
    fetch() { return Promise.resolve({ json() { return Promise.resolve({}); } }); },
    setTimeout(cb) { try { cb(0); } catch (e) {} return 0; },
    clearTimeout() {}, setInterval() { return 0; }, clearInterval() {}, console,
  };
  vm.createContext(sandbox);
  try { vm.runInContext(appCode, sandbox); } catch (e) { console.log('LOAD ERROR:', e.message, e.stack); process.exit(1); }
  const drainRaf = () => { const q = rafQ.splice(0); q.forEach(cb => cb(0)); };
  return { sandbox, els, Plotly, resizeCalls, drainRaf, rafQ };
}
const mkLS = () => ({ _d: {}, getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; }, setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; }, clear() { this._d = {}; } });

const flush = async () => { for (let i = 0; i < 4; i++) await new Promise(r => setImmediate(r)); };
let fail = 0;
const ok = (c, m) => { console.log((c ? '  ✓ ' : '  ✗ ') + m); if (!c) fail++; };

(async () => {
  const env = mkEnv(mkLS());
  await flush();
  const S = env.sandbox;
  const E = id => S.document.getElementById(id);
  const P = S.__P;
  if (!P || P.__err) { console.log('probe 失败:', P && P.__err); process.exit(1); }

  // ═══ [A] breaks() 缓存 ═══
  console.log('[A] breaks() 按 sym|period 缓存');
  const b1 = P.breaks();
  const b2 = P.breaks();
  ok(b1 === b2, '同上下文连续两次 → 同一数组引用（命中缓存）');
  const n1 = b1.length;
  P.state.period = '15';
  const b3 = P.breaks();
  ok(b3 !== b1, '切到 15min → 返回新数组（未命中）');
  P.state.period = '5';
  const b4 = P.breaks();
  ok(b4 !== b1, '切回 5min → 仍是新数组（单槽缓存，只保最近一次）');
  ok(b4.length === n1, `5min 结构一致：${n1} 段 vs ${b4.length} 段`);
  P.state.sym = 'P';
  const b5 = P.breaks();
  ok(b5 !== b4, '换品种 → 新数组');
  ok(JSON.stringify(b5) !== '[]' && b5[0].bounds.join() === 'sat,mon', '周末段仍在（结构未被缓存写坏）');
  P.state.sym = 'M';
  P.breaks();

  // ═══ [B] safeResize 指纹 ═══
  console.log('[B] safeResize 尺寸指纹');
  P.state._rszKey = null;
  env.resizeCalls.length = 0;
  E('main').clientWidth = 1200; E('main').clientHeight = 600;
  E('intra').clientWidth = 1200; E('intra').clientHeight = 190;
  P.safeResize();
  ok(env.rafQ.length === 1, 'rAF 已排队 1 帧（未同步执行）');
  env.drainRaf();
  ok(env.resizeCalls.length === 2, `首次 → 主图+分时各 1 次（got ${env.resizeCalls.length}）`);
  ok(env.resizeCalls[0] === 'main' && env.resizeCalls[1] === 'intra', '顺序 main → intra');
  env.resizeCalls.length = 0;
  P.safeResize(); env.drainRaf();
  ok(env.resizeCalls.length === 0, '尺寸没变 → 一次都不 resize（指纹跳过）');
  E('main').clientHeight = 700;
  P.safeResize(); env.drainRaf();
  ok(env.resizeCalls.length === 2, '高度变了 → 重新 resize');
  E('main').clientHeight = 710;          // 再改一次尺寸，否则指纹不变本来就该跳过
  env.resizeCalls.length = 0;
  P.safeResize(); P.safeResize(); P.safeResize();
  ok(env.rafQ.length === 1, '连点三次只排 1 帧（_rszPending 合并）');
  env.drainRaf();
  ok(env.resizeCalls.length === 2, '合并后只 resize 一轮（2 次调用）');

  // ═══ [C] P4 视野继承 ═══
  console.log('[C] P4 切周期保持视野中心/跨度');
  P.setContext({ sym: 'M', period: '5', range: 'ALL' });
  await flush();
  const M5 = bundle.M.periods['5'];
  const X5 = M5.x;
  // 手工把"用户当前视野"设成中间 60 根
  // ★ 2026-10-18 改：原用 400/460，但这两根在 2026-10-08 **之前**。
  //   视图左界锁死后（用户要求"10月8日之前不能拖"），那种视野会被顶回 10-08，
  //   中心自然对不上 ⇒ 这不是回归，是测试前提过期。改成 10-08 之后的一段。
  //   （5min 里 10-08 的索引是 viewMin().i ≈ 711）
  const a = 750, b = 810;
  E('main').layout = { xaxis: { range: [X5[a], X5[b]] } };
  const cMs = (new Date(X5[a].replace(' ', 'T')).getTime() + new Date(X5[b].replace(' ', 'T')).getTime()) / 2;
  P.setContext({ period: '15' });
  await flush();
  const kc = P.state._keepCenter;
  ok(kc === null, '_keepCenter 已被 renderMain 消费（置 null）');
  const mainCalls = env.Plotly._calls.filter(c => c.div === 'main');
  const call = mainCalls[mainCalls.length - 1];
  const xr = (call && call.layout && call.layout.xaxis && call.layout.xaxis.range) || [];
  ok(xr[0] !== X5[0] && xr[1] !== X5[X5.length - 1], `切到 15min 后 xr 不是全图（${xr[0]} ~ ${xr[1]}）`);
  const X15 = bundle.M.periods['15'].x;
  const d0 = new Date(xr[0].replace(' ', 'T')).getTime(), d1 = new Date(xr[1].replace(' ', 'T')).getTime();
  const cAfter = (d0 + d1) / 2;
  const driftMin = Math.abs(cAfter - cMs) / 60000;
  ok(driftMin <= 15, `中心漂移 ${driftMin.toFixed(1)} 分钟 ≤ 1 根 15min（跨度对齐到 bar 边界）`);
  ok(X15.indexOf(xr[0]) >= 0 && X15.indexOf(xr[1]) >= 0, 'xr 两端都落在 15min 的 bar 时间点上');
  ok(P.state._lastViewKey === `M|15|ALL|${P.state.sel}`, `_lastViewKey 已重打为切后的上下文（${P.state._lastViewKey}）`);

  // 消费一次后，再渲染不会又跳一次
  const before = env.Plotly._calls.length;
  P.renderAll ? P.renderAll() : null;
  await flush();
  if (env.Plotly._calls.length > before) {
    const mc = env.Plotly._calls.filter(c => c.div === 'main');
    const c2 = mc[mc.length - 1];
    ok(JSON.stringify(c2.layout.xaxis.range) === JSON.stringify(xr), '再次渲染沿用同一范围（不跳回全图）');
  } else { ok(true, 'renderAll 未导出，跳过二次渲染检查'); }

  // ═══ [E] 切品种不继承 ═══
  console.log('[E] 切品种不继承视野 / 训练模式不画成交标记');
  E('main').layout = { xaxis: { range: [X5[a], X5[b]] } };
  P.setContext({ sym: 'P' });
  await flush();
  ok(P.state._keepCenter === null, '切品种 → _keepCenter 置 null（不继承）');
  P.setContext({ sym: 'M', period: '5', range: 'ALL' });
  await flush();

  // ═══ [D] marker 合并 ═══
  console.log('[D] P5-3 成交标记合并为 2 个 trace');
  const mc2 = env.Plotly._calls.filter(c => c.div === 'main');
  const mCall = mc2[mc2.length - 1];
  const mkTr = mCall.traces.filter(t => t.type === 'scatter' && t.mode && String(t.mode).indexOf('markers') >= 0
    && t.marker && Array.isArray(t.marker.symbol));
  ok(mkTr.length === 2, `成交标记 trace = 2（光晕+实心），got ${mkTr.length}`);
  const solid = mkTr.find(t => t.mode === 'markers+text');
  ok(!!solid, '实心层存在（markers+text，带 多/空 文字）');
  if (solid) {
    ok(Array.isArray(solid.marker.symbol) && Array.isArray(solid.marker.color) && Array.isArray(solid.marker.size),
      'symbol/color/size 三者都是数组（一 trace 画四类标记）');
    const uniq = [...new Set(solid.marker.symbol)];
    ok(uniq.length >= 2 && uniq.every(s => s === 'triangle-up' || s === 'triangle-down' || s === 'x'),
      `出现的符号都在白名单内：${uniq.join(' / ')}`);
    ok(solid.x.length === solid.y.length && solid.x.length === solid.marker.symbol.length,
      `x/y/symbol 长度一致（${solid.x.length} 个事件）`);
    ok(String(solid.hovertemplate).indexOf('customdata[0]') >= 0, 'hovertemplate 仍带手数/价格（未因合并丢 hover）');
  }
  // 训练模式
  P.setMode('train');
  await flush();
  const mc3 = env.Plotly._calls.filter(c => c.div === 'main');
  const tCall = mc3[mc3.length - 1];
  const tMk = tCall.traces.filter(t => t.marker && Array.isArray(t.marker.symbol));
  ok(tMk.length === 0, `训练模式成交标记 trace = 0（got ${tMk.length}）`);
  P.setMode('review');
  await flush();

  console.log(fail === 0 ? '\n全部通过' : `\n失败 ${fail} 项`);
  process.exit(fail === 0 ? 0 : 1);
})();
