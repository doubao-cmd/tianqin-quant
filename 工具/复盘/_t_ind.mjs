// 指标口径自检（2026-11-07，R-A）：JS 现算 vs 旧 bundle 里 Python 预烘的同一批指标。
// 用法：node _t_ind.mjs <新bundle.json> <旧bundle.json>
// 判定：同一 symbol/period、同一 x 上逐值比对，允许 ≤1 个最小单位（取整差）。
import { calcIndicators } from './src/core/indicators.js';
import fs from 'fs';

const [新, 旧] = process.argv.slice(2);
const NEW = JSON.parse(fs.readFileSync(新, 'utf8'));
const OLD = JSON.parse(fs.readFileSync(旧, 'utf8'));
const FIELDS = ['ma', 'atr', 'slope', 'atr_dist', 'boll_u', 'boll_m', 'boll_l',
  'dif', 'dea', 'hist', 'rsi', 'k', 'd', 'j'];
const TOL = { slope: 2e-3, atr_dist: 2e-3 };

let checked = 0, bad = 0;
const worst = {};

for (const sym of Object.keys(NEW)) {
  if (sym === '_meta' || !NEW[sym].periods) continue;
  for (const per of Object.keys(NEW[sym].periods)) {
    const nb = NEW[sym].periods[per], ob = OLD[sym] && OLD[sym].periods && OLD[sym].periods[per];
    if (!ob) { console.log(`  ? ${sym} ${per}: 旧 bundle 无此周期`); continue; }
    const args = {}; FIELDS.forEach(f => { args[f] = ob[f]; });
    const ind = calcIndicators(nb);
    // 旧/新 x 需对齐：新数据区间包含旧区间（窗口变长），按 x 建索引
    const oi = new Map(ob.x.map((x, i) => [x, i]));
    let n = 0, mism = 0;
    for (let i = 0; i < nb.x.length; i++) {
      const j = oi.get(nb.x[i]);
      if (j == null) continue;
      n++;
      for (const f of FIELDS) {
        const a = ind[f] ? ind[f][i] : undefined;
        const b = ob[f] ? ob[f][j] : undefined;
        const tol = TOL[f] || 0.011;
        if (a == null && b == null) continue;
        if (a == null || b == null || Math.abs(a - b) > tol) {
          mism++;
          const d = (a == null || b == null) ? 'null' : Math.abs(a - b).toFixed(4);
          const kk = `${f}`;
          if (!worst[kk] || worst[kk].d < +d) worst[kk] = { d: isNaN(+d) ? 0 : +d, sym, per, i, x: nb.x[i], a, b };
          if (mism <= 3) console.log(`  ✘ ${sym} ${per} ${f} x=${nb.x[i]} js=${a} py=${b} diff=${d}`);
          break;
        }
      }
    }
    checked += n;
    if (mism) bad++;
    console.log(`${mism ? '✘' : '✔'} ${sym} ${per}: 对齐 ${n} 根，不一致 ${mism} 根`);
  }
}
console.log(`\n总对齐 ${checked} 根；有问题的 symbol/period 组 ${bad} 个`);
if (Object.keys(worst).length) console.log('最大偏差：', worst);
