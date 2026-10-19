// 渲染编排 + 统一出口（供 boot / engine 引用）
import { state } from '../core/state.js';
import { pd, rangeStart } from '../core/time.js';
import { $ } from '../core/fmt.js';
import { DB } from '../data/index.js';
import { renderTop, applyLayout, setTab, renderFreshness, syncMarkTab } from './topbar.js';
import { renderMain } from './main.js';
import { renderIntraday } from './intra.js';
import { renderTable } from './table.js';
import { renderDetail } from './detail.js';
import { renderStats } from './stats.js';
import { renderSim } from './simpane.js';
import { renderCompare } from './compare.js';
import { renderMkTab } from './mklist.js';
import { updateReplayUI } from '../replay/engine.js';

export { renderMain, renderIntraday, renderTable, renderTop, applyLayout, setTab, renderFreshness, renderDetail, renderStats, renderSim, renderCompare, renderMkTab, syncMarkTab };

export function renderAll() {
  if (state.sel != null && !DB.data[state.sym].trades[state.sel]) state.sel = null;
  if (state.replay == null) {
    const d0 = pd(); const j0 = rangeStart(d0.x);
    if (d0.x.length) {
      const el = $('rdate');
      // 可选日期 = 整段模拟盘（不受"近N日"限制），默认值 = 当前区间起点
      el.min = String(d0.x[0]).slice(0,10);
      el.max = String(d0.x[d0.x.length-1]).slice(0,10);
      if (!el.value || el.value < el.min || el.value > el.max) {
        el.value = String(d0.x[j0]).slice(0,10);
      }
    }
  }
  renderTop();
  renderMain();
  renderIntraday();
  renderTable();
  renderDetail();
  renderStats();
  renderSim();
  renderCompare();
  syncMarkTab();          // ★ T2.3：训练模式隐藏「标记」tab（不渲染，不是 disabled）
  renderMkTab();          // ★ T2.3：标记列表随进度/模式刷新
  updateReplayUI();
}
