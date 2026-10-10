# -*- coding: utf-8 -*-
"""出水芙蓉/断头铡刀 · 信号频率预算（短周期版）

★ 只数信号，不建回测引擎（卡 §0 方法论教训：频率预算先于引擎）。
★ 唯一改动 = K 线周期（86400→300/900），其余核心.py 冻结参数原样照搬。
★ 用法：
    python 频率预算_出水芙蓉.py --周期 300     # 5 分钟
    python 频率预算_出水芙蓉.py --周期 900     # 15 分钟
    python 频率预算_出水芙蓉.py --周期 300 --品种 MA
"""
import os
import sys
import time
import json
import argparse
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, HERE)

import 核心 as K                                        # noqa: E402
import config_symbols as cs                             # noqa: E402
import config_account                                  # noqa: E402
from tqsdk import TqApi, TqAuth, TqSim                  # noqa: E402

池 = ["MA", "M", "RB", "V", "JD", "JM", "P", "RU", "SR"]
DENAM = {86400: "日频", 300: "5min", 900: "15min"}


def 收集(keys, dur):
    """★ 实时模式一次拉全部品种的短周期 K 线（volume 真实）。
    data_length 拉满 10000（≈5min 3.3 月 / 15min 10 月），够频率预算用。"""
    user, pwd = config_account.check()
    api = TqApi(TqSim(200000.0), auth=TqAuth(user, pwd))
    out = {}
    try:
        ks = {}
        for k in keys:
            _名, code, mult, tick = cs.get(k)
            ks[k] = (api.get_kline_serial(code, dur, data_length=10000),
                     mult, tick, float(cs.COST[k]))
        prev_first = None
        for _ in range(12):
            api.wait_update(deadline=time.time() + 60)
            firsts = tuple(int(kk["datetime"].to_numpy()[0]) if len(kk) else 0
                           for kk, _, _, _ in ks.values())
            if all(f > 0 for f in firsts) and firsts == prev_first:
                break
            prev_first = firsts
        for k, (kk, mult, tick, comm) in ks.items():
            n = len(kk)
            out[k] = dict(
                dates=kk["datetime"].to_numpy().astype(np.int64)[:n],
                o=kk["open"].to_numpy(float)[:n],
                h=kk["high"].to_numpy(float)[:n],
                l=kk["low"].to_numpy(float)[:n],
                c=kk["close"].to_numpy(float)[:n],
                v=kk["volume"].to_numpy(float)[:n],
                mult=mult, tick=tick, comm=comm)
    finally:
        try:
            api.close()
        except Exception:
            pass
    return out


def 向量指标(o, h, l, c, v):
    """全部用截至 t−1 窗口（与核心.py 一致）：shift(1) 排除当根。"""
    oc = pd.Series(c)
    ma5 = oc.shift(1).rolling(K.MA_FAST).mean().to_numpy()
    ma10 = oc.shift(1).rolling(K.MA_MID).mean().to_numpy()
    ma20 = oc.shift(1).rolling(K.MA_SLOW).mean().to_numpy()
    pc = pd.Series(c).shift(1)
    hc, lc = pd.Series(h), pd.Series(l)
    tr = np.maximum.reduce([
        (hc - lc).to_numpy(),
        (hc - pc).abs().to_numpy(),
        (lc - pc).abs().to_numpy()])
    atr = pd.Series(tr).shift(1).rolling(K.ATR_N).mean().to_numpy()
    volma = pd.Series(v).shift(1).rolling(K.VOL_MA_N).mean().to_numpy()
    hh = hc.shift(1).rolling(K.DONCHIAN_N).max().to_numpy()
    ll = lc.shift(1).rolling(K.DONCHIAN_N).min().to_numpy()
    return ma5, ma10, ma20, atr, volma, hh, ll


def 数信号(原始):
    """返回每品种 {信号总数, 多, 空, 总根数, 首根日期, 末根日期, volume零值}。"""
    res = {}
    for k, s in 原始.items():
        o, h, l, c, v = s["o"], s["h"], s["l"], s["c"], s["v"]
        if len(c) < K.MA_SLOW + 2:
            res[k] = dict(信号=0, 多=0, 空=0, 根数=len(c))
            continue
        ma5, ma10, ma20, atr, volma, hh, ll = 向量指标(o, h, l, c, v)
        up = np.maximum(np.maximum(ma5, ma10), ma20)
        dn = np.minimum(np.minimum(ma5, ma10), ma20)
        fin = np.isfinite(up) & np.isfinite(atr) & np.isfinite(volma) & np.isfinite(c) & (c > 0)
        long_sig = np.zeros(len(c), bool)
        short_sig = np.zeros(len(c), bool)
        idx = np.where(fin)[0]
        for i in idx:
            if K.是多头信号(o[i], c[i], v[i], up[i], atr[i], volma[i]):
                long_sig[i] = True
            elif K.是空头信号(o[i], c[i], v[i], dn[i], atr[i], volma[i]):
                short_sig[i] = True
        res[k] = dict(
            信号=int(long_sig.sum() + short_sig.sum()),
            多=int(long_sig.sum()), 空=int(short_sig.sum()),
            根数=len(c),
            首根=pd.Timestamp(int(s["dates"][0])).strftime("%Y-%m-%d"),
            末根=pd.Timestamp(int(s["dates"][-1])).strftime("%Y-%m-%d"),
            零量=int((v <= 0).sum()))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--周期", type=int, required=True, choices=[300, 900])
    ap.add_argument("--品种", default=None, help="单品种调试，默认全 9 品种")
    args = ap.parse_args()
    keys = [args.品种] if args.品种 else 池
    名 = DENAM.get(args.周期, str(args.周期))

    print("=" * 70, flush=True)
    print("出水芙蓉信号频率预算 · 周期=%s · 冻结参数(MA5/10/20,ATR20,VOL×3,实体1.5ATR,前高60) · 9品种池"
          % 名, flush=True)
    print("=" * 70, flush=True)

    原始 = 收集(keys, args.周期)
    for k in keys:
        n = len(原始[k]["dates"])
        print("  %s: %d 根, %s~%s, volume零值=%d" % (
            k, n, 原始[k]["首根"] if "首根" in 原始[k] else "-",
            "收集中", 0), flush=True) if False else None
    res = 数信号(原始)

    print()
    print("%-6s %8s %6s %6s %10s %14s" % ("品种", "信号数", "多", "空", "总根数", "数据区间"))
    print("-" * 56)
    总 = 0
    for k in keys:
        r = res[k]
        print("%-6s %8d %6d %6d %10d %s~%s" % (
            k, r["信号"], r["多"], r["空"], r["根数"],
            r.get("首根", "-"), r.get("末根", "-")))
        总 += r["信号"]
    print("-" * 56)
    print("★ 合计信号 = %d （9 品种池，周期 %s）" % (总, 名))
    print()
    if 总 == 0:
        print("⇒ 该周期下冻结参数仍 0 信号：形态在短周期也激发不出来，维持无效收档。")
    else:
        # 每千根信号率（跨品种口径可比）
        总根 = sum(r["根数"] for r in res.values())
        print("⇒ 信号率 ≈ %.2f 个/千根（总根数 %d）。日频 5 年×9 品种曾为 0 ⇒ 短周期确实能触发。"
              % (总 / 总根 * 1000, 总根))

    out = dict(周期=args.周期, 周期名=名, 每品种=res, 总信号=总)
    p = os.path.join(HERE, "频率预算_%s.json" % 名)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print("已落盘:", p)


if __name__ == "__main__":
    main()
