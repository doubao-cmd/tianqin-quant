# -*- coding: utf-8 -*-
"""PVC 30 分钟序列 · 方差比(VR)体检 —— 步 0，纯统计诊断，不涉及任何策略参数。

回答一个问题：30 分钟尺度上，价格还是不是随机游走？

为什么先做这一步：
  「改出场」被鞅的可选停止定理否证（工具/MFE诊断.py），但那条定理的前提是
  「价格是鞅」。现有证据只到「5 分钟无法拒绝随机游走」(VR=0.972, |z|<1.96)，
  30 分钟尺度的 VR 从未测过 —— 这是让结论更硬唯一剩下的补测。
  它同时是所有「择时类」想法（改出场 / 时段过滤 / 大周期动量 / 拉长持仓）
  的公共前提。

两件事一起做：
  ① 30 分钟自己的 VR(q) —— 判别动量/均值回归/随机
  ② 跨尺度对照：在**相同时间跨度**上，30 分钟 bar 与 5 分钟 bar 算出的 VR
     是否一致。若一致 → 换 K 线周期没改变结论；若 30 分钟更偏离 1 →
     说明粗 bar 去掉微观噪声后暴露了结构（这才是有价值的方向）。

判据（事先写死，避免事后找理由）：
  A. 各段 30 分钟的 VR(2)/(4)/(8) 全部 |z| < 1.96
       → 30 分钟同样拒绝不了随机游走
       → 「择时类」想法（改出场 / 时段过滤 / 大周期择时）整体失去依据，
         只剩「换问题口径」（跨品种价差）一条路。
  B. 任一段显著 → 看符号：VR>1 动量结构，VR<1 均值回归结构。
  C. 各段符号不一致 → 结构不稳定，视同「无」（沿用 5 分钟体检的规则）。
  D. 跨尺度：同一时间跨度下，两套 bar 的 VR 95% 区间是否重叠。
       重叠 → 尺度无关；不重叠且粗 bar 更远 → 粗 bar 揭示了结构。
  F. 【A1 补样本 · 2026-09-19 新增】段数从 3 扩到 5（+2022、+2023）：
     ① 符号同号段数 ≥ ⌈0.8N⌉（N=5 → 需 ≥4 段）
     ② Stouffer 合并 Z = Σz/√N，要求 |Z| ≥ 1.96
     两条同时满足 → **盘活**（值得继续投入样本）；否则 → **维持「无」，判死**。
     ⚠️ 为什么只补 2 段：2020 / 2021 已被结论 14（顺风局诊断）碰过，不再干净；
        2022 / 2023 是仅剩的两段「从未被任何实验用过」的区间。
     ⚠️ 本脚本不取 2025 任何数据（样本外 2025-03-13~09-13 必须保持干净）。

口径说明（与 5 分钟体检保持一致，便于对照）：
  · 对数收益 r = diff(log(close))；点数收益 rp = diff(close)
  · 方差比用 Lo-MacKay(1988) 异方差稳健 z 统计量
  · 结构强度（点/根）= |VR(2) - 1| × mean|rp|，其中 VR(2)-1 ≈ 一阶自相关 ρ1
    （5 分钟那次得 0.15 点/根，往返成本 2.4 点 → 差 13.5 倍）

数据：工具/_归档/序列/KQ-m_DCE-v_{30min,5min}_*.csv（5 段：2019/2022/2023/2024/2026）
用法：python 工具/VR体检_30min.py
"""
import io
import os
import sys

import numpy as np
from scipy.stats import norm

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
序目录 = os.path.join(ROOT, "工具", "_归档", "序列")

段 = [
    ("练习区间 2026-03-13~09-13",
     "KQ-m_DCE-v_30min_2026-03-13_2026-09-13.csv",
     "KQ-m_DCE-v_5min_2026-03-13_2026-09-13.csv"),
    ("独立段 2024-01-01~07-01",
     "KQ-m_DCE-v_30min_2024-01-01_2024-07-01.csv",
     "KQ-m_DCE-v_5min_2024-01-01_2024-07-01.csv"),
    ("独立段 2019-01-01~07-01",
     "KQ-m_DCE-v_30min_2019-01-01_2019-07-01.csv",
     "KQ-m_DCE-v_5min_2019-01-01_2019-07-01.csv"),
    # ---- A1 补样本（2026-09-19）：原判据 C 只有三段、符号不一致。
    #      2022 / 2023 是仅剩的两段「从未被任何实验碰过」的区间
    #      （2020/2021 已被结论 14 顺风局诊断用过）。
    ("补样本 2022-01-01~07-01",
     "KQ-m_DCE-v_30min_2022-01-01_2022-07-01.csv",
     "KQ-m_DCE-v_5min_2022-01-01_2022-07-01.csv"),
    ("补样本 2023-01-01~07-01",
     "KQ-m_DCE-v_30min_2023-01-01_2023-07-01.csv",
     "KQ-m_DCE-v_5min_2023-01-01_2023-07-01.csv"),
]

# (跨度名, 30min 的 q, 5min 的 q)  —— 5min 的 q = 30min 的 q × 6
跨度 = [("1 小时", 2, 12), ("2 小时", 4, 24), ("4 小时", 8, 48),
        ("8 小时", 16, 96), ("16 小时", 32, 192), ("32 小时", 64, 384)]


# ---------------------------------------------------------------- 基础工具
def 读序列(路径):
    ts, cl = [], []
    with io.open(路径, encoding="utf-8") as f:
        next(f)
        for line in f:
            p = line.rstrip("\n").split(",")
            if len(p) < 5:
                continue
            ts.append(p[0])
            cl.append(float(p[4]))
    return np.array(ts), np.array(cl)


def 自相关(x, K):
    d = x - x.mean()
    den = (d * d).sum()
    return np.array([(d[k:] * d[:-k]).sum() / den for k in range(1, K + 1)])


def 方差比(r, q):
    """Lo-MacKay(1988) 方差比 + 异方差稳健 z 统计量。"""
    N = len(r)
    mu = r.mean()
    d = r - mu
    var1 = (d * d).sum() / (N - 1)
    cs = np.concatenate(([0.0], np.cumsum(d)))
    qs = cs[q:] - cs[:-q]
    varq = (qs * qs).sum() / len(qs)
    VR = varq / (q * var1)
    s2 = (d * d).sum()
    theta = 0.0
    for j in range(1, q):
        delta = (d[j:] ** 2 * d[:-j] ** 2).sum() / (s2 * s2)
        theta += (2.0 * (q - j) / q) ** 2 * delta
    z = (VR - 1) / np.sqrt(theta) if theta > 0 else np.nan
    return VR, z


def 星号(z):
    if not np.isfinite(z):
        return "  "
    a = abs(z)
    return "**" if a >= 2.58 else ("* " if a >= 1.96 else "  ")


def p值(z):
    return 2 * (1 - norm.cdf(abs(z))) if np.isfinite(z) else float("nan")


def 区间(vr, z):
    """VR 的 95% 置信区间（以 z 的定义反推）。"""
    if not np.isfinite(z) or vr == 1:
        return None
    se = abs(vr - 1) / abs(z)
    return vr - 1.96 * se, vr + 1.96 * se


# ---------------------------------------------------------------- 单段体检
def 检验一段(名, f30, f5, out):
    def P(s=""):
        out.append(s)

    p30 = os.path.join(序目录, f30)
    p5 = os.path.join(序目录, f5)
    if not os.path.exists(p30) or not os.path.exists(p5):
        缺 = f30 if not os.path.exists(p30) else f5
        P(f"  [缺] {缺}")
        return None

    ts30, c30 = 读序列(p30)
    ts5, c5 = 读序列(p5)
    r30, rp30 = np.diff(np.log(c30)), np.diff(c30)
    r5, rp5 = np.diff(np.log(c5)), np.diff(c5)

    P("=" * 100)
    P(f"  {名}")
    P("=" * 100)
    P(f"  30 分钟：{len(c30):,} 根   {ts30[0]} ~ {ts30[-1]}"
      f"   每根 {np.abs(rp30).mean():.2f} 点  σ={rp30.std():.2f} 点")
    P(f"   5 分钟：{len(c5):,} 根   {ts5[0]} ~ {ts5[-1]}"
      f"   每根 {np.abs(rp5).mean():.2f} 点  σ={rp5.std():.2f} 点")
    P(f"   两套 bar 的点数波动比 = {rp30.std() / rp5.std():.3f}"
      f"（√6 = 2.449，越接近说明两序列越自洽）")

    # ① 30 分钟自己的 VR
    P()
    P("  ① 30 分钟 VR(q)      VR=1 随机游走 / >1 动量 / <1 均值回归")
    P(f"     {'q':>4}{'跨度':>10}{'VR':>10}{'z':>9}{'p':>9}   ")
    vr30表 = {}
    for q in (2, 4, 8, 16, 32, 64):
        if q >= len(r30) // 4:
            continue
        VR, z = 方差比(r30, q)
        vr30表[q] = (VR, z)
        跨 = f"{q * 0.5:.0f} 小时" if q * 0.5 < 24 else f"{q * 0.5 / 24:.1f} 天"
        P(f"     {q:>4}{跨:>10}{VR:>10.4f}{z:>+9.2f}{p值(z):>9.4f} {星号(z)}")
    P(f"     90% 的交易日只有 {len(c30) / len(set(t[:10] for t in ts30)):.1f} 根 bar"
      f" → q 超过这个数就是跨日持仓")

    # ② 跨尺度对照（同时间跨度）
    P()
    P("  ② 跨尺度对照 —— 同一时间跨度，粗 bar vs 细 bar")
    P(f"     {'跨度':>9}{'30min q':>9}{'VR30':>9}{'z30':>8}"
      f"{'5min q':>8}{'VR5':>9}{'z5':>8}   两者 95% 区间")
    跨尺度一致 = []
    for 名跨, q30, q5 in 跨度:
        if q30 >= len(r30) // 4 or q5 >= len(r5) // 4:
            continue
        v30, z30_ = 方差比(r30, q30)
        v5, z5_ = 方差比(r5, q5)
        i30, i5 = 区间(v30, z30_), 区间(v5, z5_)
        if i30 and i5:
            重叠 = not (i30[1] < i5[0] or i5[1] < i30[0])
            标记 = "重叠" if 重叠 else "不重叠"
            跨尺度一致.append(重叠)
            细 = f"[{i30[0]:.3f},{i30[1]:.3f}] / [{i5[0]:.3f},{i5[1]:.3f}] {标记}"
        else:
            细 = "-"
        P(f"     {名跨:>9}{q30:>9}{v30:>9.4f}{z30_:>+8.2f}"
          f"{q5:>8}{v5:>9.4f}{z5_:>+8.2f}   {细}")

    # ③ 波动自相关（预期仍然显著）
    a30 = 自相关(np.abs(r30), 20)
    a5 = 自相关(np.abs(r5), 20)
    带30 = 1.96 / np.sqrt(len(r30))
    带5 = 1.96 / np.sqrt(len(r5))
    显30 = [i + 1 for i, v in enumerate(a30) if abs(v) > 带30]
    显5 = [i + 1 for i, v in enumerate(a5) if abs(v) > 带5]
    P()
    P("  ③ 波动自相关 |r| 的 ACF(1..20)（这是唯一预期仍然显著的东西）")
    P(f"     30 分钟：显著 {len(显30)}/20 个 lag   带=±{带30:.4f}   前5: "
      + "  ".join(f"{v:+.3f}" for v in a30[:5]))
    P(f"      5 分钟：显著 {len(显5)}/20 个 lag   带=±{带5:.4f}   前5: "
      + "  ".join(f"{v:+.3f}" for v in a5[:5]))
    P(f"     方向自相关 ACF(1)：30min {自相关(r30,1)[0]:+.4f} / 5min {自相关(r5,1)[0]:+.4f}")

    # ④ 结构强度折算
    vr2 = vr30表.get(2)
    强30 = abs(vr2[0] - 1) * np.abs(rp30).mean() if vr2 else float("nan")
    强5 = abs(方差比(r5, 2)[0] - 1) * np.abs(rp5).mean()
    P()
    P("  ④ 结构强度折算（点/根）= |VR(2) - 1| × 平均每根点数")
    P(f"     30 分钟：{强30:.3f} 点/根     5 分钟：{强5:.3f} 点/根")
    P("     对照：v5 的每笔往返成本 2.56 点（PVC 1 手 5 元/跳）")
    if 强30 > 0:
        P(f"     → 30 分钟需持仓 {2.56 / 强30:.0f} 根才能让结构强度盖过成本"
          f"（≈ {2.56 / 强30 * 0.5:.1f} 小时）")

    # ⑤ 最朴素的动量探针 —— 不依赖任何统计模型，直接看扣成本后能不能活
    cum30 = np.concatenate(([0.0], np.cumsum(rp30)))
    cum5 = np.concatenate(([0.0], np.cumsum(rp5)))
    P()
    P("  ⑤ 最朴素探针：上一根涨就跟多、跌就跟空，持有 K 根（毛，未扣成本）")
    P("     ⚠️ 口径必须跳过信号那一根：第 t 根收盘出信号 → 第 t+1 根入场 →")
    P("        持有 K 根到第 t+1+K 根 → 收益 = close[t+1+K] - close[t+1]。")
    P("        （若写成 close[t+K]-close[t] 会包含信号根自己的涨跌，K=1 时等于 |Δ|，")
    P("          必然给出 +16 点/t=31 的假值 —— 这正是防未来函数的意义。）")
    P(f"     {'K根':>5}{'≈时长':>9}{'跟方向':>10}{'反着做':>10}{'标准误':>9}{'t':>8}{'扣2.56后':>11}")
    探30 = {}
    n30c = len(cum30)
    for K in (1, 2, 3, 4, 6, 8, 12, 16, 24, 32):
        if K >= len(rp30) // 4:
            continue
        ii = np.arange(n30c - 1 - K)
        v = np.sign(rp30[ii]) * (cum30[ii + 1 + K] - cum30[ii + 1])
        毛 = v.mean()
        误 = v.std() / np.sqrt(len(v))
        t = 毛 / 误 if 误 > 0 else float("nan")
        时 = f"{K * 0.5:.0f} 小时" if K * 0.5 < 24 else f"{K * 0.5 / 24:.1f} 天"
        P(f"     {K:>5}{时:>9}{毛:>+10.3f}{-毛:>+10.3f}{误:>9.3f}{t:>+8.2f}{毛 - 2.56:>+11.2f}")
        探30[K] = (毛, t)
    P()
    P("     同口径下 5 分钟 bar 的对照（K×6 对齐同一时间跨度）：")
    P(f"     {'跨度':>7}{'K5':>7}{'跟方向毛':>11}{'t':>9}{'扣2.56后':>11}")
    n5c = len(cum5)
    for K30 in (2, 4, 8):
        K5 = K30 * 6
        if K5 >= len(rp5) // 4:
            continue
        ii = np.arange(n5c - 1 - K5)
        v = np.sign(rp5[ii]) * (cum5[ii + 1 + K5] - cum5[ii + 1])
        毛 = v.mean()
        误 = v.std() / np.sqrt(len(v))
        tv = 毛 / 误 if 误 > 0 else float("nan")
        P(f"     {K30 * 0.5:>4.0f} 小时{K5:>7}{毛:>+11.3f}{tv:>+9.2f}{毛 - 2.56:>+11.2f}")
    P()

    return dict(名=名, n30=len(r30), n5=len(r5), vr30=vr30表,
                a30=自相关(r30, 1)[0], a5=自相关(r5, 1)[0],
                波30=len(显30), 波5=len(显5), 强30=强30, 强5=强5,
                探30=探30, 跨尺度一致=跨尺度一致)


def main():
    out = []
    out.append("")
    out.append("#" * 100)
    out.append("#  PVC 30 分钟序列 · 方差比体检（步 0）")
    out.append("#  判据事前写死：三段 VR(2)/(4)/(8) 全 |z|<1.96 → 30 分钟同样拒绝随机游走")
    out.append("#" * 100)
    out.append("")

    汇总 = []
    for 名, f30, f5 in 段:
        结果 = 检验一段(名, f30, f5, out)
        if 结果:
            汇总.append(结果)

    if not 汇总:
        print("无可用数据")
        return 1

    out.append("")
    out.append("=" * 100)
    out.append("  三段一起看")
    out.append("=" * 100)
    out.append(f"  {'数据段':<26}{'30min根数':>10}{'VR(2)':>9}{'z':>8}"
               f"{'VR(4)':>9}{'z':>8}{'VR(8)':>9}{'z':>8}{'波动显著':>9}{'强度点/根':>10}")
    for s in 汇总:
        row = f"  {s['名']:<26}{s['n30']:>10,}"
        for q in (2, 4, 8):
            if q in s['vr30']:
                row += f"{s['vr30'][q][0]:>9.4f}{s['vr30'][q][1]:>+8.2f}"
            else:
                row += f"{'-':>9}{'-':>8}"
        row += f"{s['波30']:>9}{s['强30']:>10.3f}"
        out.append(row)

    # ---- 自动判定
    out.append("")
    out.append("=" * 100)
    out.append("  判定")
    out.append("=" * 100)

    显著段 = []
    符号 = []
    for s in 汇总:
        sig = False
        for q in (2, 4, 8):
            if q in s['vr30'] and abs(s['vr30'][q][1]) >= 1.96:
                sig = True
        if sig:
            显著段.append(s['名'])
        if 2 in s['vr30']:
            符号.append(s['vr30'][2][0] - 1)

    if not 显著段:
        out.append(f"  【A】{len(汇总)} 段 30 分钟的 VR(2)/(4)/(8) 全部 |z| < 1.96")
        out.append("       → 30 分钟同样拒绝不了随机游走。")
        out.append("       → 「择时类」想法（改出场 / 时段过滤 / 大周期动量 / 拉长持仓）")
        out.append("         整体失去统计依据 —— 它们全部要求价格在某个尺度上偏离鞅。")
        out.append("       → 剩下的只有「换问题口径」：跨品种价差（不预测单品种方向）。")
    elif len(显著段) < len(汇总):
        out.append(f"  【B/C】部分段显著：{显著段}")
        out.append(f"         符号（VR(2)-1）："
                   + "  ".join(f"{v:+.4f}" for v in 符号))
        out.append(f"         {len(汇总)} 段符号不一致 → 按事前判据 C，结构不稳定，视同「无」。")
    else:
        正 = sum(1 for v in 符号 if v > 0)
        out.append(f"  【B】{len(汇总)} 段全部显著。符号："
                   + "  ".join(f"{v:+.4f}" for v in 符号))
        if 正 == len(符号):
            out.append("       → VR 显著 > 1：存在**动量结构**。")
            out.append("       → 「往大周期走 / 拉长持仓」第一次获得数学依据，值得继续。")
        elif 正 == 0:
            out.append("       → VR 显著 < 1：存在**均值回归结构**。")
            out.append("       → 对趋势策略是逆风；反而是均值回归类信号的依据。")
        else:
            out.append("       → 符号不一致，视同「无」。")

    一致 = [c for s in 汇总 for c in s['跨尺度一致']]
    if 一致:
        out.append("")
        out.append(f"  【D】跨尺度对照：{sum(一致)}/{len(一致)} 个时间跨度的 95% 区间重叠。")
        if all(一致):
            out.append("       → 30 分钟与 5 分钟在同一时间跨度上给出同一结论。")
            out.append("       → 换 K 线周期不改变统计结论（bar 粗细不是关键变量）。")
        else:
            out.append("       → 存在不重叠的时间跨度：粗 bar 与细 bar 结论有差异，")
            out.append("         需逐行核对是哪个跨度、往哪个方向偏。")

    # 朴素探针：有没有 (段, K) 组合能扛住成本
    正K = []
    for s in 汇总:
        for K, (毛, t) in sorted(s['探30'].items()):
            if 毛 - 2.56 > 0 and np.isfinite(t) and abs(t) >= 1.96:
                正K.append("%s K=%d（扣成本 %+.2f 点，t=%+.2f）"
                          % (s['名'].split()[-1], K, 毛 - 2.56, t))
    out.append("")
    if 正K:
        out.append("  【E】朴素动量探针（扣 2.56 点成本后仍为正、且 |t|≥1.96）：")
        for x in 正K:
            out.append("       " + x)
        out.append("       → 存在可交易的正毛期望组合，值得进一步验证（仍需独立段确认）。")
    else:
        out.append("  【E】朴素动量探针：没有任何（段, K）组合在扣掉 2.56 点成本后")
        out.append("       仍显著为正 → 最朴素的动量/反转交易在 30 分钟上同样不成立。")
    # ---- 【F】A1 补样本专项判定（2026-09-19，判据事前写死，跑完不改）
    out.append("")
    out.append("=" * 100)
    out.append("  【F】A1 补样本专项判定（判据事前写死：跑完不改）")
    out.append("=" * 100)

    zs = [s['vr30'][2][1] for s in 汇总
          if 2 in s['vr30'] and np.isfinite(s['vr30'][2][1])]
    N = len(符号)
    if N >= 2 and zs:
        正 = sum(1 for v in 符号 if v > 0)
        负 = sum(1 for v in 符号 if v < 0)
        需要 = int(np.ceil(N * 0.8))          # 5 段 → 需要 ≥4 段同号
        Zc = sum(zs) / np.sqrt(len(zs))       # Stouffer 合并
        Pc = p值(Zc)

        out.append(f"  {'数据段':<28}{'VR(2)':>9}{'z':>8}{'符号':>8}")
        for s in 汇总:
            if 2 in s['vr30']:
                v, z = s['vr30'][2]
                out.append(f"  {s['名']:<28}{v:>9.4f}{z:>+8.2f}"
                           f"{'动量' if v > 1 else '回归':>8}")
        out.append("")
        out.append(f"  VR(2) 符号：正（动量）{正} 段 ／ 负（均值回归）{负} 段，共 {N} 段")
        out.append(f"  判据① 符号一致性：需同号 ≥ {需要}/{N} 段 → 实际同号 "
                   f"{max(正, 负)}/{N} 段  " + ("✓" if max(正, 负) >= 需要 else "✗"))
        out.append(f"  判据② Stouffer 合并：Z = Σz/√N = {Zc:+.2f}（p = {Pc:.4f}）"
                   + ("  ✓ 显著" if abs(Zc) >= 1.96 else "  ✗ 不显著"))
        out.append("     ⚠️ Stouffer 假设各段近似独立 —— 不同年份区间可接受；但 2022/2023/2024")
        out.append("        是连续年份，微观结构可能同期变化 → 符号一致也可能只反映")
        out.append("        「这几年都这样」，不等于长期稳定。")
        out.append("")
        if max(正, 负) >= 需要 and abs(Zc) >= 1.96:
            out.append("  【总判】两条判据同时满足 → **30 分钟这条线盘活，值得继续投入样本**。")
            out.append("         ⚠️ 但「盘活」≠「能赚钱」：结论 17 已算出结构强度仅 0.97~1.12 点/根，")
            out.append("            扣掉 2.56 点往返成本后仍需持仓数根才可能盖过成本。")
            out.append("            这是「值得继续」，不是「可交易」。")
        else:
            out.append("  【总判】判据未同时满足 → **维持「无」，30 分钟这条线判死**。")
            out.append("         → 不再往这个尺度追加样本；剩余方向只剩换形态（三腿压榨利润）")
            out.append("            或收口（工程链路 + 波动率风控）。")
    else:
        out.append("  数据不足，无法判定。")

    out.append("")
    out.append("  判据回顾（事前写死，不事后解释）：")
    out.append("    A  各段 VR(2)/(4)/(8) 全 |z|<1.96  → 30 分钟同样随机，择时类全关")
    out.append("    B  任一段显著 → 看符号：>1 动量 / <1 均值回归")
    out.append("    C  各段符号不一致 → 结构不稳定，视同无")
    out.append("    D  跨尺度 95% 区间重叠 → 换 bar 周期不改变结论")
    out.append("    F  A1 专项（见上）：符号同号 ≥ ⌈0.8N⌉ 且 Stouffer |Z|≥1.96 → 盘活；")
    out.append("       否则维持「无」，30 分钟这条线判死")
    out.append("")

    for s in out:
        print(s)

    return 0


if __name__ == "__main__":
    sys.exit(main())
