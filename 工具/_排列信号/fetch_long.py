# -*- coding: utf-8 -*-
"""
排列信号 · 41 品种长周期日线（3000 根，约 2014 起）
======================================================
与 fetch_daily.py 同一口径，只是把 n_bars 拉到 3000 并覆盖同名文件。
输出 data/<KEY>_1440min.csv
"""
from __future__ import annotations
import os, sys, csv, time, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config_account as ca
import config_symbols as cs


def main(n_bars: int = 3000):
    from tqsdk import TqApi, TqAuth
    os.makedirs(DATA, exist_ok=True)
    today = dt.date.today()
    t0 = time.time()

    api = TqApi(auth=TqAuth(ca.USER, ca.PASSWORD))
    try:
        kl = {}
        for k, (cn, sym, mult, tick) in cs.SYMBOLS.items():
            try:
                kl[k] = api.get_kline_serial(sym, 86400, data_length=n_bars)
            except Exception as e:
                print(f"[skip] {k} {sym}: {e}", flush=True)
        print(f"已订阅 {len(kl)} 个，等数据… {time.time()-t0:.0f}s", flush=True)

        last_ok, stall = -1, 0
        while time.time() - t0 < 600:
            api.wait_update(deadline=time.time() + 3)
            ready = sum(1 for s in kl.values() if float(s["close"].iloc[-1]) > 0)
            filled = sum(1 for s in kl.values() if (s["open"] > 0).sum() > 100)
            if filled == ready:
                stall += 1
            else:
                stall = 0
            if ready > 0 and stall >= 2:
                break
            if ready != last_ok:
                print(f"  就绪 {ready}/{len(kl)} 有效根>100 的 {filled}  {time.time()-t0:.0f}s", flush=True)
                last_ok = ready
        print(f"数据就绪 {ready}/{len(kl)}  {time.time()-t0:.0f}s", flush=True)

        saved = 0
        for k, s in kl.items():
            rows = []
            for i in range(len(s)):
                ts = int(s["datetime"].iloc[i])
                if ts <= 0:
                    continue
                t = dt.datetime.fromtimestamp(ts / 1e9)
                close = float(s["close"].iloc[i])
                if close <= 0:
                    continue
                trade_date = t.date()
                if trade_date >= today:
                    continue
                rows.append((t.strftime("%Y-%m-%d %H:%M:%S"),
                             float(s["open"].iloc[i]), float(s["high"].iloc[i]),
                             float(s["low"].iloc[i]), close, float(s["volume"].iloc[i])))
            if len(rows) < 60:
                print(f"[skip] {k} 只有 {len(rows)} 根")
                continue
            p = os.path.join(DATA, f"{k}_1440min.csv")
            with open(p, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["datetime", "open", "high", "low", "close", "volume"])
                w.writerows(rows)
            saved += 1
            print(f"  {k:4s} {cs.SYMBOLS[k][0]:8s} {len(rows):5d} 根  {rows[0][0][:10]} ~ {rows[-1][0][:10]}", flush=True)
        print(f"\n完成 {saved}/{len(kl)} 个品种 → {DATA}")
    finally:
        api.close()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    main(n)
