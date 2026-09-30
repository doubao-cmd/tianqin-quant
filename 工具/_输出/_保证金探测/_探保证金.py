# -*- coding: utf-8 -*-
"""
【只读探测 · 一次性】回测模式下，quote 能不能拿到「每手保证金」？
==========================================================
目的：判断「保证金 ≤ 权益 30%」这条风控，能否用**动态**值实现。
     需要在 TqBacktest（回测）模式下确认以下字段是否可用：
       quote.margin            —— 天勤文档里的「每手保证金」
       quote.volume_multiple   —— 合约乘数
       quote.last_price        —— 最新价
       quote.price_tick        —— 最小变动价位
     并反推 margin_ratio = margin / (last_price × volume_multiple)。

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

START = date(2026, 8, 3)
END = date(2026, 9, 3)


def _g(q, name):
    try:
        v = getattr(q, name)
    except Exception as e:
        return f"<{type(e).__name__}>"
    return v


def main():
    print("=" * 96)
    print(f"  回测模式保证金字段探测 | {START} ~ {END}")
    print("=" * 96)

    api = TqApi(backtest=TqBacktest(start_dt=START, end_dt=END),
                auth=TqAuth(USER, PWD), web_gui=False)
    try:
        quotes = {}
        for key, (cn, main, mult, tick) in sorted(config_symbols.SYMBOLS.items()):
            quotes[key] = api.get_quote(main)

        # 推进几拍，让 quote 有值
        for _ in range(6):
            api.wait_update()

        hdr = (f"{'品种':<8}{'主连':<16}{'最新价':>10}{'乘数':>8}{'一跳':>7}"
               f"{'margin':>12}{'保证金率':>10}{'一手名义':>12}")
        print(hdr)
        print("-" * 96)
        for key, q in quotes.items():
            cn, main, mult_cfg, tick_cfg = config_symbols.SYMBOLS[key]
            px = _g(q, "last_price")
            mg = _g(q, "margin")
            vm = _g(q, "volume_multiple")
            tk = _g(q, "price_tick")

            def _f(x):
                try:
                    return float(x)
                except (TypeError, ValueError):
                    return float("nan")

            fpx, fmg, fvm = _f(px), _f(mg), _f(vm)
            notional = fpx * fvm
            ratio = (fmg / notional * 100.0) if (notional and notional == notional
                                                 and fmg == fmg and notional > 0) else float("nan")

            def _s(x, w, p=2):
                try:
                    return f"{float(x):>{w}.{p}f}"
                except (TypeError, ValueError):
                    return f"{str(x):>{w}}"

            print(f"{cn:<8}{main:<16}{_s(fpx,10)}{_s(fvm,8,0)}{_s(tk,7,2)}"
                  f"{_s(fmg,12)}{_s(ratio,10,2)}%{_s(notional,12,0)}")

        print("-" * 96)
        print("判读：")
        print("  · margin 是正数        ⇒ 『动态每手保证金』可直接用 quote.margin")
        print("  · margin 是 nan/0/异常 ⇒ 只能退而用 价格 × 乘数 × 保证金率(需另配)")
        print("  · 与配置乘数比对        ⇒ 验证 quote.volume_multiple 是否可信")

        # 额外：直接看主连 quote 挂着的 underlying_symbol（真实月份合约）
        print("\n  换月标的（underlying_symbol）与具体合约的 margin：")
        for key, q in list(quotes.items())[:4]:
            cn = config_symbols.SYMBOLS[key][0]
            und = _g(q, "underlying_symbol")
            print(f"    {cn:<8} 主连 {str(_g(q,'instrument_id')):<16} -> {und}")
            try:
                q2 = api.get_quote(und)
                api.wait_update()
                print(f"            具体合约 margin={_g(q2,'margin')}"
                      f" | volume_multiple={_g(q2,'volume_multiple')}"
                      f" | last_price={_g(q2,'last_price')}")
            except Exception as e:
                print(f"            取具体合约失败：{type(e).__name__}: {e}")
    finally:
        api.close()


if __name__ == "__main__":
    main()
