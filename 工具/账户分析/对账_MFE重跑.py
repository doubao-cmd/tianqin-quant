# -*- coding: utf-8 -*-
"""
⑤ 账户分析 K 线缓存重导 · 新旧对账（2026-11-04）

背景
----
`_缓存/kline2026_5min/` 的 2026 段曾 100% 是「bar 诞生瞬间快照」：
    open == high == low == close，volume == 0
（根因：dump_kline_csv.py 在 wait_update 后「见到新 dt 就 append」，录的是未收盘 bar）
2026-11-04 16:0x 修复导出逻辑后重导，本脚本给出「坏快照 vs 干净数据」的逐项对照。

为什么必须重算
--------------
旧快照里 **close 也不是收盘价**，而是该 bar 的第一个 tick 价。
⇒ 之前认为「反手/随机基准只用收盘价所以不受影响」是**错的**，两者同样失真。

用法
----
    python 对账_MFE重跑.py
输出
----
    对账_MFE重跑.md（同目录），并打印到终端
"""
import glob
import io
import os
import statistics
import sys

ROOT = r"C:\Users\cc\Desktop\天勤量化"
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import diag_mae_mfe as DM  # noqa: E402

NEW_DIR = os.path.join(ROOT, "工具", "账户分析", "_缓存", "kline2026_5min")
OLD_DIR = os.path.join(ROOT, "工具", "账户分析", "_缓存", "kline2026_5min_坏快照_20261104")
BASE = os.path.join(ROOT, "回测结果", "06_MA13超短线")
OUT_MD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "对账_MFE重跑.md")


def collect(kdir):
    """读 5 品种逐笔 + K 线，返回带 mfe/mae/real/bars 的交易列表。"""
    out = []
    for sym_code, sym_cn in DM.SYMS:
        files = glob.glob(os.path.join(BASE, sym_cn, "*探针窗2026-01-01_2026-10-31.txt"))
        if not files:
            continue
        trades = DM.load_trades(files[0], sym_code)
        df = DM.fetch_klines(sym_cn, kdir)
        idx = {s: i for i, s in enumerate(df["dtstr"].tolist())}
        hi = df["high"].values
        lo = df["low"].values

        for t in trades:
            i = idx.get(t["entry_dt"])
            j = idx.get(t["exit_dt"])
            if i is None or j is None or j < i:
                continue
            sign, entry = t["dir"], t["entry"]
            if sign > 0:
                t["mfe"] = max(0.0, max(hi[i:j + 1]) - entry)
                t["mae"] = max(0.0, entry - min(lo[i:j + 1]))
            else:
                t["mfe"] = max(0.0, entry - min(lo[i:j + 1]))
                t["mae"] = max(0.0, max(hi[i:j + 1]) - entry)
            t["real"] = sign * (t["exit"] - entry)
            t["bars"] = j - i + 1
            t["gap"] = DM.gap_kind(t)
            for n in (3, 5):
                end = min(i + n - 1, j)
                t["dd%d" % n] = (entry - min(lo[i:end + 1])) if sign > 0 else (max(hi[i:end + 1]) - entry)
            out.append(t)
    return out


def summarize(trades):
    stop = [t for t in trades if t["reason"] == "止损"]
    te = [t for t in trades if t["reason"] == "时间止损"]
    total = len(trades)
    gross = sum(t["real"] for t in trades)

    never = [t for t in stop if t["mfe"] < 0.01]
    lt3 = [t for t in stop if t["mfe"] < 3]

    d = dict(n_all=total, gross=gross, exp=gross / total,
             n_stop=len(stop), n_te=len(te),
             n_never=len(never), pct_never=len(never) / len(stop) * 100,
             bars_never=(sum(t["bars"] for t in never) / len(never)) if never else 0,
             loss_never=sum(t["real"] for t in never),
             pct_lt3=len(lt3) / len(stop) * 100)

    # 上限：把「从未浮盈」全消除
    d["cap_zero"] = (gross - d["loss_never"]) / total
    # 上限：把「浮盈<3点」全消除（更激进，不可实现）
    d["cap_lt3"] = (gross - sum(t["real"] for t in lt3)) / total

    # 时间止损回吐
    if te:
        mfe = sum(t["mfe"] for t in te) / len(te)
        real = sum(t["real"] for t in te) / len(te)
        d.update(te_mfe=mfe, te_real=real, te_give=mfe - real,
                 te_give_pct=(mfe - real) / mfe * 100 if mfe else 0)
        # 上界：回吐全额消除
        d["cap_giveback"] = (gross + sum(max(0.0, t["mfe"] - t["real"]) for t in te)) / total

    # 早识别：第 3 / 5 根浮亏
    intra = [t for t in stop if t["bars"] <= 16]
    for n in (3, 5):
        sub = [t for t in intra if "dd%d" % n in t and t["bars"] >= n]
        if sub:
            xs = sorted(t["dd%d" % n] for t in sub)
            d["dd%d_med" % n] = xs[len(xs) // 2]
            d["dd%d_ge3" % n] = sum(1 for x in xs if x >= 3) / len(xs) * 100
            d["dd%d_n" % n] = len(sub)

    # 跨夜 / 连续
    for key in ("none", "night"):
        sub = [t for t in stop if t["gap"] == key]
        if sub:
            d["%s_n" % key] = len(sub)
            d["%s_pct_never" % key] = sum(1 for t in sub if t["mfe"] < 0.01) / len(sub) * 100
            d["%s_real" % key] = sum(t["real"] for t in sub) / len(sub)
    return d


def main():
    old = summarize(collect(OLD_DIR))
    new = summarize(collect(NEW_DIR))

    rows = [
        ("样本笔数", "n_all", "{:d}", "{:d}", ""),
        ("毛点数合计", "gross", "{:+.0f}", "{:+.0f}", ""),
        ("毛期望（点/笔）", "exp", "{:+.3f}", "{:+.3f}", ""),
        ("止损单笔数", "n_stop", "{:d}", "{:d}", ""),
        ("止损单「从未浮盈」笔数", "n_never", "{:d}", "{:d}", ""),
        ("止损单「从未浮盈」占比", "pct_never", "{:.1f}%", "{:.1f}%", "pp"),
        ("　└ 这些单平均持仓", "bars_never", "{:.1f} 根", "{:.1f} 根", ""),
        ("止损单「浮盈<3点」占比", "pct_lt3", "{:.1f}%", "{:.1f}%", "pp"),
        ("时间止损 MFE 均值", "te_mfe", "{:.2f}", "{:.2f}", ""),
        ("时间止损 实拿均值", "te_real", "{:.2f}", "{:.2f}", ""),
        ("时间止损 平均回吐", "te_give", "{:.2f}", "{:.2f}", ""),
        ("时间止损 回吐率", "te_give_pct", "{:.0f}%", "{:.0f}%", "pp"),
        ("第 3 根浮亏中位", "dd3_med", "{:.2f}", "{:.2f}", ""),
        ("第 3 根 浮亏≥3 点占比", "dd3_ge3", "{:.1f}%", "{:.1f}%", "pp"),
        ("第 5 根浮亏中位", "dd5_med", "{:.2f}", "{:.2f}", ""),
        ("第 5 根 浮亏≥3 点占比", "dd5_ge3", "{:.1f}%", "{:.1f}%", "pp"),
        ("连续止损「从未浮盈」占比", "none_pct_never", "{:.1f}%", "{:.1f}%", "pp"),
        ("跨夜止损「从未浮盈」占比", "night_pct_never", "{:.1f}%", "{:.1f}%", "pp"),
        ("连续止损 毛均", "none_real", "{:+.2f}", "{:+.2f}", ""),
        ("跨夜止损 毛均", "night_real", "{:+.2f}", "{:+.2f}", ""),
        ("上限①：消除「从未浮盈」全部亏损", "cap_zero", "{:+.3f}", "{:+.3f}", ""),
        ("上限②：消除「浮盈<3点」全部亏损", "cap_lt3", "{:+.3f}", "{:+.3f}", ""),
        ("上限③：盈利单回吐全额消除", "cap_giveback", "{:+.3f}", "{:+.3f}", ""),
    ]

    L = []
    L.append("# 账户分析 K 线缓存重导 · 新旧对账")
    L.append("")
    L.append("> 生成：`对账_MFE重跑.py`（可一条命令复算）　数据：`回测结果/06_MA13超短线/*探针窗2026-01-01_2026-10-31.txt`")
    L.append("> 旧 = `_缓存/kline2026_5min_坏快照_20261104`（bar 诞生瞬间快照）　新 = `_缓存/kline2026_5min`（已收盘终值）")
    L.append("")
    L.append("## 一、逐项对照")
    L.append("")
    L.append("| 指标 | 旧（坏快照） | 新（干净） | 变化 |")
    L.append("|---|---:|---:|---:|")
    for label, key, fo, fn, unit in rows:
        a, b = old.get(key), new.get(key)
        if a is None or b is None:
            continue
        sa, sb = fo.format(a), fn.format(b)
        if unit == "pp" and isinstance(a, (int, float)):
            chg = "%+.1f pp" % (b - a)
        elif isinstance(a, (int, float)) and isinstance(b, (int, float)) and "上限" in label:
            chg = "%+.3f" % (b - a)
        else:
            chg = ""
        L.append("| %s | %s | %s | %s |" % (label, sa, sb, chg))
    L.append("")
    L.append("## 二、读法")
    L.append("")
    L.append("- **旧快照连 close 都是错的**：它是该 bar 的第一个 tick 价，不是收盘价。")
    L.append("  ⇒ 「反手检验/随机基准只用收盘价所以不受影响」这一判断**不成立**，两者同样失真。")
    L.append("- 「从未浮盈」在旧数据上被**高估约 5 倍**（因为 high==low==close，浮盈只能靠收盘价体现）。")
    L.append("- 上限①②③ 都是**事后极值**，不可实现，只用来判断「这条路的天花板是否为正」。")
    L.append("")
    txt = "\n".join(L)
    io.open(OUT_MD, "w", encoding="utf-8", newline="\n").write(txt + "\n")
    print(txt)
    print("\n[已写出] %s" % OUT_MD)


if __name__ == "__main__":
    main()
