# -*- coding: utf-8 -*-
"""
探测：天勤免费账号能否取外盘行情（不碰模拟盘，用主账号）
输出每个候选 symbol 的：能否订阅 / K线根数 / 时间范围 / 最新价
"""
import sys, os, traceback
sys.path.insert(0, r"C:/Users/cc/Desktop/天勤量化")
import config_account
from tqsdk import TqApi, TqAuth

USER, PWD = config_account.check()

CANDS = [
    # (标签, symbol)
    ("CBOT大豆",   "KQ.m@CBOT.ZS"),
    ("CBOT豆粕",   "KQ.m@CBOT.ZM"),
    ("CBOT豆油",   "KQ.m@CBOT.ZL"),
    ("CBOT玉米",   "KQ.m@CBOT.ZC"),
    ("NYMEX原油",  "KQ.m@NYMEX.CL"),
    ("COMEX铜",    "KQ.m@COMEX.HG"),
    ("COMEX金",    "KQ.m@COMEX.GC"),
    ("LME铜",      "KQ.m@LME.CU3"),
    ("ICE棉花",    "KQ.m@ICE.CT"),
    ("ICE原糖",    "KQ.m@ICE.SB"),
    ("BMD棕榈",    "KQ.m@BMD.FCPO"),
    ("SGX铁矿",    "KQ.m@SGX.FEF"),
    ("HKFE恒指",   "KQ.m@HKFE.HSI"),
    ("CME瘦肉猪",  "KQ.m@CME.LH"),
]

api = TqApi(auth=TqAuth(USER, PWD))

print("=" * 78)
print(f"{'标签':10s} {'symbol':18s} {'根数':>5s} {'起':>12s} {'止':>12s} {'最新':>10s}")
print("=" * 78)

ok, bad = [], []
for tag, sym in CANDS:
    try:
        kl = api.get_kline_serial(sym, duration_seconds=86400, data_length=200)
        api.wait_update()
        # 再等一次，确保填充
        for _ in range(3):
            api.wait_update()
        n = int(kl["close"].notna().sum())
        if n == 0:
            print(f"{tag:10s} {sym:18s} {'0':>5s}  {'-':>12s} {'-':>12s} {'-':>10s}   <空>")
            bad.append((tag, sym, "空数据"))
            continue
        import datetime as dt
        t0 = dt.datetime.fromtimestamp(kl["datetime"].iloc[0] / 1e9) + dt.timedelta(hours=8)
        t1 = dt.datetime.fromtimestamp(kl["datetime"].iloc[-1] / 1e9) + dt.timedelta(hours=8)
        last = float(kl["close"].iloc[-1])
        print(f"{tag:10s} {sym:18s} {n:>5d} {t0.strftime('%Y-%m-%d'):>12s} "
              f"{t1.strftime('%Y-%m-%d'):>12s} {last:>10.2f}")
        ok.append((tag, sym, n))
    except Exception as e:
        msg = str(e).split("\n")[0][:60]
        print(f"{tag:10s} {sym:18s} {'ERR':>5s}  {msg}")
        bad.append((tag, sym, msg))

print("=" * 78)
print(f"可用 {len(ok)} / {len(CANDS)}")
if ok:
    print("可用列表：", [x[1] for x in ok])

# 顺带探一下：能否列出外盘交易所的全部品种
for ex in ["CBOT", "NYMEX", "COMEX", "LME", "ICE", "BMD", "SGX", "HKFE", "CME"]:
    try:
        q = api.query_quotes(ins_class="FUTURE", exchange_id=ex)
        print(f"[query] {ex:6s} -> {len(q)} 个: {q[:8]}")
    except Exception as e:
        print(f"[query] {ex:6s} -> {str(e).splitlines()[0][:70]}")

api.close()
