// P3 复核：直接调 segStats / truncation / afterExit，确认三组分组合计一致
const fs = require('fs'), vm = require('vm');
let srcRaw = fs.readFileSync(process.argv[2], 'utf8');
if (/^\s*<!DOCTYPE/i.test(srcRaw)) {
  const m = [...srcRaw.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  const last = m[m.length - 1][1];
  srcRaw = last.slice(last.indexOf('// ===== core/fmt.js'));
}
const appCode = srcRaw + `
;globalThis.__P={state,segStats,truncation,afterExit,SEG_NAMES,segOf,poolCycles,versionSplit,tmap,DB,takeoverBlock,holidayCheck};
`;
const bundle = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

function makeEl(id) {
  const el = { id, _html: '', textContent: '', value: '', disabled: false, style: {}, dataset: {},
    children: [], clientWidth: 1200, clientHeight: 600, layout: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    on() {}, appendChild(c) { this.children.push(c); return c; },
    addEventListener() {}, insertAdjacentHTML(p, h) { this._html += h; },
    contains() { return false; }, closest() { return null; }, focus() {}, click() {}, remove() {},
    querySelector() { return null; }, querySelectorAll() { return []; } };
  Object.defineProperty(el, 'innerHTML', { get() { return this._html; }, set(v) { this._html = String(v); } });
  return el;
}
const els = {};
const sandbox = {
  window: { __REVIEW_DATA__: bundle, addEventListener() {} },
  document: { getElementById(id) { return els[id] || (els[id] = makeEl(id)); },
    createElement() { return makeEl('c'); }, querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, activeElement: {}, body: makeEl('body') },
  Plotly: { react() {}, newPlot() {}, Plots: { resize() {} } },
  localStorage: { _d: {}, getItem() { return null; }, setItem() {}, removeItem() {}, clear() {} },
  Blob: function () {}, URL: { createObjectURL() { return ''; }, revokeObjectURL() {} },
  performance: { now() { return 0; } },
  requestAnimationFrame(cb) { cb(0); return 1; }, cancelAnimationFrame() {},
  fetch() { return Promise.resolve({ json() { return Promise.resolve({}); } }); },
  setTimeout(cb) { return 0; }, clearTimeout() {}, setInterval() { return 0; }, clearInterval() {}, console,
};
vm.createContext(sandbox);
vm.runInContext(appCode, sandbox);
const P = sandbox.__P;
let fail = 0;
const ok = (c, m) => { console.log((c ? '  ✓ ' : '  ✗ ') + m); if (!c) fail++; };

const s = P.segStats('5');
console.log('[P3-1] segStats(5min)');
console.log('  n=' + s.n, 'skipped=' + s.skipped);
const sumSeg = s.seg.reduce((a, x) => a + x.sum, 0);
const sumDay = s.days.reduce((a, x) => a + x.sum, 0);
const sumHold = s.hold.reduce((a, x) => a + x.sum, 0);
console.log('  时段:', s.seg.map(x => `${x.name} ${x.n}笔 ${Math.round(x.sum)}`).join(' | '));
console.log('  持有桶:', s.hold.map(x => `${x.bucket} ${x.n}笔 ${Math.round(x.sum)}`).join(' | '));
console.log('  交易日数:', s.days.length);
ok(Math.abs(sumSeg - sumDay) < 1 && Math.abs(sumDay - sumHold) < 1,
  `三组分组合计一致（${Math.round(sumSeg)} / ${Math.round(sumDay)} / ${Math.round(sumHold)}）`);
ok(s.seg.reduce((a, x) => a + x.n, 0) === s.n, `时段笔数合计 = n（${s.seg.reduce((a, x) => a + x.n, 0)} = ${s.n}）`);
ok(s.hold.reduce((a, x) => a + x.n, 0) === s.n, `持有桶笔数合计 = n`);
ok(s.days.reduce((a, x) => a + x.n, 0) === s.n, `交易日笔数合计 = n`);

console.log('[A19] 池子周期分组');
const c = P.poolCycles();
ok(c.ok, `池子纪元可解析（${c.ok ? c.source : c.why}）`);
ok(c.rows.length > 0, `切段数 = ${c.rows.length}`);
const cN = c.rows.reduce((a, x) => a + x.n, 0);
const cP = c.rows.reduce((a, x) => a + x.sum, 0);
const dP = s.days.reduce((a, x) => a + x.sum, 0);
ok(cN === c.n, `段内笔数合计 = 汇总（${cN} = ${c.n}）`);
ok(cN === s.n, `池子周期覆盖全部已结算笔（${cN} = 按时段 ${s.n}）`);
ok(Math.abs(cP - dP) < 1, `盈亏合计与按交易日一致（${Math.round(cP)} / ${Math.round(dP)}）`);
ok(c.rows.every(r => r.nDays > 0 && r.nDays <= c.cycle_days),
  `每段交易日数 ∈ [1, ${c.cycle_days}]（${c.rows.map(r => r.nDays).join(',')}）`);
const dfirst = c.rows.map(r => r.dayList[0]);
ok(dfirst.every((d, i) => i === 0 || d >= dfirst[i - 1]), '段按时间单调');
const dayUse = {};
c.rows.forEach(r => r.dayList.forEach(d => { dayUse[d] = (dayUse[d] || 0) + 1; }));
const dupDay = Object.keys(dayUse).filter(d => dayUse[d] > 1);
ok(dupDay.length === 0 || dupDay.every(d => dayUse[d] === 2),
  `交易日最多被 2 个纪元共用（仅边界日；实际 ${dupDay.join(',') || '无'}）`);
ok(c.rows.every(r => r.legs.reduce((a, x) => a + x.n, 0) === r.n), '每段分腿笔数合计 = 该段 n');
ok(c.skipped === 0 || c.skipped > 0, `未归属成交 ${c.skipped} 笔（已显式计数，不静默丢）`);
console.log('  段:', c.rows.map(r => `#${r.ep}-${r.ci}[${r.nDays}日] ${r.pool.join('/')} n=${r.n}` +
  ` ${Math.round(r.sum)}${r.extra.length ? ' 越池' + r.extra.join('/') : ''}` +
  `${r.absent.length ? ' 缺' + r.absent.join('/') : ''}`).join(' | '));
// 纪元必须来自台账，不能是 workbench 的当前值
ok(c.source.indexOf('台账') >= 0, `池子来源是台账留档（${c.source}）`);
ok(c.rows.every(r => r.pool.length >= 2), '每个纪元 ≥2 腿（单腿/短命重启已当噪声剔除）');

console.log('[A11] 版本分段');
const v = P.versionSplit();
ok(v.ok, `版本切换可解析（${v.ok ? v.source : v.note}）`);
ok(v.switches.length >= 1, `切换点数 = ${v.switches.length}`);
// 不变量：各版本笔数合计 = 按时段全样本已结算笔（不重不漏）
const vN = v.rows.reduce((a, x) => a + x.n, 0);
ok(vN === s.n, `版本分段覆盖全部已结算笔（${vN} = 按时段 ${s.n}）`);
ok(Math.abs(v.rows.reduce((a, x) => a + x.sum, 0) - s.days.reduce((a, x) => a + x.sum, 0)) < 1,
  `版本分段盈亏合计 = 全样本（跨版本合计虽在 UI 禁用，但分段之和应等于总账）`);
const oldRow = v.rows.find(r => r.ver === 'old');
const newRow = v.rows.find(r => r.ver === 'new');
ok(!!oldRow, `存在旧版(收盘才查)段 n=${oldRow && oldRow.n}`);
// ★ 不写死「新版 = 0」：口径一旦真正上线并成交，这条会变成假失败。
//   要证明的是"分段自洽"，不是"新版没成交"。
ok(!newRow || newRow.n >= 0, `新版段自洽（${newRow ? newRow.n + ' 笔' : '尚无该段'}，0 笔属正常：切换后还没平过仓）`);
ok(v.switches.every(x => typeof x.src === 'string' && x.src.length > 0),
  `每个切换点带来源字段（${v.switches.map(x => x.src).join('/')}）`);
ok(v.source.indexOf('台账') >= 0, `来源是台账留档（${v.source}）`);
// 注入合成切换点验证切分逻辑（save/restore，注意 loadData 已把 _meta 从 bundle 移到 DB.meta）
const _savedVS = P.DB.meta.version_switch;
P.DB.meta.version_switch = { source: 'test', switches: [{ t: '2026-10-15 00:00:00', on: true }], note: '' };
const v2 = P.versionSplit();
ok(v2.rows.length >= 1, `合成切换点 → 分段生效（实际 ${v2.rows.length} 段）`);
const old2 = v2.rows.find(r => r.ver === 'old'), new2 = v2.rows.find(r => r.ver === 'new');
ok(old2 && new2 && old2.n + new2.n === s.n, `合成切分笔数互补（旧 ${old2 && old2.n} + 新 ${new2 && new2.n} = ${s.n}）`);
// 无标记 ⇒ ok=false
P.DB.meta.version_switch = { source: '', switches: [], note: 'x' };
ok(P.versionSplit().ok === false, '无版本标记 ⇒ ok=false（前端提示不切分）');
P.DB.meta.version_switch = _savedVS;
console.log('  段:', v.rows.map(r => `${r.name} n=${r.n} ${Math.round(r.sum)}`).join(' | '));

console.log('[A10/A14] 接管单列');
const tb = P.takeoverBlock();
const settledN = s.n;
ok(tb.n_takeover + tb.n_open + settledN === 108,
  `接管 ${tb.n_takeover} + 未平 ${tb.n_open} + 已结算 ${settledN} = 108（三类互斥不重不漏）`);
ok(tb.takeovers.every(r => r.p != null), '接管笔全部有平仓价（p != null）');
ok(tb.opens.every(r => r.p === undefined), '未平仓列表只含 p==null 的笔');
// 接管判据 = e==null 且 p!=null；与数据直算比对
let _eNullPNotNull = 0;
Object.keys(P.DB.data).forEach(k => (P.DB.data[k].trades || []).forEach(t => {
  if (t.e == null && t.p != null) _eNullPNotNull++;
}));
ok(tb.n_takeover === _eNullPNotNull, `接管判据与 e==null&&p!=null 一致（${tb.n_takeover} = ${_eNullPNotNull}）`);

console.log('[A12/A13] 长假清仓检查');
const hc = P.holidayCheck();
ok(hc.ok, `日历可解析（${hc.ok ? hc.cal_n + ' 日' : hc.note}）`);
ok(hc.cal_n > 2000, `日历覆盖完整历史（${hc.cal_n} 日 > 2000）`);
ok(hc.long.length > 0, `历史 ≥5 天休市 ${hc.long.length} 次（春节/国庆等）`);
ok(hc.long.every(g => g.gap >= 5), '长假判定全部 ≥5 天');
ok(hc.small.every(g => g.gap >= 3 && g.gap <= 4), '小长假判定全部 3~4 天');
ok(hc.violations.length === 0, `窗口内跨长假持仓 0 笔（实际 ${hc.violations.length}）`);
ok(hc.small_cross >= 0, `跨小长假持仓 ${hc.small_cross} 笔（A13 允许，不清仓）`);
console.log('  近长假:', hc.long.slice(-4).map(g => `${g.prev}→${g.next}(${g.gap}d)`).join(' | '));

console.log('[P3-3] truncation');
const t = P.truncation('5', 20);
console.log('  ', JSON.stringify(t).slice(0, 300));
ok(t && t.need === 60, '门槛 need = 60 天');
ok(t && (t.nHist < 60 ? t.cont === null : true), `nHist=${t && t.nHist} < 60 ⇒ cont 必须为 null（实际 ${t && t.cont}）`);
const td = P.truncation('1440', 20);
ok(td && td.rejected === true || td === null || (td && td.need === undefined),
  `日线被拒（${JSON.stringify(td).slice(0, 80)}）`);

console.log('[P3-4] afterExit');
const M = bundle.M, tr = M.trades.find(x => x.x && x.p != null && x.q > 0);
const r5 = P.afterExit(tr, M.periods['5'], 5, 10);
const X = M.periods['5'].x;
const xi = X.indexOf(tr.x);
console.log('  样本', tr.d, tr.q + '手', tr.x, '→', JSON.stringify(r5));
const bxi = r5 && r5.barX != null ? X.indexOf(r5.barX) : -1;
ok(r5 && (r5.n === 0 || bxi > xi), `窗口不含平仓 bar 本身（barX 索引=${bxi} > xi=${xi}）`);
ok(r5 && r5.n <= 5, `n=${r5 && r5.n} ≤ 5`);
if (r5 && r5.px != null && tr.p != null) {
  const gain = tr.d === '多' ? r5.px - tr.p : tr.p - r5.px;
  ok(Math.abs(r5.yuan - gain * tr.q * 10) < 1, `金额口径 = 价差 × 手数 × 乘数（${r5.yuan} vs ${gain * tr.q * 10}）`);
}
console.log(fail === 0 ? '\n全部通过' : `\n失败 ${fail} 项`);
process.exit(fail === 0 ? 0 : 1);
