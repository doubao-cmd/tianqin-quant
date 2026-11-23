# -*- coding: utf-8 -*-
"""参数稳健性：HOLD × 止损 网格，只报不挑。

目的
----
判死结论建立在 HOLD=12 / 2ATR 这一个点上。本脚本对 3×3 个外生组合
各跑一遍，统计「有几个组合是净负」，回答：
  · 全负  -> 结论不依赖参数点（可升级措辞）
  · 有正  -> 判死只能限定在该参数点，不得外推

纪律
----
- **不选最优、不报"最好那个组合"、不做任何择优**
- 信号定义与成本口径完全复用 `进场体检.py`，不改一行策略逻辑
- HOLD / stop 是**外生取值**（与 15min 实验 A 同一组），不是搜出来的

用法
----
python 参数稳健性.py --period 5min --sigs retrace_ma13,cross_ma13
python 参数稳健性.py --period 15min --sigs all
"""
import argparse
import io
import json
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import 进场体检 as ep  # noqa: E402

HOLDS = [6, 12, 24]
STOPS = [1.5, 2.0, 3.0]
KEYS = ["JM", "M", "JD", "P", "SR", "MA", "RB", "V", "RU", "SA", "EB", "LH"]


def screen_fixed(key, sig_name, o, h, l, c, atr, ma, sig, hold, stop):
    """与 进场体检.screen_one 同算法，只把 HOLD / stop 参数化（供网格复用同一份信号）。"""
    N = len(c)
    bad = np.isnan(atr) | np.isnan(ma)
    sig = np.where(bad, 0.0, np.nan_to_num(sig, nan=0.0))

    valid = np.arange(0, N - hold - 1)
    idx = valid[sig[valid] != 0]
    if len(idx) == 0:
        return None

    sgn = sig[idx]
    entry = o[idx + 1]
    cp = ep.cost_points(key)

    fwd = (o[idx + 1 + hold] - entry) * sgn
    st = np.where(sgn > 0, entry - stop * atr[idx], entry + stop * atr[idx])
    exit_px = o[idx + 1 + hold].copy()
    hit = np.zeros(len(idx), dtype=bool)
    for j, (e_i, s0, s) in enumerate(zip(idx, st, sgn)):
        for jj in range(e_i + 1, e_i + 1 + hold):
            if s > 0:
                if l[jj] <= s0:
                    exit_px[j] = min(s0, o[jj]); hit[j] = True; break
            else:
                if h[jj] >= s0:
                    exit_px[j] = max(s0, o[jj]); hit[j] = True; break
    g1 = (exit_px - entry) * sgn
    net1 = g1 - cp

    def _t(x):
        sd = float(np.std(x, ddof=1)) if len(x) > 1 else 0.0
        return float(np.mean(x) / (sd / np.sqrt(len(x)))) if sd > 1e-12 else 0.0

    return dict(key=key, signal=sig_name, n=int(len(idx)),
                hold=hold, stop=stop,
                L0_gross=round(float(np.mean(fwd)), 3),
                L1_gross=round(float(np.mean(g1)), 3),
                L1_net=round(float(np.mean(net1)), 3),
                L1_t=round(_t(net1), 2),
                ratio=round(float(np.mean(g1) / cp), 3) if cp > 0 else None,
                stop_share=round(float(hit.mean()), 4))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="5min")
    ap.add_argument("--sigs", default="retrace_ma13,cross_ma13,breakout_20,tsmom_20,rev_2bar,vol_break")
    ap.add_argument("--keys", default=None, help="逗号分隔，默认 12 品种")
    args = ap.parse_args()

    sigs = list(ep.SIGNALS) if args.sigs == "all" else [s.strip() for s in args.sigs.split(",") if s.strip()]
    keys = [k.strip() for k in args.keys.split(",")] if args.keys else KEYS

    out = []
    for key in keys:
        df = ep.load(key, args.period)
        if df is None or len(df) < 100:
            print(f"[skip] {key} 无数据")
            continue
        o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
        atr = ep._atr(h, l, c, ep.ATR_N)
        ma = ep._sma(c, ep.MA_N)
        for sig_name in sigs:
            sig = ep.SIGNALS[sig_name][1](o, h, l, c, atr, ma)
            for hold in HOLDS:
                for stop in STOPS:
                    r = screen_fixed(key, sig_name, o, h, l, c, atr, ma, sig, hold, stop)
                    if r:
                        r["period"] = args.period
                        out.append(r)
        print(f"[ok] {key} 信号 {len(sigs)} × 组合 9", flush=True)

    # 汇总：只看「9 组合里有几个净负」，不排名
    print("\n" + "=" * 92)
    print(f"{args.period}  参数网格 {len(HOLDS)}×{len(STOPS)}  信号 {len(sigs)}  品种 {len(keys)}")
    print("=" * 92)
    by = {}
    for r in out:
        by.setdefault((r["signal"], r["key"]), []).append(r)

    all_neg = 0
    totals = 0
    print(f"{'signal':<16}{'sym':<5}{'格数':>4}{'净负格':>6}{'全负?':>6}"
          f"{'ratio_min':>10}{'ratio_max':>10}{'net_min':>9}{'net_max':>9}")
    for (s, k), rs in sorted(by.items()):
        neg = sum(1 for r in rs if r["L1_net"] < 0)
        rat = [r["ratio"] for r in rs if r["ratio"] is not None]
        nets = [r["L1_net"] for r in rs]
        flag = "YES" if neg == len(rs) else "no"
        all_neg += (neg == len(rs)); totals += 1
        print(f"{s:<16}{k:<5}{len(rs):>4}{neg:>6}{flag:>6}"
              f"{min(rat):>10.3f}{max(rat):>10.3f}{min(nets):>9.3f}{max(nets):>9.3f}")

    print(f"\n全负的 (信号,品种) 组合：{all_neg} / {totals}")
    print("纪律：本表只用于判断「结论是否依赖参数点」，禁止挑 ratio 最高格。")

    fn = f"参数稳健性_{args.period}_{len(sigs)}信号.json"
    io.open(os.path.join(_HERE, fn), "w", encoding="utf-8").write(
        json.dumps(out, ensure_ascii=False, indent=1))
    print(f"[写入] {fn}  共 {len(out)} 条")


if __name__ == "__main__":
    main()
