"""N1 面板构建：合约级隔夜跳空（禁主连）

输入（只读）：工具/_数据/展期_合约日线.csv  key,sym,日期,open,close,volume,oi
输出：data/gap_panel.csv
规则（预注册 v1.1 §5）：
  · t-1 全部行里 oi 最大者记为 c；t 日必须有 c 且 open 可读
  · 特征全部基于 c 自身序列、截至 t-1
"""
import os, sys, io, json
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = r"C:\Users\cc\Desktop\天勤量化\工具\_数据\展期_合约日线.csv"
OUT = os.path.join(HERE, "data", "gap_panel.csv")
LOG = os.path.join(HERE, "data", "panel_build_log.txt")

def main():
    df = pd.read_csv(SRC, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    df["日期"] = pd.to_datetime(df["日期"], format="%Y-%m-%d")
    df = df.sort_values(["key", "日期", "sym"]).reset_index(drop=True)
    lines = [f"源行数={len(df)}  品种={df['key'].nunique()}  区间={df['日期'].min().date()}~{df['日期'].max().date()}"]
    lines.append("品种明细: " + ", ".join(f"{k}:{n}" for k, n in df.groupby("key").size().items()))

    out = []
    drop = {"no_prev": 0, "no_open": 0, "zero_oi": 0, "short_hist": 0}

    for key, g in df.groupby("key", sort=True):
        g = g.sort_values("日期")
        # 每个合约自己的序列（用于 rolling 特征）
        per_contract = {sym: sub.sort_values("日期").reset_index(drop=True)
                        for sym, sub in g.groupby("sym")}
        dates = list(g["日期"].drop_duplicates().sort_values())
        # 每个交易日 -> {sym: row}
        day_rows = {}
        for d, sub in g.groupby("日期"):
            day_rows[d] = sub.set_index("sym")[["open", "close", "volume", "oi"]]

        for i in range(1, len(dates)):
            p, t = dates[i - 1], dates[i]
            prev = day_rows.get(p)
            cur = day_rows.get(t)
            if prev is None or cur is None or len(prev) == 0:
                drop["no_prev"] += 1
                continue
            c = prev["oi"].idxmax()
            oi_p = float(prev.loc[c, "oi"])
            if not np.isfinite(oi_p) or oi_p <= 0:
                drop["zero_oi"] += 1
                continue
            if c not in cur.index:
                drop["no_open"] += 1
                continue
            row_t = cur.loc[c]
            if not np.isfinite(float(row_t.get("open", np.nan))) or float(row_t["open"]) <= 0:
                drop["no_open"] += 1
                continue

            # 合约 c 自身序列（截至 p）
            s = per_contract.get(c)
            if s is None:
                drop["short_hist"] += 1
                continue
            s = s[s["日期"] <= p].reset_index(drop=True)
            if len(s) < 23:            # 需要 20 个日变动 + 前推 2 根拿 doi
                drop["short_hist"] += 1
                continue

            cl = s["close"].astype(float).values
            vol = s["volume"].astype(float).values
            oi = s["oi"].astype(float).values
            d_cl = np.abs(np.diff(cl))
            sd20 = float(np.mean(d_cl[-20:]))
            r = np.diff(np.log(cl))
            # EWMA sigma20（λ=0.94，用最近 20 个收益递推）
            lam, var = 0.94, float(np.var(r[-20:]))
            for x in r[-20:]:
                var = lam * var + (1 - lam) * x * x
            ewma = float(np.sqrt(var))
            lam97, var97 = 0.97, float(np.var(r[-20:]))
            for x in r[-20:]:
                var97 = lam97 * var97 + (1 - lam97) * x * x
            ewma97 = float(np.sqrt(var97))
            close_p = float(cl[-1])
            gap = float(np.log(float(row_t["open"]) / close_p))
            if sd20 <= 0 or not np.isfinite(sd20):
                drop["short_hist"] += 1
                continue

            out.append(dict(
                symbol=key, date=t.strftime("%Y-%m-%d"), contract=c,
                oi_prev=oi_p, vol_prev=float(vol[-1]), close_prev=close_p,
                open_t=float(row_t["open"]), close_t=float(row_t["close"]),
                gap=gap,
                g=gap * close_p / sd20,
                label=int(abs(gap * close_p / sd20) >= 1.0),
                sd20_norm=sd20 / close_p,
                ewma_sigma20=ewma,
                abs_ret_ratio=abs(float(r[-1])) * close_p / sd20,
                doi=float(oi[-1] / oi[-2] - 1.0) if oi[-2] > 0 else np.nan,
                vol_ratio=float(vol[-1] / np.mean(vol[-20:])) if np.mean(vol[-20:]) > 0 else np.nan,
                ewma97=ewma97,
                abs_ret_ratio_lag1=abs(float(r[-2])) * close_p / sd20,
            ))

    res = pd.DataFrame(out)
    res.to_csv(OUT, index=False, encoding="utf-8")
    lines.append(f"面板观测={len(res)}  品种={res['symbol'].nunique()}  区间={res['date'].min()}~{res['date'].max()}")
    lines.append("剔除: " + json.dumps(drop, ensure_ascii=False))
    lines.append("逐年观测:\n" + res.assign(y=res["date"].str[:4]).groupby("y").size().to_string())
    lines.append("标签率(总体)=%.4f" % res["label"].mean())
    lines.append("标签率(分品种):\n" + res.groupby("symbol")["label"].agg(["mean", "size"]).round(4).to_string())
    io.open(LOG, "w", encoding="utf-8").write("\n".join(lines))
    print("\n".join(lines))

if __name__ == "__main__":
    main()
