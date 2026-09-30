# -*- coding: utf-8 -*-
"""跨品种价差 · 纯算术体检（步 2）
================================================================
目的：**在写任何策略代码之前**，用统计量判断「跨品种价差」这条路值不值得做。
      前五条路（改出场 / 换信号 / 换品种 / 换量仓 / 换尺度）都是"写完策略跑完
      回测才知道不行"，这一步要在写代码前就否掉它。

---------- 逻辑假设（先立假设，再看数据）----------
对 1 · 油粕（豆油 Y − 豆粕 M）
    大豆压榨同源：1 吨大豆 → 约 0.785 吨豆粕 + 0.185 吨豆油。
    油厂按此比例生产销售，某一品种相对另一品种过度偏离时，油厂调整
    生产/套保形成回归压力。**产业逻辑最硬**，且用户本来就在做这两个品种。
对 2 · 煤化工（PVC V − 甲醇 MA）
    成本端都与煤炭相关（PVC 电石法走兰炭-电石、甲醇走煤制）。
    相关性弱于油粕，作为对照。

---------- 价差口径（不引入任何可调参数）----------
对数价差  S_t = ln(P_A,t) − ln(P_B,t)
    = 「等名义金额 1:1」的配比，无需产业系数、无需回归叉优化。
    ⚠️ 不按手数配比（那需要 P_A/P_B 的动态比例），体检阶段只看统计性质。

---------- 事前写死的判据（四条全过才继续，任一不过即关闭）----------
A. 相关性    ρ ≥ 0.70                    —— 配对才有降噪意义
B. 波动比    σ_S / σ_单腿 ≤ 0.70         —— 价差确实比单腿安静（降噪有效）
C. 均值回归  AR(1) 的 b 显著 < 0（t < −2）
             且半衰期 ∈ [4, 60] 根(30分钟) —— 确实回归，且时间尺度合理
D. 成本可行性 总成本率 / σ_价差(单根) ≤ 单品种 PVC 基准
             —— 配对必须真的把价差波动压下来，否则成本翻倍必然恶化
             ⚠️ 初版判据曾用「成本 / σ_eq」，实测发现 σ_eq 由 AR(1) 外推、
                φ→1 时分母 √(1−φ²)→0 爆炸式放大（数学假象），该量**无效**，已弃用。
附加（不作硬判据）：前后半样本的 b 符号必须一致（结构稳定）。

---------- 已知陷阱（本脚本主动处理）----------
· 主连换月跳空：KQ.m@ 主连不复权，换月时价差会出现台阶。脚本检测并报告。
· 微观结构伪影：结论 17 已证明 5 分钟 bar 上的"强均值回归"主要是
  bid-ask bounce 噪声。**故本体检一律用 30 分钟 bar**，不用 5 分钟。

用法：python 工具/配对体检.py [期]
输出：工具/_输出/_配对体检_<期>.txt
"""
import csv
import io
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
序列目录 = os.path.join(ROOT, '工具', '_归档', '序列')

# ---------- 合约参数（由 工具/_查合约参数.py 实测填入）----------
# 结构：品种名 -> (主连代码, 文件名代号, 乘数, 最小变动价位, 手续费元/手)
合约 = {
    'PVC':  ('KQ.m@DCE.v',  'KQ-m_DCE-v',  5.0, 1.0, 1.4),
    '豆粕': ('KQ.m@DCE.m',  'KQ-m_DCE-m', 10.0, 1.0, 1.5),
    '豆油': ('KQ.m@DCE.y',  'KQ-m_DCE-y', 10.0, 1.0, 2.5),
    '甲醇': ('KQ.m@CZCE.MA', 'KQ-m_CZCE-MA', 10.0, 1.0, 2.0),
    '螺纹': ('KQ.m@SHFE.rb', 'KQ-m_SHFE-rb', 10.0, 1.0, 3.0),
    '热卷': ('KQ.m@SHFE.hc', 'KQ-m_SHFE-hc', 10.0, 1.0, 3.0),
    '棕榈油': ('KQ.m@DCE.p',  'KQ-m_DCE-p',  10.0, 2.0, 2.5),
    '菜粕': ('KQ.m@CZCE.RM', 'KQ-m_CZCE-RM', 10.0, 1.0, 1.5),
}
# ⚠️ 豆油 tick：tqsdk 报 1.0 元/吨，但 DCE 豆油真实最小变动价位为 2 元/吨。
#    本体检按 tqsdk 口径（1 跳 = 10 元/手）计算，并在产出里标注该低估。
# ⚠️ 手续费为交易所常见值估算（非实测），占单次成本约 20%，误差影响有限。
#    螺纹/热卷上期所按成交金额万分之一计（约 3 元/手）。

对列表 = [
    ('油粕',   '豆油', '豆粕'),
    ('煤化工', 'PVC', '甲醇'),
]
# 扩大筛查（3 对高相关候选）——⚠️ 承认多重比较风险：测 5 对，至少 1 对可能偶然「通过」。
# 故通过者一律只作**候选**，必须外部样本复验，不得直接进入策略开发。
扩大对 = [
    ('钢材',   '螺纹', '热卷'),
    ('油脂',   '豆油', '棕榈油'),
    ('粕类',   '豆粕', '菜粕'),
]

起, 止 = '2026-03-13', '2026-09-13'


def 读序列(代号):
    p = os.path.join(序列目录, '%s_30min_%s_%s.csv' % (代号, 起, 止))
    if not os.path.exists(p):
        return None
    d = {}
    rows = list(csv.DictReader(io.open(p, newline='', encoding='utf-8')))
    for r in rows:
        try:
            d[r['datetime']] = (float(r['close']), float(r['open']))
        except (ValueError, KeyError):
            pass
    return d


def ar1(y):
    """ΔS_{t+1} = a + b·S_t + ε  →  (b, se_b, phi, 半衰期, sigma_eq, sigma_eps)
    等价离散 OU：S_{t+1} = mu + phi(S_t-mu) + ε，phi = 1 + b。
    """
    s0 = y[:-1]
    ds = np.diff(y)
    n = len(ds)
    X = np.column_stack([np.ones(n), s0])
    coef, *_ = np.linalg.lstsq(X, ds, rcond=None)
    a, b = float(coef[0]), float(coef[1])
    res = ds - X @ coef
    dof = max(n - 2, 1)
    s2 = float(res @ res) / dof
    XtX_inv = np.linalg.inv(X.T @ X)
    se_b = float(np.sqrt(s2 * XtX_inv[1, 1]))
    t_b = b / se_b if se_b > 0 else float('nan')
    phi = 1.0 + b
    if 0.0 < phi < 1.0:
        半衰期 = -np.log(2.0) / np.log(phi)
    else:
        半衰期 = float('inf')
    sigma_eps = float(np.std(res, ddof=1))
    if 0.0 < phi < 1.0:
        sigma_eq = sigma_eps / np.sqrt(1.0 - phi * phi)
    else:
        sigma_eq = float('nan')
    return b, se_b, t_b, phi, 半衰期, sigma_eq, sigma_eps


def 成本率(品种名, 价):
    """单次成交的成本率（占名义价值的比例，即对数价差单位）。
    滑点 1 跳：tick 元/吨 / P 元/吨 = tick/P
    手续费：fee 元/手 / (P × mult) 元/手
    """
    _, _, mult, tick, fee = 合约[品种名]
    return tick / 价 + fee / (价 * mult)


def main():
    期 = sys.argv[1] if len(sys.argv) > 1 else 'in'
    out = []
    P = out.append

    P('#' * 100)
    P('#  跨品种价差 · 纯算术体检（步 2）   区间 %s ~ %s   30 分钟 bar' % (起, 止))
    P('#' * 100)
    P('')

    # ---------- 读取并对齐 ----------
    数据 = {}
    for 名 in 合约:
        d = 读序列(合约[名][1])
        if d is None:
            P('  [!] 缺序列：%s（%s_30min_%s_%s.csv）' % (名, 合约[名][1], 起, 止))
        else:
            数据[名] = d
            P('  读入 %-4s %5d 根' % (名, len(d)))
    P('')
    if len(数据) < 4:
        P('  [!] 数据不全，退出。')
        print('\n'.join(out))
        return 1

    # 单品种基准：PVC 的成本/σ
    v_close = np.array([数据['PVC'][k][0] for k in sorted(数据['PVC'])])
    r_v = np.diff(np.log(v_close))
    单腿成本基准 = {}
    for 名 in [m for m in 合约 if m in 数据]:
        cs = np.array([数据[名][k][0] for k in sorted(数据[名])])
        r = np.diff(np.log(cs))
        单腿成本基准[名] = (float(np.std(r, ddof=1)), float(np.mean(cs)))
    P('【0】单腿基准（30 分钟对数收益率标准差 / 单次成交成本率）')
    P('     %-6s %11s %12s %12s' % ('品种', 'σ(对数)', '单次成本率', '往返成本率'))
    for 名 in [m for m in 合约 if m in 数据]:
        sd, px = 单腿成本基准[名]
        c = 成本率(名, px)
        P('     %-6s %10.4f%% %11.4f%% %11.4f%%' % (名, sd * 100, c * 100, 2 * c * 100))
    sd_v, px_v = 单腿成本基准['PVC']
    c_v = 成本率('PVC', px_v)
    基准比 = 2 * c_v / sd_v
    P('     → 单品种 PVC 对照：往返成本 / σ = %.1f%%  ← 判据 D 的基准' % (基准比 * 100))
    P('')

    # ---------- 逐对体检 ----------
    结果 = {}
    for 对名, A, B in (对列表 + 扩大对):
        if A not in 数据 or B not in 数据:
            P('【对：%s】 %s − %s：序列缺失，跳过' % (对名, A, B))
            P('')
            continue
        P('=' * 100)
        P('【对：%s】  A = %s    B = %s      S = ln(A) − ln(B)' % (对名, A, B))
        P('=' * 100)
        P('')
        ka, kb = sorted(数据[A]), sorted(数据[B])
        共同 = sorted(set(ka) & set(kb))
        P('     对齐后共同 bar：%d 根（A %d / B %d）' % (len(共同), len(ka), len(kb)))
        if len(共同) < 400:
            P('     [!] 共同 bar 太少，跳过本对。')
            P('')
            continue
        pa = np.array([数据[A][k][0] for k in 共同])
        pb = np.array([数据[B][k][0] for k in 共同])
        S = np.log(pa) - np.log(pb)

        # --- 换月跳空检测 ---
        dS = np.diff(S)
        med = np.median(dS)
        mad = np.median(np.abs(dS - med)) or 1e-9
        跳 = np.where(np.abs(dS - med) > 8 * mad * 1.4826)[0]
        P('     换月跳空/异常跳变检测（|ΔS| > 8×稳健σ）：%d 处' % len(跳))
        for i in 跳[:6]:
            P('        %s → %s   ΔS = %+.4f' % (共同[i], 共同[i + 1], dS[i]))
        P('')

        # --- ① 相关性 ---
        ra = np.diff(np.log(pa))
        rb = np.diff(np.log(pb))
        rho = float(np.corrcoef(ra, rb)[0, 1])
        sd_a, sd_b = float(np.std(ra, ddof=1)), float(np.std(rb, ddof=1))
        sd_S = float(np.std(dS, ddof=1))
        单腿均值 = (sd_a + sd_b) / 2
        波动比 = sd_S / 单腿均值
        理论 = np.sqrt(2 * (1 - rho))
        P('  ① 相关性')
        P('     ρ(A, B)                      = %+.3f' % rho)
        P('     σ_A / σ_B                    = %.4f%% / %.4f%%' % (sd_a * 100, sd_b * 100))
        P('     σ_价差                       = %.4f%%' % (sd_S * 100))
        P('     → 波动比 σ_价差 / σ_单腿均值 = %.3f   （ρ=%.2f 时理论值 %.3f）'
          % (波动比, rho, 理论))
        P('     → 判据 A（ρ ≥ 0.70）：%s' % ('过' if rho >= 0.70 else '**不过**'))
        P('     → 判据 B（波动比 ≤ 0.70）：%s' % ('过' if 波动比 <= 0.70 else '**不过**'))
        P('')

        # --- ② 均值回归 / 半衰期 ---
        b, se_b, t_b, phi, 半衰期, sigma_eq, sigma_eps = ar1(S)
        P('  ② 均值回归（AR(1)：ΔS = a + b·S + ε）')
        P('     b = %+.6f   se = %.6f   t = %+.2f' % (b, se_b, t_b))
        P('     → φ = %.5f   半衰期 = %.1f 根(30分钟) = %.1f 小时 = %.1f 天'
          % (phi, 半衰期, 半衰期 * 0.5, 半衰期 * 0.5 / 24))
        P('     σ_ε = %.4f%%   σ_eq（稳态） = %.4f%%' % (sigma_eps * 100, sigma_eq * 100))
        c_ok = (t_b < -2) and (4 <= 半衰期 <= 60)
        P('     → 判据 C（t<−2 且半衰期∈[4,60] 根）：%s' % ('过' if c_ok else '**不过**'))
        P('')

        # --- ②b 剔除换月跳空后重估（稳健性对照，不作判据）---
        if len(跳) > 0:
            idx = [0] + [int(i) + 1 for i in 跳] + [len(S)]
            y2 = np.empty_like(S)
            for i in range(len(idx) - 1):
                a2, b2 = idx[i], idx[i + 1]
                if b2 > a2:
                    y2[a2:b2] = S[a2:b2] - S[a2:b2].mean()
            b_c, _, t_c, _, h_c, _, _ = ar1(y2)
            P('  ②b 剔除换月跳空后重估（在 %d 个跳变点切开、段内去均值）' % len(跳))
            P('     b = %+.6f   t = %+.2f   半衰期 %.1f 根' % (b_c, t_c, h_c))
            P('     对照：b %+.6f → %+.6f   |   半衰期 %.1f → %.1f 根'
              % (b, b_c, 半衰期, h_c))
            P('     → 主连不复权，两腿换月时点不同 → 每次换月都会在价差里插入人造台阶，')
            P('       而台阶会被 AR(1) 误读为「偏离后回归」。故本量仅作稳健性参考。')
            P('')

        # --- ③ 成本可行性 ---
        价A = float(np.mean(pa))
        价B = float(np.mean(pb))
        cA = 成本率(A, 价A)
        cB = 成本率(B, 价B)
        总成本率 = 2 * (cA + cB)
        成本占单根 = 总成本率 / sd_S
        d_ok = 成本占单根 <= 基准比
        P('  ③ 成本可行性（全部换算成对数价差单位）')
        P('     A 腿单次成本率 %.4f%%（滑点 %.4f%% + 费 %.4f%%）  ≈ %.1f 元/手'
          % (cA * 100, (合约[A][3] / 价A) * 100, (合约[A][4] / (价A * 合约[A][2])) * 100,
             cA * 价A * 合约[A][2]))
        P('     B 腿单次成本率 %.4f%%（滑点 %.4f%% + 费 %.4f%%）  ≈ %.1f 元/手'
          % (cB * 100, (合约[B][3] / 价B) * 100, (合约[B][4] / (价B * 合约[B][2])) * 100,
             cB * 价B * 合约[B][2]))
        P('     → 一轮往返（两腿各开各平 = 4 次成交）总成本率 = %.4f%%' % (总成本率 * 100))
        P('     → ★ 主判据 D：总成本率 / σ_价差(单根) = %.1f%%  （单品种 PVC 基准 %.1f%%）'
          % (成本占单根 * 100, 基准比 * 100))
        P('       含义：跨品种成本翻倍；若配对没能把价差波动压下来，这一比值必然恶化。')
        P('     → 判据 D（≤ 单品种基准）：%s' % ('过' if d_ok else '**不过**'))
        if phi < 0.995:
            P('     · 参考：σ_eq（OU 稳态）= %.4f%%，成本/σ_eq = %.1f%%'
              % (sigma_eq * 100, 总成本率 / sigma_eq * 100))
        else:
            P('     · ⚠️ 参考（**不可信**）：φ = %.5f 已逼近 1，AR(1) 对 σ_eq 的外推失效'
              % phi)
            P('       （分母 √(1−φ²) 趋 0 → σ_eq = %.2f%% 是数学假象，物理上不可能）→ 该量不参与判定。'
              % (sigma_eq * 100))
        P('')

        # --- ③b 朴素价差反转探针（最直接的「能不能赚钱」检验）---
        W = 20
        ma = np.array([S[max(0, i - W + 1):i + 1].mean() for i in range(len(S))])
        dev = S - ma
        P('  ③b 朴素反转探针：S 相对 %d 根滚动均值的偏离，反向入场，持有 K 根' % W)
        P('     口径：信号在 t 根收盘出 → t+1 根入场 → 持有 K 根（**跳过信号根**，防未来函数）')
        P('     收益单位 = 对数价差 = 「1:1 名义金额」的收益率；剔除换月跳空异常值。')
        P('     %8s %12s %12s %13s %8s' % ('K根', '毛收益', '标准误', '扣成本后', 't'))
        for K in (1, 2, 4, 8, 16, 32):
            lo = W - 1
            hi = len(S) - 1 - K
            if hi <= lo:
                continue
            j = np.arange(lo, hi + 1)
            v = -np.sign(dev[j]) * (S[j + K] - S[j + 1])
            v = v[np.abs(v) < 1.0]
            if len(v) < 30:
                continue
            毛 = float(v.mean())
            误 = float(v.std(ddof=1) / np.sqrt(len(v)))
            P('     %8d %11.4f%% %11.4f%% %12.4f%% %+8.2f'
              % (K, 毛 * 100, 误 * 100, (毛 - 总成本率) * 100, 毛 / 误 if 误 > 0 else 0))
        P('     （「扣成本后」= 毛 − %.4f%%；>0 才代表扣掉两腿往返成本仍有剩余）'
          % (总成本率 * 100))
        P('')

        # --- ④ 前后半稳定性 ---
        半 = len(S) // 2
        b1, _, t1, _, h1, _, _ = ar1(S[:半])
        b2, _, t2, _, h2, _, _ = ar1(S[半:])
        P('  ④ 前后半稳定性（不作硬判据，只看符号）')
        P('     前半：b = %+.6f  t = %+.2f  半衰期 %.1f 根' % (b1, t1, h1))
        P('     后半：b = %+.6f  t = %+.2f  半衰期 %.1f 根' % (b2, t2, h2))
        稳 = (b1 < 0) and (b2 < 0)
        P('     → 两半同号：%s' % ('是' if 稳 else '**否（结构不稳定）**'))
        P('')

        全过 = (rho >= 0.70) and (波动比 <= 0.70) and c_ok and d_ok and 稳
        结果[对名] = 全过
        P('  判定：%s' % ('★ 通过 → 可进入步 2（协整 + 策略假设）' if 全过 else '**不通过 → 本对关闭**'))
        P('')

    # ---------- 汇总 ----------
    P('=' * 100)
    P('【汇总】')
    for 对名, A, B in (对列表 + 扩大对):
        if 对名 in 结果:
            P('   %-6s（%s − %s）：%s' % (对名, A, B, '★ 通过' if 结果[对名] else '不通过'))
        else:
            P('   %-6s（%s − %s）：数据不足/跳过' % (对名, A, B))
    P('')
    P('  单品种对照（PVC）：往返成本率 %.4f%% / σ %.4f%% = 成本/σ = %.1f%%  ← 判据 D 基准'
      % (2 * c_v * 100, sd_v * 100, 基准比 * 100))
    P('')
    P('  ⚠️ 多重比较声明：本轮共筛 %d 对。测得多必有偶然「通过」，故通过者只作候选，'
      % len(对列表 + 扩大对))
    P('     必须用外部样本复验后才可进入策略开发；不得直接据本表出发写代码。')

    p = os.path.join(ROOT, '工具', '_输出', '_配对体检_%s.txt' % 期)
    io.open(p, 'w', encoding='utf-8', newline='\n').write('\n'.join(out) + '\n')
    print('\n'.join(out))
    print()
    print('  已存 %s' % os.path.relpath(p, ROOT))
    return 0


if __name__ == '__main__':
    sys.exit(main())
