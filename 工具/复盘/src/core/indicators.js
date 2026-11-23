// 指标层（2026-11-07，R-A）——把指标从「构建期 Python 预烘」改成「前端装载时算」。
//
// 为什么改：2026 全年 5min 有 1.4 万根/品种，指标数组占每根 90/154 字节（58%）；
//   不搬走的话单文件 HTML 会到 25MB+。搬走之后 bundle 只剩 o/h/l/c/v/day，
//   gzip 后约 5MB，全年数据才塞得进一个双击即开的文件。
//
// ⚠️ 口径必须与 **features.py 逐字一致**（不是"差不多就行"）：
//   · ma[j]  = mean(close[j-13 : j])        右端**不含** close[j]，j<13 为 NaN
//   · atr[j] = mean(tr[max(1,j-14) : j])    j>=2；否则 tr[j]。右端不含 tr[j]，**非 Wilder**
//   · tr[j]  = max(H-L, |H-prevC|, |L-prevC|)，prevC[0]=close[0]
//   · slope  = ma − ma.shift(5)
//   · 布林/RSI/KDJ 用的是 pandas 的 **右含** rolling（与上面两个恰好相反，别抄错）
//   · 取整位数照 build_data._arr：价格类 2 位、slope/atr_dist 3 位

const MA_N = 13, SLOPE_N = 5, ATR_N = 14, BOLL_N = 20, BOLL_K = 2;

const r2 = v => (v == null || !isFinite(v)) ? null : Math.round(v * 100) / 100;
const r3 = v => (v == null || !isFinite(v)) ? null : Math.round(v * 1000) / 1000;

// 右含 rolling 均值（pandas rolling(n).mean()）：前 n-1 个为 NaN
function rollMean(a, n) {
  const out = new Array(a.length).fill(NaN);
  let s = 0;
  for (let i = 0; i < a.length; i++) {
    s += a[i];
    if (i >= n) s -= a[i - n];
    if (i >= n - 1) out[i] = s / n;
  }
  return out;
}

// 右含 rolling 总体标准差（pandas rolling(n).std(ddof=0)）
function rollStd(a, n) {
  const out = new Array(a.length).fill(NaN);
  for (let i = n - 1; i < a.length; i++) {
    let s = 0, s2 = 0;
    for (let k = i - n + 1; k <= i; k++) { s += a[k]; s2 += a[k] * a[k]; }
    const m = s / n;
    out[i] = Math.sqrt(Math.max(0, s2 / n - m * m));
  }
  return out;
}

function rollMin(a, n) {
  const out = new Array(a.length).fill(NaN);
  for (let i = n - 1; i < a.length; i++) {
    let m = Infinity;
    for (let k = i - n + 1; k <= i; k++) if (a[k] < m) m = a[k];
    out[i] = m;
  }
  return out;
}

function rollMax(a, n) {
  const out = new Array(a.length).fill(NaN);
  for (let i = n - 1; i < a.length; i++) {
    let m = -Infinity;
    for (let k = i - n + 1; k <= i; k++) if (a[k] > m) m = a[k];
    out[i] = m;
  }
  return out;
}

// pandas ewm(adjust=False)：y0=x0, y_i = (1-α)y_{i-1} + α x_i
//   span=n → α = 2/(n+1)；com=k → α = 1/(1+k)。两者别混：
//   MACD 用的是 span，KDJ 用的是 **com=2（α=1/3，等价 span=5）**。
//
// ★ NaN 的处理是这里唯一的坑（2026-11-07 实测对不上 KDJ 才发现）：
//   pandas 在 NaN 处**输出沿用上一个平滑值**（不是 NaN），且把上一个值的权重按 (1-α)
//   衰减；下一个有效点用 y = (wt·y + α·x)/(wt + α) 归一，然后权重复位 1。
//   手算验证（com=2, x=[10,NaN,NaN,20,NaN,30] ⇒ [10,10,10,15.294,15.294,21.597]）。
//   若写成"遇 NaN 直接 continue"，KDJ 会在"9 根全平"（high9==low9 ⇒ rsv=NaN）处跑偏，
//   MA 的 2026-03-02 那一段差到 44 个点。
function ewm(a, n) { return ewmAlpha(a, 2 / (n + 1)); }
function ewmCom(a, com) { return ewmAlpha(a, 1 / (1 + com)); }
function ewmAlpha(a, al) {
  const out = new Array(a.length).fill(NaN);
  let y = null, wt = 1;
  for (let i = 0; i < a.length; i++) {
    const v = a[i], ok = isFinite(v);
    if (y === null) {                       // 还没遇到第一个有效值
      if (ok) { y = v; wt = 1; out[i] = y; }
      continue;
    }
    wt *= (1 - al);
    if (ok) {
      y = (wt * y + al * v) / (wt + al);
      wt = 1;                               // 观测后权重复位
      out[i] = y;
    } else {
      out[i] = y;                           // NaN 处沿用上一个平滑值
    }
  }
  return out;
}

// 主入口：吃 {o,h,l,c}，吐出与旧 bundle 完全同名的指标字段
export function calcIndicators(blk) {
  const o = blk.o, h = blk.h, l = blk.l, c = blk.c, n = c.length;
  if (!n) return {};

  // ---- TR / ATR（右端不含当根）----
  const tr = new Array(n);
  for (let j = 0; j < n; j++) {
    const pc = j > 0 ? c[j - 1] : c[0];
    tr[j] = Math.max(h[j] - l[j], Math.abs(h[j] - pc), Math.abs(l[j] - pc));
  }
  const atr = new Array(n);
  for (let j = 0; j < n; j++) {
    if (j < 2) { atr[j] = tr[j]; continue; }
    const a = Math.max(1, j - ATR_N);
    let s = 0;
    for (let k = a; k < j; k++) s += tr[k];
    atr[j] = s / (j - a);
  }

  // ---- MA13（右端不含当根）----
  const ma = new Array(n).fill(NaN);
  let run = 0;
  for (let j = MA_N; j < n; j++) {
    if (j === MA_N) for (let k = 0; k < MA_N; k++) run += c[k];
    else run += c[j - 1] - c[j - MA_N - 1];
    ma[j] = run / MA_N;
  }

  // ---- slope = ma − ma[−5] ----
  const slope = new Array(n).fill(NaN);
  for (let j = SLOPE_N; j < n; j++) {
    if (isFinite(ma[j]) && isFinite(ma[j - SLOPE_N])) slope[j] = ma[j] - ma[j - SLOPE_N];
  }

  // ---- 布林(20,2)（右含）----
  const bm = rollMean(c, BOLL_N), sd = rollStd(c, BOLL_N);
  const bu = new Array(n), bl = new Array(n);
  for (let j = 0; j < n; j++) {
    bu[j] = isFinite(bm[j]) ? bm[j] + BOLL_K * sd[j] : NaN;
    bl[j] = isFinite(bm[j]) ? bm[j] - BOLL_K * sd[j] : NaN;
  }

  // ---- 距 MA13 的 ATR 倍数 ----
  const atrDist = new Array(n).fill(NaN);
  for (let j = 0; j < n; j++) {
    if (isFinite(ma[j]) && atr[j] > 0) atrDist[j] = (c[j] - ma[j]) / atr[j];
  }

  // ---- MACD(12,26,9)：hist 按 A 股口径 ×2 ----
  const e12 = ewm(c, 12), e26 = ewm(c, 26);
  const dif = new Array(n), dea = new Array(n), hist = new Array(n);
  for (let j = 0; j < n; j++) {
    dif[j] = (isFinite(e12[j]) && isFinite(e26[j])) ? e12[j] - e26[j] : NaN;
  }
  const deaRaw = ewm(dif, 9);
  for (let j = 0; j < n; j++) {
    dea[j] = deaRaw[j];
    hist[j] = (isFinite(dif[j]) && isFinite(dea[j])) ? (dif[j] - dea[j]) * 2 : NaN;
  }

  // ---- RSI14（右含；涨跌幅各自取正部再均）----
  const up = new Array(n).fill(0), dn = new Array(n).fill(0);
  for (let j = 1; j < n; j++) {
    const d = c[j] - c[j - 1];
    up[j] = d > 0 ? d : 0;
    dn[j] = d < 0 ? -d : 0;
  }
  const mu = rollMean(up, 14), md = rollMean(dn, 14);
  const rsi = new Array(n).fill(NaN);
  for (let j = 0; j < n; j++) {
    // ★ j=0 的 delta 是 NaN（pandas diff 的首值），rolling(14) 要 14 个有效值
    //   ⇒ 第一条有效 RSI 在 j=14，不是 j=13。差一根就会和 Python 对不上。
    if (j < 14) continue;
    if (!isFinite(mu[j]) || !isFinite(md[j])) continue;
    // ★ 与 Python 口径一致：全涨无跌时 dn=0 → rs 变 NaN → RSI 记 NaN（不是 100）。
    //   这是 features 的既有行为（rs = up/dn.replace(0,nan)），照抄以免图上数值对不上。
    if (md[j] === 0) continue;
    rsi[j] = 100 - 100 / (1 + mu[j] / md[j]);
  }

  // ---- KDJ(9,3,3)：k/d 用 com=2 的 ewm（α=1/3），j=3k−2d ----
  const lo9 = rollMin(l, 9), hi9 = rollMax(h, 9);
  const rsv = new Array(n).fill(NaN);
  for (let j = 0; j < n; j++) {
    if (!isFinite(lo9[j]) || !isFinite(hi9[j])) continue;
    const rng = hi9[j] - lo9[j];
    rsv[j] = rng === 0 ? NaN : (c[j] - lo9[j]) / rng * 100;
  }
  const kk = ewmCom(rsv, 2);          // ★ com=2 ⇒ α=1/3（不是 span=3！）
  const dd = ewmCom(kk, 2);
  const jj = new Array(n).fill(NaN);
  for (let j = 0; j < n; j++) {
    if (isFinite(kk[j]) && isFinite(dd[j])) jj[j] = 3 * kk[j] - 2 * dd[j];
  }

  const wrap2 = a => a.map(r2);
  return {
    ma: wrap2(ma), atr: wrap2(atr), slope: slope.map(r3), atr_dist: atrDist.map(r3),
    boll_u: wrap2(bu), boll_m: wrap2(bm), boll_l: wrap2(bl),
    dif: wrap2(dif), dea: wrap2(dea), hist: wrap2(hist),
    rsi: wrap2(rsi), k: wrap2(kk), d: wrap2(dd), j: wrap2(jj),
  };
}
