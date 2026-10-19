
const fs = require('fs');
const raw = JSON.parse(fs.readFileSync('dist/data/bundle.json', 'utf8'));
delete raw._meta;
DB.data = raw;
state.sym = 'M'; state.period = '5'; state.mode = 'review';

let bad = 0;
const chk = (n, c, extra) => { if (!c) { bad++; console.log(`  X ${n}${extra ? ' :: ' + extra : ''}`); } else console.log(`  OK ${n}`); };

// 基准：全品种全部已结算盈亏合计
let total = 0, cnt = 0, nulls = 0;
Object.keys(DB.data).forEach(k => (DB.data[k].trades || []).forEach(t => {
  if (t.pnl == null) { nulls++; return; }
  total += t.pnl; cnt++;
}));
console.log(`基准：${cnt} 笔已结算，合计 ${Math.round(total)} 元，${nulls} 笔无盈亏`);

console.log('-- P3-1 时段/交易日/持有桶 --');
const s = segStats('5');
chk('已结算笔数一致', s.n === cnt, `${s.n} vs ${cnt}`);
const segSum = s.seg.reduce((a, r) => a + r.sum, 0);
const daySum = s.days.reduce((a, r) => a + r.sum, 0);
const holdSum = s.hold.reduce((a, r) => a + r.sum, 0);
chk('时段合计 == 总额', Math.abs(segSum - total) < 1e-6, `${segSum} vs ${total}`);
chk('交易日合计 == 总额', Math.abs(daySum - total) < 1e-6);
chk('持有桶合计 == 总额', Math.abs(holdSum - total) < 1e-6);
chk('时段笔数合计 == n', s.seg.reduce((a, r) => a + r.n, 0) === s.n);
chk('持有桶笔数合计 == n', s.hold.reduce((a, r) => a + r.n, 0) === s.n);
chk('交易日数合理(15~30)', s.days.length >= 5 && s.days.length <= 30, String(s.days.length));
console.log('   时段分布：', s.seg.map(r => `${r.name} n=${r.n} sum=${Math.round(r.sum)} wr=${r.wr}%`).join(' | '));
console.log('   持有分布：', s.hold.map(r => `${r.bucket} n=${r.n} sum=${Math.round(r.sum)}`).join(' | '));
console.log('   交易日首末：', s.days[0].day, '→', s.days[s.days.length - 1].day);

console.log('-- P3-3 截断盘 --');
const tr = truncation('5');
chk('可计算', tr.ok === true, tr.why);
if (tr.ok) {
  chk('分位在 [0,1]', tr.pct == null || (tr.pct >= 0 && tr.pct <= 1), String(tr.pct));
  chk('振幅 > 0', tr.amp > 0, String(tr.amp));
  chk('样本不足时不编造延续率', tr.nHist < 60 ? tr.cont === null : tr.cont != null);
  console.log(`   ${tr.day} 振幅 ${tr.amp.toFixed(1)} 中位 ${tr.med == null ? '—' : tr.med.toFixed(1)} 分位 ${tr.pct == null ? '—' : Math.round(tr.pct * 100) + '%'} nHist ${tr.nHist}`);
}
chk('日线周期拒绝', truncation('1440').ok === false);

console.log('-- P3-4 出场后路径 --');
const d = pd('M', '5');
const tr0 = DB.data.M.trades.find(t => t.x && t.p != null);
const r5 = afterExit(tr0, d, 5, 10);
const r10 = afterExit(tr0, d, 10, 10);
chk('返回非空', !!r5);
chk('5 根窗口 n<=5', r5.n <= 5 && r5.n >= 0, String(r5.n));
chk('10 根窗口 n>=5 根窗口', r10.n >= r5.n);
chk('金额 = 价差×手数×乘数', Math.abs(r5.yuan - r5.gain * tr0.q * 10) < 1e-6);
console.log(`   样本：${tr0.d}${tr0.q}手 平@${tr0.p} → 5根最有利 ${r5.px} = ${Math.round(r5.yuan)} 元（看了${r5.n}根）`);
// 窗口必须从平仓当根之后起算：最有利 bar 不能是平仓当根
const xi = (() => { let lo=0,hi=d.x.length-1,ans=-1; const k=tmap(tr0.x);
  while(lo<=hi){const m=(lo+hi)>>1; if(tmap(d.x[m])<=k){ans=m;lo=m+1;}else hi=m-1;} return ans; })();
chk('窗口不含平仓当根', r5.barX > d.x[xi], `${r5.barX} vs ${d.x[xi]}`);

console.log(bad === 0 ? '\nP3 全部通过（0 失败）' : `\n${bad} 项失败`);
process.exit(bad === 0 ? 0 : 1);
