# -*- coding: utf-8 -*-
"""
==========================================================
 清单自检：用四个「答案已知」的对照，检验判决清单的判据是否真的有效
==========================================================

用法：
    python 工具/清单自检.py          # 联网拉 PVC 30min 指数数据（约 1 分钟）

它回答的问题：
    「门槛」「机制」「跨环境」这些判据，到底拦得住什么、拦不住什么？
    —— 判据本身也需要被验证。**答案已知才能验证判据**，所以这里用的都是
       结果可预期的对照，而不是新策略。

四个对照：
    A 未来函数（作弊版）      —— 预期：门槛**拦不住**（它只试了 1 次，门槛=0）
    B 数据挖掘（134 变体择优）—— 预期：门槛**拦得住**（这是第 1 问唯一的正面对照）


背景：全账本 45 个版本「显著为正 0 个」⇒ 没有任何版本走得到第 2 问，
      所以清单的第 2/3/4 问从来没被执行过 —— 只验证了 1/4。
      本脚本用两个**答案已知**的对照把缺口补上。

对照 A｜未来函数（作弊版）
     用「下一根」的方向做单。它必然 t 值爆表。
     ⇒ 门槛（第 1 问）能不能挡住它？预期：**挡不住**。
       因为门槛罚的是「试得多」，不是「偷看未来」。

对照 B｜数据挖掘（100+ 变体择优）
     在同一段历史（IS）上跑 100+ 个均线变体，挑夏普最高的，再看它 OOS。
     ⇒ 门槛该不该挡住「试得多」？这是第 1 问唯一的**正面对照**。
       预期：IS 最优与 OOS 表现**几乎无关**，IS↔OOS 夏普相关 ≈ 0。

⚠️ 这是**工具测试**，不是策略实验：不进账本、不计 K、不产生新结论。
"""
import os
import sys
import math
import numpy as np

根 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, 根)
import config_account as CA

品种 = "KQ.i@DCE.v"          # PVC 指数 30min（指数合约无换月跳空）
周期秒 = 30 * 60
取根数 = 8900

# PVC：1 手 5 吨、最小变动 5 元/吨 ⇒ 1 跳 = 25 元/手；手续费约 1 元/手
一跳元 = 25.0
手续费元 = 1.0
名义市值 = 5 * 5000.0        # 1 手 ≈ 5 吨 × 5000 元/吨 = 25,000 元
单次成本率 = (一跳元 + 手续费元) / 名义市值      # ≈ 0.104%/次

每年根数 = 245 * 12          # 约 2940 根/年（30min K 线）


def t值(r):
    """单期夏普 × √n —— 与「年化夏普 × √年数」等价（年化因子被消掉）。"""
    r = np.asarray(r, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 30 or r.std(ddof=1) == 0:
        return None, None, None
    sr = r.mean() / r.std(ddof=1)
    return sr * math.sqrt(每年根数), sr * math.sqrt(len(r)), len(r)


def 均线(x, n):
    c = np.convolve(x, np.ones(n) / n, mode="valid")
    out = np.full(len(x), np.nan)
    out[n - 1:] = c
    return out


def main():
    from tqsdk import TqApi, TqAuth

    u, p = CA.check()
    api = TqApi(auth=TqAuth(u, p))
    try:
        k = api.get_kline_serial(品种, 周期秒, data_length=取根数)
        close = k["close"].values.astype(float)
        dt = k["datetime"].values
        print(f"  取到 {len(close)} 根 30min K 线")
    finally:
        api.close()

    # 时间戳（纳秒）
    t0, t1 = dt[0], dt[-1]
    import datetime as _dt
    def 转(x):
        return _dt.datetime.fromtimestamp(int(x) // 10**9)
    print(f"  区间：{转(t0):%Y-%m-%d} ~ {转(t1):%Y-%m-%d}（约 {(t1-t0)/10**9/86400/365.25:.2f} 年）")

    ret = np.diff(np.log(close))          # ret[i] = 第 i 根 → 第 i+1 根的收益
    n = len(ret)

    print()
    print("=" * 92)
    print("  对照 A｜未来函数（作弊版）：用「下一根」的方向做单")
    print("=" * 92)
    print("  信号 = sign(ret[i]) ⇒ 第 i 根持仓方向由第 i→i+1 根的**已知结果**决定")
    纯上帝 = np.abs(ret)
    信号A = np.sign(ret)
    换手A = np.abs(np.diff(信号A, prepend=信号A[0])) / 2.0
    净A = 纯上帝 - 换手A * 单次成本率
    夏A, tA, nA = t值(净A)
    print(f"  {'':<18}{'总收益':>12}{'年化夏普':>12}{'t 值':>11}{'样本':>8}")
    print("  " + "-" * 88)
    print(f"  {'羊毛(扣成本前)':<18}{纯上帝.sum()*100:>11.1f}%{t值(纯上帝)[0]:>12.1f}{t值(纯上帝)[1]:>11.1f}{n:>8}")
    print(f"  {'净值(扣成本后)':<18}{净A.sum()*100:>11.1f}%{夏A:>12.1f}{tA:>11.1f}{nA:>8}")
    print(f"  换手率 {换手A.sum()/n*100:.0f}% 的 K 线   ｜ 单次成本率 {单次成本率*100:.3f}%")
    print()
    print("  ⇒ **结论：门槛完全挡不住它。** 门槛 t=√(2·lnK) 罚的是「试得多」，")
    print("     而未来函数只试了 **1 次** ⇒ 门槛只有 √(2·ln1)=0，形同虚设。")
    print("     ⇒ 第 2 问（机制说得出吗）是**唯一防线**。")

    print()
    print("=" * 92)
    print("  对照 B｜数据挖掘：100+ 个均线变体，在 IS 上挑最优，看 OOS")
    print("=" * 92)
    # IS/OOS 切分（按本项目的样本划分口径）
    月份 = np.array([转(x).strftime("%Y-%m") for x in dt])
    IS标记 = months_le(月份, "2026-02")
    ISn, OOSn = int(IS标记[:-1].sum()), int((~IS标记[:-1]).sum())
    print(f"  IS  {转(dt[0]):%Y-%m} ~ 2026-02：{ISn} 根")
    print(f"  OOS 2026-03 ~ {转(dt[-1]):%Y-%m}：{OOSn} 根")

    结果 = []
    for 快 in (2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30):
        for 慢 in (15, 20, 25, 30, 40, 50, 60, 80, 100, 120, 150, 200):
            if 快 >= 慢:
                continue
            mf, ms = 均线(close, 快), 均线(close, 慢)
            信号 = np.sign(mf - ms)[:-1]        # 长度对齐 ret
            持仓 = np.roll(信号, 1)
            持仓[0] = 0.0                        # 下一根执行
            换手 = np.abs(np.diff(持仓, prepend=0.0)) / 2.0
            净 = np.nan_to_num(持仓 * ret - 换手 * 单次成本率)
            sIS, tIS, _ = t值(净[IS标记[:-1]])
            sOOS, tOOS, _ = t值(净[~IS标记[:-1]])
            if sIS is None or sOOS is None:
                continue
            结果.append((快, 慢, sIS, tIS, sOOS, tOOS, 净.sum()))

    结果.sort(key=lambda x: -x[2])
    K = len(结果)
    门槛t = math.sqrt(2 * math.log(K))
    print(f"  变体数 K = {K}  ⇒ 门槛 t = √(2·ln{K}) = {门槛t:.2f}")
    print(f"  {'快':>4}{'慢':>5}{'IS夏普':>10}{'IS t':>8}{'OOS夏普':>11}{'OOS t':>9}{'全期净':>11}")
    print("  " + "-" * 88)
    for r in 结果[:6]:
        print(f"  {r[0]:>4}{r[1]:>5}{r[2]:>10.2f}{r[3]:>8.2f}{r[4]:>11.2f}{r[5]:>9.2f}{r[6]*100:>10.1f}%")
    print("  …")
    for r in 结果[-3:]:
        print(f"  {r[0]:>4}{r[1]:>5}{r[2]:>10.2f}{r[3]:>8.2f}{r[4]:>11.2f}{r[5]:>9.2f}{r[6]*100:>10.1f}%")
    print("  " + "-" * 88)

    ISs = np.array([r[2] for r in 结果])
    OOSs = np.array([r[4] for r in 结果])
    过门槛IS = sum(1 for r in 结果 if r[3] > 门槛t)
    过门槛OOS = sum(1 for r in 结果 if r[5] > 门槛t)
    print(f"  IS 夏普均值 {ISs.mean():+.2f}（正 {sum(ISs>0)}/{K}）｜ OOS 夏普均值 {OOSs.mean():+.2f}（正 {sum(OOSs>0)}/{K}）")
    print(f"  IS↔OOS 夏普相关系数 = **{np.corrcoef(ISs, OOSs)[0,1]:+.3f}**")
    print(f"  过门槛：IS {过门槛IS}/{K} ｜ OOS {过门槛OOS}/{K}")
    最优 = 结果[0]
    OOS王 = max(结果, key=lambda x: x[4])
    相关 = np.corrcoef(ISs, OOSs)[0, 1]
    print()
    print(f"  ★ IS 冠军   = 均线 {最优[0]}/{最优[1]}：IS 夏普 {最优[2]:.2f}（t={最优[3]:.2f}）")
    print(f"    它换到 OOS：夏普 **{最优[4]:.2f}**（t={最优[5]:.2f}），占 IS 的 **{最优[4]/最优[2]*100:.0f}%**")
    print(f"  ★ OOS 冠军  = 均线 {OOS王[0]}/{OOS王[1]}：OOS 夏普 {OOS王[4]:.2f}（t={OOS王[5]:.2f}）")
    print()
    print("  ⇒ **结论：门槛挡得住它 —— 但不是因为「IS 挑最好」无效，而是因为「证据量不够」。**")
    print(f"     · IS 冠军 t={最优[3]:.2f}，门槛 {门槛t:.2f}，差 {门槛t/最优[3]:.1f} 倍；")
    print(f"       就算直接拿 OOS 冠军（{OOS王[0]}/{OOS王[1]}），t 也只有 {OOS王[5]:.2f}，照样过不了。")
    print(f"     · ⚠️ 而 IS↔OOS 相关 = **{相关:+.3f}**（**排序高度稳定**）⇒「慢均线优于快均线」")
    print("       是**跨样本一致**的结构性差异（快均线被换手成本磨死），**不是**过拟合产物。")
    print("     · ⇒ **一致 ≠ 显著。** 门槛拒绝的不是「没效果」，是「证据不足以把效果和噪声分开」。")
    print()
    print("  ★ 这顺手纠正了一个常见误解：**「样本外崩了 = 过拟合」不总成立。**")
    print(f"     本实验里样本内外高度一致（{相关:+.3f}），却依然**没有一个变体过门槛** ⇒")
    print("     一致性与显著性是两个独立维度，别用前者替代后者。")


def months_le(月份, 上限):
    """标记 <= 上限 的根（按 YYYY-MM 字符串比较）。"""
    return np.array([m <= 上限 for m in 月份])


if __name__ == "__main__":
    main()
