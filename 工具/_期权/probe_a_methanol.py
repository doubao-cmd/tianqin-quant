# -*- coding: utf-8 -*-
"""
A 路线探针：甲醇期权 IV 曲面 skew -> 期货未来收益
严格按 R6a_预注册卡执行，口径不得改。

关键口径：
  - 近月 = DTE>=7 的最早交割月；到期日 = 交割月前一月的第 3 个交易日
  - 25D 用线性插值定位，剔除 |delta|>0.95 与占位结算价
  - 收益 = close(D+1+h)/close(D+1) - 1（D+1 收盘建仓，无未来函数）
"""
import sys, os, re, time, warnings
import numpy as np, pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from scipy import stats

warnings.filterwarnings("ignore")
import akshare as ak

OUT = os.path.dirname(os.path.abspath(__file__))
SYM = "甲醇期权"
CACHE = os.path.join(OUT, "_cache_m_opt_raw.csv")
START, END = "2019-11-01", "2026-11-02"


# ---------- 交易日历 ----------
def trade_days():
    cal = ak.tool_trade_date_hist_sina()
    d = pd.to_datetime(cal["trade_date"])
    return pd.DatetimeIndex([x for x in d if pd.Timestamp(START) <= x <= pd.Timestamp(END)])


def third_trade_day_of_month(year, month, cal):
    """某年某月的第 3 个交易日"""
    m = cal[(cal.year == year) & (cal.month == month)]
    return m[2] if len(m) >= 3 else (m[-1] if len(m) else pd.NaT)


# ---------- 拉取 ----------
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
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")
    p = d["合约代码"].str.extract(r"^([A-Za-z]+)(\d{3})([CP])(\d+(?:\.\d+)?)$")
    d["mon3"], d["cp"], d["K"] = p[1], p[2], p[3].astype(float)
    d = d[d["mon3"].notna()].copy()
    if len(d) == 0:
        return ds, None
    d["date"] = pd.Timestamp(day)
    # 3 位月份 -> 交割年月
    cur = pd.Timestamp(day)
    y0 = (cur.year // 10) * 10

    def real_mon(s):
        yy = y0 + int(s[0])
        while yy * 100 + int(s[1:]) < (cur.year - 2) * 100 + cur.month:
            yy += 10
        return yy * 100 + int(s[1:])

    d["ym"] = d["mon3"].map(real_mon)
    return ds, d


def fetch_all(days):
    if os.path.exists(CACHE):
        c = pd.read_csv(CACHE, dtype={"mon3": str}, parse_dates=["date"])
        print(f"[cache] {CACHE}  {c.shape}  {c.date.min().date()}~{c.date.max().date()}")
        have = set(c.date.dt.strftime("%Y%m%d"))
        todo = [d for d in days if d.strftime("%Y%m%d") not in have]
        print(f"[cache] 已有 {len(have)} 天，需补 {len(todo)} 天")
    else:
        c = None
        todo = list(days)

    if todo:
        got, t0 = [], time.time()
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(pull, d): d for d in todo}
            for i, f in enumerate(as_completed(futs), 1):
                ds, d = f.result()
                if d is not None:
                    got.append(d)
                if i % 100 == 0:
                    print(f"  {i}/{len(todo)}  {time.time()-t0:.0f}s  成功 {len(got)}")
        if got:
            new = pd.concat(got, ignore_index=True)
            c = new if c is None else pd.concat([c, new], ignore_index=True)
            c = c.drop_duplicates(subset=["date", "合约代码"])
            c.to_csv(CACHE, index=False, encoding="utf-8-sig")
            print(f"[cache] 写入 {c.shape}")

    if c is None or len(c) == 0:
        sys.exit("无数据")
    return c


# ---------- 单日特征 ----------
def interp_iv_at(sub, target):
    """按 |delta| 对 IV 线性插值; target 取正"""
    s = sub.dropna(subset=["delta", "iv"]).copy()
    if len(s) < 4:
        return np.nan
    s["ad"] = s["delta"].abs()
    s = s[(s["ad"] > 0.02) & (s["ad"] < 0.95)]
    if len(s) < 4:
        return np.nan
    s = s.sort_values("ad")
    x, y = s["ad"].values, s["iv"].values
    # 去重（同 ad 取均值）
    dfx = pd.DataFrame({"x": x, "y": y}).groupby("x", as_index=False).y.mean()
    if len(dfx) < 4 or target < dfx.x.min() or target > dfx.x.max():
        return np.nan
    return float(np.interp(target, dfx.x.values, dfx.y.values))


def feat_day(date, g, cal, dte_map):
    v = g.dropna(subset=["iv", "delta"]).copy()
    v = v[v["iv"].between(1, 300)]
    if len(v) < 10:
        return None
    # 剔除占位结算价：Put 结算价 ∈ {0.5,1.0,1.5} 且深度实值
    ph = v[(v.cp == "P") & (v["settle"].isin([0.5, 1.0, 1.5])) & (v.delta.abs() > 0.85)]
    if len(ph):
        v = v.drop(ph.index)

    ms = sorted(v["ym"].unique())
    cands = []
    for m in ms:
        exp = dte_map.get(m)
        if exp is None or pd.isna(exp):
            continue
        cands.append((m, (pd.Timestamp(exp) - pd.Timestamp(date)).days))
    cands = [(m, t) for m, t in cands if t >= 7]
    if not cands:
        return None
    cands.sort(key=lambda z: z[1])
    near, dte_n = cands[0]
    n = v[v["ym"] == near]
    if len(n) < 10:
        return None

    row = {"date": date, "ym": near, "dte": dte_n, "n_chain": len(n),
           "n_traded": int((n["vol"].fillna(0) > 0).sum())}

    # ATM: |delta| 最接近 0.5
    n2 = n.copy()
    n2["dist"] = (n2["delta"].abs() - 0.5).abs()
    n2["traded"] = n2["vol"].fillna(0) > 0
    a = n2.sort_values(["traded", "dist"], ascending=[False, True]).iloc[0]
    row.update(atm_iv=a["iv"], atm_delta=a["delta"], atm_K=a["K"], atm_traded=bool(a["traded"]))

    # 25D skew（插值）
    ivp = interp_iv_at(n[n.cp == "P"], 0.25)
    ivc = interp_iv_at(n[n.cp == "C"], 0.25)
    row["iv_p25"], row["iv_c25"] = ivp, ivc
    row["skew25"] = ivp - ivc if np.isfinite(ivp) and np.isfinite(ivc) else np.nan

    # 平价反推 F（仅自检）
    try:
        pv = n.pivot_table(index="K", columns="cp", values="settle", aggfunc="first")
        if "C" in pv.columns and "P" in pv.columns:
            f_est = (pv["C"] - pv["P"] + pv.index.to_series()).dropna()
            if len(f_est):
                row["F_parity"] = float(np.median(f_est.values))
    except Exception:
        pass
    return row


# ---------- 主流程 ----------
def main():
    days = trade_days()
    print(f"候选交易日 {len(days)} 天：{days[0].date()} ~ {days[-1].date()}")

    raw = fetch_all(days)
    raw["date"] = pd.to_datetime(raw["date"])
    print(f"期权原始面板 {raw.shape}，覆盖 {raw.date.nunique()} 天")

    # 到期日映射：ym -> 到期日（交割月前一月的第 3 个交易日）
    cal = pd.DatetimeIndex(ak.tool_trade_date_hist_sina()["trade_date"])
    cal = pd.DatetimeIndex(pd.to_datetime(cal))
    yms = sorted(raw["ym"].unique())
    dte_map = {}
    for ym in yms:
        y, m = int(ym) // 100, int(ym) % 100
        py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
        dte_map[ym] = third_trade_day_of_month(py, pm, cal)
    print(f"交割月 {len(yms)} 个，示例：" +
          ", ".join(f"{k}->{pd.Timestamp(v).date()}" for k, v in list(dte_map.items())[:4] if pd.notna(v)))

    rows, errs = [], []
    for date, g in raw.groupby("date"):
        try:
            r = feat_day(date, g, cal, dte_map)
            if r:
                rows.append(r)
        except Exception as e:
            errs.append(f"{pd.Timestamp(date).date()} {type(e).__name__}: {str(e)[:60]}")
    if errs:
        print(f"[warn] {len(errs)} 天算特征失败，前 3 条：")
        for e in errs[:3]:
            print("   ", e)
    if not rows:
        sys.exit("特征全为 None，检查 feat_day 过滤条件")
    F = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    print(f"特征面板 {F.shape}  {F.date.min().date()} ~ {F.date.max().date()}")
    print("\n--- 特征描述 ---")
    print(F[["atm_iv", "skew25", "dte", "n_chain", "n_traded", "atm_traded"]].describe().round(3).to_string())

    # 期货
    fut = ak.futures_zh_daily_sina(symbol="MA0")
    fut["date"] = pd.to_datetime(fut["date"])
    fut = fut.sort_values("date").reset_index(drop=True)
    fut["close"] = pd.to_numeric(fut["close"], errors="coerce")
    print(f"\n期货日线 {len(fut)} 行  {fut.date.min().date()} ~ {fut.date.max().date()}")

    M = F.merge(fut[["date", "close", "settle"]], on="date", how="inner")
    print(f"合并后 {len(M)} 行")

    # z 分
    M["skew_ma20"] = M["skew25"].rolling(20, min_periods=15).mean()
    M["skew_sd20"] = M["skew25"].rolling(20, min_periods=15).std()
    M["z"] = (M["skew25"] - M["skew_ma20"]) / M["skew_sd20"]

    # 收益：close(D+1+h)/close(D+1)-1
    for h in (5, 10):
        M[f"ret{h}"] = M["close"].shift(-(1 + h)) / M["close"].shift(-1) - 1

    M.to_csv(os.path.join(OUT, "m_skew_feature.csv"), index=False, encoding="utf-8-sig")

    # ---------- 检验 ----------
    def nw_t(y, x, lag):
        s = pd.DataFrame({"y": y, "x": x}).dropna()
        if len(s) < 50:
            return np.nan, np.nan, np.nan, len(s)
        X = np.column_stack([np.ones(len(s)), s.x.values])
        b = np.linalg.lstsq(X, s.y.values, rcond=None)[0]
        e = s.y.values - X @ b
        XtXi = np.linalg.inv(X.T @ X)
        S = (X * e[:, None]).T @ (X * e[:, None])
        for l in range(1, lag + 1):
            w = 1 - l / (lag + 1)
            G = (X[l:] * e[l:, None]).T @ (X[:-l] * e[:-l, None])
            S += w * (G + G.T)
        V = XtXi @ S @ XtXi
        return b[1], b[1] / np.sqrt(V[1, 1]), b[1] / np.sqrt(XtXi[1, 1] * (e @ e) / len(s)), len(s)

    print("\n" + "=" * 78)
    print("主检验：ret 对 z 回归（NW 调整）")
    print("=" * 78)
    res = []
    for h in (5, 10):
        b, t, t_ols, n = nw_t(M[f"ret{h}"], M["z"], lag=h)
        res.append({"h": h, "beta": b, "nw_t": t, "ols_t": t_ols, "n": n,
                    "z=+2 隐含 bp": b * 2 * 1e4})
    R = pd.DataFrame(res)
    print(R.round(4).to_string(index=False))

    # 分位描述
    print("\n--- z 五分位（描述用，不参与判据）---")
    Mm = M.dropna(subset=["z", "ret5"]).copy()
    Mm["q"] = pd.qcut(Mm["z"], 5, labels=["Q1低", "Q2", "Q3", "Q4", "Q5高"])
    g = Mm.groupby("q", observed=True).agg(n=("ret5", "size"), mean_bp=("ret5", lambda s: s.mean() * 1e4),
                                           win=("ret5", lambda s: (s > 0).mean()),
                                           z_lo=("z", "min"), z_hi=("z", "max"))
    print(g.round(2).to_string())

    # 安慰剂：block bootstrap z
    print("\n--- 安慰剂 1000 次 block bootstrap (block=20) ---")
    rng = np.random.default_rng(20261102)
    d5 = Mm[["z", "ret5"]].dropna().reset_index(drop=True)
    nb = int(np.ceil(len(d5) / 20))
    null = []
    zv = d5.z.values
    for i in range(1000):
        st = rng.integers(0, len(d5) - 20, nb)
        idx = np.concatenate([np.arange(s, s + 20) for s in st])[:len(d5)]
        bs = pd.DataFrame({"z": zv[idx], "ret5": d5.ret5.values})
        b_, t_, _, _ = nw_t(bs.ret5, bs.z, lag=5)
        null.append(b_)
    null = np.array(null)
    act = R.loc[R.h == 5, "beta"].iloc[0]
    pct = (null < act).mean()
    print(f"实际 beta = {act:.6f}；零分布 mean={null.mean():.6f} sd={null.std():.6f}")
    print(f"零分布 95分位 = {np.percentile(null,95):.6f}；实际值所在分位 = {pct*100:.1f}%")

    # 稳健性：剔除大跳
    big = fut["close"].pct_change().abs() > 0.05
    bigdays = set(fut.loc[big, "date"])
    M2 = M[~M.date.isin(bigdays)]
    b2, t2, _, n2 = nw_t(M2["ret5"], M2["z"], lag=5)
    print(f"\n[稳健性] 剔除 |日收益|>5% 的 {len(bigdays)} 天后：beta={b2:.6f} nw_t={t2:.2f} n={n2}")

    # ---------- 判据 ----------
    print("\n" + "=" * 78)
    print("判据核对（预注册卡 §七）")
    print("=" * 78)
    b5 = R.loc[R.h == 5, "beta"].iloc[0]
    t5 = R.loc[R.h == 5, "nw_t"].iloc[0]
    econ = b5 * 2 * 1e4
    c1 = b5 > 0
    c2 = t5 >= 2.0
    c3 = econ >= 20
    c4 = pct >= 0.95
    for lab, ok, val in [("beta > 0", c1, f"{b5:.6f}"),
                         ("NW-t >= 2.0", c2, f"{t5:.2f}"),
                         ("z:+2 隐含收益 >= 20bp", c3, f"{econ:.1f}bp"),
                         ("安慰剂 >= 95分位", c4, f"{pct*100:.1f}%")]:
        print(f"  [{'PASS' if ok else 'FAIL'}] {lab:28s} = {val}")
    print("\n>>> 结论：" + ("通过，可继续" if all([c1, c2, c3, c4]) else "未通过 ⇒ 归档"))


if __name__ == "__main__":
    main()
