// 数据层（P0-A）：DATA 由 build_data.py 产出，前端按需加载。
// 用 DB.data 持有，避免循环依赖；loader 在启动时填充。
import { calcIndicators } from '../core/indicators.js';

export const DB = { data: null };

export function setData(d) { DB.data = d; }

// 数据源分级（P0-E 会用；本步只声明，hist 源暂未接）
export const SOURCES = {
  live:  { label: '实盘台账', range: '7天', hasVolume: true,  hasTrades: true  },
  hist:  { label: '历史回放', range: '3年', hasVolume: false, hasTrades: false },
};

// ★ 2026-11-07（R-A）：单文件版把 bundle 以 **gzip+base64** 内嵌（全年数据 21MB → 5MB）。
//   DecompressionStream 是浏览器原生 API（Chrome/Edge 80+），不引第三方包。
async function gunzipJson(b64) {
  const bin = atob(b64);
  const u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  if (typeof DecompressionStream === 'undefined') {
    throw new Error('当前浏览器不支持 DecompressionStream（需 Chrome/Edge 80+）'
      + '，请改用 dist/ 目录的 HTTP 版：启动复盘终端.bat');
  }
  const stream = new Blob([u8]).stream().pipeThrough(new DecompressionStream('gzip'));
  return JSON.parse(await new Response(stream).text());
}

// ★ 指标在装载时现算（口径见 core/indicators.js）。已算过（有 ma）则跳过。
function hydrate(d) {
  for (const sym of Object.keys(d)) {
    const s = d[sym];
    if (!s || !s.periods) continue;
    for (const p of Object.keys(s.periods)) {
      const blk = s.periods[p];
      if (!blk || !blk.c || blk.ma) continue;
      Object.assign(blk, calcIndicators(blk));
    }
  }
  return d;
}

// HTTP 模式：从 ./data/bundle.json 拉取；单文件模式由 boot 注入 window.__REVIEW_DATA__ / __REVIEW_GZ__
export async function loadData() {
  let d;
  if (window.__REVIEW_DATA__) { d = window.__REVIEW_DATA__; }
  else if (window.__REVIEW_GZ__) { d = await gunzipJson(window.__REVIEW_GZ__); }
  else { const r = await fetch('./data/bundle.json'); d = await r.json(); }
  // ★ _meta 是构建期写入的元信息（data_end / window_days / built），单独抽出，避免被当成品种
  DB.meta = d._meta || null;
  if (d._meta) delete d._meta;
  DB.data = hydrate(d);
  return DB.data;
}
