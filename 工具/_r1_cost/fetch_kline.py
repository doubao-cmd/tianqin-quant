# -*- coding: utf-8 -*-
"""
R1 · K 线取数（按**实际交易合约**逐个拉，1min / 5min）
=========================================================
为什么按合约而不是按品种：台账期间跨了换月
（FU 10-19 交易 fu2611 @5792，10-21 之后 fu2701 @4810）。
拿单一合约的 K 线去算另一合约的「成交后 15min 漂移」，价格基准完全不同 ⇒ 结论必错。

★ 账号安全：只用【主账号 USER】(`config_account.check()`)，不用 LIVE_*
  —— 同一账号同时只能一处登录，用 LIVE 会踢掉线上模拟盘。
★ 时区：tqsdk K 线 datetime 为 UTC 纳秒时间戳，`time_to_datetime` 转北京时间。
  落盘前断言首根 bar 的时:分 ≥ 09:00（若把 UTC 当北京时间，会落在 01:00，断言会红）。

产出：out/kline/<CONTRACT>_<period>min.csv
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\cc\Desktop\天勤量化")

OUT = Path(__file__).parent / "out" / "kline"
CONTRACTS = [
    "DCE.m2701", "DCE.jm2701", "CZCE.SR701", "DCE.p2701", "CZCE.SA701",
    "SHFE.bu2611", "DCE.eg2611", "CZCE.MA701", "SHFE.fu2611", "SHFE.fu2701",
    "DCE.jd2611", "DCE.jd2612",
]
START = "2026-10-12 00:00:00"
END = "2026-10-23 00:00:00"


def main():
    import pandas as pd
    import config_account
    from tqsdk import TqApi, TqAuth
    from tqsdk.tafunc import time_to_datetime

    OUT.mkdir(parents=True, exist_ok=True)
    user, pwd = config_account.check()
    api = TqApi(auth=TqAuth(user, pwd))
    ok = 0
    try:
        for c in CONTRACTS:
            for period in (60, 300):
                kl = api.get_kline_serial(c, period, data_length=8000)
                api.wait_update()
                df = pd.DataFrame({
                    "dt": pd.to_datetime([time_to_datetime(x) for x in kl["datetime"]]),
                    "open": kl["open"], "high": kl["high"],
                    "low": kl["low"], "close": kl["close"], "volume": kl["volume"],
                })
                df = df[(df["dt"] >= START) & (df["dt"] < END)]
                if df.empty:
                    print(f"[空] {c} {period // 60}min")
                    continue
                assert df.iloc[0]["dt"].hour >= 9, \
                    f"{c} 首根 bar 落在 {df.iloc[0]['dt']}，疑似时区未转换"
                dst = OUT / f"{c}_{period // 60}min.csv"
                df.to_csv(dst, index=False, encoding="utf-8-sig", float_format="%.4f")
                print(f"[OK] {c} {period // 60}min n={len(df)} "
                      f"{df.iloc[0]['dt']} ~ {df.iloc[-1]['dt']}")
                ok += 1
    finally:
        api.close()
    print(f"\n共 {ok} 个文件 -> {OUT}")


if __name__ == "__main__":
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)
    main()
