# -*- coding: utf-8 -*-
"""
甲醇期权 IV 曲面时序探针
算三件事，看它们到底动不动、有没有信息：
  1. ATM IV（|Delta| 最接近 0.5）
  2. 25D skew = IV(25D put) - IV(25D call)
  3. 期限结构 = ATM IV(近月) / ATM IV(次月)
  4. 由 put-call parity 反推标的期货价 F = K + C - P
不做任何回测，只看数据形状。
"""
import sys, os, re, time, warnings
import numpy as np, pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")
import akshare as ak

OUT = os.path.dirname(os.path.abspath(__file__))
SYM = "甲醇期权"


def cal_days():
    cal = ak.tool_trade_date_hist_sina()
    d = pd.to_datetime(cal["trade_date"])
    return [x for x in d if pd.Timestamp("2026-06-01") <= x <= pd.Timestamp("2026-10-30")]


def pull(day):
    ds = day.strftime("%Y%m%d")
    try:
        df = ak.option_hist_czce(symbol=SYM, trade_date=ds)
    except Exception:
        return ds, None
    if df is None or len(df) == 0:
        return ds, None
    d = df.rename(columns={"今收盘": "close", "今结算": "settle", "成交量(手)": "vol",
                           "持仓量": "oi", "DELTA": "delta", "隐含波动率": "iv"}).copy()
    for c in ["close", "settle", "vol", "oi", "delta", "iv"]:
        d[c] = pd.to_numeric(d.get(c), errors="coerce")
    p = d["合约代码"].str.extract(r"^([A-Za-z]+)(\d{3})([CP])(\d+(?:\.\d+)?)$")
    d["mon3"], d["cp"], d["K"] = p[1], p[2], p[3].astype(float)
    d = d[d["mon3"].notna()].copy()
    d["date"] = pd.Timestamp(day)
    # 3 位月份 -> 真实年月
    y0 = (pd.Timestamp(day).year // 10) * 10
    def real_mon(s):
        yy = y0 + int(s[0])
        if yy < pd.Timestamp(day).year:
            yy += 10
        return yy * 100 + int(s[1:])
    d["ym"] = d["mon3"].map(real_mon)
    return ds, d


def feats(d):
    """单日单品种 -> 一行特征"""
    out = {"date": d["date"].iloc[0]}
    # 只保留 iv 与 delta 都有效的
    v = d.dropna(subset=["iv", "delta"])
    v = v[v["iv"].between(1, 300)]
    if len(v) < 8:
        return None
    # 近月：剩余到期至少 7 天的最近月
    cur = d["date"].iloc[0]
    ms = sorted(v["ym"].unique())
    def dte(ym):
        y, m = ym // 100, ym % 100
        # 郑商所期权到期日 ≈ 标的交割月前一个月第 3 个交易日，粗略按月末
        end = pd.Timestamp(year=y, month=m, day=1) - pd.Timedelta(days=1)
        return (end - cur).days
    ms = [m for m in ms if dte(m) >= 7]
    if not ms:
        return None
    near = ms[0]; nxt = ms[1] if len(ms) > 1 else None

    def atm_of(sub):
        s = sub.copy()
        s["dist"] = (s["delta"].abs() - 0.5).abs()
        # 优先选有成交的
        s["traded"] = s["vol"].fillna(0) > 0
        s = s.sort_values(["traded", "dist"], ascending=[False, True])
        return s.iloc[0] if len(s) else None

    n = v[v["ym"] == near]
    a = atm_of(n)
    if a is None:
        return None
    out["atm_iv"] = a["iv"]; out["atm_delta"] = a["delta"]; out["atm_K"] = a["K"]
    out["atm_traded"] = bool(a["vol"] and a["vol"] > 0)
    out["dte"] = dte(near)

    # 用平价反推 F：同 K 的 C 与 P 都在时最准
    pv = n.pivot_table(index="K", columns="cp", values=["settle", "iv", "delta"], aggfunc="first")
    if ("settle", "C") in pv.columns and ("settle", "P") in pv.columns:
        par = pv[("settle", "C")] - pv[("settle", "P")]
        f_est = (pv.index.to_series() + par).dropna()
        if len(f_est):
            out["F_parity"] = float(np.median(f_est.values))

    # 25D skew（用 delta 分档，put 侧 delta≈-0.25，call 侧≈0.25）
    def iv_at(target, sign):
        s = n[n["cp"] == sign].dropna(subset=["delta", "iv"])
        if len(s) == 0:
            return np.nan
        s = s.copy()
        s["dist"] = (s["delta"] - target).abs()
        s = s.sort_values("dist")
        return float(s.iloc[0]["iv"])
    iv_p25 = iv_at(-0.25, "P"); iv_c25 = iv_at(0.25, "C")
    out["iv_p25"] = iv_p25; out["iv_c25"] = iv_c25
    out["skew25"] = iv_p25 - iv_c25 if np.isfinite(iv_p25) and np.isfinite(iv_c25) else np.nan

    if nxt is not None:
        a2 = atm_of(v[v["ym"] == nxt])
        if a2 is not None:
            out["atm_iv_next"] = a2["iv"]
            out["ts_slope"] = a["iv"] - a2["iv"]
    # 全链 ATM 附近宽度（衡量曲面陡峭度）
    out["n_chain"] = len(v)
    out["n_traded"] = int((v["vol"].fillna(0) > 0).sum())
    return out


def main():
    days = cal_days()
    print(f"交易日 {len(days)} 天：{days[0].date()} ~ {days[-1].date()}")
    raws = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(pull, d): d for d in days}
        for i, f in enumerate(as_completed(futs), 1):
            ds, d = f.result()
            if d is not None:
                raws[ds] = d
            if i % 20 == 0:
                print(f"  {i}/{len(days)}  {time.time()-t0:.0f}s")
    print(f"拉到 {len(raws)} 天，耗时 {time.time()-t0:.0f}s")

    rows = []
    for ds in sorted(raws):
        try:
            r = feats(raws[ds])
            if r:
                rows.append(r)
        except Exception as e:
            print("feat err", ds, str(e)[:80])
    df = pd.DataFrame(rows).sort_values("date")
    p = os.path.join(OUT, "m_iv_ts_probe.csv")
    df.to_csv(p, index=False, encoding="utf-8-sig")
    pd.set_option("display.width", 250); pd.set_option("display.max_rows", 200)
    print(f"\nsaved -> {p}  shape={df.shape}\n")
    print(df.round(3).to_string(index=False))
    print("\n--- 描述统计 ---")
    print(df[["atm_iv", "skew25", "ts_slope", "dte", "n_traded"]].describe().round(3).to_string())


if __name__ == "__main__":
    main()
