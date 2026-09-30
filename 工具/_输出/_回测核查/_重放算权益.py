# -*- coding: utf-8 -*-
"""从回测日志的成交明细重放，算出「资金中断那一刻」的账户权益。

为什么需要它：tqsdk 的模拟账户在下单被拒时抛异常，引擎只接 BacktestFinished
-> 整个回测半途而废、一份报告都不出，账户余额也就没地方看。这个脚本不改项目
任何代码，纯外部复算，回答一个问题：**到底是策略真把本金亏光了，还是引擎口径有问题**。

口径（与引擎一致）：
  · 初始权益 200,000 元
  · 成本 23.5 元/手/单边（手续费 3.5 + 滑点 1 跳 20，纯碱 1 跳 = 1 点 × 20 元）
  · 已实现盈亏 = (平仓价 − 开仓均价) × 方向系数 × 手数 × 合约乘数
  · 收盘后按最后一根 K 线的收盘价补浮盈（近似）

用法：python _重放算权益.py <日志路径>
"""
import re
import sys

乘数 = 20.0          # 纯碱：20 元/点
单边成本 = 23.5      # 元/手/单边
初始 = 200000.0

pat = re.compile(
    r'模拟交易下单.*?时间: ([\d\-]+ [\d:\.]+), 合约: ([\w\.]+), '
    r'开平: (\w+), 方向: (\w+), 手数: (\d+), 价格: ([\d\.]+)')

路径 = sys.argv[1] if len(sys.argv) > 1 else "工具/_输出/_回测核查/_复现_03纯碱.log"
文本 = open(路径, encoding="utf-8", errors="replace").read()

# 只保留「全部成交」紧随其后的下单（最后一笔「资金不足」不算）
成交 = []
行 = 文本.split("\n")
for i, L in enumerate(行):
    m = pat.search(L)
    if not m:
        continue
    后面 = "\n".join(行[i + 1:i + 4])
    if "开仓资金不足" in 后面 or "平仓资金不足" in 后面:
        continue                      # 这笔没成交
    成交.append({
        "t": m.group(1), "sym": m.group(2), "oc": m.group(3),
        "dir": m.group(4), "vol": int(m.group(5)), "px": float(m.group(6)),
    })

print("成交笔数: %d" % len(成交))

权益 = 初始
持仓 = {}          # 合约 -> [方向系数(+1/-1), 手数, 开仓均价]
成本合计 = 0.0
已实现 = 0.0
换月次数 = 0
曲线 = []

for k, r in enumerate(成交):
    d = 1 if r["dir"] == "SELL" else -1        # tqsdk: SELL=卖出，方向系数见下
    # ★ tqsdk 的 方向 是「买卖方向」：BUY=买入、SELL=卖出。
    #   开仓时 SELL = 做空(-1)；平仓时 SELL = 卖出多头(平多)。
    #   这里按「开平 + 方向」还原真实的多空方向。
    sym = r["sym"]
    pos = 持仓.get(sym)
    if r["oc"] == "OPEN":
        开向 = -1 if r["dir"] == "SELL" else 1
        if pos and pos[0] != 开向 and pos[1] > 0:
            # 理论上引擎会先 CLOSE，不该出现；出现了就记一笔
            pass
        if pos and pos[0] == 开向:
            新量 = pos[1] + r["vol"]
            pos[2] = (pos[2] * pos[1] + r["px"] * r["vol"]) / 新量
            pos[1] = 新量
        else:
            持仓[sym] = [开向, r["vol"], r["px"]]
    else:  # CLOSE
        if pos and pos[1] > 0:
            平量 = min(pos[1], r["vol"])
            盈亏 = (r["px"] - pos[2]) * pos[0] * 平量 * 乘数
            已实现 += 盈亏
            权益 += 盈亏
            pos[1] -= 平量
            if pos[1] <= 0:
                持仓[sym] = [pos[0], 0, pos[2]]
    c = 单边成本 * r["vol"]
    成本合计 += c
    权益 -= c
    曲线.append((r["t"], 权益, r["sym"]))

print("累计成本      : %10.0f 元（占初始 %.1f%%）" % (成本合计, 成本合计 / 初始 * 100))
print("已实现盈亏    : %10.0f 元" % 已实现)
print("已实现后权益  : %10.0f 元" % 权益)
print()
print("各合约残留持仓 :")
for s, p in 持仓.items():
    if p[1] > 0:
        print("   %-12s 方向 %+d  手数 %d  均价 %.2f" % (s, p[0], p[1], p[2]))
print()
print("权益曲线抽样（每 10%% 进度一行）:")
for i in range(0, len(曲线), max(1, len(曲线) // 10)):
    t, e, s = 曲线[i]
    print("   %s  %-12s  权益 %10.0f" % (t, s, e))
if 曲线:
    t, e, s = 曲线[-1]
    print("   末笔 %s  %-12s  权益 %10.0f" % (t, s, e))
