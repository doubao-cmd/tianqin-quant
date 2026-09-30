# -*- coding: utf-8 -*-
"""
路线二(carry) · 跨期结构 carry 实时模拟盘核心（12 品种共用）
==========================================================
逻辑与回测滚动版逐条对齐（策略/路线二_carry/回测/<品种>/跨期结构carry_回测.py）：
  - 近月 = 主连 quote.underlying_symbol（实时主力）；远月 = 活跃月序列下一位
  - spread = 近月收 − 远月收；backwardation 多价差 / contango 空价差；翻转才换向
  - 主力换月 = 换月事件（观察模式只记录；真下单 = 平旧开新各 2 腿）
默认 ALLOW_TRADE=False 观察模式：每日收盘打印结构方向 + 心跳落盘，不报单。
记录：策略视角 CSV（成交流水 + 运行心跳），复用 lib.engine._LiveRecorder。
"""
import sys, os
import pandas as pd

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tqsdk import TqApi, TqSim, TqAuth
from lib.engine import _LiveRecorder
import config_account
import config_symbols

INIT = 200000.0


def next_active(sym_near, act_months):
    """由近月合约推同品种下一个活跃月合约（含年份进位）。CZCE 3位码禁用（铁律）。"""
    exch, code = sym_near.split(".")[0], sym_near.split(".")[1]
    prod, yy, mm = code[:-4], int(code[-4:-2]), int(code[-2:])
    cand = [(m, yy + (1 if m <= mm else 0)) for m in act_months if m > mm]
    if not cand:
        cand = [(act_months[0], yy + 1)]
    m2, y2 = min(cand, key=lambda t: (t[1], t[0]))
    if exch == "CZCE":
        raise SystemExit("CZCE 3位码 10年一循环，禁构造老代码（项目铁律）")
    return f"{exch}.{prod}{y2 % 100:02d}{m2:02d}"


def run_live_carry(symbol_key, act_months, allow_trade=False, web_gui=True,
                   record_dir=None, heartbeat_sec=300, lots=1):
    name_cn, cont, mult, tick = config_symbols.get(symbol_key)
    user, pwd = config_account.check()
    api = TqApi(TqSim(INIT), auth=TqAuth(user, pwd), web_gui=web_gui)
    q_cont = api.get_quote(cont)
    km = api.get_kline_serial(cont, 86400, data_length=100)   # 主连日线 = 时钟
    rec = _LiveRecorder(record_dir, name_cn, heartbeat_sec=heartbeat_sec)

    near = q_cont.underlying_symbol
    far = next_active(near, act_months)
    kn = api.get_kline_serial(near, 86400, data_length=100)
    kf = api.get_kline_serial(far, 86400, data_length=100)

    pos = 0; prev_spread = None; last_day = None
    rec.start(f"carry 实时盘启动 near={near} far={far} allow_trade={allow_trade}")
    print(f"[carry] {name_cn} 近月={near} 远月={far}", flush=True)

    try:
        while True:
            api.wait_update()
            if len(km) == 0:
                continue
            kmb = km.iloc[-1]
            if kmb["id"] < 0 or not kmb["datetime"]:
                continue
            day = pd.Timestamp(kmb["datetime"]).date()
            if day == last_day:
                continue
            last_day = day

            # ---- 换月检测 ----
            u = q_cont.underlying_symbol
            if u != near:
                old_spread_last = prev_spread
                near, far = u, next_active(u, act_months)
                kn = api.get_kline_serial(near, 86400, data_length=100)
                kf = api.get_kline_serial(far, 86400, data_length=100)
                for _ in range(50):
                    api.wait_update()
                    if len(kn) > 0 and len(kf) > 0:
                        break
                prev_spread = None
                rec.event("ROLL", f"换月 -> {near}/{far}", "", "")
                print(f"[ROLL] 换月 -> {near}/{far}", flush=True)
                continue

            if len(kn) == 0 or len(kf) == 0:
                continue
            nc, fc = kn.iloc[-1]["close"], kf.iloc[-1]["close"]
            if not nc or not fc:
                continue
            spread = nc - fc
            new_pos = 1 if spread > 0 else -1
            dir_cn = "多价差" if new_pos > 0 else "空价差"

            if prev_spread is None:
                pos = new_pos
                rec.signal(dir_cn, lots, spread, q_cont.datetime, spread, 0,
                           f"结构初始化 {near}-{far} spread={spread:.1f}")
                prev_spread = spread
            else:
                if new_pos != pos:
                    rec.event("FLIP", f"{dir_cn} 结构翻转 {prev_spread:.1f}->{spread:.1f}", dir_cn, spread)
                    print(f"[FLIP] 结构翻转 -> {dir_cn} (spread={spread:.1f})", flush=True)
                    pos = new_pos
                rec.signal(dir_cn, lots, spread, q_cont.datetime, spread, 0,
                           f"每日收盘 {near}-{far} spread={spread:.1f} (前值{prev_spread:.1f})")
                prev_spread = spread
            print(f"[DAY {day}] spread={spread:.1f} {dir_cn} (back天数见台账)", flush=True)
            rec.heartbeat(q_cont.datetime, spread, pos, pos, force=True)
    finally:
        rec.stop()
        rec.close()
        try:
            api.close()
        except Exception:
            pass


if __name__ == "__main__":
    pass
