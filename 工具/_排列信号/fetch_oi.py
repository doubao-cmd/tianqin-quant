# -*- coding: utf-8 -*-
"""
补「持仓量」信息源：41 品种日线 总持仓 / 成交量。
会员排名（top20_long）大商所拿不到，但总持仓变化（OI）全品种都有，可作「增仓/减仓」的代理。

输出 data_oi/<SYM>_oi.csv  列：dt,open_interest,volume
"""
import datetime as dt
import os
import sys
import time

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import config_account as ca  # noqa: E402
import config_symbols as cs  # noqa: E402

OUT = os.path.join(HERE, "data_oi")


def main(n_bars=3000):
    from tqsdk import TqApi, TqAuth

    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    api = TqApi(auth=TqAuth(ca.USER, ca.PASSWORD))
    try:
        kl = {}
        for k, (cn, sym, mult, tick) in cs.SYMBOLS.items():
            try:
                kl[k] = api.get_kline_serial(sym, 86400, data_length=n_bars)
            except Exception as e:
                print(f"[skip] {k}: {e}", flush=True)
        print(f"订阅 {len(kl)} 个，等待…", flush=True)
        last, stall = -1, 0
        while time.time() - t0 < 300:
            api.wait_update(deadline=time.time() + 3)
            ready = sum(1 for s in kl.values() if float(s["close"].iloc[-1]) > 0)
            filled = sum(1 for s in kl.values() if (s["open_oi"] > 0).sum() > 100)
            if filled == ready and ready:
                stall += 1
                if stall >= 3:
                    break
            else:
                stall = 0
            if ready != last:
                print(f"  {filled}/{ready}", flush=True)
                last = ready

        n_ok = 0
        for k, s in kl.items():
            d = pd.DataFrame({
                "dt": pd.to_datetime(s["datetime"], unit="ns") + pd.Timedelta(hours=8),
                "open_interest": s["close_oi"].to_numpy(),
                "volume": s["volume"].to_numpy(),
            })
            d["dt"] = d["dt"].dt.strftime("%Y-%m-%d")
            d = d[(d.open_interest > 0) & (d.dt <= dt.date.today().isoformat())]
            d = d.drop_duplicates("dt").sort_values("dt")
            if len(d) < 200:
                print(f"  [薄] {k} {len(d)}")
                continue
            d.to_csv(os.path.join(OUT, f"{k}_oi.csv"), index=False, encoding="utf-8")
            n_ok += 1
        print(f"完成 {n_ok}/{len(kl)}，{time.time()-t0:.0f}s")
    finally:
        api.close()


if __name__ == "__main__":
    main()
