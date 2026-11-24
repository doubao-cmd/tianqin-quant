# -*- coding: utf-8 -*-
"""
排列信号 · 日线拉取（当前主力具体合约）
==============================================================
★ 2026-11-24 口径变更：原来直拉主连 KQ.m@，MA40/MA60 在换月点被不同月份
  合约拼接污染。实测甲醇 11-23：主连 MA60=3270.30 vs MA701 自身 3087.98，
  差 182 点，导致同一品种两个口径给出相反结论（主连说空头成立，看盘说没有）。
  现在改为两步：
    1. 读主连 quote 的 underlying_symbol（当前主力具体合约，如 CZCE.MA701）
    2. 拉该具体合约的日线
  换月时 underlying_symbol 自动变，下一次拉取自动跟随。

每次同时写 data/_contracts.json：
  {品种: {contract, prev, roll_bar_date}}
  roll_bar_date = 换月后第一根日线的日期；scan_daily_push.py 用它判断
  「换月当天不出新信号」（新旧合约均线不可比，会出假信号）。

★ 只用已收盘日线：tqsdk 日线 datetime = 交易日当天 00:00。
  当天 bar 在日盘 15:00 收盘后就已完整（实测 11-24 15:14 拉到的 11-24 bar
  成交量 286 万，与前一交易日 273 万同量级，说明已收完）；21:00 起的夜盘
  属于下一交易日，不会再回填到当天 bar。
  因此：16:00 之后运行 = 可以取当天 bar；16:00 之前运行 = 只取到上一交易日。
"""
from __future__ import annotations
import os, sys, csv, json, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config_account as ca
from pool import POOL, EXCLUDED   # 看板专用品种池，不用全局 config_symbols


def load_prev_meta() -> dict:
    p = os.path.join(DATA, "_contracts.json")
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def main(n_bars: int = 400):
    import time
    from tqsdk import TqApi, TqAuth
    os.makedirs(DATA, exist_ok=True)
    today = dt.date.today()
    # 当天 bar 要等日盘收盘（15:00）才完整，留到 16:00 更稳
    allow_today = dt.datetime.now().hour >= 16
    t0 = time.time()
    prev_meta = load_prev_meta()

    api = TqApi(auth=TqAuth(ca.USER, ca.PASSWORD))
    try:
        # ---- ① 订阅主连 quote，拿当前主力具体合约 ----
        quotes = {}
        for k, (cn, sym, mult) in POOL.items():
            try:
                quotes[k] = api.get_quote(sym)
            except Exception as e:
                print(f"[skip] {k} {sym}: {e}", flush=True)

        while time.time() - t0 < 120:
            api.wait_update(deadline=time.time() + 3)
            if all(str(q.underlying_symbol or "") for q in quotes.values()):
                break

        contracts, kl = {}, {}
        for k, q in quotes.items():
            c = str(q.underlying_symbol or "")
            if not c:
                print(f"[skip] {k} 主力合约未就绪", flush=True)
                continue
            contracts[k] = c
            try:
                kl[k] = api.get_kline_serial(c, 86400, data_length=n_bars)
            except Exception as e:
                print(f"[skip] {k} {c}: {e}", flush=True)

        print(f"已订阅 {len(kl)} 个具体合约，等数据… {time.time()-t0:.0f}s", flush=True)

        # ---- ② 等 K 线就绪 ----
        t1 = time.time()
        ready = 0
        while time.time() - t1 < 180:
            api.wait_update(deadline=time.time() + 3)
            ready = sum(1 for s in kl.values() if len(s) and float(s["close"].iloc[-1]) > 0)
            if ready == len(kl):
                break
        print(f"数据就绪 {ready}/{len(kl)}  {time.time()-t0:.0f}s", flush=True)

        # ---- ③ 写 CSV + 合约元数据 ----
        meta, saved = {}, 0
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
                if t.date() > today:                           # 未来 bar，剔除
                    continue
                if t.date() == today and not allow_today:      # 当天未收盘，剔除
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

            c = contracts[k]
            old = (prev_meta.get(k) or {}).get("contract")
            rolled = bool(old and old != c)
            last_date = rows[-1][0][:10]
            roll_bar_date = last_date if rolled else (prev_meta.get(k, {}).get("roll_bar_date") or "")
            meta[k] = dict(contract=c, prev=old or c, roll_bar_date=roll_bar_date)
            flag = f"  [换月 {old} → {c}]" if rolled else ""
            print(f"  {k:4s} {POOL[k][0]:6s} {c:14s} {len(rows):4d} 根  "
                  f"末根 {last_date}  close={rows[-1][4]:.2f}{flag}")

        with open(os.path.join(DATA, "_contracts.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        print(f"\n完成 {saved}/{len(kl)} 个品种 → {DATA}")
    finally:
        api.close()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    main(n)
