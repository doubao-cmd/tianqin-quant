# -*- coding: utf-8 -*-
"""
R1 · cost_model.py —— 成本尺（交付物主文件）
=============================================
接口（卡 §六）：
    cost(symbol, h="5min") -> {pts, p10, p90, n, source}

`pts` 的定义（**单边、元/手、成本为正**）：
    pts = 执行成本 = C_spread + C_impact + C_fee + C_delay
    （不含 C_adverse —— 它是「成交后价格漂移」，与 μ(h) 同源，算进成本会重复扣，
      h* 处用 pts×2 对 μ(h)，见 r1_t4_hstar.py 顶部说明）

样本纪律（卡 §六）：
    n < 20 ⇒ source="fallback"，pts 回落为 **1 跳**（= tick_value），且**显式打标**，
    绝不静默。返回值同时保留 pts_measured，便于看到"实测是多少、为什么没用"。

h 参数：
    pts 与持有期无关（实测：8 手规模下参与率 ≈0.1%，冲击 0.02~0.31 元/手，可忽略）；
    随 h 变的只有 C_adverse，所以返回值里把它单列成 adverse_at_h（= −μ(h)），
    并给 total_card_at_h = pts + adverse_at_h（= 卡 §三 定义的 C_total 在 h 处的取值）。
    **不要把 total_card_at_h 直接和 μ(h) 相减**（同一段位移扣两次），h* 请读 h_star。

用法：
    from cost_model import cost
    cost("M")                     -> h 默认 "5min"
    cost("SA", h="60min")
    python cost_model.py          -> 打印全部品种 x h 的尺子
"""
import csv
import json
from pathlib import Path

BASE = Path(__file__).parent
OUT = BASE / "out"
H_MAP = {"5min": 5, "15min": 15, "30min": 30, "60min": 60, "120min": 120, "240min": 240}
MIN_N = 20          # 卡 §六：样本 <20 ⇒ fallback


def _read(name):
    p = OUT / name
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


_CONTRACTS = {r["symbol"]: r for r in _read("contracts.csv")}
_TABLE = {r["symbol"]: r for r in _read("cost_table.csv")}
_HSTAR = {r["symbol"]: r for r in _read("h_star.csv")}
_MU = {}
for _r in _read("mu_curve.csv"):
    _MU.setdefault(_r["symbol"], {})[int(_r["h_min"])] = float(_r["mu_mean"])


def _f(x, default=None):
    try:
        return float(x)
    except Exception:
        return default


def cost(symbol, h="5min"):
    """返回该品种的成本尺读数。symbol 用品种代码（M/SA/FU/BU/EG/MA/JM/P/SR/JD）。"""
    symbol = symbol.upper()
    if symbol not in _CONTRACTS:
        raise KeyError(f"未知品种 {symbol}；已支持：{sorted(_CONTRACTS)}")
    if h not in H_MAP:
        raise KeyError(f"h 只支持 {list(H_MAP)}")
    hm = H_MAP[h]
    con, tab = _CONTRACTS[symbol], _TABLE[symbol]
    tv = _f(con["tick_value"])
    n = int(_f(tab["n_可算C_total"], 0) or 0)

    # 实测执行成本（单边）= 分项中位相加，下界为手续费
    sp = _f(tab["C_spread_med"], 0.0)
    im = _f(tab["C_impact_hi_med"], 0.0)
    fee = _f(tab["C_fee"], 0.0)
    dly_raw = tab["C_delay_med"]
    dly = _f(dly_raw, 0.0) if dly_raw and not str(dly_raw).endswith(")") else 0.0
    pts_measured = max(sp + im + fee + dly, fee)

    p10 = _f(tab["C_total_lo_p10"])
    p90 = _f(tab["C_total_lo_p90"])
    card_total = _f(tab["C_total_lo_med"])

    source = "measured" if n >= MIN_N else "fallback"
    pts = pts_measured if source == "measured" else tv

    # h 相关项
    mu_h = _MU.get(symbol, {}).get(hm)
    adverse_h = -mu_h if mu_h is not None else None
    hstar = _HSTAR.get(symbol, {}).get("h_star_min", "")

    return {
        "symbol": symbol,
        "h": h,
        "pts": round(pts, 3),                    # 单边成本（元/手），fallback 时为 1 跳
        "pts_ticks": round(pts / tv, 3),
        "p10": round(p10, 3) if p10 is not None else None,
        "p90": round(p90, 3) if p90 is not None else None,
        "n": n,
        "source": source,
        "pts_measured": round(pts_measured, 3),
        "pts_measured_ticks": round(pts_measured / tv, 3),
        "tick_value": tv,
        "adverse_at_h": None if adverse_h is None else round(adverse_h, 3),
        "total_card_at_h": (None if adverse_h is None
                            else round(pts_measured + adverse_h, 3)),
        "card_total_med_15min": card_total,
        "h_star_min": int(hstar) if hstar not in ("", None) else None,
        "note": ("样本 n<20 ⇒ 回落 1 跳（卡 §六），pts_measured 仅作参考"
                 if source == "fallback" else "实测"),
    }


def table_all(h="5min"):
    return [cost(s, h) for s in ["M", "SA", "FU", "BU", "EG", "MA", "JM", "P", "SR", "JD"]]


if __name__ == "__main__":
    print(f"{'品种':4s}{'h':>7s}{'pts(元)':>9s}{'跳':>6s}{'n':>4s}{'source':>10s}"
          f"{'实测跳':>8s}{'p10':>8s}{'p90':>8s}{'adv@h':>8s}{'h*':>6s}")
    for r in table_all("15min"):
        print(f"{r['symbol']:4s}{r['h']:>7s}{r['pts']:9.2f}{r['pts_ticks']:6.2f}{r['n']:4d}"
              f"{r['source']:>10s}{r['pts_measured_ticks']:8.2f}"
              f"{(r['p10'] if r['p10'] is not None else 0):8.2f}"
              f"{(r['p90'] if r['p90'] is not None else 0):8.2f}"
              f"{(r['adverse_at_h'] if r['adverse_at_h'] is not None else 0):8.2f}"
              f"{(str(r['h_star_min'])+'m' if r['h_star_min'] else 'null'):>6s}")
    print("\n示例：", json.dumps(cost("M"), ensure_ascii=False))
