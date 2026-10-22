# -*- coding: utf-8 -*-
"""
R1 · T4 h* 求解（out/h_star.csv / out/mu_curve.csv）
=====================================================
问题：每笔期望 μ(h) 与往返成本交点在哪 ⇒ 该品种最小可行持有期 h*。

μ(h) 的来源（任务书 T4：用台账已算出的每笔期望，**不为本任务新跑回测**）：
  取 cost_detail.csv 里每一笔**真实成交**当入场，按成交方向盯市到 h 分钟后，
  算「如果持有 h 分钟」的毛盈亏（元/手，未扣成本），再对同品种全部笔取均值。
  h ∈ {15,30,60,120,240} 分钟，**固定集合、不挑区间**。
  ⇒ 这是把已有成交按不同持有期重标价，不是新回测，也没有新增任何信号/过滤。

成本侧用 E = 2 × C_exec（往返）：
  C_exec = spread + impact + fee + delay，**不含 adverse**。
  理由：adverse 本身就是「成交后价格漂移」，与 μ(h) 是同一段位移；
  把它算进成本再与 μ 比，同一段行情被扣两次，h* 会被系统性推后。
  卡 §四 J1 仍按含 adverse 的 C_total 判（那是成本判据），两者分开报，不混用。

h* 判定：
  h* = min{ h : μ(h) ≥ E }；若全区间 μ(h) < E ⇒ 写 null + 原因「全区间 μ<E」
  样本 < 8 笔 ⇒ 写 null + 「样本不足」

用法：python r1_t4_hstar.py
"""
import csv
import json
import statistics as st
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
OUT = BASE / "out"
KLINE = OUT / "kline"
HORIZONS = [5, 15, 30, 60, 120, 240]
FMT = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")


def pdt(s):
    s = str(s).strip().split("+")[0]
    for f in FMT:
        try:
            return datetime.strptime(s, f)
        except Exception:
            pass
    raise ValueError(s)


def load5(contract):
    p = KLINE / f"{contract}_5min.csv"
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        rows = [{"dt": pdt(r["dt"]), "close": float(r["close"])} for r in csv.DictReader(f)]
    rows.sort(key=lambda x: x["dt"])
    return rows


def close_after(bars, ts, h):
    """成交后 h 分钟的价格：取「结束时刻最接近 ts+h」的 5min bar 收盘，返回 (price, span_min)。"""
    target = ts + timedelta(minutes=h)
    best, bi = None, None
    for i, b in enumerate(bars):
        gap = abs(((b["dt"] + timedelta(minutes=5)) - target).total_seconds())
        if best is None or gap < best:
            best, bi = gap, i
    if bi is None:
        return None, None
    span = ((bars[bi]["dt"] + timedelta(minutes=5)) - ts).total_seconds() / 60
    if span < 1:            # 不能用成交前的 bar
        return None, None
    return bars[bi]["close"], round(span, 1)


def main():
    cparams = {}
    with open(OUT / "contracts.csv", "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            cparams[r["symbol"]] = {"mult": float(r["mult"]),
                                    "tick_value": float(r["tick_value"]),
                                    "tier": r["tier"]}
    table = {}
    with open(OUT / "cost_table.csv", "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            table[r["symbol"]] = r

    rows = [r for r in csv.DictReader(open(OUT / "cost_detail.csv", encoding="utf-8-sig"))]
    kl = {}
    mu_rows, mu_curve = [], []
    for code in sorted({r["code"] for r in rows}):
        sub = [r for r in rows if r["code"] == code and r["C_total_lo"] is not None]
        prm = cparams[code]
        tickv = prm["tick_value"]
        for h in HORIZONS:
            vals = []
            for r in sub:
                c = r["contract"]
                if c not in kl:
                    kl[c] = load5(c)
                if not kl[c]:
                    continue
                px, span = close_after(kl[c], pdt(r["时间"]), h)
                if px is None:
                    continue
                fill = float(r["成交价"])
                sign = 1.0 if r["side"] == "buy" else -1.0
                vals.append((px - fill) * prm["mult"] * sign)
            if not vals:
                continue
            mean = st.mean(vals)
            sd = st.pstdev(vals) if len(vals) > 1 else 0.0
            mu_curve.append({"symbol": code, "tier": prm["tier"], "h_min": h, "n": len(vals),
                             "mu_med": round(st.median(vals), 3), "mu_mean": round(mean, 3),
                             "mu_sd": round(sd, 3),
                             "se": round(sd / (len(vals) ** 0.5), 3) if vals else "",
                             "hit_rate": round(100 * sum(1 for v in vals if v > 0) / len(vals), 1)})

        # 成本（往返）。★ 用「分项中位相加」而不是「逐笔总和的分布中位」：
        #   逐笔总和里 spread 可以为负（有利成交），逐笔中位会出现负成本（EG/MA 实测），
        #   负成本会让 h* 平凡地等于最短持有期，失去意义。
        #   分项中位相加更稳，并以「手续费」为下界（成本不可能低于手续费）。
        try:
            sp = float(table[code]["C_spread_med"])
            imhi = float(table[code]["C_impact_hi_med"])
            fee = float(table[code]["C_fee"])
            dly_raw = table[code]["C_delay_med"]
            dly = float(dly_raw) if dly_raw not in ("", None) and not str(dly_raw).endswith(")") else 0.0
            c_exec_sum = max(sp + imhi + fee + dly, fee)
            c_exec_trade = float(table[code]["C_exec_med"])
        except Exception:
            c_exec_sum, c_exec_trade = None, None
        if c_exec_sum is None:
            mu_rows.append({"symbol": code, "tier": prm["tier"], "E_round_trip": "",
                            "h_star_min": "", "可解": "否", "原因": "C_exec 不可得"})
            continue
        e_rt = 2 * c_exec_sum
        curve = {m["h_min"]: m for m in mu_curve if m["symbol"] == code}
        hstar, why = None, ""
        if not curve or max(m["n"] for m in curve.values()) < 8:
            why = f"样本不足（最大 n={max((m['n'] for m in curve.values()), default=0)}<8）"
        else:
            for h in HORIZONS:
                m = curve.get(h)
                if m and m["n"] >= 8 and m["mu_mean"] >= e_rt:
                    hstar = h
                    break
            if hstar is None:
                best = max(curve.values(), key=lambda m: m["mu_mean"])
                why = (f"全区间 μ(h) < 往返成本 {e_rt:.2f} 元/手"
                       f"（μ 最高 {best['mu_mean']:.2f} @h={best['h_min']}min；"
                       f"即样本内不存在可行持有期）")
        mu_rows.append({
            "symbol": code, "tier": prm["tier"], "tick_value": tickv,
            "C_exec_单边": round(c_exec_sum, 3),
            "C_exec_逐笔中位(参考)": round(c_exec_trade, 3),
            "E_round_trip": round(e_rt, 3),
            "E_round_trip_ticks": round(e_rt / tickv, 3),
            "mu_h5": curve.get(5, {}).get("mu_mean", ""),
            "mu_h15": curve.get(15, {}).get("mu_mean", ""),
            "mu_h30": curve.get(30, {}).get("mu_mean", ""),
            "mu_h60": curve.get(60, {}).get("mu_mean", ""),
            "mu_h120": curve.get(120, {}).get("mu_mean", ""),
            "mu_h240": curve.get(240, {}).get("mu_mean", ""),
            "h_star_min": "" if hstar is None else hstar,
            "可解": "是" if hstar is not None else "否",
            "原因": "" if hstar is not None else why,
            "n_笔": max((m["n"] for m in curve.values()), default=0),
        })

    def dump(path, rows_):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows_[0].keys()))
            w.writeheader()
            w.writerows(rows_)

    dump(OUT / "h_star.csv", mu_rows)
    dump(OUT / "mu_curve.csv", mu_curve)

    print(f"{'品种':4s}{'栏':2s}{'E往返':>9s}{'跳':>6s}{'μ15':>8s}{'μ30':>8s}{'μ60':>8s}"
          f"{'μ120':>8s}{'μ240':>8s}{'h*':>6s}  结论")
    for r in mu_rows:
        f_ = lambda k: (f"{r[k]:8.1f}" if r[k] != "" else f"{'—':>8s}")
        print(f"{r['symbol']:4s}{r['tier']:2s}{r['E_round_trip']:9.1f}"
              f"{r['E_round_trip_ticks']:6.2f}{f_('mu_h15')}{f_('mu_h30')}{f_('mu_h60')}"
              f"{f_('mu_h120')}{f_('mu_h240')}"
              f"{(str(r['h_star_min'])+'min' if r['h_star_min'] != '' else 'null'):>6s}  "
              f"{'可解' if r['可解']=='是' else r['原因'][:40]}")
    print(f"\n-> {OUT/'h_star.csv'} / {OUT/'mu_curve.csv'}")


if __name__ == "__main__":
    main()
