# -*- coding: utf-8 -*-
"""
06 MA13 模拟盘 · 台账统计（可重跑）
====================================
用途：把 `台账/*_成交流水.csv` 里的 FILL 记录配对成「往返交易」，输出
     逐品种 + 合计的 笔数 / 胜率 / 毛盈亏 / 出场原因分布 / 样本量置信区间。

为什么要有这个脚本：README §11 里那些数字（9 笔 → 41 笔）**每天都在变**，
写死在文档里迟早过期。每次要引用之前，先跑一遍本脚本取最新快照。

口径说明（重要）：
  · **毛盈亏** = (平仓价 − 开仓价) × 方向 × 手数 × 合约乘数，**未扣手续费与滑点**
    ⇒ 与账户基数无关，可直接和回测的「毛盈亏」比结构（README §7 结论⑤）。
  · **净盈亏估计** = 毛盈亏 − 手数 × 2 × (滑点×一跳×乘数 + 佣金)
    ⇒ 用 config_symbols 的当前成本表估算，**不等于**台账里的账户权益变化。
  · **账户权益** 来自 EQUITY 记录，是**云端 TqKq 账号（约 100 万基数）**，
    **不可与回测的 20 万基数直接比**。

配对规则：按 FILL 出现顺序，遇到「多开/空开」记一个未平仓头寸，
遇到「多平/空平」即与最近一个未平仓头寸配对。最后仍未平掉的**不计入统计**。

禁并发提示：本脚本只读 CSV，**不连网**，可以随时跑（模拟盘在跑也不受影响）。
"""
import os
import sys
import math
import glob
import datetime

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_symbols as cs  # noqa: E402

LEDGER = os.path.join(_HERE, "台账")
SLIP = 1          # 与 06 回测/实盘同口径（1 跳）


def _params(cn):
    """由中文品种名反查 config_symbols 的 (key, MULT, TICK, COMM)。

    ⚠️ 2026-10-12 血泪：本函数第一版写成「遍历 dir(cs) 找长度为 4 的元组」，
    但品种表其实是 **dict `cs.SYMBOLS`**，元组在字典的**值**里 ⇒ 反查必然失败，
    于是静默 `return None, 10, 1.0, 0.0`（MULT 用 10）⇒ 焦煤毛盈亏被算成 −1500，
    而正确值是 −9000（差 6 倍，恰好 60/10）。**属于典型「静默失效」**。
    本版：直接读 `cs.SYMBOLS`，**查不到就抛异常**，不留默认值。
    """
    for k, v in cs.SYMBOLS.items():
        if v[0] == cn:
            return k, v[2], v[3], float(cs.COST.get(k, 0.0))
    raise KeyError(f"品种「{cn}」不在 config_symbols.SYMBOLS 里 —— "
                   f"拒绝用默认乘数计算（会静默算错）。请先补品种配置。")


def main():
    files = sorted(glob.glob(os.path.join(LEDGER, "*_成交流水.csv")))
    if not files:
        print("找不到台账文件：", LEDGER)
        return

    rows = []
    tot_n = tot_w = 0
    tot_gross = 0.0
    tot_net = 0.0
    exit_px = exit_tm = 0

    for f in files:
        cn = os.path.basename(f).replace("_成交流水.csv", "")
        key, MULT, TICK, COMM = _params(cn)
        df = pd.read_csv(f, encoding="utf-8-sig", dtype=str)
        fills = df[df["类型"] == "FILL"].copy()
        if len(fills) == 0:
            continue
        fills["手数"] = pd.to_numeric(fills["手数"], errors="coerce")
        fills["成交价"] = pd.to_numeric(fills["成交价"], errors="coerce")

        pos = None
        trades = []
        for _, r in fills.iterrows():
            d = str(r["方向"])
            if d in ("多开", "空开"):
                pos = (d, r["手数"], r["成交价"])
            elif d in ("多平", "空平") and pos is not None:
                sign = 1 if pos[0] == "多开" else -1
                gross = (r["成交价"] - pos[2]) * sign * pos[1] * MULT
                cost = pos[1] * 2 * (SLIP * TICK * MULT + COMM)
                trades.append((gross, gross - cost))
                pos = None

        # 出场原因（STOPLOSS 记录）
        sl = df[df["类型"] == "STOPLOSS"]
        px = sum("止损触发=True" in str(x) for x in sl["备注"])
        tm = sum("时间止损=True" in str(x) for x in sl["备注"])

        # 最新权益
        eq = df[df["类型"] == "EQUITY"]
        eq_txt = ""
        if len(eq):
            eq_txt = f"{eq.tail(1)['备注'].iloc[0]} @ {eq.tail(1)['记录时间'].iloc[0]}"

        n = len(trades)
        w = sum(1 for g, _ in trades if g > 0)
        g_sum = sum(g for g, _ in trades)
        n_sum = sum(x for _, x in trades)
        if pos is not None:
            open_txt = f"★ 未平仓: {pos[0]} {pos[1]:g} 手 @ {pos[2]}"
        else:
            open_txt = ""

        rows.append((cn, key, len(fills), n, w, n - w, g_sum, n_sum, px, tm, open_txt, eq_txt))

        tot_n += n
        tot_w += w
        tot_gross += g_sum
        tot_net += n_sum
        exit_px += px
        exit_tm += tm

    # ---------- 输出 ----------
    L = []
    L.append("=" * 88)
    L.append(f"06 MA13 模拟盘 台账统计  快照时间 {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    L.append(f"数据目录 {LEDGER}")
    L.append("=" * 88)
    L.append(f"{'品种':<7}{'FILL':>6}{'配对':>6}{'盈':>4}{'亏':>4}{'毛盈亏':>11}{'净估':>11}{'价损':>6}{'时损':>6}")
    for cn, key, nf, n, w, l, g, nn, px, tm, op, eq in rows:
        L.append(f"{cn:<7}{nf:>6}{n:>6}{w:>4}{l:>4}{g:>11.0f}{nn:>11.0f}{px:>6}{tm:>6}  {op}")
        if eq:
            L.append(f"         账户权益: {eq}")
    L.append("-" * 88)
    if tot_n:
        p = tot_w / tot_n
        se = math.sqrt(p * (1 - p) / tot_n)
        lo, hi = max(0.0, p - 1.96 * se), min(1.0, p + 1.96 * se)
        L.append(f"合计  配对 {tot_n} 笔：{tot_w} 盈 / {tot_n - tot_w} 亏")
        L.append(f"      胜率 {p*100:.1f}%   95% CI ≈ [{lo*100:.1f}%, {hi*100:.1f}%]  (±{1.96*se*100:.1f}pp)")
        L.append(f"      毛盈亏 {tot_gross:,.0f} 元   净盈亏估计 {tot_net:,.0f} 元（按当前成本表）")
        if exit_px + exit_tm:
            L.append(f"      出场结构：价格止损 {exit_px} ({exit_px/(exit_px+exit_tm)*100:.1f}%)"
                     f" / 时间止损 {exit_tm} ({exit_tm/(exit_px+exit_tm)*100:.1f}%)")
        L.append("")
        L.append(f"⚠️ 样本量判定：{tot_n} 笔 " +
                 ("< 100 ⇒ **只报数、不下结论**（README §11 纪律）" if tot_n < 100
                  else "≥ 100 ⇒ 可以做初步比较，但仍需持续累积"))
        L.append("⚠️ 权益口径：EQUITY 是云端 TqKq 账号（约 100 万基数），不可与回测 20 万基数直接比；")
        L.append("   胜率/毛盈亏按逐笔成交价重算，与基数无关 ⇒ 比结构是干净的。")
    txt = "\n".join(L)
    print(txt)

    out = os.path.join(LEDGER, f"_样本量快照_{datetime.datetime.now():%Y%m%d}.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(txt + "\n")
    print(f"\n[done] 已落盘 {out}")


if __name__ == "__main__":
    main()
