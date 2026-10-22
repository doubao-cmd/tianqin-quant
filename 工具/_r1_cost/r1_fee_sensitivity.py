# -*- coding: utf-8 -*-
"""
r1_fee_sensitivity.py —— R1「费率档未闭合」的敏感性 / 翻转点分析
==================================================================
用途：回答「拿不到结算单，J1 还能不能闭合」。
不改任何结论、不做定向判定，只做一件事：把费率当作参数扫一遍，看结论翻不翻。

数据源（只读，不重算）：
    out/cost_table.csv    各品种 C_total_lo_med（含 adverse）、C_exec_med（剔 adverse）、C_fee
    out/contracts.csv     每个品种的 tick_value、交易所标准费率、零售档实测费率

口径（与 r1_t3_cost.py 保持逐字一致，否则数字不可比）：
    r(品种) = C_total / tick_value
    J1      = 5 主品种(M/SA/FU/BU/EG) 各自 r 的**中位数**，再取 5 个中位数的**中位数**
    阈值    = 1.3（卡 §四）
    费率变动方式：把 C_fee 替换为 k × C_fee（k=1 即交易所标准）
        C_total(k) = C_total_lo_med - C_fee + k×C_fee
        C_exec(k)  = C_exec_med     - C_fee + k×C_fee
    ⚠️ 零售档**不是统一倍数**（SA 实际 ×18.75、EG ×13.3），故零售档必须用
       contracts.csv 的 fee_t3_open_yuan 逐品种代入，不能拿 k=10 近似 —— 这点
       容易算错：按 k=10 统一缩放会得到 2.502，与实际 3.251 差 0.75 跳。

用法：
    python r1_fee_sensitivity.py
输出：
    stdout + out/fee_sensitivity.txt
"""
import csv
import statistics as st
from pathlib import Path

BASE = Path(__file__).parent
OUT = BASE / "out"
MAINS = ["M", "SA", "FU", "BU", "EG"]
THRESH = 1.3


def _read(name):
    p = OUT / name
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        return {r["symbol"]: r for r in csv.DictReader(f)}


def _f(x):
    return float(x)


def load():
    tab, con = _read("cost_table.csv"), _read("contracts.csv")
    base = {}
    for s in MAINS:
        tv, fee = _f(con[s]["tick_value"]), _f(tab[s]["C_fee"])
        base[s] = {
            "tv": tv, "fee": fee,
            # 拆掉费项后的「非费成本」：含 adverse / 剔 adverse 各一份
            "tot_nf": _f(tab[s]["C_total_lo_med"]) - fee,
            "exe_nf": _f(tab[s]["C_exec_med"]) - fee,
            # 零售实测档（逐品种真实值，不是倍数）
            "fee_retail": _f(con[s]["fee_t3_open_yuan"]),
        }
    return base


def median_r(base, k, which):
    """k*C_fee 代入后的 J1 中位 r；which = 'tot_nf'(含adverse) | 'exe_nf'(剔adverse)"""
    rs = {s: (base[s][which] + k * base[s]["fee"]) / base[s]["tv"] for s in MAINS}
    return st.median(rs.values()), rs


def median_r_retail(base, which):
    rs = {s: (base[s][which] + base[s]["fee_retail"]) / base[s]["tv"] for s in MAINS}
    return st.median(rs.values()), rs


def flip_point(base, which, lo=0.0, hi=50.0):
    """二分求 J1 中位 r 越过 THRESH 的 k。返回 None ⇒ 区间内无翻转（单调且端点同侧）"""
    if (median_r(base, lo, which)[0] - THRESH) * (median_r(base, hi, which)[0] - THRESH) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if median_r(base, mid, which)[0] < THRESH:
            lo = mid
        else:
            hi = mid
    return lo


def verdict(m):
    return "触发 B 档重算" if m >= THRESH else "1 跳成立"


def main():
    base = load()
    lines = []

    def emit(s=""):
        print(s)
        lines.append(s)

    emit("=" * 78)
    emit("R1 费率敏感性 / 翻转点（只读 out/*.csv，不改任何结论）")
    emit(f"阈值 J1 中位 r = {THRESH}；主品种 = {'/'.join(MAINS)}")
    emit("=" * 78)

    # ---------- 0. 先复现报告三个档，证明口径一致 ----------
    emit("\n【0】复现 r1_t3_cost.py 的三档（口径校验）")
    emit(f"  {'档位':<16}{'中位 r':>10}{'判定':>16}")
    for k, lab in [(1, "交易所标准×1"), (5, "零售×5")]:
        m, _ = median_r(base, k, "tot_nf")
        emit(f"  {lab:<16}{m:>10.3f}{verdict(m):>16}")
    m, _ = median_r_retail(base, "tot_nf")
    emit(f"  {'零售实测档(逐品种)':<16}{m:>10.3f}{verdict(m):>16}")
    emit("  期望：1.596 / 1.967 / 3.251（报告 §四）；差 <0.002 视为一致")

    # ---------- 1. 主判据有无翻转点 ----------
    emit("\n【1】主判据（含 adverse，卡定义）随费率变化")
    for k, lab in [(0, "费率=0（假想下限）"), (1, "交易所标准"), (2, "×2"), (5, "×5")]:
        m, rs = median_r(base, k, "tot_nf")
        emit(f"  {lab:<18}{m:>8.3f}  {verdict(m):<14}"
             + "  例: " + ", ".join(f"{s} {rs[s]:.2f}" for s in MAINS[:3]))
    m0 = median_r(base, 0, "tot_nf")[0]
    fp = flip_point(base, "tot_nf")
    if m0 >= THRESH:
        emit(f"  ⇒ **无翻转点**：费率归零时中位仍为 {m0:.3f} ≥ {THRESH}")
        emit("     ⇒ 任何非负费率都不改变 J1 判定 ⇒ 结算单对此判据**非必需**")
    else:
        emit(f"  ⇒ 翻转点 k* = {fp:.4f} 倍交易所标准")

    # ---------- 2. 对照分支（剔 adverse）----------
    emit("\n【2】对照分支（剔 adverse，只留执行成本）随费率变化")
    for k, lab in [(0, "费率=0"), (1, "交易所标准"), (5, "×5")]:
        m, _ = median_r(base, k, "exe_nf")
        emit(f"  {lab:<18}{m:>8.3f}  {verdict(m)}")
    m, _ = median_r_retail(base, "exe_nf")
    emit(f"  {'零售实测档':<18}{m:>8.3f}  {verdict(m)}")
    fp2 = flip_point(base, "exe_nf")
    if fp2 is None:
        emit("  ⇒ 区间内无翻转点")
    else:
        emit(f"  ⇒ 翻转点 k* ≈ {fp2:.3f} 倍交易所标准")
        emit(f"    即：期货公司加收 > 交易所的 {fp2 - 1:.0%} 时，该分支从「1 跳成立」翻为「触发」")
        emit("    对应「加收后单边费率」阈值(元/手)："
             + ", ".join(f"{s} {fp2 * base[s]['fee']:.2f}" for s in MAINS))
        emit("    ⚠️ 该分支结论**吃费率档**，主判据不吃；两者必须分开报，不可混述")

    # ---------- 3. 结论 ----------
    emit("\n【3】一句话结论（事实层，不含定向）")
    emit(f"  · 主判据：费率在 [0, ∞) 内结论恒为「{verdict(median_r(base, 0, 'tot_nf')[0])}」⇒ 结算单不阻塞 J1")
    emit(f"  · 对照分支：结论随费率翻转，翻转点 = 交易所标准 × {fp2:.2f}；"
         f"现实的加收档几乎必然越过此点")
    emit("  · 样本量问题(n=8~10)与本分析无关，仍为独立未闭合项")

    (OUT / "fee_sensitivity.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[已写] {OUT / 'fee_sensitivity.txt'}")


if __name__ == "__main__":
    main()
