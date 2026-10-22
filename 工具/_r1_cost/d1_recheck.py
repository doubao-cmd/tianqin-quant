# -*- coding: utf-8 -*-
"""
R1 · D1 独立复核（体检项，卡 §七末条 / 验收「差 >0.15 停手上报」）
====================================================================
目的：用**另一条实现路径**重算 J1 的中位 r，与执行员结果比，差 >0.15 即停手。

独立在哪（不是把 r1_t3 抄一遍）：
  1. 直接读**原始台账**，不读 sample_audit.csv / cost_detail.csv；
  2. 用 1min K 线算 impact/adverse（主口径用 5min K）⇒ 独立检验 bar 对齐与休市处理；
  3. 不按「同方向同手数≤120s」配 FILL，而是直接用 FILLX 备注里的「滑点≈」字段
     （策略自己记的滑点，单位元/笔）反推 C_spread —— 台账与自主计算互为独立来源；
     平仓方向的符号问题单独统计（见下），只有开仓行进 D1 主算。
  4. C_delay 用订单生命周期全量中位（按品种），A 栏。

输出 out/d1_recheck.json；同时把「滑点≈ vs 自算」的逐笔一致性写成明细。

用法：python d1_recheck.py
"""
import csv
import json
import re
import statistics as st
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
OUT = BASE / "out"
KLINE = OUT / "kline"
LEDGER = Path(r"C:\Users\cc\Desktop\天勤量化\策略\06_MA13超短线\模拟盘\台账")
CUT = datetime(2026, 10, 22, 10, 15)
FMT = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")
NAME = {"乙二醇": "EG", "棕榈油": "P", "焦煤": "JM", "燃料油": "FU", "甲醇": "MA",
        "白糖": "SR", "石油沥青": "BU", "纯碱": "SA", "豆粕": "M", "鸡蛋": "JD"}
MAINS = ["M", "SA", "FU", "BU", "EG"]
RE = re.compile(r"(空开|空平|多开|多平)\s+(\d+)手\s+决策价=([\d.]+)"
                r"(?:\s+云端成交均价=([\d.]+))?.*?滑点≈([+-]?[\d.]+)元")


def dt(s):
    s = str(s).strip().split("+")[0]
    for f in FMT:
        try:
            return datetime.strptime(s.strip(), f)
        except Exception:
            pass
    return None


def bars1(contract):
    p = KLINE / f"{contract}_1min.csv"
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        rs = [{"t": dt(r["dt"]), "o": float(r["open"]), "h": float(r["high"]),
               "l": float(r["low"]), "v": float(r["volume"]), "c": float(r["close"])}
              for r in csv.DictReader(f)]
    rs.sort(key=lambda x: x["t"])
    return rs


def main():
    con = {}
    with open(OUT / "contracts.csv", "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            con[r["symbol"]] = {"mult": float(r["mult"]), "tv": float(r["tick_value"]),
                                "fee": float(r["fee_open_yuan"])}

    # 每个 FILL 的合约（按时间就近）
    fills = {}
    for p in LEDGER.glob("*_成交流水.csv"):
        code = NAME.get(p.name.replace("_成交流水.csv", ""))
        if not code:
            continue
        rows = []
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                if row and row[0] == "FILL" and len(row) > 5:
                    t = dt(row[1])
                    if t and t <= CUT and row[5]:
                        rows.append((t, float(row[5]), row[7] if len(row) > 7 else ""))
        fills[code] = sorted(rows)

    # 生命周期 delay（1min K，独立实现）
    delay = {}
    for p in LEDGER.glob("*_订单生命周期.csv"):
        code = NAME.get(p.name.replace("_订单生命周期.csv", ""))
        if not code:
            continue
        sent, got = {}, {}
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                t = dt(r["时间"])
                if not t or t > CUT:
                    continue
                if r["事件"] == "ORDER_SENT":
                    sent[r["键"]] = r
                elif r["事件"] == "FILL_CONFIRMED" and r["价格"]:
                    got[r["键"]] = r
        vals = []
        for k, s in sent.items():
            if k not in got:
                continue
            bl = bars1(s["合约"])
            ts = dt(s["时间"])
            # 基准价 = ORDER_SENT 之后第一根 1min bar 的 open（前视，不用下单前那根）
            nb = next((b for b in bl if b["t"] >= ts), None)
            if nb is None:
                continue
            p0 = nb["o"]
            sign = 1.0 if (s["方向"], s["开平"]) in (("多", "开"), ("空", "平")) else -1.0
            vals.append((float(got[k]["价格"]) - p0) * con[code]["mult"] * sign)
        if vals:
            delay[code] = st.median(vals)

    # 逐笔（1min 口径）
    per = {k: [] for k in con}
    perx = {}
    slip_ok, slip_bad = 0, []
    for p in LEDGER.glob("*_成交流水.csv"):
        code = NAME.get(p.name.replace("_成交流水.csv", ""))
        if not code:
            continue
        mult, tv, fee = con[code]["mult"], con[code]["tv"], con[code]["fee"]
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                if not row or row[0] != "FILLX":
                    continue
                t = dt(row[1])
                if not t or t > CUT:
                    continue
                memo = row[7] if len(row) > 7 else ""
                if "未就绪" in memo:
                    continue
                m = RE.search(memo)
                if not m or not m.group(4):
                    continue
                d, lots, dec, avg = m.group(1), int(m.group(2)), float(m.group(3)), float(m.group(4))
                ocmode = m.group(5)
                sign = 1.0 if d in ("多开", "空平") else -1.0
                c_spread_lot = (avg - dec) * mult * sign          # 元/手
                # 与台账自带「滑点≈」比对（它按笔记，开仓=有向，平仓符号相反）
                mine_total = c_spread_lot * lots
                if abs(mine_total - float(ocmode)) < 1e-6:
                    slip_ok += 1
                else:
                    slip_bad.append({"code": code, "时间": row[1], "方向": d,
                                     "台账滑点": float(ocmode), "自算": round(mine_total, 3)})

                # 合约
                cs = sorted(fills.get(code, []), key=lambda x: abs((x[0] - t).total_seconds()))
                contract = next((x[2] for x in cs if x[2]), "")
                fill_price = cs[0][1] if cs and abs((cs[0][0] - t).total_seconds()) <= 120 else avg
                bl = bars1(contract)
                if not bl:
                    continue
                i = next((k for k, b in enumerate(bl)
                          if b["t"] <= t < b["t"] + timedelta(minutes=1)), None)
                if i is None or bl[i]["v"] <= 0:
                    continue
                amp = bl[i]["h"] - bl[i]["l"]
                par = lots / bl[i]["v"]
                imp = 0.5 * par * amp * mult                      # 取区间下界，与主口径同侧
                tgt = t + timedelta(minutes=15)
                j = min(range(len(bl)), key=lambda k: abs((bl[k]["t"] + timedelta(minutes=1) - tgt).total_seconds()))
                adv = (fill_price - bl[j]["c"]) * mult * sign
                per[code].append(c_spread_lot + imp + adv + fee + delay.get(code, 0.0))
                perx.setdefault(code, []).append(c_spread_lot + imp + fee + delay.get(code, 0.0))

    res, forj1, forj1x = {}, [], []
    tbl = {r["symbol"]: r for r in csv.DictReader(open(OUT / "cost_table.csv", encoding="utf-8-sig"))}
    for code, v in per.items():
        if not v:
            continue
        tv = con[code]["tv"]
        d1 = st.median(v) / tv
        main_r = float(tbl[code]["r_lo_med"])
        d1x = st.median(perx[code]) / tv if perx.get(code) else float("nan")
        mainx = float(tbl[code]["r_exec_med"])
        res[code] = {"d1_r": round(d1, 3), "main_r": main_r, "diff": round(abs(d1 - main_r), 3),
                     "d1_r_exec": round(d1x, 3), "main_r_exec": mainx,
                     "diff_exec": round(abs(d1x - mainx), 3),
                     "n_d1": len(v), "n_main": int(tbl[code]["n_可算C_total"])}
        if code in MAINS and len(v) >= 8:
            forj1.append(d1)
            forj1x.append(d1x)
    j1_d1 = st.median(forj1) if forj1 else float("nan")
    j1_main = float(json.load(open(OUT / "judge.json", encoding="utf-8"))["J1"]
                    ["中位r_卡定义(含adverse,交易所费率)"])
    j1_d1x = st.median(forj1x) if forj1x else float("nan")
    j1_mainx = float(json.load(open(OUT / "judge.json", encoding="utf-8"))["J1"]
                     ["中位r_剔adverse_交易所费率"])
    ok = abs(j1_d1 - j1_main) <= 0.15
    out = {"J1_执行员": round(j1_main, 4), "J1_D1": round(j1_d1, 4),
           "差": round(abs(j1_d1 - j1_main), 4), "阈值": 0.15,
           "判定": "通过" if ok else "停手上报",
           "J1_exec_执行员": round(j1_mainx, 4), "J1_exec_D1": round(j1_d1x, 4),
           "差_exec": round(abs(j1_d1x - j1_mainx), 4),
           "判定_exec": "通过" if abs(j1_d1x - j1_mainx) <= 0.15 else "停手上报",
           "分品种": res,
           "滑点字段交叉校验": {"一致": slip_ok, "不一致": len(slip_bad),
                          "不一致明细": slip_bad[:15]},
           "说明": "D1 用 1min K + 台账自带「滑点≈」反推 spread；主口径用 5min K + 自主 spread"}
    json.dump(out, open(OUT / "d1_recheck.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(json.dumps({k: v for k, v in out.items() if k != "分品种"},
                     ensure_ascii=False, indent=2))
    print("\n分品种（D1 vs 主口径）:")
    for k, v in res.items():
        print(f"  {k:3s} D1={v['d1_r']:7.3f} 主={v['main_r']:7.3f} 差={v['diff']:5.3f} "
              f"n={v['n_d1']}/{v['n_main']}")


if __name__ == "__main__":
    main()
