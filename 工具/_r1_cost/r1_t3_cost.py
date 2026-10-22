# -*- coding: utf-8 -*-
"""
R1 · T3 成本分项 + 成本表（out/cost_detail.csv / out/cost_table.csv / out/judge.json）
=====================================================================================
口径冻结自 讨论/预注册_R1成本信息半径_20261022.md §三（v1.0，不得事后改）：

    C_total = C_spread + C_delay + C_impact + C_adverse + C_fee      （元/手，**单边**）
    成本为正、有利为负，任何分项出现负号**不取绝对值**。

单边口径理由：项目回测 lib/engine.py 的 cost_per_hand 就是「元/手/单边」；
J1 的「1 跳假设」也是单边 1 跳。往返成本 = 2 × C_total（h* 处再乘 2）。

各分项实现口径（比上一版更严，逐条可复算）：
  C_spread  = 决策价 vs 云端成交均价。买：(avg−dec)×mult；卖：(dec−avg)×mult
  C_delay   = ORDER_SENT→FILL_CONFIRMED 的价格漂移。基准 = ORDER_SENT 之后第一根
              1min bar 的 open（不用「分钟收盘」，否则把该分钟内行情算成延迟、与 spread 重复）。
              **只在真配对到同一订单时计入**；品种层面用实测中位数（要求 n≥3）补偿，
              不把单点中位数复制成整品种（JM/JD 仅 1 对 ⇒ 不补，记「不可测」）。
  C_impact  = 参与率 × 当根 5min 振幅，**只给区间**：
              下界 0.5×(lots/bar_volume)×(high−low)×mult，上界 1.0×同式
  C_adverse = 成交后 15min 的水平位移，按成交方向取符号（正=不利=逆向选择）
  C_fee     = 交易所标准单边手续费（contracts.csv；比例品种按当笔成交价×mult×费率）
              另给 ×5 零售档（申万公示）与 ≈×10 自助开户档（建信公示）敏感性。

数据缺口（**显式打标，不静默填 0**）：
  - 1min/5min K 线可得区间自 2026-10-12 09:00 起（tqsdk 返回上限 2845 根），
    更早的成交算不出 impact/adverse ⇒ 该笔 C_total 记空、列 `缺项`，不进 r 分布。
  - FILLX 与 FILL 匹配要求「同方向 + 同手数 + ≤120s」，否则记 no_fill。

用法：python r1_t3_cost.py
"""
import csv
import json
import statistics as st
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
OUT = BASE / "out"
KLINE = OUT / "kline"
LEDGER = Path(r"C:\Users\cc\Desktop\天勤量化\策略\06_MA13超短线\模拟盘\台账")

DIR_SIDE = {"多开": "buy", "空平": "buy", "空开": "sell", "多平": "sell"}
FMT = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")


def pdt(s):
    s = str(s).strip().split("+")[0]
    for f in FMT:
        try:
            return datetime.strptime(s, f)
        except Exception:
            pass
    raise ValueError(f"时间解析失败: {s!r}")


def load_kline(contract, period):
    p = KLINE / f"{contract}_{period}min.csv"
    if not p.exists():
        return []
    rows = []
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append({"dt": pdt(r["dt"]), "open": float(r["open"]),
                         "high": float(r["high"]), "low": float(r["low"]),
                         "close": float(r["close"]), "volume": float(r["volume"])})
    rows.sort(key=lambda x: x["dt"])
    return rows


def bar_containing(bars, ts, minutes):
    """ts 所在 bar（dt 为 bar 起始时间）；不在任何 bar 内返回 None。"""
    for i, b in enumerate(bars):
        if b["dt"] <= ts < b["dt"] + timedelta(minutes=minutes):
            return i
    return None


def first_bar_ge(bars, ts):
    """第一根 dt >= ts 的 bar（严格前视：不含下单前那根 bar 已走过的行情）。"""
    for i, b in enumerate(bars):
        if b["dt"] >= ts:
            return i
    return None


def bar_at_or_after(bars, ts, minutes):
    for i, b in enumerate(bars):
        if b["dt"] <= ts < b["dt"] + timedelta(minutes=minutes):
            return i
        if b["dt"] > ts:
            return i
    return None


def pct(vals, q):
    if not vals:
        return float("nan")
    v = sorted(vals)
    if len(v) == 1:
        return v[0]
    k = (len(v) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def main():
    # ---------- 合约参数 ----------
    cparams = {}
    with open(OUT / "contracts.csv", "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            cparams[r["symbol"]] = {
                "mult": float(r["mult"]), "tick": float(r["tick"]),
                "tick_value": float(r["tick_value"]),
                "mode": r["fee_mode"],
                "rate": float(r["fee_open_rate"]),
                "yuan": float(r["fee_open_yuan"]),
                "fee_t2": float(r["fee_t2_open_yuan"]),
                "fee_t3": float(r["fee_t3_open_yuan"]),
                "tier": r["tier"],
            }

    # ---------- 样本 ----------
    fill_rows, main_rows = [], []
    with open(OUT / "sample_audit.csv", "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["来源"] == "FILL":
                if r["成交价"]:
                    fill_rows.append(r)
            elif r["可用标记"] == "yes":       # R-2 单列的行不进主表
                main_rows.append(r)

    fills_by_code = defaultdict(list)
    for r in fill_rows:
        fills_by_code[r["品种"]].append(r)

    # ---------- 订单生命周期配对 ----------
    NAME2CODE = {"乙二醇": "EG", "棕榈油": "P", "焦煤": "JM", "燃料油": "FU",
                 "甲醇": "MA", "白糖": "SR", "石油沥青": "BU", "纯碱": "SA",
                 "豆粕": "M", "鸡蛋": "JD"}
    sent_all, filled_all = [], []
    for p in LEDGER.glob("*_订单生命周期.csv"):
        code = NAME2CODE.get(p.name.replace("_订单生命周期.csv", ""))
        if not code:
            continue
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if not r.get("时间") or pdt(r["时间"]) > datetime(2026, 10, 22, 10, 15):
                    continue
                if r["事件"] == "ORDER_SENT":
                    sent_all.append({"code": code, "ts": pdt(r["时间"]), "key": r["键"],
                                     "contract": r["合约"], "dir": r["方向"],
                                     "oc": r["开平"], "lots": int(r["手数"]),
                                     "price": r["价格"]})
                elif r["事件"] == "FILL_CONFIRMED":
                    filled_all.append({"code": code, "ts": pdt(r["时间"]), "key": r["键"],
                                       "price": r["价格"]})

    filled_by_key = {f["key"]: f for f in filled_all if f["key"]}
    delay_pairs = []
    for s in sent_all:
        f = filled_by_key.get(s["key"])
        if not f or not f["price"]:
            continue
        delay_pairs.append({**s, "ts_fill": f["ts"], "price_fill": float(f["price"]),
                            "lat_sec": (f["ts"] - s["ts"]).total_seconds()})
    print(f"订单生命周期：SENT={len(sent_all)} FILL_CONFIRMED={len(filled_all)} "
          f"配对={len(delay_pairs)}")

    kl1 = {}
    for dp in delay_pairs:
        c = dp["contract"]
        if c not in kl1:
            kl1[c] = load_kline(c, 1)
        bars = kl1[c]
        # 基准价 = **ORDER_SENT 之后（含同刻）第一根 1min bar 的 open**。
        # 不用「包含 ts 的那根 bar 的 open」——那会把下单前最多 59 秒的行情算进延迟。
        i = first_bar_ge(bars, dp["ts"]) if bars else None
        if i is None:
            dp["delay"] = None
            continue
        p0 = bars[i]["open"]
        side = "buy" if dp["dir"] == "多" else "sell"
        # 开/平都按「相对持仓的成交方向」：开多/平空=买，开空/平多=卖
        if (dp["dir"], dp["oc"]) == ("多", "开") or (dp["dir"], dp["oc"]) == ("空", "平"):
            side = "buy"
        else:
            side = "sell"
        sign = 1.0 if side == "buy" else -1.0
        mult = cparams[dp["code"]]["mult"]
        dp["delay"] = (dp["price_fill"] - p0) * mult * sign
        dp["p0"] = p0
        dp["side"] = side

    delay_by_code = defaultdict(list)
    for dp in delay_pairs:
        if dp["delay"] is not None and dp["delay"] == dp["delay"]:
            delay_by_code[dp["code"]].append(dp["delay"])
    delay_med = {k: st.median(v) for k, v in delay_by_code.items() if len(v) >= 3}

    # ---------- 主表逐笔 ----------
    detail = []
    for r in main_rows:
        code = r["品种"]
        prm = cparams[code]
        mult, tv = prm["mult"], prm["tick_value"]
        ts = pdt(r["时间"])
        dec, avg = float(r["决策价"]), float(r["云端均价"])
        side = DIR_SIDE.get(r["方向"], "")
        sign = 1.0 if side == "buy" else -1.0
        lots = int(float(r["手数"]))

        # 同方向同手数、≤120s 最近的 FILL
        cand = [x for x in fills_by_code[code]
                if x["方向"] == r["方向"] and abs((pdt(x["时间"]) - ts).total_seconds()) <= 120]
        if not cand:
            cand = [x for x in fills_by_code[code]
                    if abs((pdt(x["时间"]) - ts).total_seconds()) <= 180]
        fr = min(cand, key=lambda x: abs((pdt(x["时间"]) - ts).total_seconds())) if cand else None
        if fr:
            contract = fr["contract"]
            fill_price = float(fr["成交价"])
            fill_ts = pdt(fr["时间"])
            match = "fill_同向同手数" if fr["方向"] == r["方向"] else "fill_仅时间近"
        else:
            contract, fill_price, fill_ts, match = r["contract"], avg, ts, "no_fill(用云端均价)"

        c_spread = (avg - dec) * mult * sign

        # fee（单边，交易所标准）
        c_fee = prm["yuan"] if prm["mode"] == "fixed" else prm["rate"] * fill_price * mult
        c_fee_t2 = prm["fee_t2"]
        c_fee_t3 = prm["fee_t3"]

        bars5 = load_kline(contract, 5) if contract else []
        c_impact_lo = c_impact_hi = c_adverse = None
        adv_strict = None
        i_bar = bar_containing(bars5, fill_ts, 5) if bars5 else None
        if i_bar is not None and bars5[i_bar]["volume"] > 0:
            b = bars5[i_bar]
            par = lots / b["volume"]
            amp = b["high"] - b["low"]
            c_impact_lo = 0.5 * par * amp * mult
            c_impact_hi = 1.0 * par * amp * mult
        # 成交后 15min：取「结束时刻最接近 fill_ts+15min」的那根 5min bar 的收盘。
        # 盘中休市（豆粕 10:15-10:30 等）会让 t15 落进空档，此时选最近的一根并记下实际跨度。
        adv_min = None
        if bars5:
            target = fill_ts + timedelta(minutes=15)
            j, best = None, None
            for k, b in enumerate(bars5):
                if k == i_bar:
                    continue
                gap = abs(((b["dt"] + timedelta(minutes=5)) - target).total_seconds())
                if best is None or gap < best:
                    j, best = k, gap
            if j is not None:
                c15 = bars5[j]["close"]
                c_adverse = (fill_price - c15) * mult * sign
                adv_min = round(((bars5[j]["dt"] + timedelta(minutes=5)) - fill_ts).total_seconds() / 60, 1)
            if i_bar is not None:
                seg = bars5[i_bar + 1: i_bar + 4]
                if seg:
                    if side == "buy":
                        adv_strict = (fill_price - min(b["low"] for b in seg)) * mult
                    else:
                        adv_strict = (max(b["high"] for b in seg) - fill_price) * mult

        # delay：优先真配对（同合约+同方向+同开平+同手数，≤300s）
        c_delay, delay_src = None, "无配对"
        for dp in delay_pairs:
            if (dp["code"] == code and dp["contract"] == contract
                    and dp["lots"] == lots
                    and abs((dp["ts_fill"] - fill_ts).total_seconds()) <= 300):
                if dp.get("delay") is not None:
                    c_delay, delay_src = dp["delay"], "measured"
                    break
        if c_delay is None and code in delay_med:
            c_delay = delay_med[code]
            delay_src = f"imputed(品种中位,n={len(delay_by_code[code])})"
        if c_delay is None:
            delay_src = "不可测(实测对<3,记0)"

        miss = []
        if i_bar is None:
            miss.append("无5minK")
        if c_impact_lo is None:
            miss.append("impact缺")
        if c_adverse is None:
            miss.append("adverse缺")
        if c_delay is None:
            miss.append("delay不可测")
            c_delay_val = 0.0
        else:
            c_delay_val = c_delay

        if c_impact_lo is None or c_adverse is None:
            c_total_lo = c_total_hi = None
        else:
            c_total_lo = c_spread + c_delay_val + c_impact_lo + c_adverse + c_fee
            c_total_hi = c_spread + c_delay_val + c_impact_hi + c_adverse + c_fee
        # 手续费敏感性（同笔、同分项，只换 fee 档）
        if c_total_lo is None:
            c_t2_lo = c_t3_lo = None
        else:
            c_t2_lo = c_total_lo - c_fee + c_fee_t2
            c_t3_lo = c_total_lo - c_fee + c_fee_t3
        # 纯执行成本（剔除 adverse）：adverse 含行情方向成分，卡要求 J4 诚实标注
        c_exec = (c_spread + c_delay_val + (c_impact_lo + c_impact_hi) / 2 + c_fee
                  if c_impact_lo is not None else None)

        detail.append({
            "code": code, "tier": prm["tier"], "contract": contract, "时间": r["时间"],
            "方向": r["方向"], "side": side, "lots": lots,
            "决策价": dec, "云端均价": avg, "成交价": fill_price, "match": match,
            "mult": mult, "tick_value": tv,
            "C_spread": round(c_spread, 4),
            "C_delay": None if c_delay is None else round(c_delay, 4),
            "C_delay_src": delay_src,
            "C_impact_lo": None if c_impact_lo is None else round(c_impact_lo, 4),
            "C_impact_hi": None if c_impact_hi is None else round(c_impact_hi, 4),
            "C_adverse": None if c_adverse is None else round(c_adverse, 4),
            "C_adverse_strict": None if adv_strict is None else round(adv_strict, 4),
            "adv_span_min": adv_min,
            "C_fee": round(c_fee, 4), "C_fee_t2": round(c_fee_t2, 4),
            "C_fee_t3": round(c_fee_t3, 4),
            "C_total_lo": None if c_total_lo is None else round(c_total_lo, 4),
            "C_total_hi": None if c_total_hi is None else round(c_total_hi, 4),
            "C_total_t2_lo": None if c_t2_lo is None else round(c_t2_lo, 4),
            "C_total_t3_lo": None if c_t3_lo is None else round(c_t3_lo, 4),
            "C_exec": None if c_exec is None else round(c_exec, 4),
            "r_lo": None if c_total_lo is None else round(c_total_lo / tv, 4),
            "r_hi": None if c_total_hi is None else round(c_total_hi / tv, 4),
            "r_t2": None if c_t2_lo is None else round(c_t2_lo / tv, 4),
            "r_t3": None if c_t3_lo is None else round(c_t3_lo / tv, 4),
            "缺项": "；".join(miss),
            "memo": r["剔除原因"],
        })

    def dump(path, rows):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    dump(OUT / "cost_detail.csv", detail)

    # ---------- 残差验收（逐笔，用落盘后已四舍五入的值复算）----------
    resid_max = defaultdict(float)
    for d in detail:
        if d["C_total_lo"] is None:
            continue
        parts = [d["C_spread"], d["C_delay"] or 0.0, d["C_impact_lo"], d["C_adverse"], d["C_fee"]]
        s = sum(parts)
        resid_max[d["code"]] = max(resid_max[d["code"]],
                                   abs(d["C_total_lo"] - s) / abs(d["C_total_lo"]) if d["C_total_lo"] else 0.0)

    # ---------- 分品种汇总 ----------
    def nums(sub, k):
        return [d[k] for d in sub if d[k] is not None]

    table = []
    for code in sorted({d["code"] for d in detail},
                       key=lambda c: (cparams[c]["tier"], c)):
        sub = [d for d in detail if d["code"] == code]
        tv = cparams[code]["tick_value"]
        tl = nums(sub, "C_total_lo")
        th = nums(sub, "C_total_hi")
        r_lo = [x / tv for x in tl]
        sp = nums(sub, "C_spread")
        dl = nums(sub, "C_delay")
        ad = nums(sub, "C_adverse")
        im = nums(sub, "C_impact_lo")
        ex = nums(sub, "C_exec")
        t2 = nums(sub, "C_total_t2_lo")
        t3 = nums(sub, "C_total_t3_lo")
        ex_t2 = [x - cparams[code]["yuan"] + cparams[code]["fee_t2"] for x in ex]
        ex_t3 = [x - cparams[code]["yuan"] + cparams[code]["fee_t3"] for x in ex]
        # R-4：异常值保留，另报 p99 与 winsorized（在 p01/p99 截尾后取中位）
        r_sorted = sorted(r_lo)
        if r_sorted:
            lo_c, hi_c = pct(r_sorted, .01), pct(r_sorted, .99)
            r_wins = [min(max(x, lo_c), hi_c) for x in r_sorted]
        else:
            lo_c = hi_c = float("nan")
            r_wins = []
        table.append({
            "symbol": code, "tier": cparams[code]["tier"],
            "n_主表": len(sub), "n_可算C_total": len(tl),
            "n_spread": len(sp), "n_delay_measured": sum(1 for d in sub if d["C_delay_src"] == "measured"),
            "n_adverse": len(ad),
            "tick_value": tv,
            "C_spread_med": round(st.median(sp), 3) if sp else "",
            "C_spread_p10": round(pct(sp, .10), 3) if sp else "",
            "C_spread_p90": round(pct(sp, .90), 3) if sp else "",
            "C_delay_med": (round(st.median(dl), 3) if len(dl) >= 3
                            else (f"{round(dl[0],3)}(n=1单点)" if dl else "")),
            "C_impact_lo_med": round(st.median(im), 4) if im else "",
            "C_impact_hi_med": round(st.median(nums(sub, "C_impact_hi")), 4) if im else "",
            "C_adverse_med": round(st.median(ad), 3) if ad else "",
            "C_adverse_p10": round(pct(ad, .10), 3) if ad else "",
            "C_adverse_p90": round(pct(ad, .90), 3) if ad else "",
            "C_fee": round(cparams[code]["yuan"], 3),
            "C_total_lo_med": round(st.median(tl), 3) if tl else "",
            "C_total_lo_p10": round(pct(tl, .10), 3) if tl else "",
            "C_total_lo_p90": round(pct(tl, .90), 3) if tl else "",
            "C_total_lo_p99": round(pct(tl, .99), 3) if tl else "",
            "C_total_hi_med": round(st.median(th), 3) if th else "",
            "r_lo_med": round(st.median(r_lo), 3) if r_lo else "",
            "r_p10": round(pct(r_lo, .10), 3) if r_lo else "",
            "r_p90": round(pct(r_lo, .90), 3) if r_lo else "",
            "r_hi_med": round(st.median([x / tv for x in th]), 3) if th else "",
            "r_p99": round(hi_c, 3) if r_sorted else "",
            "r_winsor_med": round(st.median(r_wins), 3) if r_wins else "",
            "C_exec_med": round(st.median(ex), 3) if ex else "",
            "r_exec_med": round(st.median([x / tv for x in ex]), 3) if ex else "",
            "r_exec_t2_med": round(st.median([x / tv for x in ex_t2]), 3) if ex_t2 else "",
            "r_exec_t3_med": round(st.median([x / tv for x in ex_t3]), 3) if ex_t3 else "",
            "C_total_t2_med": round(st.median(t2), 3) if t2 else "",
            "r_t2_med": round(st.median([x / tv for x in t2]), 3) if t2 else "",
            "r_t3_med": round(st.median([x / tv for x in t3]), 3) if t3 else "",
            "spread_zero_pct": round(100 * sum(1 for x in sp if abs(x) < 1e-9) / len(sp), 1) if sp else "",
            "resid_pct": round(resid_max.get(code, 0.0) * 100, 4),
        })

    dump(OUT / "cost_table.csv", table)

    # ---------- J1~J4 ----------
    mains = ["M", "SA", "FU", "BU", "EG"]          # 卡 §四 J1 点名的 5 个主品种
    apool = ["FU", "BU", "MA", "M", "SA", "EG"]
    row = {t["symbol"]: t for t in table}
    j1_items = [row[c] for c in mains if c in row and row[c]["n_可算C_total"] >= 8]

    def med_of(items, key):
        v = [t[key] for t in items if t[key] != ""]
        return st.median(v) if v else float("nan")

    j1 = {
        "定义": "5 主品种(M/SA/FU/BU/EG) 各自中位 r，再对 5 个中位取中位（卡 §四 J1）",
        "各品种": {t["symbol"]: {"r_lo_med": t["r_lo_med"], "n": t["n_可算C_total"]} for t in j1_items},
        "中位r_卡定义(含adverse,交易所费率)": round(med_of(j1_items, "r_lo_med"), 4),
        "中位r_剔adverse_交易所费率": round(med_of(j1_items, "r_exec_med"), 4),
        "中位r_含adverse_×5档": round(med_of(j1_items, "r_t2_med"), 4),
        "中位r_含adverse_零售档": round(med_of(j1_items, "r_t3_med"), 4),
        "中位r_剔adverse_×5档": round(med_of(j1_items, "r_exec_t2_med"), 4),
        "中位r_剔adverse_零售档": round(med_of(j1_items, "r_exec_t3_med"), 4),
        "阈值": 1.3,
    }
    j1["判定_卡定义"] = "1跳成立" if j1["中位r_卡定义(含adverse,交易所费率)"] < 1.3 else "触发B档重算"
    j1["判定_剔adverse_交易所费率"] = "1跳成立" if j1["中位r_剔adverse_交易所费率"] < 1.3 else "触发B档重算"
    j1["判定_零售档"] = "1跳成立" if j1["中位r_含adverse_零售档"] < 1.3 else "触发B档重算"
    j1["判定_剔adverse_零售档"] = "1跳成立" if j1["中位r_剔adverse_零售档"] < 1.3 else "触发B档重算"

    # J2：品种间离散度。r 有符号（有利成交为负），离散度用 |r| 度量；
    #     这只是度量离散程度，不改任何分项符号与主表数值。
    pool_r = [x / cparams[c]["tick_value"] for c in apool
              for x in nums([d for d in detail if d["code"] == c], "C_total_lo")]
    abs_r = [abs(x) for x in pool_r]
    sym_med_abs = {c: abs(row[c]["r_lo_med"]) for c in apool if row[c]["r_lo_med"] != ""}
    j2_ratio = pct(abs_r, .90) / pct(abs_r, .10) if abs_r and pct(abs_r, .10) else float("nan")
    j2 = {"度量": "A 栏合并逐笔 r 的 |r| 分位",
          "p10_abs": round(pct(abs_r, .10), 4), "p90_abs": round(pct(abs_r, .90), 4),
          "p90/p10": round(j2_ratio, 3) if j2_ratio == j2_ratio else None,
          "各品种中位|r|": {k: round(v, 3) for k, v in sym_med_abs.items()},
          "最大/最小(品种中位)": (round(max(sym_med_abs.values()) / min(sym_med_abs.values()), 2)
                                if sym_med_abs and min(sym_med_abs.values()) > 0 else None)}
    j2["判定"] = "禁统一成本(须分品种)" if (j2_ratio == j2_ratio and j2_ratio > 3) else "可用统一成本"

    # J3：delay 占比。只用「真配对到 delay 且 C_total>0」的笔算比值（分母<=0 时比值无意义），
    #     品种内样本 >=3 才报中位，A 栏再取中位。
    share_by_code = {}
    for c in apool:
        v = [d["C_delay"] / d["C_total_lo"] for d in detail
             if d["code"] == c and d["C_delay"] is not None and d["C_total_lo"] not in (None, 0)
             and d["C_total_lo"] > 0 and d["C_delay_src"] == "measured"]
        share_by_code[c] = (round(st.median(v), 4), len(v)) if len(v) >= 3 else ("", len(v))
    a_share = [v for v, n in share_by_code.values() if v != ""]
    j3 = {"各品种delay占比(中位,n)": {k: {"ratio": v, "n": n} for k, (v, n) in share_by_code.items()},
          "A栏中位占比": round(st.median(a_share), 4) if a_share else None,
          "n_measured_total": sum(row[c]["n_delay_measured"] for c in apool)}
    j3["判定"] = ("记不可测" if a_share and st.median(a_share) < 0.05 else "需计入")

    # J4：impact 区间（只给区间）
    j4 = {"impact区间_元每手_中位": {c: [row[c]["C_impact_lo_med"], row[c]["C_impact_hi_med"]]
                                  for c in row},
          "说明": "无 L2 数据 ⇒ 只能代理：参与率×当根振幅，线性/平方根模型差别用 0.5~1.0 覆盖；"
                  "不给点估计"}

    judge = {
        "J1": j1, "J2": j2, "J3": j3, "J4": j4,
        "样本": {"主表n": len(detail),
                 "可算C_total": sum(1 for d in detail if d["C_total_lo"] is not None),
                 "无5minK被排除": sum(1 for d in detail if "无5minK" in d["缺项"]),
                 "delay不可测": sum(1 for d in detail if "delay不可测" in d["缺项"])},
    }
    with open(OUT / "judge.json", "w", encoding="utf-8") as f:
        json.dump(judge, f, ensure_ascii=False, indent=2)

    # ---------- 打印 ----------
    print("\n=== C_delay（真配对，1min K）===")
    for k in sorted(delay_by_code):
        v = delay_by_code[k]
        print(f"  {k:3s} n={len(v):2d} 中位={st.median(v):+8.2f} 元/手")
    print("\n=== 分品种（元/手，单边；正=成本）===")
    print(f"{'品种':4s}{'栏':2s}{'n':>4s}{'spread':>8s}{'delay':>8s}{'impact':>15s}"
          f"{'adverse':>9s}{'fee':>7s}{'total':>9s}{'r':>7s}{'r×5':>7s}{'r零售':>8s}")
    for t in table:
        dmed = t["C_delay_med"]
        dstr = f"{dmed:8.2f}" if isinstance(dmed, (int, float)) else f"{'—':>8s}"
        print(f"{t['symbol']:4s}{t['tier']:2s}{t['n_可算C_total']:4d}{t['C_spread_med']:8.2f}"
              f"{dstr}"
              f"{str(t['C_impact_lo_med'])+'~'+str(t['C_impact_hi_med']):>15s}"
              f"{(t['C_adverse_med'] if t['C_adverse_med'] != '' else 0):9.2f}"
              f"{t['C_fee']:7.2f}{t['C_total_lo_med']:9.2f}{t['r_lo_med']:7.2f}"
              f"{t['r_t2_med']:7.2f}{t['r_t3_med']:8.2f}")
    print("\n[J1]", json.dumps(judge["J1"], ensure_ascii=False))
    print("[J2]", json.dumps(judge["J2"], ensure_ascii=False))
    print("[J3]", json.dumps(judge["J3"], ensure_ascii=False))
    print(f"\n-> {OUT/'cost_detail.csv'} / {OUT/'cost_table.csv'} / {OUT/'judge.json'}")


if __name__ == "__main__":
    main()
