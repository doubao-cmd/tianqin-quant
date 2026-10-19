// S5 UI 批次 harness：A20 风险贡献面板 / A21 EB 清除 / 数据新鲜度角标
// 起因（用户 2026-10-18）：「现在做的是复盘工具吗，复盘工具还有多少才能完成第四期」
//   ⇒ 四期验收表 A20/A21 + S4 的 freshness 必须在工具里真正可见，不能只在 Python 层通过。
//
// [A] A21：bundle 里没有 EB（品种层 + by_symbol 统计都不许有）
// [B] A21：_meta.excluded_symbols 记录 EB（避免以后有人问"EB 呢"）
// [C] A20：_meta.risk 结构完整（params/totals/legs/note）
// [D] A20：risk 面板「完整性」标记正确（池子腿数 vs bundle 腿数）
// [E] A20：参考手数复算一致（用 bundle 里的 atr/price/mult/tick 按 RiskParSizer 公式验）
// [F] A20：手数必须落在 [hand_min, hand_max] 且名义不超 cap
// [G] freshness：角标文本存在，且 stale 时带 stale class
// [H] freshness：缺 _meta.freshness 必须显示告警（不许留空 = 失败隐身）
const fs = require('fs'), vm = require('vm');

let srcRaw = fs.readFileSync(process.argv[2], 'utf8');
if (/^\s*<!DOCTYPE/i.test(srcRaw)) {
  const m = [...srcRaw.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  const last = m[m.length - 1][1];
  srcRaw = last.slice(last.indexOf('// ===== core/fmt.js'));
}
const appCode = srcRaw + `
;try{globalThis.__P = {state, DB, renderAll, renderStats, renderFreshness,
  renderTop: (typeof renderTop==='function'?renderTop:null)};}catch(e){globalThis.__P={__err:e.message};}
`;
const bundle = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

function makeEl(id) {
  const el = {
    id, _html: '', textContent: '', value: '', disabled: false, className: '',
    min: '', max: '', title: '', checked: false, tagName: 'DIV',
    style: {}, dataset: {}, children: [], clientWidth: 1200, clientHeight: 600,
    layout: { xaxis: { range: ['2026-10-08 09:00', '2026-10-16 22:55'] } },
    _ls: {},
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
                 toggle() {}, contains(c) { return this._s.has(c); } },
    onclick: null, onchange: null,
    on() {}, appendChild(c) { this.children.push(c); return c; },
    addEventListener(t, cb) { (this._ls[t] = this._ls[t] || []).push(cb); },
    set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html; },
    insertAdjacentHTML(p, h) { this._html += h; },
    contains() { return false; }, closest() { return null; },
    focus() {}, blur() {}, click() {}, remove() {}, scrollIntoView() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
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
  querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener() {}, body: makeEl('body'), documentElement: makeEl('html'), hidden: false,
};
const _win = {
  document: doc, devicePixelRatio: 1,
  addEventListener() {}, removeEventListener() {},
  requestAnimationFrame(cb) { return setTimeout(() => cb(Date.now()), 0); },
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
_win.__REVIEW_DATA__ = bundle;

const Plotly = {
  react(div, traces, layout) {
    const el = typeof div === 'string' ? doc.getElementById(div) : div;
    if (el && layout && layout.xaxis) {
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
  Plots: { resize() {} }, newPlot: () => Promise.resolve(),
};

const ctx = { ..._win, Plotly, console };
ctx.globalThis = ctx;
vm.createContext(ctx);

let pass = 0, fail = 0;
function chk(name, cond, extra) {
  if (cond) { pass++; console.log('  PASS  ' + name + (extra ? `（${extra}）` : '')); }
  else { fail++; console.log('  FAIL  ' + name + (extra ? `（${extra}）` : '')); }
}

async function main() {
  console.log('='.repeat(76));
  console.log('S5：A20 风险贡献 / A21 EB 清除 / 数据新鲜度');
  console.log('='.repeat(76));

  let bootErr = null;
  try { vm.runInContext(appCode, ctx, { filename: 'bundle.js' }); }
  catch (e) { bootErr = e; }
  if (bootErr) { console.log('  FAIL  boot 抛错：' + bootErr.message); process.exit(1); }
  const P = ctx.__P || ctx.globalThis.__P;
  if (!P || P.__err) { console.log('  FAIL  取不到符号：' + (P && P.__err)); process.exit(1); }
  await new Promise(r => setTimeout(r, 120));

  const { state, DB } = P;
  const meta = DB.meta || {};

  // ---------- [A] A21：bundle 里没有 EB ----------
  (function () {
    const syms = Object.keys(bundle).filter(k => k !== '_meta');
    chk('[A] bundle 无 EB', !syms.includes('EB'), `品种: ${syms.join(',')}`);
    const byS = ((DB.data[state.sym] || {}).stats_all || {}).by_symbol || [];
    chk('[A] by_symbol 统计无 EB', !byS.some(x => x.key === 'EB'));
  })();

  // ---------- [B] 记录 excluded_symbols ----------
  (function () {
    chk('[B] _meta.excluded_symbols 含 EB',
        Array.isArray(meta.excluded_symbols) && meta.excluded_symbols.includes('EB'),
        JSON.stringify(meta.excluded_symbols));
  })();

  // ---------- [C] A20：risk 结构 ----------
  const r = meta.risk || {};
  (function () {
    chk('[C] _meta.risk 存在', !!meta.risk);
    chk('[C] params 完整', ['equity_base', 'risk_pct', 'hand_min', 'hand_max', 'notional_cap']
        .every(k => r.params && r.params[k] != null), JSON.stringify(r.params));
    chk('[C] legs 非空', Array.isArray(r.legs) && r.legs.length > 0, (r.legs || []).length + ' 腿');
    chk('[C] note 声明「不参与下单」', /不参与下单/.test(r.note || ''));
  })();

  // ---------- [D] 完整性标记 ----------
  (function () {
    const t = r.totals || {};
    const miss = r.missing_from_bundle || [];
    chk('[D] totals 有 pool_n / complete',
        t.pool_n != null && typeof t.complete === 'boolean',
        `pool_n=${t.pool_n} complete=${t.complete} missing=${miss.length}`);
    chk('[D] complete 与 missing 一致',
        t.complete === (miss.length === 0), `complete=${t.complete} missing=${JSON.stringify(miss)}`);
    chk('[D] n_legs + missing == pool_n',
        (t.n_legs + miss.length) === t.pool_n,
        `${t.n_legs}+${miss.length} vs ${t.pool_n}`);
  })();

  // ---------- [E] 参考手数复算 ----------
  (function () {
    const p = r.params || {};
    let bad = [];
    for (const x of (r.legs || [])) {
      if (!x.hand) continue;
      // RiskParSizer 逐字复刻
      let h = Math.round((p.equity_base * p.risk_pct) / (x.atr * x.tick * x.mult));
      h = Math.max(p.hand_min, Math.min(p.hand_max, h));
      const capH = Math.floor((p.equity_base * p.notional_cap) / (x.price * x.mult));
      h = Math.min(h, Math.max(p.hand_min, capH));
      if (h !== x.hand) bad.push(`${x.key}: 复算 ${h} vs bundle ${x.hand}`);
    }
    chk('[E] 参考手数与 RiskParSizer 公式一致', bad.length === 0, bad.join('; ') || 'all ok');
  })();

  // ---------- [F] 手数/名义约束 ----------
  (function () {
    const p = r.params || {};
    let bad = [];
    for (const x of (r.legs || [])) {
      if (!x.hand) continue;
      if (x.hand < p.hand_min || x.hand > p.hand_max) bad.push(`${x.key} 手数 ${x.hand} 越界`);
      // 名义只能因 hand_min 兜底才可能超 cap（1 手就超），那种情况要标出来
      const capAbs = p.equity_base * p.notional_cap;
      if (x.notional > capAbs * 1.001 && x.capped_by !== 'hand_min') {
        bad.push(`${x.key} 名义 ${x.notional} > cap ${capAbs}（capped_by=${x.capped_by}）`);
      }
    }
    chk('[F] 手数在夹取区间内、名义不超 cap', bad.length === 0, bad.join('; ') || 'all ok');
  })();

  // ---------- [G] freshness 角标正常渲染 ----------
  (function () {
    const f = meta.freshness;
    chk('[G] _meta.freshness 存在', !!f, JSON.stringify(f));
    if (!f) return;
    P.renderFreshness();
    const el = doc.getElementById('fresh');
    chk('[G] 角标文本非空', !!String(el.textContent || '').length, el.textContent);
    chk('[G] stale 与 className 一致',
        f.stale ? /stale/.test(el.className) : !/stale/.test(el.className),
        `stale=${f.stale} class="${el.className}"`);
  })();

  // ---------- [H] freshness 缺失必须告警（不许留空） ----------
  (function () {
    const bak = DB.meta.freshness;
    delete DB.meta.freshness;
    P.renderFreshness();
    const el = doc.getElementById('fresh');
    chk('[H] 缺 freshness ⇒ 红色告警而非空白',
        /未知/.test(el.textContent) && /stale/.test(el.className),
        el.textContent);
    DB.meta.freshness = bak;      // 复原
    P.renderFreshness();
  })();


  // ---------- [I] 板块构成面板（S5 新增） ----------
  (function () {
    const sec = meta.sector;
    chk('[I] _meta.sector 存在', !!sec);
    if (!sec) return;
    chk('[I] pool 与 workbench 一致',
        Array.isArray(sec.pool) && sec.pool.length === (meta.risk && meta.risk.totals
          ? meta.risk.totals.pool_n : sec.pool.length),
        JSON.stringify(sec.pool));
    chk('[I] by_sector 腿数合计 == pool 腿数',
        Object.values(sec.by_sector || {}).reduce((a,b)=>a+b,0) === sec.pool.length,
        JSON.stringify(sec.by_sector));
    chk('[I] HHI 与份额一致',
        Math.abs(sec.hhi - Object.values(sec.shares||{})
          .reduce((a,x)=>a+x*x,0)) < 0.002,
        `hhi=${sec.hhi}`);
    chk('[I] 有效腿数 = 1/HHI',
        Math.abs(sec.effective_legs - 1/sec.hhi) < 0.05,
        `eff=${sec.effective_legs} vs ${(1/sec.hhi).toFixed(2)}`);
    chk('[I] 无"未分类"品种（板块表覆盖完整）',
        !Object.keys(sec.by_sector||{}).includes('未分类'),
        JSON.stringify(Object.keys(sec.by_sector||{})));
    // 面板能渲染
    state.sym = Object.keys(bundle).filter(k=>k!=='_meta')[0];
    try { P.renderStats(); } catch(e) {}
    const html = doc.getElementById('stats').innerHTML || '';
    chk('[I] stats 面板含"板块构成"', html.includes('板块构成'));
    // ★ 精确检测：板块表格的表头只能是「品种/细板块/数据」，不许出现金额类列名
    //   （文本级正则会误伤说明文案，第一版就是这么挂的）
    const m = html.match(/<th>品种<\/th><th>细板块<\/th><th>([^<]*)<\/th>/);
    chk('[I] 板块表格只有 3 列且末列是「数据」',
        !!m && m[1] === '数据', m ? m[1] : '表头未找到');
    chk('[I] 板块面板内无金额符号（不做绩效比较）', (() => {
      const i = html.indexOf('板块构成');
      const j = html.indexOf('风险贡献');
      if (i < 0) return false;
      const seg = html.slice(i, j > i ? j : i + 3000);
      return !/¥|元<\/td>/.test(seg);
    })());
  })();

  console.log('='.repeat(76));
  console.log(fail === 0 ? `全部通过 ✅（${pass} 项）` : `失败 ${fail} 项 / 通过 ${pass} 项 ❌`);
  process.exit(fail === 0 ? 0 : 1);
}
main().catch(e => { console.log('  FAIL  harness 抛错：' + (e && e.stack || e)); process.exit(1); });
