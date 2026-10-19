# -*- coding: utf-8 -*-
"""run_ts.py —— ST-TS-01 回测：base（标准 ST）vs ts（时间止损）

判据全部来自预注册 §7，本脚本只做计算，不含任何调参入口。

用法：
    python run_ts.py            # 首次运行，跑完写 CONFIRM_DONE.flag
    python run_ts.py --force    # 允许覆盖（正常情况下不该用）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import st_ts as S

FLAG = os.path.join(S.OUT_DIR, "CONFIRM_DONE.flag")


# ------------------------------------------------------------------ 统计工具
def max_dd(eq: np.ndarray) -> float:
    peak = np.maximum.accumulate(eq)
    return float((eq - peak).min())


def summarize(trades: list) -> dict:
    if not trades:
        return dict(n_trades=0)
    net = np.array([t["net_pnl"] for t in trades])
    hold = np.array([t["bars_held"] for t in trades])
    return dict(
        n_trades=len(trades),
        win_rate=float((net > 0).mean()),
        gross_pnl=float(sum(t["gross_pnl"] for t in trades)),
        cost=float(sum(t["cost"] for t in trades)),
        net_pnl=float(net.sum()),
        avg_net=float(net.mean()),
        hold_median=float(np.median(hold)),
        hold_mean=float(hold.mean()),
        hold_p90=float(np.percentile(hold, 90)),
        max_hold=int(hold.max()),
    )


def nw_t(x: np.ndarray, lag: int = 20) -> tuple[float, float]:
    """Newey-West t（仅作参考，不参与判定）。"""
    x = np.asarray(x, float)
    n = len(x)
    if n < 3:
        return float("nan"), float("nan")
    mu = x.mean()
    d = x - mu
    g0 = float((d * d).sum() / n)
    var = g0
    for L in range(1, lag + 1):
        w = 1.0 - L / (lag + 1.0)
        gl = float((d[L:] * d[:-L]).sum() / n)
        var += 2.0 * w * gl
    se = np.sqrt(max(var, 0.0) / n)
    return mu, (mu / se if se > 0 else float("nan"))


def stationary_bootstrap_sum(x: np.ndarray, block: int = 20, reps: int = 5000,
                             seed: int = 20261019) -> np.ndarray:
    """Politis-Romano 平稳 bootstrap（几何块长，均值 = block）的「和」分布。"""
    x = np.asarray(x, float)
    n = len(x)
    rng = np.random.default_rng(seed)
    p = 1.0 / block
    out = np.empty(reps)
    for r in range(reps):
        lens = []
        tot = 0
        while tot < n:
            L = int(min(rng.geometric(p), 500))
            lens.append(L)
            tot += L
        lens = np.array(lens)
        starts = rng.integers(0, n, size=len(lens))
        offs = np.arange(int(lens.max()))
        idx = (starts[:, None] + offs[None, :]) % n
        flat = idx[offs[None, :] < lens[:, None]][:n]
        out[r] = x[flat].sum()
    return out


# ------------------------------------------------------------------ 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    os.makedirs(S.OUT_DIR, exist_ok=True)
    if os.path.exists(FLAG) and not a.force:
        print(f"[V9] {FLAG} 已存在 ⇒ 确认样本只跑一次，拒绝重跑（确需覆盖加 --force）")
        return 2

    logf = open(os.path.join(S.OUT_DIR, "run.log"), "w", encoding="utf-8")

    def log(s=""):
        print(s, flush=True)
        logf.write(str(s) + "\n")

    t0 = time.time()
    spec = S.load_spec()

    arms = ("base", "ts")
    all_tr = {a_: [] for a_ in arms}
    store = {}

    log("=" * 104)
    log("ST-TS-01  base（标准 ST） vs ts（持仓≥30根且浮亏即离场）")
    log(f"样本：{' '.join(S.KEYS)}  区间 {S.START_DAY.date()} ~ {S.END_DAY.date()}")
    log(f"冻结：ATR_LEN={S.ATR_LEN} FACT={S.FACT} N_TIME={S.N_TIME} "
        f"POS={S.POS_SIZE}手 SLIP={S.SLIP}跳 COMM={S.COMM}元/边")
    log("=" * 104)

    for key in S.KEYS:
        d = S.load_key(key)
        h, l, c = d["high"], d["low"], d["close"]
        atr = S.wilder_atr(h, l, c)
        st, dd, _ = S.supertrend(atr, h, l, c)
        mult, tick = spec[key]["mult"], spec[key]["tick"]

        tr_b, eq_b = S.run_arm(d, st, dd, mult, tick, "base")
        tr_t, eq_t = S.run_arm(d, st, dd, mult, tick, "ts")
        for t in tr_b:
            t["key"] = key
        for t in tr_t:
            t["key"] = key

        store[key] = dict(day=d["trade_day"], eq_b=eq_b, eq_t=eq_t,
                          n=len(c), mult=mult, tick=tick)
        all_tr["base"].extend(tr_b)
        all_tr["ts"].extend(tr_t)

        sb, stt = summarize(tr_b), summarize(tr_t)
        log(f"  {key:3s} bars={len(c):5d}  "
            f"base: n={sb['n_trades']:3d} win={sb['win_rate']:6.1%} net={sb['net_pnl']:11,.0f}  "
            f"ts: n={stt['n_trades']:3d} win={stt['win_rate']:6.1%} net={stt['net_pnl']:11,.0f}  "
            f"Δ={stt['net_pnl'] - sb['net_pnl']:11,.0f}")

    # ---------------- 总表 ----------------
    span_days = (S.END_DAY - S.START_DAY).days
    years = span_days / 365.25
    log("\n" + "-" * 104)
    total = {}
    for a_ in arms:
        s = summarize(all_tr[a_])
        s["net_per_year"] = s["net_pnl"] / years
        # 组合权益（逐日对齐后横向相加）
        idx = sorted(set().union(*[set(store[k]["day"]) for k in S.KEYS]))
        idx = pd.DatetimeIndex(idx)
        eq = np.zeros(len(idx))
        for k in S.KEYS:
            s_k = pd.Series(store[k][f"eq_{'b' if a_ == 'base' else 't'}"].copy(),
                            index=pd.DatetimeIndex(store[k]["day"]))
            s_k = s_k[~s_k.index.duplicated()]
            eq += s_k.reindex(idx).ffill().fillna(0.0).to_numpy()
        s["max_dd"] = max_dd(eq)
        total[a_] = s
        log(f"[{a_:4s}] 段数={s['n_trades']:4d} 胜率={s['win_rate']:6.1%} "
            f"gross={s['gross_pnl']:12,.0f} cost={s['cost']:9,.0f} net={s['net_pnl']:12,.0f} "
            f"net/年={s['net_per_year']:10,.0f} maxDD={s['max_dd']:12,.0f} "
            f"持仓中位={s['hold_median']:.0f} P90={s['hold_p90']:.0f} 最长={s['max_hold']}")

    # ---------------- 逐品种 Δnet + 显著性 ----------------
    log("\n" + "-" * 104)
    log("[2] 配对比较（逐日已实现权益差，按 trade_day 对齐）")
    per_key = []
    series = {}
    for key in S.KEYS:
        z = store[key]
        eb = pd.Series(z["eq_b"], index=pd.DatetimeIndex(z["day"]))
        et = pd.Series(z["eq_t"], index=pd.DatetimeIndex(z["day"]))
        eb = eb[~eb.index.duplicated()]
        et = et[~et.index.duplicated()]
        diff = (et - eb).dropna()
        series[key] = diff
        inc = np.diff(diff.to_numpy(), prepend=0.0)     # ★ 逐日增量（不是水平值）
        nb = sum(t["net_pnl"] for t in all_tr["base"] if t["key"] == key)
        nt = sum(t["net_pnl"] for t in all_tr["ts"] if t["key"] == key)
        mu, th = nw_t(inc)
        per_key.append(dict(key=key, base_net=nb, ts_net=nt, dnet=nt - nb,
                            dnet_per_year=(nt - nb) / years, t_hac=th))
        log(f"  {key:3s} base={nb:12,.0f}  ts={nt:12,.0f}  Δ={nt - nb:12,.0f}  "
            f"Δ/年={(nt - nb) / years:10,.0f}  HAC t={th:6.2f}")

    idx = sorted(set().union(*[set(s.index) for s in series.values()]))
    idx = pd.DatetimeIndex(idx)
    pooled = np.zeros(len(idx))
    for key in S.KEYS:
        pooled += series[key].reindex(idx).fillna(0.0).to_numpy()
    pooled_inc = np.diff(pooled, prepend=0.0)          # ★ 逐日增量才是"逐日权益差"

    boot = stationary_bootstrap_sum(pooled_inc, block=20, reps=5000)
    lo, hi = float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))
    mu_p, t_p = nw_t(pooled_inc)
    pooled_dnet = sum(r["dnet"] for r in per_key)
    pos_keys = sum(1 for r in per_key if r["dnet"] >= 0)
    ew = float(np.mean([r["dnet"] for r in per_key]))

    log("-" * 104)
    log(f"池化 Δnet = {pooled_dnet:,.0f}（逐日增量累计 {pooled_inc.sum():,.0f}）  Δ/年 = {pooled_dnet / years:,.0f}")
    log(f"stationary bootstrap(block=20, 5000): 95%CI = [{lo:,.0f}, {hi:,.0f}]  "
        f"均值={boot.mean():,.0f}")
    log(f"HAC(20) t = {t_p:.2f}（★仅参考，不参与判定）")
    log(f"等权口径（每品种 Δnet 算术平均） = {ew:,.0f}")
    log(f"品种层面 Δnet ≥ 0 的个数 = {pos_keys}/{len(per_key)} ({pos_keys / len(per_key):.0%})")

    # ---------------- 集中度（按 entry 配对分解 Δnet） ----------------
    delta_rows = []
    for key in S.KEYS:
        bmap = {t["entry_fill_i"]: t for t in all_tr["base"] if t["key"] == key}
        tmap = {t["entry_fill_i"]: t for t in all_tr["ts"] if t["key"] == key}
        for ei in sorted(set(bmap) | set(tmap)):
            b = bmap.get(ei)
            t = tmap.get(ei)
            dv = (t["net_pnl"] if t else 0.0) - (b["net_pnl"] if b else 0.0)
            delta_rows.append(dict(key=key, entry_fill_i=ei, delta=dv,
                                   base_net=(b["net_pnl"] if b else 0.0),
                                   ts_net=(t["net_pnl"] if t else 0.0),
                                   kind=("both" if (b and t) else ("ts_only" if t else "base_only"))))
    ddf = pd.DataFrame(delta_rows)
    ddf["absd"] = ddf["delta"].abs()
    top5 = ddf.nlargest(5, "absd")["delta"].sum()
    conc = float(top5 / pooled_dnet) if pooled_dnet != 0 else float("nan")
    log(f"Δnet 分解：配对 both={int((ddf.kind == 'both').sum())} 段、"
        f"ts_only={int((ddf.kind == 'ts_only').sum())}、base_only={int((ddf.kind == 'base_only').sum())}")
    log(f"集中度：|Δ| 前 5 段合计 = {top5:,.0f}，占池化 Δnet = {conc:.1%}")

    # ---------------- time_stop 诊断 ----------------
    ts_rows = [t for t in all_tr["ts"] if t["exit_reason"] == "time_stop"]
    bmap_all = {(t["key"], t["entry_fill_i"]): t for t in all_tr["base"]}
    cf = [bmap_all[(t["key"], t["entry_fill_i"])]["net_pnl"]
          for t in ts_rows if (t["key"], t["entry_fill_i"]) in bmap_all]
    stopped_net = sum(t["net_pnl"] for t in ts_rows)
    log("\n" + "-" * 104)
    log(f"[3] time_stop 诊断：触发 {len(ts_rows)} 次 / ts 总段数 {len(all_tr['ts'])} "
        f"= {len(ts_rows) / max(1, len(all_tr['ts'])):.1%}")
    log(f"    被止损段合计 net = {stopped_net:,.0f}")
    if cf:
        log(f"    同一入场点若不止损（base 臂同段）合计 net = {sum(cf):,.0f} ⇒ "
            f"止损省下/损失 = {stopped_net - sum(cf):,.0f}")
    by_kind = {}
    for t in ts_rows:
        by_kind.setdefault(t["key"], []).append(t["net_pnl"])
    log("    逐品种触发次数：" + "  ".join(f"{k}:{len(v)}" for k, v in sorted(by_kind.items())))

    # ---------------- 判定 ----------------
    cond = {
        "① 池化 Δnet > 0": pooled_dnet > 0,
        "② CI 下界 > 0": lo > 0,
        "③ ≥60% 品种 Δnet ≥ 0": pos_keys / len(per_key) >= 0.60,
        "④ 两臂各 ≥30 段": min(total["base"]["n_trades"], total["ts"]["n_trades"]) >= 30,
        "⑤ 等权均值 > 0": ew > 0,
    }
    passed = all(cond.values())
    killed = (pooled_dnet < 0 and hi < 0) or (pos_keys / len(per_key) <= 0.40)
    verdict = "通过" if passed else ("判死" if killed else "无结论")
    if passed and conc > 0.50:
        verdict = "无结论（集中度 >50%，预注册降级）"
    log("\n" + "-" * 104)
    log("[4] 预注册 §7 判据")
    for k, v in cond.items():
        log(f"    {'PASS' if v else 'FAIL'}  {k}")
    log(f"    集中度 {conc:.1%}（>50% 自动降级为无结论）")
    log(f"    ⇒ 判定：{verdict}")

    # ---------------- 落盘 ----------------
    for a_ in arms:
        df = pd.DataFrame(all_tr[a_])
        cols = ["key", "seg_id", "entry_time", "entry_price_raw", "entry_price",
                "exit_time", "exit_price", "side", "bars_held",
                "gross_pnl", "cost", "net_pnl", "exit_reason"]
        df[cols].to_csv(os.path.join(S.OUT_DIR, f"trades_{a_}.csv"),
                        index=False, encoding="utf-8-sig")
    ddf.to_csv(os.path.join(S.OUT_DIR, "dnet_decomp.csv"), index=False, encoding="utf-8-sig")

    summary = dict(
        scope=dict(keys=S.KEYS, start=str(S.START_DAY.date()), end=str(S.END_DAY.date()),
                   years=years, n_time=S.N_TIME, atr_len=S.ATR_LEN, fact=S.FACT),
        total=total, per_key=per_key,
        pooled_dnet=pooled_dnet, ci=[lo, hi], boot_mean=float(boot.mean()),
        t_hac=float(t_p), equal_weight=ew,
        pos_keys=pos_keys, n_keys=len(per_key),
        concentration=conc, top5_abs_delta=float(top5),
        time_stop=dict(n=len(ts_rows), rate=len(ts_rows) / max(1, len(all_tr["ts"])),
                       stopped_net=float(stopped_net),
                       counterfactual_net=float(sum(cf)) if cf else None),
        conditions=cond, verdict=verdict, elapsed_s=round(time.time() - t0, 1),
    )
    with open(os.path.join(S.OUT_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    with open(FLAG, "w", encoding="utf-8") as f:
        f.write(f"done {time.strftime('%Y-%m-%d %H:%M:%S')} verdict={verdict}\n")
    log(f"\n产物：{S.OUT_DIR}  耗时 {summary['elapsed_s']}s")
    logf.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
