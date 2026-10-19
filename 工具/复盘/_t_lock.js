// 视图左界锁死 + 回放速度档位 — 离线断言（vm 沙箱 + DOM/Plotly 桩）
// 起因（用户 2026-10-18）：
//   「十月8日之前的k线不能拖动，不然往前拖就没有图，不好看，直接锁死就行，然后其他品种也这样」
//   「回放速度再降低一级，三秒，五秒走一条，我需要思考」
//
// [A] viewMin()：返回该 sym|period「第一根 >= 2026-10-08」的索引与时间戳
// [B] viewMin() 按 sym|period 缓存（同一引用）；换品种/换周期必须换对象
// [C] clampViewLeft()：左端早于 10-08 ⇒ 被顶回 10-08；已在界内 ⇒ 原样返回（引用相等）
// [D] clampViewLeft()：退化范围（b <= 左界）⇒ 保底 3 天宽度，不会出现 0 宽
// [E] 速度档位：HTML 里有 3000 / 5000 两个新选项
// [F] 端到端：renderAll 后主图 xaxis.range[0] 不早于 10-08（六品种 × 三周期全查）
const fs = require('fs'), vm = require('vm');

let srcRaw = fs.readFileSync(process.argv[2], 'utf8');
if (/^\s*<!DOCTYPE/i.test(srcRaw)) {
  const m = [...srcRaw.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  const last = m[m.length - 1][1];
  srcRaw = last.slice(last.indexOf('// ===== core/fmt.js'));
}
const appCode = srcRaw + `
;try{globalThis.__P = {state, viewMin, clampViewLeft, VIEW_MIN_DATE, toDate,
  renderAll: (typeof renderAll === 'function' ? renderAll : null)};}catch(e){globalThis.__P = {__err: e.message};}
`;
const bundle = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

function makeEl(id) {
  const el = {
    id, _html: '', textContent: '', value: '', disabled: false,
    min: '', max: '', title: '', checked: false, className: '', tagName: 'DIV',
    style: {}, dataset: {}, children: [], clientWidth: 1200, clientHeight: 600,
    layout: { xaxis: { range: ['2026-09-15 09:00', '2026-10-16 22:55'] } },
    _ls: {}, _opts: null,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    onclick: null, onchange: null,
    on() {}, appendChild(c) { this.children.push(c); return c; },
    addEventListener(t, cb) { (this._ls[t] = this._ls[t] || []).push(cb); },
    insertAdjacentHTML(pos, html) { this._html += html; },
    contains() { return false; }, closest() { return null; },
    focus() {}, blur() {}, click() {}, remove() {}, scrollIntoView() {},
    querySelector(sel) { return null; },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 1200, height: 600 }; },
    getContext() { return { clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fillRect() {}, set strokeStyle(v) {}, set lineWidth(v) {}, set fillStyle(v) {} }; },
    setAttribute() {}, getAttribute() { return null; },
  };
  return el;
}

const els = {};
const doc = {
  getElementById(id) { return els[id] || (els[id] = makeEl(id)); },
  createElement(tag) { const e = makeEl('__new'); e.tagName = tag; return e; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  body: makeEl('body'),
  documentElement: makeEl('html'),
  hidden: false,
};
const _win = {
  document: doc, devicePixelRatio: 1,
  addEventListener() {}, removeEventListener() {},
  requestAnimationFrame(cb) { return setTimeout(() => cb(performance.now()), 0); },
  cancelAnimationFrame(h) { clearTimeout(h); },
  setTimeout, clearTimeout, setInterval, clearInterval,
  localStorage: { _d: {}, getItem(k) { return this._d[k] ?? null; }, setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } },
  performance: { now: () => Date.now() },
  console, Math, JSON, Date, Intl, Number, String, Object, Array, Boolean,
  isNaN, parseFloat, parseInt, encodeURIComponent, decodeURIComponent,
  navigator: { userAgent: 'node' }, location: { href: 'file:///x', search: '' },
  Blob: function () {}, URL: { createObjectURL() { return ''; }, revokeObjectURL() {} },
  alert() {}, confirm() { return false; },
};
_win.window = _win; _win.globalThis = _win;

const _plotCalls = [];
const Plotly = {
  react(div, traces, layout, cfg) {
    _plotCalls.push({ div, layout });
    const el = typeof div === 'string' ? doc.getElementById(div) : div;
    if (el && layout && layout.xaxis) {
      // 桩要把 xaxis.range 写回 el.layout，否则下游"读回复用视野"永远读不到
      el.layout = { xaxis: { range: layout.xaxis.range ? layout.xaxis.range.slice() : null } };
    }
    return Promise.resolve();
  },
  relayout(div, upd) {
    const el = typeof div === 'string' ? doc.getElementById(div) : div;
    if (el && upd && upd['xaxis.range']) {
      el.layout = el.layout || { xaxis: {} };
      el.layout.xaxis = el.layout.xaxis || {};
      el.layout.xaxis.range = upd['xaxis.range'].slice();
    }
    return Promise.resolve();
  },
  Plots: { resize() {} },
  newPlot: () => Promise.resolve(),
};

const ctx = {
  ..._win, Plotly, console,
};
ctx.globalThis = ctx;
// ★ boot() 的 loadData() 先查 `window.__REVIEW_DATA__`（单文件模式）。
//   注意必须挂在 `window` 对象（= _win）上，不是挂在 ctx 上 ——
//   代码里写的是 `window.__REVIEW_DATA__`，而 ctx.globalThis = ctx ≠ window。
_win.__REVIEW_DATA__ = bundle;
vm.createContext(ctx);

let P = null, bootErr = null;
try {
  vm.runInContext(appCode, ctx, { filename: 'bundle.js' });
  // boot 可能在末尾自动跑；显式再取一次
  P = ctx.__P || (ctx.globalThis && ctx.globalThis.__P);
} catch (e) {
  bootErr = e;
}

let pass = 0, fail = 0;
function chk(name, cond, extra) {
  if (cond) { pass++; console.log('  PASS  ' + name + (extra ? `（${extra}）` : '')); }
  else { fail++; console.log('  FAIL  ' + name + (extra ? `（${extra}）` : '')); }
}

async function main() {
console.log('='.repeat(74));
console.log('视图左界锁死 + 回放速度档位');
console.log('='.repeat(74));

if (bootErr) {
  console.log('  FAIL  boot 抛异常：' + bootErr.message);
  process.exit(1);
}
if (!P || P.__err) {
  console.log('  FAIL  取不到导出符号：' + (P && P.__err));
  process.exit(1);
}

// ★ 等 boot() 的 await loadData() 落地（它是 async 且未 await，见 boot.js 末尾）
await new Promise(r => setTimeout(r, 120));

const { state, viewMin, clampViewLeft, VIEW_MIN_DATE, toDate } = P;
const CUT = Date.parse(VIEW_MIN_DATE + 'T00:00:00');
const syms = Object.keys(bundle).filter(k => k !== '_meta');
const periods = ['5', '15', '1440'];

// ---------- [A] viewMin 基本正确 ----------
(function () {
  state.sym = syms[0]; state.period = '5';
  const vm1 = viewMin();
  const xs = bundle[state.sym].periods['5'].x;
  const t = toDate(xs[vm1.i]).getTime();
  chk('[A] viewMin 指向第一根 >= ' + VIEW_MIN_DATE,
      t >= CUT && (vm1.i === 0 || toDate(xs[vm1.i - 1]).getTime() < CUT),
      `sym=${state.sym} i=${vm1.i} t=${new Date(t).toISOString().slice(0, 16)}`);
  chk('[A] viewMin.t 与索引一致', Math.abs(vm1.t - t) < 1000);
})();

// ---------- [B] 缓存语义 ----------
(function () {
  state.sym = syms[0]; state.period = '5';
  const a = viewMin(), b = viewMin();
  chk('[B] 同 sym|period 返回同一对象（缓存生效）', a === b);
  state.period = '15';
  const c = viewMin();
  chk('[B] 换周期换对象', c !== a, `5min i=${a.i} / 15min i=${c.i}`);
  const s2 = syms[1];
  state.sym = s2; state.period = '5';
  const d = viewMin();
  chk('[B] 换品种换对象', d.t !== undefined && (c.i !== d.i || c.t !== d.t),
      `${syms[0]}.5 vs ${s2}.5`);
})();

// ---------- [C] 钳制：越界被顶回，界内原样 ----------
(function () {
  state.sym = syms[0]; state.period = '5';
  const v = viewMin();
  const tooEarly = ['2026-09-15 09:00', '2026-10-01 10:00'];
  const [ca, cb] = clampViewLeft(tooEarly[0], tooEarly[1]);
  chk('[C] 越界左端被顶回 ' + VIEW_MIN_DATE,
      toDate(ca).getTime() >= v.t,
      `${tooEarly[0]} → ${new Date(toDate(ca)).toISOString().slice(0, 16)}`);
  // 界内：原样返回（引用相同，便于调用方判断"没改动"）
  const inA = '2026-10-10 09:00', inB = '2026-10-12 15:00';
  const r2 = clampViewLeft(inA, inB);
  chk('[C] 界内原样返回', r2[0] === inA && r2[1] === inB);
})();

// ---------- [D] 退化范围保底宽度 ----------
(function () {
  state.sym = syms[0]; state.period = '5';
  const v = viewMin();
  // b 比左界还早 ⇒ 必须给保底跨度，不能 0 宽
  const [ca, cb] = clampViewLeft('2026-09-15 09:00', '2026-09-16 09:00');
  const span = toDate(cb).getTime() - toDate(ca).getTime();
  chk('[D] 退化范围有保底跨度（>=1天）', span >= 86400000,
      `跨度 ${(span / 86400000).toFixed(2)} 天`);
  chk('[D] 左端仍是左界', Math.abs(toDate(ca).getTime() - v.t) < 1000);
})();

// ---------- [E] 速度档位 ----------
(function () {
  const html = fs.readFileSync(process.argv[4] || 'src/index.html', 'utf8');
  chk('[E] 有 3000ms 档位', /value="3000"/.test(html));
  chk('[E] 有 5000ms 档位', /value="5000"/.test(html));
  chk('[E] 保留原 600ms 默认', /value="600"\s+selected/.test(html));
})();

// ---------- [F] 端到端：renderAll 后主图 range[0] 不早于左界 ----------
(function () {
  let bad = [];
  for (const s of syms) {
    for (const p of periods) {
      state.sym = s; state.period = p; state.range = 'ALL'; state.sel = null;
      state._lastViewKey = null;
      _plotCalls.length = 0;
      try {
        if (P.renderAll) P.renderAll();
        else if (typeof ctx.renderAll === 'function') ctx.renderAll();
      } catch (e) {
        bad.push(`${s}/${p} renderAll 抛错 ${e.message}`);
        continue;
      }
      const mainCall = _plotCalls.filter(c => c.div === 'main').pop();
      if (!mainCall || !mainCall.layout || !mainCall.layout.xaxis) continue;
      const r = mainCall.layout.xaxis.range;
      if (!r || r.length < 2) continue;
      const t0 = toDate(r[0]).getTime();
      if (t0 < CUT - 1000) bad.push(`${s}/${p} 左端 ${r[0]}`);
    }
  }
  chk('[F] 六品种 × 三周期：主图左端都不早于 ' + VIEW_MIN_DATE,
      bad.length === 0, bad.length ? bad.join('; ') : 'all ok');
})();

console.log('='.repeat(74));
console.log(fail === 0 ? `全部通过 ✅（${pass} 项）` : `失败 ${fail} 项 / 通过 ${pass} 项 ❌`);
process.exit(fail === 0 ? 0 : 1);
}
main().catch(e => { console.log('  FAIL  harness 抛错：' + (e && e.stack || e)); process.exit(1); });
