# -*- coding: utf-8 -*-
"""
截面持仓量因子 · 跑因子.py（2026-10-10）
预注册卡：`工具/_截面因子_持仓量/预注册卡_截面持仓量因子.md`
引擎：    同目录 截面引擎.py（单元测试 9 组全过）

用法：
    python 工具/_截面因子_持仓量/跑因子.py --窗 IS      # 样本内 2016-01~2018-12
    python 工具/_截面因子_持仓量/跑因子.py --窗 VAL     # 验证集 2019-01~2020-12
    python 工具/_截面因子_持仓量/跑因子.py --窗 OOS     # 期末考 2026-01~2026-10（样本外，只跑一次）

★ 数据对齐：取 9 品种日K日期的**交集**（SHFE/DCE/CZCE 交易日历几乎一致，
  交集损失极小）⇒ 不做前值填充（填充会污染 oi_chg，见 §59）。
★ 引擎喂的是「对齐后的数组」；信号用 t−1 收盘、成交用 t 开盘（引擎内已保证）。
"""
import argparse
import io
import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = r"C:\Users\cc\Desktop\天勤量化"
HERE = os.path.join(ROOT, "工具", "_截面因子_持仓量")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, HERE)

from tqsdk import TqApi, TqAuth, TqSim, TqBacktest  # noqa: E402
import config_account  # noqa: E402
import config_symbols as cs  # noqa: E402
import 截面引擎 as E  # noqa: E402

# ★ 固定品种池（2026-10-10 用户拍板 B：三窗口一致，换窗口不换品种）
#   = 12 原始品种剔除上市晚的 3 个（生猪2021 / 纯碱2019-12 / 苯乙烯2020-04）
池 = ["MA", "M", "RB", "V", "JD", "JM", "P", "RU", "SR"]

窗口表 = {
    "IS":  (pd.Timestamp("2016-01-01"), pd.Timestamp("2018-12-31"), "样本内"),
    "VAL": (pd.Timestamp("2019-01-01"), pd.Timestamp("2020-12-31"), "验证集"),
    "OOS": (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-10-01"), "期末考"),
}
预热天 = 40          # LOOKBACK=20 × 2 的预热（回测起点前移）
年bar = 243.0        # 日频年化


def 拉(起, 止):
    """一个会话同时订阅 9 个主连日K，收集 (日期→oi, close, open)。
    ⚠️ 回测跑到 END 会抛 BacktestFinished —— 捕获当正常结束（§59 教训）。"""
    user, pwd = config_account.check()
    api = TqApi(TqSim(200000.0),
                backtest=TqBacktest(起 - pd.Timedelta(days=预热天), 止),
                auth=TqAuth(user, pwd))
    数据 = {}
    try:
        串 = {}
        for k in 池:
            _名, 主连, mult, tick = cs.get(k)
            串[k] = api.get_kline_serial(主连, 86400, data_length=20)
        while True:
            try:
                api.wait_update(
                    deadline=pd.Timestamp("2026-10-10 23:50:00").timestamp())
            except Exception as e:
                if type(e).__name__ in ("BacktestFinished", "TqTimeoutError"):
                    break
                raise
            # 统一收割（任一品种有更新就把 9 个都收一遍的末值）
            for k, kv in 串.items():
                d = kv["datetime"].to_numpy()
                if not len(d) or d[-1] == 0:
                    continue
                dt = int(d[-1])
                buf = 数据.setdefault(k, {})
                if dt not in buf:
                    buf[dt] = {
                        "oi": float(kv["close_oi"].iloc[-1]),
                        "close": float(kv["close"].iloc[-1]),
                        "open": float(kv["open"].iloc[-1]),
                        "high": float(kv["high"].iloc[-1]),
                        "low": float(kv["low"].iloc[-1]),
                    }
    finally:
        try:
            api.close()
        except Exception:
            pass
    return 数据


def 对齐(数据):
    """取 9 品种日期交集，输出对齐数组（不填充）。"""
    日期集 = None
    for k in 池:
        s = set(数据.get(k, {}))
        日期集 = s if 日期集 is None else (日期集 & s)
    日期 = sorted(日期集)
    oi = {k: np.array([数据[k][d]["oi"] for d in 日期], float) for k in 池}
    close = {k: np.array([数据[k][d]["close"] for d in 日期], float) for k in 池}
    op = {k: np.array([数据[k][d]["open"] for d in 日期], float) for k in 池}
    high = {k: np.array([数据[k][d]["high"] for d in 日期], float) for k in 池}
    low = {k: np.array([数据[k][d]["low"] for d in 日期], float) for k in 池}
    return 日期, oi, close, op, high, low


def 手数默认(s, price):
    """简化手数：名义敞口 ≤ 权益×2 倍、夹 [1,8]（与 MA13 同风格）。
    ⚠️ 引擎的调用签名是 手数函数(状态字典s, price)，不是 (atr, equity, price)。"""
    if not price or price != price or price <= 0:
        return 1
    名义 = int((200000.0 * 2.0) / (price * s["mult"]))
    return max(1, min(8, 名义))


def 跑(窗名):
    起, 止, 中 = 窗口表[窗名]
    print("=" * 80)
    print("【%s】%s  %s ~ %s   品种池=%d 个（固定）" % (窗名, 中, 起.date(), 止.date(), len(池)))
    print("=" * 80)
    print("拉数据中（一个会话同时订阅 9 主连，约 1-3 分钟）...")
    原始 = 拉(起, 止)
    for k in 池:
        print("  %-4s 收到 %5d 根" % (k, len(原始.get(k, {}))))
    日期, oi, close, op, high, low = 对齐(原始)
    n = len(日期)
    print("对齐后（交集）：%d 根  首日 %s 末日 %s"
          % (n, pd.Timestamp(日期[0]).date(), pd.Timestamp(日期[-1]).date()))
    if n < E.LOOKBACK + 30:
        print("❌ 对齐后数据不足（%d 根 < LOOKBACK+30）—— 不得出任何结论" % n)
        return None

    # ---- 组装引擎 ----
    腿表 = []
    for k in 池:
        名, _主连, mult, tick = cs.get(k)
        腿表.append((名, k, float(mult), float(tick), float(cs.COST[k])))
    eng = E.截面引擎(腿表, 手数函数=手数默认)
    for k in 池:
        eng.装数据(k, oi[k], close[k], high[k], low[k])
    assert eng.就绪(), "引擎数据未就绪"

    # ---- 逐 bar 推进（信号 t−1，成交 t 开盘，引擎内部保证）----
    起始i = E.LOOKBACK + 1
    已实现 = 0.0          # ★ 引擎不累计已实现盈亏，跑因子自己记账
    全部事件 = []
    开手续费 = {}         # key → 未扣的开仓手续费（平仓时一起结算）
    权益 = E.INIT
    权益曲线 = [权益]
    日期有效 = [pd.Timestamp(日期[0])]
    零值bar = 0
    for i in range(起始i, n):
        # 硬校验素材：分母为 0 的 bar 计数（因子里 oi_chg 分母 = oi[i-1-LOOKBACK]）
        for k in 池:
            if oi[k][i - 1 - E.LOOKBACK] <= 0:
                零值bar += 1
        事件 = eng.走一根(i, {k: op[k][i] for k in 池})
        for ev in 事件:
            k = ev["key"]
            if ev["act"] == "开":
                # ★ 引擎开仓不扣手续费 ⇒ 这里记账，平仓时一起扣（开+平各一次）
                费 = ev["手"] * float(cs.COST[k])
                开手续费[k] = 开手续费.get(k, 0.0) + 费
                已实现 -= 费
            else:  # 平
                费 = ev["手"] * float(cs.COST[k])
                开手续费.pop(k, 0.0)          # 开仓那笔已在发生时扣进 已实现
                ev = dict(ev)
                ev["pnl"] = ev["pnl"] - 费          # 补扣平仓腿的手续费
                已实现 += ev["pnl"]
                全部事件.append(ev)
        # mark-to-market：逐日权益 = INIT + 已实现 + 各腿浮盈
        浮 = 0.0
        for k in 池:
            s = eng.st[k]
            if s["pos"] != 0:
                浮 += s["pos"] * s["手"] * (close[k][i] - s["entry"]) * s["mult"]
        权益 = E.INIT + 已实现 + 浮
        权益曲线.append(权益)
        日期有效.append(pd.Timestamp(日期[i]))

    # ---- 指标 ----
    eq = np.array(权益曲线, float)
    rets = np.diff(eq) / eq[:-1]
    年bar实际 = max(len(eq) - 1, 1) / max((日期有效[-1] - 日期有效[0]).days / 365.25, 1e-9)
    sharpe = (rets.mean() / rets.std() * np.sqrt(年bar实际)) if rets.std() > 1e-12 else 0.0
    峰 = np.maximum.accumulate(eq)
    回撤 = float(((eq - 峰) / 峰).min())
    平仓 = 全部事件

    # ---- 硬校验 ----
    print()
    print("-" * 80)
    print("硬校验")
    print("-" * 80)
    信号数 = sum(1 for i in range(起始i, n) if any(
        eng.腿信号(k, i) != 0 for k in 池))
    print("  [6] 信号频率   : %d 根 bar 有信号（门槛 ≥30）%s"
          % (信号数, "✅" if 信号数 >= 30 else "❌ 结果作废"))
    print("  [7] 品种数     : %d / 9（门槛 ≥9）%s"
          % (len(池), "✅" if len(池) >= 9 else "❌"))
    print("  [8] oi 零值 bar: %d 次（因子分母；要求剔除不填充）" % 零值bar)
    print("  [9] 平仓数     : %d 笔" % len(平仓))
    ok = 信号数 >= 30 and len(池) >= 9

    print()
    print("=" * 80)
    print("结果【%s · %s】" % (窗名, 中))
    print("=" * 80)
    print("  权益    : %.0f → %.0f  (%+.2f%%)"
          % (E.INIT, eq[-1], (eq[-1] / E.INIT - 1) * 100))
    print("  夏普    : %.3f   最大回撤 %.2f%%" % (sharpe, 回撤 * 100))
    print("  交易    : %d 笔平仓" % len(平仓))
    if 平仓:
        盈 = [x for x in 平仓 if x.get("pnl", 0) > 0]
        print("  胜率    : %.1f%% (%d/%d)"
              % (100 * len(盈) / len(平仓), len(盈), len(平仓)))
        多 = [x for x in 平仓 if x["dir"] > 0]
        空 = [x for x in 平仓 if x["dir"] < 0]
        print("  多腿    : %d 笔  合计 %+.0f" % (len(多), sum(x["pnl"] for x in 多)))
        print("  空腿    : %d 笔  合计 %+.0f" % (len(空), sum(x["pnl"] for x in 空)))
    print("  ✅ 有效结论" if ok else "  ❌ 校验未过，结论作废")

    out = os.path.join(HERE, "跑因子_结果_%s.json" % 窗名)
    with io.open(out, "w", encoding="utf-8") as f:
        json.dump({
            "窗口": 窗名, "起": str(起.date()), "止": str(止.date()),
            "品种池": 池, "INIT": E.INIT,
            "LOOKBACK": E.LOOKBACK, "OI_THRESH": E.OI_THRESH,
            "PX_THRESH": E.PX_THRESH, "TOP_N": E.TOP_N, "MAX_HOLD": E.MAX_HOLD,
            "对齐根数": n, "信号bar数": 信号数, "oi零值bar": 零值bar,
            "final": float(eq[-1]), "total_return": float(eq[-1] / E.INIT - 1),
            "sharpe": float(sharpe), "max_dd": 回撤,
            "平仓数": len(平仓),
            "权益曲线": [round(x, 2) for x in eq],
            "日期": [str(pd.Timestamp(d).date()) for d in 日期有效],
            "有效": bool(ok),
        }, f, ensure_ascii=False)
    print("落盘：%s" % out)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--窗", required=True, choices=list(窗口表))
    a = ap.parse_args()
    跑(a.窗)


if __name__ == "__main__":
    main()
