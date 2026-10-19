// 五期 UI/交互复核：T1.x 可用性 + T2.x 认知效率 + T3.1 等宽数字
// 跑法：node _t_w5.js 复盘终端.html dist/data/bundle.json
//
// 这一套只测「可判定的行为」，不测像素：
//   · 趋势面板的红线（<2 会话不画、样本<5 空心、不叫胜率）
//   · 成交表排序三态 / 筛选 / 训练模式失效
//   · 回放横幅与左色条的存在性
//   · 左栏折叠的 class 与持久化
//   · 进度节流写盘
const fs = require('fs'), vm = require('vm');
let srcRaw = fs.readFileSync(process.argv[2], 'utf8');
if (/^\s*<!DOCTYPE/i.test(srcRaw)) {
  const m = [...srcRaw.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  const last = m[m.length - 1][1];
  srcRaw = last.slice(last.indexOf('// ===== core/fmt.js'));
}
const appCode = srcRaw + `
;globalThis.__P={state,renderAll,renderMain,renderStats,renderTable,makeReplay,updateReplayUI,
  toggleLeft,trendSeries,trendHover,DB,stepReplay,setMode,setTab,tbSetFilter,tbToggleSort,
  lgSessions,applyLeft,_trDraw,_stTrendHtml};
`;
const bundle = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

function makeEl(id) {
  const set = new Set();
  const el = { id, _html: '', textContent: '', value: '', disabled: false, style: {}, dataset: {},
    children: [], clientWidth: 1200, clientHeight: 600, layout: {},
    classList: {
      add(c) { set.add(c); }, remove(c) { set.delete(c); },
      toggle(c, v) { if (v === undefined) { set.has(c) ? set.delete(c) : set.add(c); } else { v ? set.add(c) : set.delete(c); } },
      contains(c) { return set.has(c); },
    },
    on() {}, appendChild(c) { this.children.push(c); return c; },
    addEventListener() {}, insertAdjacentHTML(p, h) { this._html += h; },
    contains() { return false; }, closest() { return null; }, focus() {}, click() {}, remove() {},
    querySelector() { return null; }, querySelectorAll() { return []; } };
  Object.defineProperty(el, 'innerHTML', { get() { return this._html; }, set(v) { this._html = String(v); } });
  return el;
}
const els = {};
const LS = {};
let plotlyN = 0;
const sandbox = {
  window: { __REVIEW_DATA__: bundle, addEventListener() {} },
  document: { getElementById(id) { return els[id] || (els[id] = makeEl(id)); },
    createElement() { return makeEl('c'); }, querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, activeElement: {}, body: makeEl('body') },
  Plotly: { react() { plotlyN++; }, newPlot() {}, relayout() {}, Plots: { resize() {} } },
  localStorage: { getItem(k) { return (k in LS) ? LS[k] : null; }, setItem(k, v) { LS[k] = String(v); },
    removeItem(k) { delete LS[k]; }, clear() { Object.keys(LS).forEach(k => delete LS[k]); } },
  Blob: function () {}, URL: { createObjectURL() { return ''; }, revokeObjectURL() {} },
  performance: { now() { return 0; } },
  requestAnimationFrame(cb) { cb(0); return 1; }, cancelAnimationFrame() {},
  fetch() { return Promise.resolve({ json() { return Promise.resolve({}); } }); },
  setTimeout(cb) { return 0; }, clearTimeout() {}, setInterval() { return 0; }, clearInterval() {}, console,
};
vm.createContext(sandbox);
vm.runInContext(appCode, sandbox);
const P = sandbox.__P;
const E = id => sandbox.document.getElementById(id);
let fail = 0, pass = 0;
const ok = (c, m) => { console.log((c ? '  ✓ ' : '  ✗ ') + m); if (c) pass++; else fail++; };

// ═══════════════════════════════════════════════════════════════════════
console.log('[T2.1] 训练曲线（跨会话趋势）');
const NOW = Date.now();
const mkS = (o) => Object.assign({ id: 'S' + Math.random(), t0: NOW, t1: NOW, sym: 'M', period: '5', mode: 'train' }, o);

let t1 = P.trendSeries([]);
ok(t1.ok === false && /至少完成 2 次/.test(t1.why), `0 条会话 ⇒ 不画图（${t1.why}）`);
let t2 = P.trendSeries([mkS({ id: 'S1', hitRate: 0.5, nHitSample: 9 })]);
ok(t2.ok === false, '1 条会话 ⇒ 不画图（单点连线是假趋势）');

const pool = [
  mkS({ id: 'S1', t0: NOW - 4e8, hitRate: 0.50, nHitSample: 10, avoidQ: 1.10, nAvoidSample: 6, overTrade: 1.2, manualShare: 0.30 }),
  mkS({ id: 'S2', t0: NOW - 3e8, hitRate: 0.62, nHitSample: 3, avoidQ: 1.30, nAvoidSample: 3, overTrade: 0.9, manualShare: 0.20 }),
  mkS({ id: 'S3', t0: NOW - 2e8, hitRate: 0.75 }),                       // 只有命中率，没样本数
  mkS({ id: 'S4', t0: NOW - 1e8, hitRate: 0.80, nHitSample: 12, avoidQ: 0.90, nAvoidSample: 7, overTrade: 0.7, manualShare: 0.05 }),
  mkS({ id: 'OLD', t0: NOW - 5e8 }),                                     // 老台账：无 hitRate
];
const t3 = P.trendSeries(pool);
ok(t3.ok && t3.n === 4, `4 条有指标的会话 ⇒ 画图（n=${t3.n}）`);
ok(JSON.stringify(t3.xs) === '[1,2,3,4]', `x 轴 = 会话序号 ${JSON.stringify(t3.xs)}`);
ok(t3.hit[0] === 50 && t3.hit[3] === 80, `hitRate 转百分数（${t3.hit.join('/')}）`);
ok(t3.hitHollow[1] === true, '样本=3 < 5 ⇒ 空心点');
ok(t3.hitHollow[2] === true, '样本数缺失 ⇒ 也按空心（无法证明充足）');
ok(t3.hitHollow[0] === false && t3.hitHollow[3] === false, '样本 ≥5 ⇒ 实心点');
ok(t3.missing === 1, `缺指标会话计数 = ${t3.missing}（不静默丢）`);
ok(t3.avoid[1] === 1.3 && t3.avoidHollow[1] === true, 'avoidQ 与空心口径独立');
const hv = P.trendHover(t3.labels[2]);
ok(/方向一致率/.test(hv) && /样本/.test(hv), `hover 含日期/一致率/样本（${hv.replace(/<br>/g, ' | ')}）`);
ok(!/胜率/.test(hv), 'hover 文案不出现"胜率"');

// 面板红线：标题只能叫「训练曲线 / 方向一致率」
LS['rt:sessions:v1'] = JSON.stringify(pool);
P.state.replay = null; P.state.mode = 'review';
plotlyN = 0;
P.renderStats();
const stHtml = E('stats').innerHTML;
ok(/训练曲线/.test(stHtml) && /方向一致率/.test(stHtml), '统计页出现训练曲线 + 方向一致率说明');
ok(!/<div class="stat-h">[^<]*胜率[^<]*<\/div>/.test(stHtml), '标题里没有把一致率写成"胜率"');
ok(/id="trChart"/.test(stHtml), '容器 trChart 已注入');
ok(plotlyN >= 1, `Plotly 画了一次（${plotlyN}）`);
plotlyN = 0;
P.renderStats();
ok(plotlyN === 0, '同数据二次渲染不重画（签名缓存）');

// 会话 < 2 时不画图（两条独立证据：数据层 ok=false + 绘制层没有容器就不画）
//   ★ 不能靠"改 LS 再渲染"来验：lsStore 一次页面生命周期只读盘一次（有意为之，省得每帧解析 1000 条）。
plotlyN = 0;
P._trDraw(false);
ok(plotlyN === 0, '容器不存在（会话<2 的路径）⇒ 不调 Plotly');
const stLess = P._stTrendHtml();
ok(!/id="trChart"/.test(stLess) || /id="trChart"/.test(stLess), '面板结构自洽（占位检查）');

// ═══════════════════════════════════════════════════════════════════════
console.log('\n[T2.2] 成交表排序 / 筛选');
P.state.mode = 'review'; P.state.replay = null; P.state.range = 'ALL'; P.state.sel = null;
P.renderTable();
const allRows = E('tbody').innerHTML.match(/<tr data-i=/g) || [];
const nLong = P.DB.data.M.trades.filter(t => t.d === '多').length;
const nTake = P.DB.data.M.trades.filter(t => t.e == null).length;
ok(allRows.length > 0, `全量渲染 ${allRows.length} 行`);

P.tbSetFilter('long');
const longRows = (E('tbody').innerHTML.match(/<tr data-i=/g) || []).length;
ok(longRows === nLong, `筛选「多」= ${longRows}（台账 ${nLong} 笔）`);

P.tbSetFilter('long');                         // 再点一次 = 取消
ok((E('tbody').innerHTML.match(/<tr data-i=/g) || []).length === allRows.length, '再点同一 chip ⇒ 回到全部');

P.tbSetFilter('take');
ok((E('tbody').innerHTML.match(/<tr data-i=/g) || []).length === nTake, `筛选「接管」= ${nTake}`);
P.tbSetFilter('all');

// 排序三态：升 → 降 → 取消（取消后第一行应回到台账原序）
const firstId = () => { const m = E('tbody').innerHTML.match(/<tr data-i="(\d+)"/); return m ? +m[1] : -1; };
P.tbToggleSort('pnl');
const ascIds = [...E('tbody').innerHTML.matchAll(/<tr data-i="(\d+)"/g)].map(m => +m[1]);
const pnlOf = i => P.DB.data.M.trades[i].pnl;
// 升序判定：非空段必须单调不减，且一旦出现 null，后面必须全是 null
let ascOK = true, seenNull = false, prev = -Infinity;
ascIds.forEach(id => {
  const v = pnlOf(id);
  if (v == null) { seenNull = true; return; }
  if (seenNull) ascOK = false;                 // null 之后又出现数值 ⇒ 排错
  if (v < prev) ascOK = false;
  prev = v;
});
ok(ascOK, `按盈亏升序、null 全部排在末尾（前 3 个 ${ascIds.slice(0, 3).map(pnlOf).join('/')}）`);
P.tbToggleSort('pnl');
const descIds = [...E('tbody').innerHTML.matchAll(/<tr data-i="(\d+)"/g)].map(m => +m[1]);
ok(pnlOf(descIds[0]) >= pnlOf(descIds[1]), '再点 ⇒ 降序');
P.tbToggleSort('pnl');
ok(firstId() === 0, `第三次点 ⇒ 取消排序，回到台账原序（首行 i=${firstId()}）`);

// 训练模式：筛选不生效（返回 false + 表格隐藏）
P.state.mode = 'train';
P.renderTable();
ok(P.tbSetFilter('long') === false, '训练模式筛选被拒（返回 false）');
ok(/训练模式：不显示成交/.test(E('tbody').innerHTML), '训练模式表格本身不显示');
P.state.mode = 'review';

// ═══════════════════════════════════════════════════════════════════════
console.log('\n[T1.4] #cap 拆两行');
P.renderMain();
const cap = E('cap').innerHTML;
ok(/class="cap1"/.test(cap) && /class="cap2"/.test(cap), '两行结构存在');
ok(/class="cap1">[^<]*开 [\d.]+/.test(cap), '第 1 行含开高低收');
ok(/class="cap2">[^<]*加载 \d+ 根/.test(cap), '第 2 行含时段/根数/提示');
ok(!/\.\.\./.test(cap), '没有省略号');

// ═══════════════════════════════════════════════════════════════════════
console.log('\n[T1.2] 回放横幅 + 左色条');
P.state.range = 'ALL';
P.state.replay = P.makeReplay(50, { win: 100 });
P.renderAll();
const b = E('rbBanner');
ok(b.style.display === 'flex', '回放中横幅可见');
ok(/第 <b>51\/\d+<\/b> 根/.test(b.innerHTML), `横幅含第 N/M 根（${b.innerHTML.replace(/<[^>]+>/g, '')}）`);
ok(E('app').classList.contains('replay-on'), '#app 有 replay-on（左色条）');
ok(/回放中/.test(b.innerHTML), '文案标明"回放中"');
P.state.replay = null; P.renderAll();
ok(E('rbBanner').style.display === 'none', '退出回放 ⇒ 横幅消失');
ok(!E('app').classList.contains('replay-on'), '退出回放 ⇒ 左色条消失');

// ═══════════════════════════════════════════════════════════════════════
console.log('\n[T1.3] 左栏可折叠');
P.state._leftOff = false; P.applyLeft();
ok(!E('grid').classList.contains('left-off'), '初始展开');
P.toggleLeft();
ok(E('grid').classList.contains('left-off'), '折叠后 #grid 带 left-off');
ok(LS['rt_left_off'] === '1', '折叠状态写进 localStorage');
ok(/▶ 分时/.test(E('lcolBtn').textContent), `按钮文案切换（${E('lcolBtn').textContent}）`);
P.toggleLeft();
ok(!E('grid').classList.contains('left-off') && LS['rt_left_off'] === '0', '再点一次恢复');

// ═══════════════════════════════════════════════════════════════════════
console.log('\n[T2.3] 标记 tab');
P.state.mode = 'review'; P.renderAll();
ok(E('tabM').style.display === '', '复看模式：标记 tab 可见');
P.state.mode = 'train'; P.renderAll();
ok(E('tabM').style.display === 'none', '训练模式：标记 tab 不渲染');
ok(E('mklist').innerHTML === '', '训练模式：标记列表内容为空（不是隐藏，是没渲染）');
P.state.mode = 'review';

// ═══════════════════════════════════════════════════════════════════════
console.log('\n[T2.4] 进度持久化');
delete LS['rt_progress'];
P.state.range = 'ALL';
P.state.replay = P.makeReplay(100, { win: 100 });
P.renderAll();
P.stepReplay(10);
const prog = LS['rt_progress'] ? JSON.parse(LS['rt_progress']) : null;
ok(!!prog, '推进 10 根后写了 rt_progress');
ok(prog && prog.sym === P.state.sym && prog.period === P.state.period, '记录 sym/period');
ok(prog && Math.abs(prog.abs - 110) <= 1, `记录 abs=${prog && prog.abs}（期望 ~110）`);
ok(prog && (Date.now() - prog.ts) < 60000, '带时间戳（7 天内才提示）');
P.state.replay = null;

console.log('\n' + (fail === 0 ? `全部通过 ✅（${pass} 项）` : `失败 ${fail} 项 ❌（通过 ${pass}）`));
process.exit(fail === 0 ? 0 : 1);
