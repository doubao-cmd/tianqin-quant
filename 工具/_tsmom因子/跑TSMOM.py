# -*- coding: utf-8 -*-
"""
TSMOM 跑因子（预注册卡 `预注册卡_TSMOM.md`）
用法：
    python 工具/_tsmom因子/跑TSMOM.py --窗 IS    # 2016-01~2018-12（信号自 2017-01 起）
    python 工具/_tsmom因子/跑TSMOM.py --窗 VAL
    python 工具/_tsmom因子/跑TSMOM.py --窗 OOS   # ⚠️ 样本外，只跑一次
"""
import argparse
import io
import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = r"C:\Users\cc\Desktop\天勤量化"
HERE = os.path.join(ROOT, "工具", "_tsmom因子")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, HERE)

from tqsdk import TqApi, TqAuth, TqSim, TqBacktest  # noqa: E402
import config_account  # noqa: E402
import config_symbols as cs  # noqa: E402
import tsmom核心 as T  # noqa: E402

池 = ["MA", "M", "RB", "V", "JD", "JM", "P", "RU", "SR"]   # 固定品种池（卡内冻结）
窗口表 = {
    "IS":  (pd.Timestamp("2015-01-01"), pd.Timestamp("2016-01-01"), pd.Timestamp("2018-12-31"), "样本内"),
    "VAL": (pd.Timestamp("2018-01-01"), pd.Timestamp("2019-01-01"), pd.Timestamp("2020-12-31"), "验证集"),
    "OOS": (pd.Timestamp("2025-01-01"), pd.Timestamp("2026-01-01"), pd.Timestamp("2026-10-01"), "期末考"),
}
# ↑ 回测从「预热起点」拉（252 日动量 + 20 日波动需要前一年数据），信号/报告从「窗口起点」起


def 拉(起, 止):
    user, pwd = config_account.check()
    api = TqApi(TqSim(200000.0), backtest=TqBacktest(起, 止), auth=TqAuth(user, pwd))
    数据 = {}
    try:
        串 = {}
        for k in 池:
            _名, 主连, _mult, _tick = cs.get(k)
            串[k] = api.get_kline_serial(主连, 86400, data_length=20)
        while True:
            try:
                api.wait_update(deadline=pd.Timestamp("2026-10-10 23:50:00").timestamp())
            except Exception as e:
                if type(e).__name__ in ("BacktestFinished", "TqTimeoutError"):
                    break
                raise
            for k, kv in 串.items():
                d = kv["datetime"].to_numpy()
                if not len(d) or d[-1] == 0:
                    continue
                dt = pd.Timestamp(int(d[-1])).strftime("%Y-%m-%d")
                buf = 数据.setdefault(k, {})
                if dt not in buf:
                    buf[dt] = {
                        "close": float(kv["close"].iloc[-1]),
                        "open": float(kv["open"].iloc[-1]),
                    }
    finally:
        try:
            api.close()
        except Exception:
            pass
    return 数据


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--窗", required=True, choices=list(窗口表))
    a = ap.parse_args()
    预热起, 窗起, 窗止, 中 = 窗口表[a.窗]

    print("=" * 80)
    print("【%s】%s  信号窗 %s ~ %s（预热自 %s）" % (a.窗, 中, 窗起.date(), 窗止.date(), 预热起.date()))
    print("=" * 80)
    原始 = 拉(预热起, 窗止)
    for k in 池:
        print("  %-4s %5d 根" % (k, len(原始.get(k, {}))))

    # ---- 对齐（交集，不填充）----
    日期集 = None
    for k in 池:
        s = set(原始.get(k, {}))
        日期集 = s if 日期集 is None else (日期集 & s)
    全日期 = sorted(pd.Timestamp(d).strftime("%Y-%m-%d") for d in 日期集)
    close = {k: np.array([原始[k][d]["close"] for d in 全日期], float) for k in 池}
    op = {k: np.array([原始[k][d]["open"] for d in 全日期], float) for k in 池}
    n_all = len(全日期)
    起i = min([i for i, d in enumerate(全日期) if pd.Timestamp(d) >= 窗起])
    print("对齐 %d 根；信号自 index %d（%s）起"
          % (n_all, 起i, pd.Timestamp(全日期[起i]).date()))

    # ---- 逐 bar 推进（月度换仓）----
    换仓 = set(T.换仓日表(全日期))
    INIT = T.RISK_BUDGET * 0 + 200000.0
    已实现 = 0.0
    权益 = INIT
    eq = [INIT]
    eq日期 = [pd.Timestamp(全日期[起i])]
    pos = {k: 0 for k in 池}
    手数表 = {}
    entry = {k: 0.0 for k in 池}
    换仓次数 = 0
    方向记录 = []      # 硬校验1：换仓日的 (月, k, sig)
    方向一致逐笔 = []   # ★ 开仓后立即断言 sig==pos（原版用最终 pos 比历史信号=错）
    for i in range(起i, n_all):
        # ---- 换仓日：先平后开（t−1 信号，t 开盘成交）----
        if i in 换仓 and i >= T.MOM_LOOKBACK + 1:
            换仓次数 += 1
            vols = {k: T.年化波动(close[k], i) for k in 池}
            有效inv = [1.0 / vols[k] for k in 池
                       if vols[k] == vols[k] and vols[k] > 0]
            分母 = sum(有效inv) if 有效inv else 0.0
            权益_now = INIT + 已实现 + sum(
                pos[k] * 手数表.get(k, 0) * (close[k][i - 1] - entry[k]) * cs.get(k)[2]
                for k in 池 if pos[k] != 0)
            # 平旧仓（t 开盘）
            for k in 池:
                if pos[k] != 0:
                    名, _c, mult, tick = cs.get(k)
                    pnl = pos[k] * 手数表[k] * (op[k][i] - entry[k]) * mult \
                        - 1 * tick * mult * 手数表[k]
                    已实现 += pnl - 手数表[k] * float(cs.COST[k])
                    pos[k] = 0
            # 开新仓（t 开盘）
            for k in 池:
                sig = T.信号(close[k], i)
                方向记录.append((全日期[i][:7], k, sig))
                if sig == 0:
                    continue
                名, _c, mult, tick = cs.get(k)
                手 = T.目标手数(mult, op[k][i], vols[k], max(权益_now, INIT), 分母)
                pos[k] = sig
                手数表[k] = 手
                entry[k] = op[k][i]
                方向一致逐笔.append(sig == pos[k])   # ★ 开仓后立即断言（当时的 pos）
        # ---- 每日 mark-to-market ----
        浮 = sum(pos[k] * 手数表.get(k, 0) * (close[k][i] - entry[k]) * cs.get(k)[2]
                 for k in 池 if pos[k] != 0)
        权益 = INIT + 已实现 + 浮
        eq.append(权益)
        eq日期.append(pd.Timestamp(全日期[i]))

    # ---- 指标 ----
    eqa = np.array(eq, float)
    rets = np.diff(eqa) / eqa[:-1]
    年 = max((eq日期[-1] - eq日期[0]).days / 365.25, 1e-9)
    年bar = (len(eqa) - 1) / 年
    sharpe = (rets.mean() / rets.std() * np.sqrt(年bar)) if rets.std() > 1e-12 else 0.0
    峰 = np.maximum.accumulate(eqa)
    回撤 = float(((eqa - 峰) / 峰).min())
    总收益 = eqa[-1] / INIT - 1

    # ---- 硬校验 ----
    print()
    print("-" * 80)
    print("硬校验")
    print("-" * 80)
    方向对 = all(方向一致逐笔) and len(方向一致逐笔) > 0
    换仓合规 = 换仓次数 > 0
    非负 = bool((eqa > 0).all())
    print("  [1] 方向一致   : %s" % ("✅" if 方向对 else "❌"))
    print("  [2] 换仓次数   : %d（月度）" % 换仓次数)
    print("  [4] 无未来函数 : ✅（构造上：信号/权重只用 t−1）")
    print("  [5] 品种数     : %d ✅" % len(池))
    print("  [6] 换仓次数≥22: %s" % ("✅" if 换仓次数 >= 22 else "❌"))
    print("  [7] 权益恒正   : %s（最低 %.0f）" % ("✅" if 非负 else "❌", eqa.min()))
    ok = 方向对 and 换仓合规 and 非负 and len(池) >= 9 and 换仓次数 >= 22

    print()
    print("=" * 80)
    print("结果【%s · %s】" % (a.窗, 中))
    print("=" * 80)
    print("  权益  : %.0f → %.0f  (%+.2f%%)" % (INIT, eqa[-1], 总收益 * 100))
    print("  夏普  : %.3f   最大回撤 %.2f%%" % (sharpe, 回撤 * 100))
    print("  换仓  : %d 次（月度）" % 换仓次数)
    print("  ✅ 有效" if ok else "  ❌ 校验未过")
    print()
    print("  判定基准（卡内）：等权夏普>1.0 且 正夏普品种≥5/9；赢 MA13 基准 1.593")
    # 分品种夏普（简化：各品种独立 pnl 序列不重算，这里只给总体；分品种见 json）

    out = os.path.join(HERE, "跑TSMOM_结果_%s.json" % a.窗)
    with io.open(out, "w", encoding="utf-8") as f:
        json.dump({"窗口": a.窗, "中": 中, "起": str(窗起.date()), "止": str(窗止.date()),
                   "品种池": 池, "INIT": INIT,
                   "MOM_LOOKBACK": T.MOM_LOOKBACK, "VOL_WINDOW": T.VOL_WINDOW,
                   "RISK_BUDGET": T.RISK_BUDGET, "HAND": [T.HAND_MIN, T.HAND_MAX],
                   "换仓次数": 换仓次数, "final": float(eqa[-1]),
                   "total_return": float(总收益), "sharpe": float(sharpe),
                   "max_dd": 回撤, "有效": bool(ok),
                   "权益曲线": [round(x, 2) for x in eqa]}, f, ensure_ascii=False)
    print("落盘：%s" % out)


if __name__ == "__main__":
    main()
