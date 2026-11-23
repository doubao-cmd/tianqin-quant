# -*- coding: utf-8 -*-
"""诊断：外盘/内盘日期对齐差几天（lag 扫描）。独立版，不 import 主脚本。"""
import urllib.request, json, re, ssl, os, time
import numpy as np, pandas as pd

ssl._create_default_https_context = ssl._create_unverified_context
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "_cache")
HDR = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
BASE = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{s}=/"


def _get(url):
    raw = urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=20) \
        .read().decode("gbk", errors="ignore")
    m = re.search(r"=\s*\((\[.*\])\)", raw, re.S)
    return json.loads(m.group(1)) if m else None


def fetch_inner(sym):
    fp = os.path.join(CACHE, f"in_{sym}.csv")
    if os.path.exists(fp):
        return pd.read_csv(fp, parse_dates=["date"])
    rows = _get(BASE.format(s="in_" + sym) + f"InnerFuturesNewService.getDailyKLine?symbol={sym}")
    d = pd.DataFrame(rows).rename(columns={"d": "date", "o": "open", "c": "close"})
    d["date"] = pd.to_datetime(d["date"])
    for c in ["open", "close"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna().sort_values("date")
    d.to_csv(fp, index=False)
    return d


def fetch_out(sym):
    fp = os.path.join(CACHE, f"out_{sym}.csv")
    if os.path.exists(fp):
        return pd.read_csv(fp, parse_dates=["date"])
    rows = _get(BASE.format(s="out_" + sym) + f"GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={sym}")
    d = pd.DataFrame(rows)
    d["date"] = pd.to_datetime(d["date"])
    for c in ["open", "close"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna().sort_values("date")
    d = d[d["close"] > 0]
    d.to_csv(fp, index=False)
    return d


PAIRS = [("CU0", "CAD", "沪铜-LME铜"), ("M0", "S", "豆粕-CBOT大豆"),
         ("Y0", "BO", "豆油-CBOT豆油"), ("SC0", "CL", "原油-NYMEX"),
         ("RB0", "CAD", "螺纹-LME铜(对照)")]

print("=" * 92)
for isym, osym, label in PAIRS:
    di, do = fetch_inner(isym), fetch_out(osym)
    di = di.sort_values("date").reset_index(drop=True)
    di["r"] = np.log(di["close"]).diff()
    di.loc[di["r"].abs() > 0.12, "r"] = np.nan
    do = do.sort_values("date").reset_index(drop=True)
    do["r"] = np.log(do["close"]).diff()
    do.loc[do["r"].abs() > 0.30, "r"] = np.nan
    print(f"\n【{label}】 内盘 {di['date'].min().date()}~{di['date'].max().date()} "
          f"{len(di)}根 | 外盘 {do['date'].min().date()}~{do['date'].max().date()} {len(do)}根")
    print(f"  内盘日σ {di['r'].std()*100:.2f}%  外盘日σ {do['r'].std()*100:.2f}%")
    for lag in [-3, -2, -1, 0, 1, 2, 3]:
        dd = do[["date", "r"]].copy()
        dd["date"] = dd["date"] + pd.Timedelta(days=lag)
        m = pd.merge(dd, di[["date", "r"]], on="date", how="inner", suffixes=("_o", "_i")).dropna()
        if len(m) < 100:
            print(f"   lag={lag:+d}  样本不足 {len(m)}")
            continue
        c = np.corrcoef(m["r_o"], m["r_i"])[0, 1]
        flag = "  <== 峰值" if False else ""
        print(f"   lag={lag:+d}  corr={c:+.3f}  n={len(m)}{flag}")
