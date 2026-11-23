# -*- coding: utf-8 -*-
"""
外盘 → 内盘 关系强度实测
==================================================
口径（关键）：
  外盘交易日 D 的收盘发生在北京时间 D+1 凌晨（CBOT ~02:20 / LME ~01:00 / NYMEX ~02:30）
  内盘交易日 d 的日盘在 d 09:00-15:00
  ⇒ 外盘 D 的信息只能影响内盘 d = D+1 及以后（D+1 的夜盘 21:00 之前）
  对齐：merge_asof(backward, allow_exact_matches=False)，保证用到的是严格早于内盘当日的外盘收盘

内盘收益拆三段：
  gap[d]   = ln(open[d] / close[d-1])     ← 外盘信息的直接定价处
  intra[d] = ln(close[d] / open[d])       ← 剔除跳空后剩下的，**只有这段可交易**
  total[d] = gap + intra

回归：total / gap / intra ~ 外盘 D 收益
判据：β_intra 的幅度 vs 往返成本。β_total 大但 β_intra≈0 ⇒ 信息在开盘一秒定价完，不可交易。
"""
import urllib.request, json, re, ssl, os, time
import numpy as np
import pandas as pd

ssl._create_default_https_context = ssl._create_unverified_context
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "_cache")
os.makedirs(CACHE, exist_ok=True)

HDR = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
BASE = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{s}=/"


def _get(url, retries=3):
    for i in range(retries):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=20) \
                .read().decode("gbk", errors="ignore")
            m = re.search(r"=\s*\((\[.*\])\)", raw, re.S)
            return json.loads(m.group(1)) if m else None
        except Exception:
            time.sleep(1 + i)
    return None


def fetch_inner(sym):
    fp = os.path.join(CACHE, f"in_{sym}.csv")
    if os.path.exists(fp) and os.path.getsize(fp) > 5000:
        return pd.read_csv(fp, parse_dates=["date"])
    rows = _get(BASE.format(s="in_" + sym) + f"InnerFuturesNewService.getDailyKLine?symbol={sym}")
    if not rows:
        return None
    d = pd.DataFrame(rows).rename(columns={"d": "date", "o": "open", "h": "high",
                                           "l": "low", "c": "close", "v": "volume"})
    d["date"] = pd.to_datetime(d["date"])
    for c in ["open", "high", "low", "close"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["open", "close"]).sort_values("date")
    d[["date", "open", "high", "low", "close", "volume"]].to_csv(fp, index=False)
    return d[["date", "open", "high", "low", "close", "volume"]]


def fetch_out(sym):
    fp = os.path.join(CACHE, f"out_{sym}.csv")
    if os.path.exists(fp) and os.path.getsize(fp) > 5000:
        return pd.read_csv(fp, parse_dates=["date"])
    rows = _get(BASE.format(s="out_" + sym) + f"GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={sym}")
    if not rows:
        return None
    d = pd.DataFrame(rows)
    d["date"] = pd.to_datetime(d["date"])
    for c in ["open", "high", "low", "close"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["close"]).sort_values("date")
    d = d[d["close"] > 0]
    d[["date", "open", "high", "low", "close"]].to_csv(fp, index=False)
    return d[["date", "open", "high", "low", "close"]]


def ols(y, x):
    """返回 beta, t, r2, n   （y,x 已去 NaN）"""
    n = len(y)
    if n < 60:
        return np.nan, np.nan, np.nan, n
    X = np.column_stack([np.ones(n), x])
    XtX = X.T @ X
    try:
        b = np.linalg.solve(XtX, X.T @ y)
        e = y - X @ b
        s2 = (e @ e) / (n - 2)
        se = np.sqrt(np.diag(np.linalg.inv(XtX) * s2))
        t = b[1] / se[1] if se[1] > 0 else np.nan
        r2 = 1 - (e @ e) / ((y - y.mean()) @ (y - y.mean()))
        return b[1], t, r2, n
    except Exception:
        return np.nan, np.nan, np.nan, n


# ---------------- 配对表：内盘 → 外盘（可多个候选，取最强的写结论） ----------------
PAIRS = [
    # 有色（LME/COMEX 是定价中心）
    ("CU", "铜", "CAD", "LME铜"), ("CU", "铜", "HG", "COMEX铜"),
    ("AL", "铝", "AHD", "LME铝"),
    ("ZN", "锌", "ZSD", "LME锌"),
    ("NI", "镍", "NID", "LME镍"),
    # 能化（原油链条）
    ("SC", "原油", "CL", "NYMEX原油"), ("SC", "原油", "OIL", "布伦特"),
    ("FU", "燃料油", "CL", "NYMEX原油"),
    ("BU", "沥青", "CL", "NYMEX原油"),
    ("TA", "PTA", "CL", "NYMEX原油"),
    ("EG", "乙二醇", "CL", "NYMEX原油"),
    ("MA", "甲醇", "CL", "NYMEX原油"),
    ("PP", "聚丙烯", "CL", "NYMEX原油"),
    ("L", "塑料", "CL", "NYMEX原油"),
    ("V", "PVC", "CL", "NYMEX原油"),
    ("EB", "苯乙烯", "CL", "NYMEX原油"),
    # 豆系 / 油脂（CBOT 是定价中心）
    ("M", "豆粕", "SM", "CBOT豆粕"), ("M", "豆粕", "S", "CBOT大豆"),
    ("Y", "豆油", "BO", "CBOT豆油"), ("Y", "豆油", "S", "CBOT大豆"),
    ("A", "豆一", "S", "CBOT大豆"),
    ("P", "棕榈油", "FCPO", "马棕油"), ("P", "棕榈油", "BO", "CBOT豆油"),
    ("C", "玉米", "C", "CBOT玉米"),
    ("OI", "菜油", "BO", "CBOT豆油"),
    ("RM", "菜粕", "SM", "CBOT豆粕"),
    # 软商品
    ("SR", "白糖", "SB", "ICE原糖"),
    ("CF", "棉花", "CT", "ICE棉花"),
    # 无外盘对标（中国定价/品种独有）
    ("RB", "螺纹", "—", "无"),
    ("I", "铁矿", "—", "无"),
    ("JM", "焦煤", "—", "无"),
    ("RU", "橡胶", "—", "无"),
    ("JD", "鸡蛋", "—", "无"),
    ("LH", "生猪", "—", "无"),
    ("SA", "纯碱", "—", "无"),
]

print("=" * 112)
print(f"{'内盘':6s} {'名称':8s} {'外盘':6s} {'名称':10s} {'样本':>5s} {'相关':>7s} "
      f"{'β_total':>8s} {'R²':>6s} {'β_gap':>8s} {'β_intra':>8s} {'t_intra':>8s} {'日内占比':>8s}")
print("=" * 112)

out_rows = []
for isym, iname, osym, oname in PAIRS:
    di = fetch_inner(isym + "0")
    if di is None or len(di) < 200:
        print(f"{isym:6s} {iname:8s} {'--':6s} {'内盘数据缺失':10s}")
        continue
    if osym == "—":
        print(f"{isym:6s} {iname:8s} {'—':6s} {'无外盘对标':10s}  n={len(di):>5d}")
        out_rows.append(dict(inner=isym, iname=iname, outer=None, n=len(di)))
        continue
    do = fetch_out(osym)
    if do is None or len(do) < 200:
        print(f"{isym:6s} {iname:8s} {osym:6s} {'外盘数据缺失':10s}")
        continue

    # 内盘：三大收益
    di = di.sort_values("date").reset_index(drop=True)
    di["lopen"] = np.log(di["open"])
    di["lclose"] = np.log(di["close"])
    di["pclose"] = di["lclose"].shift(1)
    di["total"] = di["lclose"] - di["pclose"]
    di["gap"] = di["lopen"] - di["pclose"]
    di["intra"] = di["lclose"] - di["lopen"]
    # 换月跳空剔除（三者同时置 NaN，保证 gap+intra==total 的可加性）
    bad = (di["gap"].abs() > 0.07) | (di["total"].abs() > 0.15)
    for c in ["total", "gap", "intra"]:
        di.loc[bad, c] = np.nan

    # 外盘收益
    do = do.sort_values("date").reset_index(drop=True)
    do["oret"] = np.log(do["close"]).diff()
    do.loc[do["oret"].abs() > 0.30, "oret"] = np.nan   # 极端值（数据错误/涨跌停）

    # 对齐（经 lag 扫描确诊）：外盘 D → 内盘 D+1。
    # 实现：外盘日期 +1 天后 inner join（等价 lag=+1，已由 diag_lag 验证峰值在此）
    left = di[["date"]].copy()
    left["row"] = np.arange(len(left))
    right = do[["date", "oret"]].copy()
    right["date"] = right["date"] + pd.Timedelta(days=1)
    m = left.merge(right, on="date", how="left").sort_values("row")
    di["oret"] = m["oret"].values

    s = di.dropna(subset=["oret", "total", "gap", "intra"])
    if len(s) < 100:
        print(f"{isym:6s} {iname:8s} {osym:6s} {oname:10s} 样本不足 {len(s)}")
        continue

    rho = float(np.corrcoef(s["oret"], s["total"])[0, 1])
    b_t, t_t, r2, n = ols(s["total"].values, s["oret"].values)
    b_g, t_g, _, _ = ols(s["gap"].values, s["oret"].values)
    b_i, t_i, r2i, _ = ols(s["intra"].values, s["oret"].values)
    share = b_i / b_t * 100 if b_t not in (0, np.nan) and not np.isnan(b_t) else np.nan

    print(f"{isym:6s} {iname:8s} {osym:6s} {oname:10s} {n:>5d} {rho:>7.3f} "
          f"{b_t:>8.3f} {r2:>6.3f} {b_g:>8.3f} {b_i:>8.3f} {t_i:>8.2f} {share:>7.0f}%")
    out_rows.append(dict(inner=isym, iname=iname, outer=osym, oname=oname, n=n,
                         rho=rho, beta_total=b_t, r2=r2, beta_gap=b_g,
                         beta_intra=b_i, t_intra=t_i, share=share))

pd.DataFrame(out_rows).to_csv(os.path.join(HERE, "_外盘关系强度_20261102.csv"), index=False, encoding="utf-8-sig")
print("=" * 112)
print("口径说明：beta 为『外盘涨1%→内盘涨 beta%』；日内占比 = β_intra / β_total")
print("落盘：_外盘关系强度_20261102.csv")
