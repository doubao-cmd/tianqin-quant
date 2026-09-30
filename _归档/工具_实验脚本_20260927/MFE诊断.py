# -*- coding: utf-8 -*-
"""MFE / MAE 诊断 —— 第 1 步：出场改造到底有没有空间？
==========================================================
只读不改、不跑策略。把 v5（30 分钟）的 30 笔交易逐笔打开，算每笔持仓期内的
「最大有利偏移 MFE」与「最大不利偏移 MAE」。

口径与对齐
----------
1. 配对：(OPEN, 其后第一笔 CLOSE/CLOSETODAY)。
2. ⚠️ **报告时间 + 8 小时 = 真实时间**（engine 的 _dt_str 按 UTC 解析，早 8 小时；
   已用 5 分钟序列实测确认，价格差正好 ±1 跳 = 滑点）。本脚本自动 +8h。
3. 持仓窗口 = [开仓那根, 平仓那根 - 1]。
   开仓成交在「开仓那根的开盘」→ 该根的高低价仍属于持仓期；
   平仓成交在「平仓那根的开盘」→ 该根的高低价在出场之后，**不计入**。
4. 三个 MFE 口径（从松到紧）：
   - MFE_ideal     : 用高/低价，理论绝对上限（需要精准打在最极端的那一跳，做不到）
   - MFE_close     : 用收盘价，收盘类规则的上限
   - MFE_close_exec: 在「收盘最有利的那根」的**下一根开盘**成交
5. MAE：MAE 用高/低价（含根内极值）；MAE_close 只用收盘价（与零假设可比）。
6. 止损耗：以「信号K线收盘价 ∓ 2×ATR(14)」为静态止损线，检查持仓期内收盘是否触线。

★ 判据（先定规则，再看结果）
----------------------------
【判据 A · 事前写死，事后认定不成立】
  以 MFE_close_exec 均值为准：≥10 点 → 有空间；7~10 灰色；<7 关闭。
  ⚠️ **该判据无效**：MFE 是「事后最优」的上限，对**任何**随机数据都会给出很大的值
  （波动越大、持仓越久，MFE 越大），与「有没有可利用的漏损」无关。保留原文以便对照。

【判据 B · 有效】
  把观测的 MFE / MAE 与「**同波动率、同持仓根数、无漂移的随机游走**」对照
  （重采样 R 次，得到 30 笔平均值的零分布）。
  · p > 0.05 → 观测与随机游走无显著差异 → 持仓期内的最大偏移**只是波动的函数**，
    不含可捕获的信息 → 出场改造没有信息基础，这条路关闭。
  · p ≤ 0.05 且观测 > 零假设 → 路径存在超出波动的结构 → 才值得进第 2 步做单变量实验。
  理论依据：随机游走（鞅）下，任何**只看历史**的出场规则的期望都不变
  （可选停止定理）。所以「浮盈回吐」若不伴随超出波动的结构，就无法被任何规则捕获。

用法：
    python 工具/MFE诊断.py [报告.txt] [序列.csv] [乘数]
例：
    python 工具/MFE诊断.py 回测结果/01_双均线/PVC/PVC_双均线_30min_in.txt
"""
import io
import os
import re
import sys
import csv
import random
from statistics import mean, median

_小时偏移 = 8  # 报告时间 -> 真实时间的小时偏移（engine _dt_str 早 8 小时的已知 bug）

_行 = re.compile(
    r'^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\s+(\S+)\s+'
    r'(OPEN|CLOSE|CLOSETODAY)\s+(\d+)手\s+([\d.]+)',
    re.M,
)

成本点数 = 2.56
ATR周期 = 14
ATR倍数 = 2.0
重采样 = 400


def 找根目录():
    root = os.path.dirname(os.path.abspath(__file__))
    while root != os.path.dirname(root) and not os.path.isdir(os.path.join(root, 'lib')):
        root = os.path.dirname(root)
    return root


def 加8小时(日期, 时刻):
    import datetime
    t = datetime.datetime.strptime('%s %s' % (日期, 时刻), '%Y-%m-%d %H:%M:%S')
    return (t + datetime.timedelta(hours=_小时偏移)).strftime('%Y-%m-%d %H:%M:%S')


def 解析成交(文本):
    out = []
    for m in _行.finditer(文本):
        d, hm, side, act, lots, px = m.groups()
        out.append({
            't': 加8小时(d, hm),          # 真实时间
            'side': side, 'act': act,
            'lots': int(lots), 'px': float(px),
        })
    return out


def 配对(成交):
    pairs = []
    op = None
    for r in 成交:
        if r['act'] == 'OPEN':
            op = r
        elif op is not None:
            pairs.append({'in': op, 'out': r})
            op = None
    return pairs


def 读序列(p):
    rows = list(csv.DictReader(io.open(p, newline='', encoding='utf-8')))
    for r in rows:
        for k in ('open', 'high', 'low', 'close'):
            r[k] = float(r[k])
    idx = {r['datetime']: i for i, r in enumerate(rows)}
    return rows, idx


def 诊断(pairs, rows, idx):
    结果 = []
    for k, p in enumerate(pairs, 1):
        ti, to = p['in']['t'], p['out']['t']
        ii = idx.get(ti)
        io_ = idx.get(to)
        if ii is None or io_ is None:
            结果.append({'序号': k, '警告': '时间戳在序列中找不到：%s / %s' % (ti, to)})
            continue
        j0, j1 = ii, io_ - 1          # 持仓窗口（含首、不含平仓那根）
        if j1 < j0:
            j1 = j0
        方向 = 1 if p['in']['side'] == 'BUY' else -1
        入 = p['in']['px']
        出 = p['out']['px']
        窗 = rows[j0:j1 + 1]

        if 方向 > 0:
            有利 = [r['high'] - 入 for r in 窗]
            不利 = [入 - r['low'] for r in 窗]
        else:
            有利 = [入 - r['low'] for r in 窗]
            不利 = [r['high'] - 入 for r in 窗]

        MFE_ideal = max(有利)
        MAE = max(不利)
        # 收盘口径（与零假设可比：零假设只用收盘路径）
        收有利 = [方向 * (r['close'] - 入) for r in 窗]
        收不利 = [-方向 * (r['close'] - 入) for r in 窗]
        MAE_close = max(收不利)
        k_best = max(range(len(收有利)), key=lambda x: 收有利[x])
        MFE_close = 收有利[k_best]
        nxt = j0 + k_best + 1
        if nxt < len(rows):
            MFE_close_exec = 方向 * (rows[nxt]['open'] - 入)
        else:
            MFE_close_exec = 收有利[-1]

        实际 = 方向 * (出 - 入)
        回吐 = MFE_ideal - 实际

        # 静态止损线（基准 = 信号K线收盘价，即开仓那根的前一根收盘）
        atr = None
        止损线 = None
        触线 = False
        if ii >= ATR周期:
            trs = []
            for x in range(ii - ATR周期, ii):
                h, l = rows[x]['high'], rows[x]['low']
                pc = rows[x - 1]['close']
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            atr = mean(trs)
            基准 = rows[ii - 1]['close']
            止损线 = 基准 - 方向 * ATR倍数 * atr
            for r in 窗:
                if (r['close'] - 止损线) * 方向 < 0:
                    触线 = True
                    break

        结果.append({
            '序号': k, '方向': '多' if 方向 > 0 else '空',
            '开': ti, '平': to, '持仓根': len(窗),
            'MFE_ideal': MFE_ideal, 'MFE_close': MFE_close,
            'MFE_close_exec': MFE_close_exec,
            'MAE': MAE, 'MAE_close': MAE_close,
            '实际': 实际, '回吐': 回吐,
            'ATR': atr, '止损线': 止损线, '触线': 触线,
            '出场减止损': (出 - 止损线) * 方向 if 止损线 is not None else None,
            '入场核对': rows[ii]['open'] - 入,
        })
    return 结果


def 零假设检验(结果, rows, R=重采样, seed=20260918):
    """把 30 笔的 MFE/MAE 平均值，与「同波动率、同持仓根数的无漂移随机游走」对照。

    每套抽样 = 每个持仓窗口生成一条随机游走（步长 σ = 全序列收盘一阶差的 sd），
    取该窗口的最大有利 / 最大不利偏移，再对 30 笔求平均。重复 R 次 → 零分布。
    """
    好 = [x for x in 结果 if '警告' not in x]
    收 = [r['close'] for r in rows]
    差 = [收[i] - 收[i - 1] for i in range(1, len(收))]
    sd = (sum(d * d for d in 差) / len(差)) ** 0.5
    ns = [x['持仓根'] for x in 好]

    def 一套(s_):
        rnd = random.Random(s_)
        甲, 乙 = [], []
        for n in ns:
            p = 0.0
            mx = 0.0
            mn = 0.0
            for _ in range(n):
                p += sd * rnd.gauss(0.0, 1.0)
                if p > mx:
                    mx = p
                if p < mn:
                    mn = p
            甲.append(mx)
            乙.append(-mn)
        return mean(甲), mean(乙)

    甲s, 乙s = [], []
    for k in range(R):
        a, b = 一套(seed + 7919 * (k + 1))
        甲s.append(a)
        乙s.append(b)
    甲s.sort()
    乙s.sort()

    obs_mfe = mean([x['MFE_close'] for x in 好])
    obs_mae = mean([x['MAE_close'] for x in 好])
    理论 = sd * (2.0 * mean(ns) / 3.141592653589793) ** 0.5   # E[max] ≈ σ√(2n/π)
    return {
        'sd': sd, 'n均': mean(ns), 'R': R, '理论': 理论,
        'obs_mfe': obs_mfe, 'null_mfe': mean(甲s),
        'p_mfe': sum(1 for v in 甲s if v >= obs_mfe) / float(R),
        'q95_mfe': 甲s[int(0.95 * R)],
        'obs_mae': obs_mae, 'null_mae': mean(乙s),
        'p_mae': sum(1 for v in 乙s if v >= obs_mae) / float(R),
        'q95_mae': 乙s[int(0.95 * R)],
    }


def 报告(结果, rows, 乘数, 报告名, 序列名):
    好 = [x for x in 结果 if '警告' not in x]
    n = len(好)
    行 = []

    def P(s=''):
        行.append(s)
        print(s)

    P('=' * 96)
    P('MFE / MAE 诊断（第 1 步）—— 出场改造有没有空间')
    P('=' * 96)
    P('报告：%s' % 报告名)
    P('序列：%s' % 序列名)
    P('有效交易 %d 笔（解析成交 %d 笔配对）' % (n, len(结果)))
    P('口径：报告时间 +8h = 真实时间；持仓窗口 = [开仓那根, 平仓那根-1]')
    P()

    P('■ 自检')
    核 = [abs(x['入场核对']) for x in 好]
    P('  |开仓价 - 序列开盘价| 最大 %.1f 点，均值 %.2f 点' % (max(核), mean(核)))
    for x in 好:
        if abs(x['入场核对']) > 2:
            P('     ⚠️ 第 %d 笔偏差 %.1f 点（开仓 %s）' % (x['序号'], x['入场核对'], x['开']))
    实际额 = 乘数 * sum(x['实际'] for x in 好)
    P('  实际价差合计 %.0f 点 = %.0f 元（应等于 875 元）→ %s'
      % (sum(x['实际'] for x in 好), 实际额,
         '对账通过' if abs(实际额 - 875) < 5 else '!! 与账本不符'))
    P()

    P('■ MFE / MAE 分布（点）')
    P('%-18s %9s %9s %9s %9s' % ('口径', '均值', '中位', '最小', '最大'))
    for key in ('MFE_ideal', 'MFE_close', 'MFE_close_exec',
                'MAE', 'MAE_close', '实际'):
        v = [x[key] for x in 好]
        P('%-18s %9.2f %9.2f %9.2f %9.2f' % (key, mean(v), median(v), min(v), max(v)))
    P()

    P('■ 完美出场 vs 实际（毛价差，元）')
    完美 = 乘数 * sum(x['MFE_ideal'] for x in 好)
    收盘出 = 乘数 * sum(x['MFE_close'] for x in 好)
    收盘可执行 = 乘数 * sum(x['MFE_close_exec'] for x in 好)
    P('  MFE_ideal  完美出场（理论绝对上限，做不到）  %+8.0f 元   %+7.2f 点/笔' % (完美, 完美 / 乘数 / n))
    P('  MFE_close  收盘价出场上限                  %+8.0f 元   %+7.2f 点/笔' % (收盘出, 收盘出 / 乘数 / n))
    P('  MFE_close_exec 收盘触发·次根开盘成交       %+8.0f 元   %+7.2f 点/笔' % (收盘可执行, 收盘可执行 / 乘数 / n))
    P('  实际        v5 真实结果                    %+8.0f 元   %+7.2f 点/笔' % (实际额, 实际额 / 乘数 / n))
    P('  ── 从实际到「完美」的总回吐 = %+.0f 元（%+.2f 点/笔）'
      % (完美 - 实际额, (完美 - 实际额) / 乘数 / n))
    P()

    P('■ 逐笔明细（点）')
    P('%4s %4s %8s %8s %8s %9s %8s %9s %9s %6s %7s' % (
        '序', '向', '持仓根', 'MFE理', 'MFE收', 'MFE收执行', '实际', '回吐', 'MAE', '触线', '出-止损'))
    for x in 好:
        P('%4d %4s %8d %8.1f %8.1f %9.1f %8.1f %9.1f %9.1f %6s %7s' % (
            x['序号'], x['方向'], x['持仓根'], x['MFE_ideal'], x['MFE_close'],
            x['MFE_close_exec'], x['实际'], x['回吐'], x['MAE'],
            '是' if x['触线'] else '否',
            '%.1f' % x['出场减止损'] if x['出场减止损'] is not None else '-'))
    P()

    P('■ 关键计数')
    触线 = [x for x in 好 if x['触线']]
    P('  触及 2×ATR 止损线：%d / %d 笔（%.0f%%）' % (len(触线), n, 100.0 * len(触线) / n))
    for th in (6, 8, 10, 12):
        c_i = len([x for x in 好 if x['MFE_ideal'] >= th])
        c_e = len([x for x in 好 if x['MFE_close_exec'] >= th])
        P('  MFE ≥ %2d 点：理论 %2d 笔 / 收盘可执行 %2d 笔（%.0f%%）'
          % (th, c_i, c_e, 100.0 * c_e / n))
    回吐样本 = [x for x in 好 if x['MFE_close_exec'] >= 8 and x['实际'] <= 0]
    P('  ★「MFE_收盘可执行 ≥ 8 点、但最终 ≤ 0 点」= %d 笔' % len(回吐样本))
    P()

    P('■ 收益集中度（「尾部依赖」的量化）')
    排序 = sorted(好, key=lambda x: -x['MFE_ideal'])
    for N in (3, 5, 10):
        前 = 排序[:N]
        余 = 排序[N:]
        P('  按 MFE 排序前 %2d 笔：实际价差 %+7.1f 点（%+6.0f 元）  |  其余 %2d 笔 %+7.1f 点（%+6.0f 元）'
          % (N, sum(x['实际'] for x in 前), 乘数 * sum(x['实际'] for x in 前),
             len(余), sum(x['实际'] for x in 余), 乘数 * sum(x['实际'] for x in 余)))
    P('  ⇒ 全期毛价差只靠极少数笔撑起；「回吐」也正是集中在这几笔身上 ——')
    P('     而移动止损 / 目标止盈恰恰是会把它们砍短的工具。')
    P()

    # ---------- 判定 ----------
    z = 零假设检验(结果, rows)
    P('■ 判定')
    P('【判据 A · 事前写死的门槛，事后认定不成立】')
    mce = mean([x['MFE_close_exec'] for x in 好])
    P('  MFE_close_exec 均值 = %.2f 点/笔 → 按原判据 = %s'
      % (mce, '有空间' if mce >= 10 else ('灰色' if mce >= 7 else '没空间')))
    P('  ⚠️ 该判据无效：MFE 是「事后最优」的上限，对任何随机数据都会给出很大的值，')
    P('     波动越大、持仓越久就越大，与「有没有可利用的漏损」无关。判定改看判据 B。')
    P()
    P('【判据 B · 与随机游走零假设对照（有效）】重采样 %d 次' % z['R'])
    P('  每根收盘波动估计 sd = %.2f 点；平均持仓 %.1f 根（解析近似 σ√(2n/π) = %.2f 点，仅供参考）'
      % (z['sd'], z['n均'], z['理论']))
    P('  %-16s %10s %10s %10s %10s' % ('统计量', '观测均值', '零假设均值', '零假设95分位', 'p 值'))
    P('  %-16s %10.2f %10.2f %10.2f %10.3f'
      % ('MFE（收盘口径）', z['obs_mfe'], z['null_mfe'], z['q95_mfe'], z['p_mfe']))
    P('  %-16s %10.2f %10.2f %10.2f %10.3f'
      % ('MAE（收盘口径）', z['obs_mae'], z['null_mae'], z['q95_mae'], z['p_mae']))
    P()
    p_m, p_a = z['p_mfe'], z['p_mae']
    P('  → 单边检验（备择假设：观测 > 零假设）')
    if p_m > 0.05:
        P('     MFE：p = %.3f —— 观测**不低于**随机游走（还低了 %.2f 点）。' % (p_m, z['null_mfe'] - z['obs_mfe']))
        P('       注意偏倚方向对本检验**有利**：顺势单的持仓窗口天然更长 → MFE 天然更大，')
        P('       即便如此仍低于零假设 → 结论更硬。')
    else:
        P('     MFE：p = %.3f —— 观测显著高于随机游走，路径存在超出波动的结构。' % p_m)
    if p_a > 0.95:
        P('     MAE：p = %.3f —— 观测**显著低于**随机游走（%.2f vs %.2f 点），不利偏移被截断了。'
          % (p_a, z['obs_mae'], z['null_mae']))
        P('       两层原因：① 出场规则确实在截断亏损（止损/信号出场）；')
        P('       ② 内生性 —— 逆势时窗口被提前结束，而零假设用固定根数。')
        P('       它改变的是**分布**（砍掉左尾），不是**期望** —— 与「截断亏损」的直觉一致，')
        P('       但截断分布不产生 alpha。')
    else:
        P('     MAE：p = %.3f —— 未见显著截断。' % p_a)
    P()
    if p_m > 0.05:
        P('  → 判定：**这条路关闭**（出场改造）。决定性的 MFE 一侧没有超出波动的结构。')
        P('     持仓期内的「最大有利偏移」只是波动的函数；「浮盈回吐」是后见之明 ——')
        P('     要拿到 MFE，必须提前知道哪一根是最高点，而因果规则做不到。')
        P('     依据：鞅的可选停止定理 —— 随机游走下任何只看历史的出场规则期望都不变。')
    else:
        P('  → 判定：值得进第 2 步做单变量出场实验（一次只上一个，改完立即回测）。')
    P()
    P('⚠️ 三个 MFE 口径都不是「能实现」的收益：MFE_ideal 需要精准打在最极端的那一跳；')
    P('   MFE_close_exec 是「事后知道哪根收盘最有利」才能做到。本诊断只回答')
    P('   「上限够不够高 / 上限是不是纯波动」，不预测任何改造后的实际收益。')
    return 行


def main():
    args = [a for a in sys.argv[1:]]
    root = 找根目录()
    rep = args[0] if args else '回测结果/01_双均线/PVC/PVC_双均线_30min_in.txt'
    ser = args[1] if len(args) > 1 else '工具/_归档/序列/KQ-m_DCE-v_30min_2026-03-13_2026-09-13.csv'
    乘数 = float(args[2]) if len(args) > 2 else 5.0
    if not os.path.isabs(rep):
        rep = os.path.join(root, rep)
    if not os.path.isabs(ser):
        ser = os.path.join(root, ser)
    for p in (rep, ser):
        if not os.path.exists(p):
            print('找不到文件：%s' % p)
            return 1

    文本 = io.open(rep, encoding='utf-8', errors='replace').read()
    成交 = 解析成交(文本)
    含向 = len(re.findall(r'\b(BUY|SELL)\b', 文本))
    print('解析成交 %d 条；含 BUY/SELL 的 %d 行 → %s'
          % (len(成交), 含向, '自检通过' if len(成交) == 含向 else '!! 条数不符'))
    if len(成交) != 含向:
        return 1

    pairs = 配对(成交)
    rows, idx = 读序列(ser)
    结果 = 诊断(pairs, rows, idx)
    行 = 报告(结果, rows, 乘数, os.path.relpath(rep, root), os.path.relpath(ser, root))

    out = os.path.join(root, '工具', '_输出', '_MFE诊断_0918.txt')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with io.open(out, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(行) + '\n')
    print()
    print('已存 %s' % os.path.relpath(out, root))
    return 0


if __name__ == '__main__':
    sys.exit(main())
