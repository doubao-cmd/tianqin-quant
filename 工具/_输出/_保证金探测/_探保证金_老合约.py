# -*- coding: utf-8 -*-
"""
【只读探测 · 一次性】已下市的老合约，还能不能取到 margin？
==========================================================
已确认：具体合约（underlying_symbol）的 margin 在回测中可得，但**不随价格变**。
隐患：本项目的样本内区间是 2016-01-01 起，那时的合约（如 DCE.v1605）早已下市。
      若已下市合约取不到 margin ⇒ 动态方案在 2016-2019 区间会失效。
本轮按三个关键区间各测一次：样本内 2016 / 验证集 2020 / 样本外 2025。

不写盘、不下单、不改任何配置。
"""
import sys
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from datetime import date
import config_account
import config_symbols
from tqsdk import TqApi, TqAuth, TqBacktest

USER, PWD = config_account.check()

# 三个关键区间（各取回测起点后约 1 个月）
CASES = [
    ("样本内 2016", date(2016, 1, 4), date(2016, 2, 4)),
    ("验证集 2020", date(2020, 1, 2), date(2020, 2, 4)),
    ("样本外 2025", date(2025, 1, 2), date(2025, 2, 4)),
]

KEYS = ["V", "MA", "LH", "RB", "SA", "M"]   # 覆盖 DCE / CZCE / SHFE


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _ok(x):
    return x == x


def probe(label, start, end):
    print("\n" + "=" * 92)
    print(f"  {label} | {start} ~ {end}")
    print("=" * 92)
    api = None
    try:
        api = TqApi(backtest=TqBacktest(start_dt=start, end_dt=end),
                    auth=TqAuth(USER, PWD), web_gui=False)
        mains = {k: api.get_quote(config_symbols.SYMBOLS[k][1]) for k in KEYS}
        for _ in range(6):
            api.wait_update()

        print(f"{'品种':<8}{'当时具体合约':<16}{'最新价':>10}{'乘数':>7}"
              f"{'margin':>12}{'可得':>7}  备注")
        print("-" * 92)
        got = 0
        for k in KEYS:
            cn = config_symbols.SYMBOLS[k][0]
            q = mains[k]
            und = getattr(q, "underlying_symbol", None)
            px = _f(getattr(q, "last_price", float("nan")))
            vm = _f(getattr(q, "volume_multiple", float("nan")))
            mg = float("nan")
            err = ""
            try:
                q2 = api.get_quote(und)
                api.wait_update()
                mg = _f(getattr(q2, "margin", float("nan")))
            except Exception as e:
                err = f"{type(e).__name__}"

            avail = "是" if (_ok(mg) and mg > 0) else "否"
            if avail == "是":
                got += 1

            def _s(x, w, p=2):
                return f"{x:>{w}.{p}f}" if _ok(x) else f"{'nan':>{w}}"

            print(f"{cn:<8}{str(und):<16}{_s(px,10)}{_s(vm,7,0)}"
                  f"{_s(mg,12,1)}{avail:>7}  {err}")

        print(f"\n  可得：{got}/{len(KEYS)}")
        return got, len(KEYS)
    except Exception as e:
        print(f"  !! 该区间探测失败：{type(e).__name__}: {e}")
        return 0, len(KEYS)
    finally:
        if api is not None:
            try:
                api.close()
            except Exception:
                pass


def main():
    print("=" * 92)
    print("  已下市老合约的 margin 可得性 —— 决定动态方案能否覆盖样本内区间")
    print("=" * 92)
    tot_g, tot_n = 0, 0
    for label, s, e in CASES:
        g, n = probe(label, s, e)
        tot_g += g
        tot_n += n
    print("\n" + "=" * 92)
    print(f"  合计：{tot_g}/{tot_n}")
    print("  判读：")
    print("    · 三段全可得 ⇒ 动态方案覆盖全时段，2016 起的老回测也能用")
    print("    · 老区间 nan ⇒ 老区间必须退化到『价格 × 乘数 × 备用保证金率』")
    print("=" * 92)


if __name__ == "__main__":
    main()
