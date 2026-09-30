# -*- coding: utf-8 -*-
"""资金容量 —— 小资金到底养得起几个品种？

2026-09-26 立项。起因：用户指出「九个品种组合」在 40 万资金下不成立。

★ 本工具要纠正一个常见误解：**瓶颈不是保证金，是「一手的最小风险」。**
  保证金只是入场门槛（V 一手只要 1,670 元）；
  真正卡死小资金的是「一手止损就亏掉多少钱」——
  橡胶一手止损亏 5,257 元，而 0.3% 预算只有 1,200 元 ⇒ 预算失效。

三个可测的量：

  ① 够格权益 = 止损倍数 × 中位ATR × 乘数 ÷ risk_pct
     含义：一手风险刚好落进预算所需的本金。
     本金 < 够格权益 ⇒ 风险预算失效，实际风险由「一手」决定（风控审计已证）。

  ② 保证金占用 = 同时持有 N 个品种各一手要占用多少资金。

  ③ 相关性 —— 养得起的品种之间，到底能不能分散。

⚠️ 本工具**不消耗试验次数 K**（量具，与 风控审计.py / 功效检验.py 同类）。
跑法： python 工具/资金容量.py
"""

import os
import sys
import json
import time
import datetime as dt

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_account  # noqa: E402

# ============================================================
# 预注册参数（跑前写死）
# ============================================================
品种列表 = ["RU", "EB", "JM", "P", "M", "V", "LH", "JD", "SR", "MA", "RB", "SA"]

止损倍数档 = [1.0, 2.0]        # 1×ATR（03布林带口径）/ 2×ATR（01双均线口径）
RISK_PCT = 0.30                # 名义单笔风险预算（% 权益）—— 够格权益按它对标
风险预算档 = [0.30, 1.0, 2.0]  # 小资金被迫放宽预算时的对照档
ATR周期 = 14
ATR窗口 = 500                  # 用最近 500 个交易日算中位 ATR（约 2 年）
# ★ 2026-09-26 用户补充：模拟盘 100 万、实盘估计几万 ⇒ 必须覆盖小资金档
本金档 = [30000, 50000, 80000, 100000, 200000, 400000, 1000000]

输出目录 = os.path.join(_HERE, "_输出")


def 读配置():
    """从 品种池/<key>.py 读静态字段。"""
    import importlib.util as ilu
    out = {}
    for k in 品种列表:
        p = os.path.join(_ROOT, "品种池", "%s.py" % k)
        s = ilu.spec_from_file_location("_品种池_%s" % k, p)
        m = ilu.module_from_spec(s)
        s.loader.exec_module(m)
        out[k] = dict(m.品种)
    return out


def 取日线(keys, cfg, 长度=ATR窗口 + 60):
    """主线程串行拉主连日线。返回 {key: DataFrame}。"""
    from tqsdk import TqApi, TqAuth
    user, pwd = config_account.check()
    api = TqApi(auth=TqAuth(user, pwd), web_gui=False)
    件 = {}
    try:
        for k in keys:
            sym = cfg[k]["主连"]
            t0 = time.time()
            ks = api.get_kline_serial(sym, 86400, data_length=长度)
            d = ks[ks["datetime"] > 0].copy()
            d["日期"] = pd.to_datetime(d["datetime"], unit="ns", utc=True) \
                          .dt.tz_convert("Asia/Shanghai").dt.date
            件[k] = d.reset_index(drop=True)
            print("  取数 %-4s %-16s %4d 根  %.2f 秒" % (
                k, sym, len(d), time.time() - t0), flush=True)
    finally:
        try:
            api.close()
        except Exception:
            pass
    return 件


def 算ATR(d, n=ATR周期):
    """TR 的 n 期简单均值（与 lib/engine._atr 同口径）。"""
    h = d["high"].astype(float).values
    l = d["low"].astype(float).values
    c = d["close"].astype(float).values
    tr = np.zeros(len(d))
    tr[0] = h[0] - l[0]
    for i in range(1, len(d)):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr = pd.Series(tr).rolling(n).mean().values
    return atr


def 主流程():
    cfg = 读配置()
    print("== 取数（主线程串行，%d 个主连）==" % len(品种列表), flush=True)
    件 = 取日线(品种列表, cfg)

    # ---- 每品种：中位 ATR / 中位价格 ----
    行 = []
    for k in 品种列表:
        d = 件.get(k)
        if d is None or len(d) < ATR周期 + 20:
            行.append({"key": k, "有效": False})
            continue
        atr = 算ATR(d)
        m = ~np.isnan(atr)
        用 = slice(max(0, len(d) - ATR窗口), len(d))
        a = atr[用]
        a = a[~np.isnan(a)]
        px = d["close"].astype(float).values[用]
        c = cfg[k]
        行.append({
            "key": k, "有效": True,
            "中文名": c.get("中文名", k),
            "乘数": float(c["乘数"]),
            "一跳": float(c["一跳"]),
            "保证金": float(c.get("保证金", 0) or 0),
            "中位ATR": float(np.median(a)),
            "中位价": float(np.median(px)),
            "最新价": float(d["close"].astype(float).values[-1]),
            "ATR占价%": float(np.median(a) / np.median(px) * 100.0),
            "样本根数": int(len(a)),
            "起": str(d["日期"].values[0]),
            "止": str(d["日期"].values[-1]),
        })

    表 = pd.DataFrame(行)
    if not 表["有效"].all():
        print("⚠️ 有品种取数失败：", 表[~表["有效"]]["key"].tolist())

    # ---- ① 一手风险与够格权益 ----
    for 倍 in 止损倍数档:
        表["一手风险%.1fATR" % 倍] = 表["中位ATR"] * 表["乘数"] * 倍
        表["够格权益%.1fATR" % 倍] = 表["一手风险%.1fATR" % 倍] / (RISK_PCT / 100.0)

    表["名义1手"] = 表["中位价"] * 表["乘数"]
    表["保证金率%"] = 表["保证金"] / 表["名义1手"] * 100.0

    # 成本/一手风险（开平双边，用品种池里的单边成本 × 2）
    成本单边 = {k: float(cfg[k].get("成本", 0) or 0) for k in 品种列表}
    表["成本单边"] = 表["key"].map(成本单边)
    表["开平成本"] = 表["成本单边"] * 2.0
    表["成本占2ATR风险%"] = 表["开平成本"] / 表["一手风险2.0ATR"] * 100.0

    表 = 表.sort_values("够格权益2.0ATR")

    # ---- ② 各本金 × 各风险预算档：够格品种与可开手数 ----
    档结果 = {}
    for 本金 in 本金档:
        for rp in 风险预算档:
            够 = []
            for _, r in 表.iterrows():
                if not r["有效"]:
                    continue
                一手风险 = float(r["一手风险2.0ATR"])
                手数 = int(本金 * (rp / 100.0) // 一手风险)
                够.append({
                    "key": r["key"], "中文名": r["中文名"],
                    "一手风险": 一手风险,
                    "够格权益": 一手风险 / (rp / 100.0),
                    "可开手数": 手数,
                    "实际风险%": 一手风险 / 本金 * 100.0,
                    "保证金": float(r["保证金"]),
                })
            够 = sorted(够, key=lambda x: x["够格权益"])
            档结果[(本金, rp)] = 够

    # ---- ③ 相关性（共同交易日日收益） ----
    closes = {}
    for k in 品种列表:
        d = 件.get(k)
        if d is None or len(d) < 60:
            continue
        s = pd.Series(d["close"].astype(float).values, index=pd.to_datetime(d["日期"]))
        closes[k] = s
    价表 = pd.DataFrame(closes).dropna()
    去年 = 价表.iloc[-ATR窗口:] if len(价表) > ATR窗口 else 价表
    收益 = 去年.pct_change().dropna()
    相关 = 收益.corr()

    # ---- 落盘 ----
    os.makedirs(输出目录, exist_ok=True)
    表.to_csv(os.path.join(输出目录, "_资金容量_表.csv"),
             index=False, encoding="utf-8-sig")
    with open(os.path.join(输出目录, "_资金容量_相关.csv"), "w",
              encoding="utf-8-sig") as f:
        相关.round(3).to_csv(f)

    print("\n=== ① 一手风险与够格权益（risk_pct = %.2f%%，ATR 取近 %d 日中位）==="
          % (RISK_PCT, ATR窗口), flush=True)
    print("%-4s %-8s %7s %7s %10s %10s %10s %8s" % (
        "key", "中文名", "乘数", "保证金", "中位ATR", "一手风险2x", "够格权益2x", "ATR%"), flush=True)
    for _, r in 表.iterrows():
        if not r["有效"]:
            continue
        print("%-4s %-8s %7.0f %7.0f %10.1f %10.0f %10s %7.2f%%" % (
            r["key"], r["中文名"], r["乘数"], r["保证金"], r["中位ATR"],
            r["一手风险2.0ATR"], "{:,.0f}".format(r["够格权益2.0ATR"]), r["ATR占价%"]),
            flush=True)

    print("\n=== ② 各本金 × 各风险预算：可开手数 ≥1 的品种数 ===", flush=True)
    print("%-10s %12s %12s %12s" % (
        "本金", *["预算 %.2f%%" % rp for rp in 风险预算档]), flush=True)
    for 本金 in 本金档:
        格 = []
        for rp in 风险预算档:
            n = sum(1 for x in 档结果[(本金, rp)] if x["可开手数"] >= 1)
            格.append("%d / %d" % (n, len(品种列表)))
        print("%-10s %12s %12s %12s" % ("{:,.0f}".format(本金), *格), flush=True)

    print("\n--- 名单：0.30% 预算（标准口径）---", flush=True)
    for 本金 in 本金档:
        够 = [x for x in 档结果[(本金, 0.30)] if x["可开手数"] >= 1]
        print("  %5s 万：%s" % ("{:,.1f}".format(本金 / 10000),
              "  ".join("%s(%d手)" % (x["key"], x["可开手数"]) for x in 够)
              if 够 else "✗ 无"), flush=True)

    print("\n--- 名单：放宽预算后（实盘小资金被迫的档位）---", flush=True)
    for 本金 in [30000, 50000, 80000, 100000, 200000]:
        for rp in [1.0, 2.0]:
            够 = [x for x in 档结果[(本金, rp)] if x["可开手数"] >= 1]
            print("  %5s 万 · 预算 %.1f%%：%s" % (
                "{:,.1f}".format(本金 / 10000), rp,
                "  ".join("%s(%d手, 单笔风险%.2f%%)" % (x["key"], x["可开手数"], x["实际风险%"])
                          for x in 够) if 够 else "✗ 无"), flush=True)

    print("\n=== ③ 相关性（近 %d 个共同交易日，日收益）===" % len(收益), flush=True)
    print(相关.round(2).to_string(), flush=True)

    # 保存 JSON（含全部中间量）
    out = {
        "参数": {"RISK_PCT": RISK_PCT, "ATR周期": ATR周期, "ATR窗口": ATR窗口,
                 "止损倍数档": 止损倍数档, "本金档": 本金档},
        "表": 表.replace({np.nan: None}).to_dict("records"),
        "档结果": {str(k): v for k, v in 档结果.items()},
        "相关": 相关.round(4).to_dict(),
        "相关_起止": [str(收益.index[0].date()), str(收益.index[-1].date())],
    }
    with open(os.path.join(输出目录, "_资金容量_结果.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print("\n落盘：%s" % os.path.join(输出目录, "_资金容量_结果.json"), flush=True)


if __name__ == "__main__":
    主流程()
