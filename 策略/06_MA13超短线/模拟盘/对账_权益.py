# -*- coding: utf-8 -*-
"""
06 MA13 模拟盘 · 权益对账（可重跑，只读，不连网）
==================================================
为什么需要这个脚本
------------------
`台账统计.py` 的毛盈亏**不等于账户赚的钱**，长期对不上权益。三处口径差：

  ① 成交价口径：`*_成交流水.csv` 里 FILL 的「成交价」写的是**决策价**
     （信号根收盘），不是云端真实成交均价。真实均价只在 `*_滑点.csv`
     的「成交均价」列（`ma13_live_multi.py:1741` 那行 `决策价={ref}` 可证）。
     ⇒ 用决策价配对会凭空产生/吞掉盈亏，主连换月窗口最严重
       （决策价来自新合约、成交在旧合约，价差数百点全算成盈亏）。

  ② 手续费：`台账统计.py` 只估算、不扣真实值；本脚本按
     `config_symbols.COST`（每手单边）逐单扣。

  ③ 账户基线：2026-10-08 13:28:27 权益从 201,599.30 跳到 999,997.50，
     即当天从本地 TqSim(20 万) 切到 TqKq 云端(100 万)。
     切换前的成交属于**另一个账户**，不能与之后混算。

口径定义
--------
  真实已实现 = Σ[(卖出真实均价 − 买入真实均价) × 方向 × 手数 × 乘数]
  净         = 真实已实现 − Σ手续费 + 未平仓浮动
  权益变化   = 期末权益 − 切换点权益

匹配规则
--------
  FILL 与 `*_滑点.csv` 按 **(品种, 方向, 开平, 手数)** + 时间最近（±180 秒）
  一一匹配，每条只消费一次。匹配不上的单子用决策价、并计入 `n_miss`
  （未记录滑点），在输出里单列，不做静默填充。

⚠️ 严禁用本脚本的数字做策略评价：它只回答「账对不对」，不回答「策略好不好」。
  样本 6 周、净额接近 0，任何胜率/盈亏比都不可下结论。
"""
import os
import sys
import glob
import re
import datetime

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_symbols as cs  # noqa: E402

LEDGER = os.path.join(_HERE, "台账")
EQ_PAT = re.compile(r"权益=([0-9.]+)")
MATCH_TOL = 180  # 秒


def _params(cn):
    for k, v in cs.SYMBOLS.items():
        if v[0] == cn:
            return k, float(v[2]), float(v[3]), float(cs.COST.get(k, 0.0))
    raise KeyError(f"品种「{cn}」不在 config_symbols.SYMBOLS 里")


def load_equity():
    """返回 (去重后的权益序列 DataFrame[t, eq], 切换点 Timestamp 或 None)。"""
    rows = []
    for f in glob.glob(os.path.join(LEDGER, "*_成交流水.csv")):
        d = pd.read_csv(f, encoding="utf-8-sig", dtype=str)
        e = d[d["类型"] == "EQUITY"]
        for _, r in e.iterrows():
            m = EQ_PAT.search(str(r["备注"]))
            if m:
                rows.append((r["记录时间"], float(m.group(1))))
    ed = pd.DataFrame(rows, columns=["t", "eq"])
    ed["t"] = pd.to_datetime(ed["t"])
    ed = ed.drop_duplicates("t").sort_values("t").reset_index(drop=True)

    # 账户切换点：单步跳变 > 50% 且绝对值 > 10 万
    cut = None
    prev = ed["eq"].shift(1)
    jump = (ed["eq"] - prev).abs()
    cand = ed[(jump > 100000) & (jump / prev.abs() > 0.5)]
    if len(cand):
        cut = cand.iloc[0]["t"]
    return ed, cut


def load_slip(cn):
    """读单品种滑点表，标准化成一单一行。"""
    f = os.path.join(LEDGER, f"{cn}_滑点.csv")
    if not os.path.exists(f):
        return pd.DataFrame(columns=["t", "dir", "side", "hands", "avg", "sym", "amt", "used"])
    d = pd.read_csv(f, encoding="utf-8-sig", dtype=str)
    d["t"] = pd.to_datetime(d["时间"])
    d["hands"] = pd.to_numeric(d["手数"], errors="coerce")
    d["avg"] = pd.to_numeric(d["成交均价"], errors="coerce")
    d["amt"] = pd.to_numeric(d["滑点金额"], errors="coerce")
    d["dir"] = d["方向"].astype(str)
    d["side"] = d["开平"].astype(str)
    d["sym"] = d["合约"].astype(str)
    d["used"] = False
    return d[["t", "dir", "side", "hands", "avg", "sym", "amt", "used"]]


def build_trades(cn, key, mult, cut):
    """把 FILL 配成往返，返回 (trades list, 手续费, 未平仓, 匹配/缺失计数)。"""
    f = os.path.join(LEDGER, f"{cn}_成交流水.csv")
    d = pd.read_csv(f, encoding="utf-8-sig", dtype=str)
    fl = d[d["类型"] == "FILL"].copy()
    if not len(fl):
        return [], 0.0, None, 0, 0
    fl["hands"] = pd.to_numeric(fl["手数"], errors="coerce")
    fl["px"] = pd.to_numeric(fl["成交价"], errors="coerce")
    fl["t"] = pd.to_datetime(fl["记录时间"])
    fl = fl.dropna(subset=["hands", "px"]).sort_values("t", kind="stable")

    slip = load_slip(cn)
    fee = 0.0
    n_hit = n_miss = 0

    # ---- 给每条 FILL 找真实成交均价 ----
    records = []
    for _, r in fl.iterrows():
        dr = str(r["方向"])
        if dr not in ("多开", "空开", "多平", "空平"):
            continue
        side = "开" if dr.endswith("开") else "平"
        direction = dr[0]
        real_px = float(r["px"])
        real_sym = str(r["备注"]).strip()
        if len(slip):
            cand = slip[(~slip["used"]) & (slip["dir"] == direction) &
                        (slip["side"] == side) &
                        (slip["hands"] == r["hands"]) &
                        ((slip["t"] - r["t"]).abs() <= pd.Timedelta(seconds=MATCH_TOL))]
            if len(cand):
                i = (cand["t"] - r["t"]).abs().idxmin()
                if pd.notna(slip.at[i, "avg"]):
                    real_px = float(slip.at[i, "avg"])
                    real_sym = str(slip.at[i, "sym"])
                    slip.at[i, "used"] = True
                    n_hit += 1
                else:
                    n_miss += 1
            else:
                n_miss += 1
        else:
            n_miss += 1
        fee += float(r["hands"]) * float(cs.COST.get(key, 0.0))
        records.append({"t": r["t"], "dir": dr, "hands": float(r["hands"]),
                        "px": real_px, "sym": real_sym, "cut": r["t"] < cut})

    # ---- 配对（按合约，禁止跨合约串配）----
    trades = []
    open_pos = {}  # sym -> (dr, hands, px, t, pre_cut)
    for rec in records:
        if rec["dir"] in ("多开", "空开"):
            open_pos[rec["sym"]] = (rec["dir"], rec["hands"], rec["px"], rec["t"], rec["cut"])
        elif rec["dir"] in ("多平", "空平"):
            k = rec["sym"]
            if k not in open_pos:
                continue  # 换月移交的旧仓 / 切换前遗留：开仓不在本段，丢弃
            o = open_pos.pop(k)
            sign = 1 if o[0] == "多开" else -1
            g = (rec["px"] - o[2]) * sign * o[1] * mult
            trades.append({"sym": k, "open_t": o[3], "close_t": rec["t"],
                           "gross": g, "pre_cut": o[4]})
    left = {k: v for k, v in open_pos.items()}
    return trades, fee, left, n_hit, n_miss


def main():
    ed, cut = load_equity()
    if cut is None:
        print("[!] 未发现账户切换点，按全样本处理")
        cut = ed["t"].iloc[0]
    eq_start = float(ed[ed["t"] >= cut]["eq"].iloc[0])
    eq_end = float(ed["eq"].iloc[-1])
    actual = eq_end - eq_start

    names = sorted(os.path.basename(f).replace("_成交流水.csv", "")
                   for f in glob.glob(os.path.join(LEDGER, "*_成交流水.csv")))

    rows = []
    tot_g = tot_fee = 0.0
    tot_hit = tot_miss = 0
    for cn in names:
        try:
            key, mult, tick, comm = _params(cn)
        except KeyError as e:
            print(f"[skip] {e}")
            continue
        tr, fee, left, hit, miss = build_trades(cn, key, mult, cut)
        if not tr and not fee:
            continue
        g = sum(t["gross"] for t in tr)
        n = len(tr)
        w = sum(1 for t in tr if t["gross"] > 0)
        rows.append((cn, key, n, w, g, fee, hit, miss,
                     (left or {}), ) )
        tot_g += g
        tot_fee += fee
        tot_hit += hit
        tot_miss += miss

    # ---- 未平仓浮动（用运行心跳最新价）----
    # ★ 只认 underlying_{key}.json 记录的「当前持仓合约」。
    #   台账里会出现「有开仓、无平仓」的历史残留（如 JD 2026-10-12 空开 jd2611 8 手，
    #   之后被换月移交平掉但没补记；2026-10-15 还出现过 CZCE.SR701 串台单）。
    #   若不剔除，这些会被当成未平仓，按最新价算出几万元假浮盈。
    float_pnl = 0.0
    float_txt = []
    zombie_txt = []
    for cn in names:
        f = os.path.join(LEDGER, f"{cn}_运行心跳.csv")
        if not os.path.exists(f):
            continue
        try:
            key, mult, tick, comm = _params(cn)
        except KeyError:
            continue
        d = pd.read_csv(f, encoding="utf-8-sig", dtype=str)
        if not len(d):
            continue
        last = d.iloc[-1]
        pos = pd.to_numeric(last.get("持仓手数"), errors="coerce")
        px = pd.to_numeric(last.get("现价"), errors="coerce")
        if pd.isna(pos) or pd.isna(px):
            continue
        held_sym = None
        jf = os.path.join(LEDGER, f"underlying_{key}.json")
        if os.path.exists(jf):
            try:
                import json
                with open(jf, encoding="utf-8") as fh:
                    held_sym = json.load(fh).get("underlying")
            except Exception:  # noqa: BLE001
                held_sym = None
        _, _, left, _, _ = build_trades(cn, key, mult, cut)
        for sym, o in left.items():
            if held_sym and sym != held_sym:
                zombie_txt.append(f"{cn} {sym} 开{o[0]} {o[1]:.0f}手 @{o[2]:.0f}"
                                  f"（{o[3]}）— 当前持仓合约是 {held_sym} ⇒ 判为历史残留，不计浮盈亏")
                continue
            if pos == 0:
                zombie_txt.append(f"{cn} {sym} 开{o[0]} {o[1]:.0f}手 @{o[2]:.0f}"
                                  f" — 心跳持仓=0 ⇒ 判为历史残留")
                continue
            sign = -1 if pos < 0 else 1
            pnl = (float(px) - o[2]) * sign * abs(float(pos)) * mult
            float_pnl += pnl
            float_txt.append(f"{cn} {sym} {'空' if sign<0 else '多'}{abs(int(pos))}手 "
                             f"成本{o[2]:.0f} 现价{float(px):.0f} 浮动{pnl:+,.0f}")

    L = []
    A = L.append
    A("=" * 92)
    A(f"06 MA13 模拟盘 · 权益对账   {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    A("=" * 92)
    A(f"账户切换点 {cut}   起始权益 {eq_start:,.2f}   当前权益 {eq_end:,.2f}")
    A(f"★ 实际权益变化 = {actual:+,.2f} 元")
    A("")
    A(f"{'品种':<8}{'配平':>5}{'盈':>5}{'真实毛盈亏':>13}{'手续费':>10}{'成交价命中':>11}{'未命中':>7}")
    for cn, key, n, w, g, fee, hit, miss, left in rows:
        A(f"{cn:<8}{n:>5}{w:>5}{g:>13,.0f}{-fee:>10,.0f}{hit:>11}{miss:>7}")
    A("-" * 92)
    A(f"合计     {tot_g:>13,.0f}{-tot_fee:>10,.0f}{tot_hit:>11}{tot_miss:>7}")
    A("")
    A(f"真实已实现（扣手续费前） {tot_g:+,.0f}")
    A(f"手续费                   {-tot_fee:+,.0f}")
    A(f"未平仓浮动               {float_pnl:+,.0f}")
    if float_txt:
        for s in float_txt:
            A(f"    {s}")
    est = tot_g - tot_fee + float_pnl
    A(f"推算净额                 {est:+,.0f}")
    A(f"实际权益变化             {actual:+,.0f}")
    A(f"★ 未解释残差             {est - actual:+,.0f}")
    A("")
    if zombie_txt:
        A("僵尸持仓（不计入浮盈亏）：")
        for s in zombie_txt:
            A(f"    {s}")
        A("")
    A(f"成交价命中 {tot_hit} / 未命中 {tot_miss}"
      f"（未命中=该单无滑点记录，按决策价计，是残差主要来源）")
    A("")
    A("口径提醒：")
    A("  · 换月移交的旧仓在配对时会被丢弃（开仓不在同合约），故笔数会少于 FILL/2")
    A("  · 6 周样本、净额近 0 ⇒ 本表只验账，不做策略评价")
    txt = "\n".join(L)
    print(txt)
    out = os.path.join(LEDGER, f"_对账_{datetime.datetime.now():%Y%m%d_%H%M}.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(txt + "\n")
    print(f"\n[done] {out}")


if __name__ == "__main__":
    main()
