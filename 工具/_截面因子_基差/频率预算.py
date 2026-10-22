# -*- coding: utf-8 -*-
"""
截面基差因子 —— 截面「频率预算」（覆盖度预算），冻结前必做。

与单因子频率预算不同：截面因子要的不是「信号触发次数」，而是
  ★ 每个再平衡日，横截面上有多少个「有效品种」（基差率非 NaN）
  ★ 横截面的基差率离散度（若全池同向，则排序无区分度）

数据：akshare futures_spot_price_daily（逐日抓取，慢 ⇒ 必须后台跑）
输出：工具/_截面因子_基差/数据/基差_<起>_<止>.csv + 频率预算_结果.txt
"""
import os
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
import akshare as ak

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "数据")
os.makedirs(OUT_DIR, exist_ok=True)

START = "20190101"
END = "20211231"
CSV = os.path.join(OUT_DIR, f"基差_{START}_{END}.csv")
LOG = os.path.join(HERE, "频率预算_结果.txt")


def main():
    out = []
    def log(s=""):
        print(s, flush=True)
        out.append(str(s))

    log("=" * 78)
    log("截面基差因子 · 截面频率预算（覆盖度）")
    log(f"数据 ak.futures_spot_price_daily  {START} ~ {END}")
    log("=" * 78)

    if os.path.exists(CSV):
        df = pd.read_csv(CSV)
        log(f"复用已落盘数据 {CSV}")
    else:
        t = time.time()
        df = ak.futures_spot_price_daily(start_day=START, end_day=END)
        log(f"拉取耗时 {time.time()-t:.0f}s  shape={df.shape}")
        df.to_csv(CSV, index=False, encoding="utf-8-sig")
        log(f"落盘 {CSV}")

    # ⚠️ date 列落盘后是 20190102 形式的**整数**；pd.to_datetime 会把整数当成
    # 「自 epoch 的纳秒」解析 ⇒ 全部静默塌成 1970-01-01（不报错）。
    # 2026-10-22 D2 修：原写法只有这一处漏改（回测_截面基差.py:56-58 当时已正确处理，
    # 注释也写了同一个坑，但兄弟文件没跟着改）⇒ 走缓存路径重跑时「再平衡期数=1」，
    # gate 会误判成 ❌「数据可行性不通过」。两条路径（拉数 / 复用）必须行为一致。
    if pd.api.types.is_numeric_dtype(df["date"]):
        df["date"] = pd.to_datetime(df["date"].astype("int64").astype(str), format="%Y%m%d")
    else:
        df["date"] = pd.to_datetime(df["date"])
    df["basis"] = pd.to_numeric(df["dom_basis_rate"], errors="coerce")

    log(f"\n交易日 {df['date'].nunique()} 天  区间 {df['date'].min().date()} ~ {df['date'].max().date()}")
    log(f"出现过的品种 {df['symbol'].nunique()} 个：{sorted(df['symbol'].unique())}")

    per = df.groupby("date")["basis"].agg(["count", "std"])
    log("\n【每日横截面】")
    log(f"  有效品种数  min/中位/max = {int(per['count'].min())} / {int(per['count'].median())} / {int(per['count'].max())}")
    log(f"  基差率横截面 std 中位 = {per['std'].median():.4f}")

    # 月末再平衡日
    df["ym"] = df["date"].dt.to_period("M")
    rows = []
    for ym, g in df.groupby("ym"):
        last = g["date"].max()
        gg = g[g["date"] == last]
        rows.append({
            "期": str(ym),
            "月末日": last.date(),
            "有效品种": int(gg["basis"].notna().sum()),
            "跨度(std)": round(float(gg["basis"].std()), 4),
        })
    m = pd.DataFrame(rows)
    log("\n【月末再平衡日】前 12 期：")
    log(m.head(12).to_string(index=False))
    log(f"\n月末有效品种数  min/中位/max = {int(m['有效品种'].min())} / {int(m['有效品种'].median())} / {int(m['有效品种'].max())}")
    log(f"再平衡期数 = {len(m)}（阈值：≥24）")

    ok = (m["有效品种"] >= 15).mean()
    log(f"\n有效品种数 ≥15 的期数占比 = {ok:.0%}（阈值：多数期 ≥15）")

    log("\n" + "-" * 78)
    # 2026-10-22 D2 措辞修正：原写「可冻结参数进引擎」把**前置数据可行性检查**
    # 冒充成了**最终裁决**。本 gate 只判"数据够不够跑"，不判"因子行不行"——
    # 卡片 §五 四条判据才是裁决者。原措辞导致产物末行 ✅ 与卡片 ⛔ 直接对立
    # （产物 2026-10-12 23:40 写「可冻结」，卡片 2026-10-13 判「无效」）。
    if ok >= 0.9 and len(m) >= 24:
        log("✅ 数据可行性通过：宽度够、期数够 ⇒ **可进入 IS 回测**")
        log("   ★ 本行只说明「数据够跑」，不等于因子有效；有效性由卡 §五 四条判据裁决。")
    else:
        log("❌ 数据可行性不通过 ⇒ 按卡规定收档，不调参")
    log("=" * 78)

    # newline="\n"：Windows 文本模式默认把 \n 写成 \r\n，会让产物每次重跑都产生
    # 纯行尾 diff 噪声（项目规范 txt/csv = LF，见 .gitattributes）
    with open(LOG, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
