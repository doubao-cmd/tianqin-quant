# -*- coding: utf-8 -*-
"""
排列信号 · 日线拉取（41 品种，主连）
==============================================================
一次连接批量拉，存 data/<KEY>_1440min.csv（列：datetime,open,high,low,close,volume）

★ 只用已收盘日线：实测 tqsdk 日线 datetime = 交易日当天 00:00，
  因此 trade_date = datetime.date()；只保留 trade_date < 今天 的记录。
"""
from __future__ import annotations
import os, sys, csv, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config_account as ca
import config_symbols as cs


def main(n_bars: int = 400):
    import time
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

        # 等到所有序列都有收盘价（最多 180 秒）
        while time.time() - t0 < 180:
            api.wait_update(deadline=time.time() + 3)
            ready = sum(1 for s in kl.values() if float(s["close"].iloc[-1]) > 0)
            if ready == len(kl):
                break
        print(f"数据就绪 {ready}/{len(kl)}  {time.time()-t0:.0f}s", flush=True)


        now = dt.datetime.now()
        saved = 0
        for k, s in kl.items():
            rows = []
            for i in range(len(s)):
                ts = int(s["datetime"].iloc[i])
                if ts <= 0:
                    continue
                t = dt.datetime.fromtimestamp(ts / 1e9)      # 本地时区（+8）
                close = float(s["close"].iloc[i])
                if close <= 0:
                    continue
                trade_date = t.date()
                if trade_date >= today:                        # 未收盘，剔除
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
            print(f"  {k:4s} {cs.SYMBOLS[k][0]:6s} {len(rows):4d} 根  末根 {rows[-1][0][:10]}  close={rows[-1][4]:.2f}")
        print(f"\n完成 {saved}/{len(kl)} 个品种 → {DATA}")
    finally:
        api.close()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    main(n)
