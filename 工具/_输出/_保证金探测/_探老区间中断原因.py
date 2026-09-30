# -*- coding: utf-8 -*-
"""
【只读探测 · 一次性】定位：老区间回测到底为什么被中断？
==========================================================
上一轮：2016 / 2020 区间在「get_quote(具体合约)」后抛 BacktestFinished，2025 正常。
本轮做**对照实验**（单一变量），只跑 PVC 一个品种：
  阶段1  只订阅【主连】K线 + quote，wait N 次  -> 判断主连行情本身有没有
  阶段2  再 subscribe【具体合约】quote 并 wait  -> 判断是不是它把回测打断的
三个区间各做一次：2016 / 2020 / 2025。

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
from tqsdk import TqApi, TqAuth, TqBacktest

USER, PWD = config_account.check()

CASES = [
    ("2016", date(2016, 1, 4), date(2016, 3, 1)),
    ("2020", date(2020, 1, 2), date(2020, 3, 1)),
    ("2025", date(2025, 1, 2), date(2025, 3, 1)),
]
MAIN = "KQ.m@DCE.v"


def run(label, start, end):
    print("\n" + "=" * 88)
    print(f"  【{label}】{start} ~ {end}  |  品种 PVC ({MAIN})")
    print("=" * 88)
    api = None
    try:
        api = TqApi(backtest=TqBacktest(start_dt=start, end_dt=end),
                    auth=TqAuth(USER, PWD), web_gui=False)

        # ---------- 阶段1：只碰主连 ----------
        kl = api.get_kline_serial(MAIN, 1800, 60)
        quote = api.get_quote(MAIN)
        n1 = 0
        try:
            for _ in range(30):
                api.wait_update()
                n1 += 1
        except Exception as e:
            print(f"  [阶段1] 只碰主连 -> 第 {n1} 次 wait 抛 {type(e).__name__}: {e}")
            return
        last_bar = ""
        try:
            last_bar = str(kl.iloc[-1]["datetime"])
        except Exception:
            pass
        print(f"  [阶段1] 只碰主连 -> OK，推进 {n1} 拍")
        print(f"          主连最新K线时间 = {last_bar}")
        print(f"          主连 quote.last_price = {getattr(quote, 'last_price', None)}")
        print(f"          主连 underlying_symbol = {getattr(quote, 'underlying_symbol', None)}")

        # ---------- 阶段2：加订阅具体合约 ----------
        und = getattr(quote, "underlying_symbol", None)
        if not und:
            print("  [阶段2] 主连没有 underlying_symbol，跳过")
            return
        print(f"  [阶段2] 现在订阅具体合约 {und} ...")
        try:
            q2 = api.get_quote(und)
            api.wait_update()
            print(f"          OK | margin={getattr(q2,'margin',None)}"
                  f" | last_price={getattr(q2,'last_price',None)}")
        except Exception as e:
            print(f"          !! 抛 {type(e).__name__}: {e}")
            print("          => 结论：订阅具体合约会把老区间回测打断")

        try:
            b2 = str(kl.iloc[-1]["datetime"])
            print(f"          此时主连最新K线时间 = {b2}")
        except Exception:
            pass
        print(f"          quote.underlying_symbol 现在 = {getattr(quote,'underlying_symbol',None)}")
    finally:
        if api is not None:
            try:
                api.close()
            except Exception:
                pass


def main():
    print("=" * 88)
    print("  对照实验：老区间回测中断的根因定位")
    print("=" * 88)
    for label, s, e in CASES:
        run(label, s, e)


if __name__ == "__main__":
    main()
