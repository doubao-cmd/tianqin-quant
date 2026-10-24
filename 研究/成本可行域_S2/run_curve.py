# -*- coding: utf-8 -*-
"""成本—频率可行域 S2：edge 的期限结构（预注册 v1.0，sha256 0b94e977…）

只读：逐笔 BE0 + 5min 历史 + contracts.csv
不写任何生产数据；输出仅到 研究/成本可行域_S2/out/
"""
import os, json
import numpy as np
import pandas as pd

ROOT = r"C:\Users\cc\Desktop\天勤量化"
D = os.path.join(ROOT, "工具/_进场体检/数据")
T = os.path.join(ROOT, "工具/_保本止损/结果")
OUT = os.path.join(ROOT, "研究/成本可行域_S2/out")
os.makedirs(OUT, exist_ok=True)

SYMS = ["JM", "M", "JD", "P", "SR"]
H_GRID = [1, 2, 3, 6, 12, 24, 48, 96]
SEED = 20261023
NBOOT = 1000

ct = pd.read_csv(os.path.join(ROOT, "工具/_r1_cost/out/contracts.csv"), encoding="utf-8-sig")
FEE = {r.symbol: (float(r.fee_open_yuan), float(r.fee_ct_yuan), float(r.tick), float(r.tick_value))
       for r in ct.itertuples()}

rows = []          # 每笔基础信息
curve = []         # h × symbol 汇总
for s in SYMS:
    tr = pd.read_csv(os.path.join(T, f"{s}_BE0_逐笔.txt"), encoding="utf-8-sig")
    tr.columns = [c.strip() for c in tr.columns]
    k = pd.read_csv(os.path.join(D, f"{s}_5min_2021-01-01_2023-12-31.csv"), encoding="utf-8-sig")
    k.columns = [c.strip() for c in k.columns]
    k["dt"] = pd.to_datetime(k["datetime"])
    k = k.sort_values("dt").reset_index(drop=True)

    pos = {t: i for i, t in enumerate(k["dt"])}
    fo, fc, tick, tv = FEE[s]
    cost_pts = (fo + fc) / tv + 2 * tick

    miss = 0
    for r in tr.itertuples():
        t = pd.Timestamp(getattr(r, "开仓时间"))
        i = pos.get(t)
        if i is None:
            miss += 1
            continue
        direction = 1.0 if str(getattr(r, "方向")).strip() == "多" else -1.0
        px = float(getattr(r, "开仓价"))
        atr = float(getattr(r, "开仓ATR"))
        if not np.isfinite(atr) or atr <= 0:
            miss += 1
            continue
        rows.append(dict(sym=s, i=i, date=t.date(), dir=direction, px=px, atr=atr,
                         c=cost_pts / atr, open_err=abs(k["open"].iloc[i] - px) / px))

    print(f"{s}: 逐笔 {len(tr)}，未吸附 {miss}，可用 {sum(1 for x in rows if x['sym']==s)}")

base = pd.DataFrame(rows)
base.to_csv(os.path.join(OUT, "trades_base.csv"), index=False, encoding="utf-8-sig")
print("\n[校验] 入场价 vs 入场bar开盘 相对误差：中位 %.4f%%，p90 %.4f%%"
      % (100 * base["open_err"].median(), 100 * base["open_err"].quantile(0.9)))

# 预加载各品种 close 序列
CLOSE = {}
for s in SYMS:
    k = pd.read_csv(os.path.join(D, f"{s}_5min_2021-01-01_2023-12-31.csv"), encoding="utf-8-sig")
    k.columns = [c.strip() for c in k.columns]
    CLOSE[s] = k.sort_values("datetime")["close"].to_numpy(float)

# 计算每个 h
for h in H_GRID:
    recs = []
    for s in SYMS:
        b = base[base["sym"] == s]
        cl = CLOSE[s]
        end = b["i"].to_numpy() + h - 1
        ok = end < len(cl)
        bb = b[ok].copy()
        g = (cl[end[ok]] - bb["px"].to_numpy()) * bb["dir"].to_numpy() / bb["atr"].to_numpy()
        bb["g"] = g
        bb["n"] = g - bb["c"].to_numpy()
        recs.append(bb)
        curve.append(dict(h=h, sym=s, n=len(bb), n_days=bb["date"].nunique(),
                          med_g=float(np.median(g)) if len(bb) else np.nan,
                          mean_g=float(np.mean(g)) if len(bb) else np.nan,
                          med_c=float(np.median(bb["c"])) if len(bb) else np.nan,
                          med_n=float(np.median(bb["n"])) if len(bb) else np.nan,
                          win=float((bb["n"] > 0).mean()) if len(bb) else np.nan))
    allb = pd.concat(recs, ignore_index=True)

    # 按交易日块 bootstrap
    dates = allb["date"].unique()
    rng = np.random.default_rng(SEED)
    groups = {d: allb[allb["date"] == d]["n"].to_numpy() for d in dates}
    meds = []
    for _ in range(NBOOT):
        pick = rng.choice(dates, size=len(dates), replace=True)
        arr = np.concatenate([groups[d] for d in pick])
        meds.append(np.median(arr))
    lo, hi = np.percentile(meds, [2.5, 97.5])

    curve.append(dict(h=h, sym="ALL", n=len(allb), n_days=len(dates),
                      med_g=float(allb["g"].median()), mean_g=float(allb["g"].mean()),
                      med_c=float(allb["c"].median()), med_n=float(allb["n"].median()),
                      win=float((allb["n"] > 0).mean()),
                      ci_lo=float(lo), ci_hi=float(hi),
                      n_pos_sym=int(sum(1 for s in SYMS
                                        if np.median(allb[allb["sym"] == s]["n"]) > 0))))
    print(f"h={h:>3}  笔数 {len(allb):>5}  中位g {allb['g'].median():+.3f}  "
          f"中位c {allb['c'].median():.3f}  中位n {allb['n'].median():+.3f}  "
          f"CI [{lo:+.3f},{hi:+.3f}]  正品种 {curve[-1]['n_pos_sym']}/5")

cv = pd.DataFrame(curve)
cv.to_csv(os.path.join(OUT, "curve_by_h.csv"), index=False, encoding="utf-8-sig")
print("\n已写出:", os.path.join(OUT, "curve_by_h.csv"))
