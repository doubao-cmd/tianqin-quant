// 数据层（P0-A）：DATA 由 build_data.py 产出，前端按需加载。
// 用 DB.data 持有，避免循环依赖；loader 在启动时填充。
export const DB = { data: null };

export function setData(d) { DB.data = d; }

// 数据源分级（P0-E 会用；本步只声明，hist 源暂未接）
export const SOURCES = {
  live:  { label: '实盘台账', range: '7天', hasVolume: true,  hasTrades: true  },
  hist:  { label: '历史回放', range: '3年', hasVolume: false, hasTrades: false },
};

// HTTP 模式：从 ./data/bundle.json 拉取；单文件模式由 boot 注入 window.__REVIEW_DATA__
export async function loadData() {
  let d;
  if (window.__REVIEW_DATA__) { d = window.__REVIEW_DATA__; }
  else { const r = await fetch('./data/bundle.json'); d = await r.json(); }
  // ★ _meta 是构建期写入的元信息（data_end / window_days / built），单独抽出，避免被当成品种
  DB.meta = d._meta || null;
  if (d._meta) delete d._meta;
  DB.data = d;
  return DB.data;
}
