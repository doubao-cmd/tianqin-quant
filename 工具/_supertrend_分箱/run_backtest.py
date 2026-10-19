# -*- coding: utf-8 -*-
"""
run_backtest.py —— ATR 分箱 SuperTrend 回测主脚本（v3 口径）

用法：
  python run_backtest.py --range IS          # 样本内（默认）
  python run_backtest.py --range OOS         # 样本外（只跑一次，有 flag 守卫）
  python run_backtest.py --range ALL         # 全区间（仅诊断，不用于结论）

产物（out/）：
  trades_{base,bin}.csv        逐笔台账
  signals_{arm}_{key}.csv      每 bar 的 atr / adaptive_atr / st / dir
  equity_{arm}.csv             每 bar 已实现权益
  summary_{range}.json         统计摘要
  run.log                      运行日志
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import st_binned as S   # noqa: E402

OUT = S.OUT_DIR
os.makedirs(OUT, exist_ok=True)

RANGES = {
    "IS": (S.IS_START, S.IS_END),
    "OOS": (S.OOS_START, S.OOS_END),
    "ALL": ("2000-01-01", "2099-12-31"),
}


# ------------------------------------------------------------------ 统计工具
def seg_t(x: np.ndarray) -> float:
    """按段 t（不按 bar，按 bar 会虚高）。"""
    if len(x) < 3:
        return float("nan")
    sd = x.std(ddof=1)
    if sd == 0:
        return float("nan")
    return float(x.mean() / sd * math.sqrt(len(x)))


def nw_t(x: np.ndarray, lag: int = 20) -> tuple[float, float]:
    """Newey-West HAC t。返回 (mean, t_hac)。"""
    n = len(x)
    if n < lag + 3:
        return float(x.mean()) if n else float("nan"), float("nan")
    xm = x - x.mean()
    s = float((xm ** 2).mean())
    for k in range(1, lag + 1):
        gk = float((xm[k:] * xm[:-k]).mean())
        s += 2.0 * (1.0 - k / (lag + 1.0)) * gk
    se = math.sqrt(max(s, 1e-18) / n)
    return float(x.mean()), float(x.mean() / se) if se > 0 else float("nan")


def block_boot_t(x: np.ndarray, block: int = 20, n_boot: int = 4000,
                 seed: int = 20261019) -> tuple[float, float, float, float]:
    """Moving block bootstrap。返回 (mean, t_boot, lo, hi)。"""
    rng = np.random.default_rng(seed)
    n = len(x)
    if n < block * 2:
        return float(x.mean()), float("nan"), float("nan"), float("nan")
    nb = int(math.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=(n_boot, nb))
    offs = np.arange(block)
    means = np.empty(n_boot)
    for b in range(n_boot):
        idx = (starts[b][:, None] + offs[None, :]).ravel()[:n]
        means[b] = x[idx].mean()
    sd = means.std(ddof=1)
    return float(x.mean()), float(x.mean() / sd) if sd > 0 else float("nan"), \
        float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def summarize(tr: list, n_years: float) -> dict:
    if not tr:
        return {"n_trades": 0, "win_rate": float("nan"), "gross_pnl": 0.0, "cost": 0.0,
                "net_pnl": 0.0, "ratio": float("nan"), "t_seg": float("nan"),
                "max_dd": 0.0, "max_dd_pct": float("nan"), "avg_hold_bars": float("nan"),
                "max_hold_bars": 0, "pnl_per_year": 0.0, "flips_per_year": 0.0}
    net = np.array([t["net_pnl"] for t in tr], float)
    gross = float(sum(t["gross_pnl"] for t in tr))
    cost = float(sum(t["cost"] for t in tr))
    eq = np.cumsum(net)
    peak = np.maximum.accumulate(np.r_[0.0, eq])[1:]
    dd = eq - peak
    # 回撤百分比：以「累计最高权益 + 初始名义」为分母不直观，用「峰值绝对额」的正值近似
    mdd = float(dd.min()) if len(dd) else 0.0
    base = float(peak[np.argmin(dd)] + 1e-9) if len(dd) else 0.0
    holds = np.array([t["bars_held"] for t in tr], float)
    return {
        "n_trades": int(len(tr)),
        "win_rate": float((net > 0).mean()),
        "gross_pnl": gross,
        "cost": cost,
        "net_pnl": float(net.sum()),
        "ratio": float(gross / cost) if cost else float("nan"),
        "t_seg": seg_t(net),
        "max_dd": mdd,
        "max_dd_pct": float(mdd / base) if base > 0 else float("nan"),
        "avg_hold_bars": float(holds.mean()),
        "max_hold_bars": int(holds.max()),
        "pnl_per_year": float(net.sum() / n_years) if n_years > 0 else float("nan"),
        "flips_per_year": float(len(tr) / n_years) if n_years > 0 else float("nan"),
    }


# ------------------------------------------------------------------ 主流程
def in_range(tr: list, lo, hi) -> list:
    return [t for t in tr if t["entry_trade_day"] >= lo and t["exit_trade_day"] <= hi]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", default="IS", choices=list(RANGES))
    ap.add_argument("--force-oos", action="store_true")
    args = ap.parse_args()

    rng_name = args.range
    lo_s, hi_s = RANGES[rng_name]
    lo = pd.Timestamp(lo_s).date()
    hi = pd.Timestamp(hi_s).date()

    flag = os.path.join(OUT, "OOS_DONE.flag")
    if rng_name == "OOS":
        if os.path.exists(flag) and not args.force_oos:
            print(f"[V9] 拒绝重跑：{flag} 已存在（样本外只跑一次）。确需重跑请加 --force-oos。")
            return 2

    t0 = time.time()
    log_lines = []

    def log(s: str = ""):
        print(s)
        log_lines.append(s)

    log("=" * 92)
    log(f"  ATR 分箱 SuperTrend 回测   range={rng_name} ({lo_s} ~ {hi_s})")
    log(f"  PY={sys.version.split()[0]}  numpy={np.__version__}  pandas={pd.__version__}")
    log(f"  参数 ATR_LEN={S.ATR_LEN} FACT={S.FACT} LOOKBACK={S.LOOKBACK} "
        f"Q={S.Q} PCT={S.PCT_METHOD} POS={S.POS_SIZE} SLIP={S.SLIP} COMM={S.COMM}")
    log(f"  起点 START_IDX={S.START_IDX}  seed=20261019")
    log("=" * 92)

    spec = S.load_spec()
    log("\n[规格] " + "  ".join(
        f"{k}:{v['name']} mult={v['mult']:g} tick={v['tick']:g}" for k, v in spec.items()))

    # ---------------- 1) 逐品种计算 ----------------
    store = {}
    log("\n[1] 逐品种计算（全历史递推，最后按区间切段）")
    for key in S.KEYS:
        d = S.load_key(key)
        h, l, c = d["high"], d["low"], d["close"]
        atr = S.wilder_atr(h, l, c)
        ada, lo_s_, mid_s_, hi_s_, lab = S.adaptive_atr_seq(atr)
        st_b, dir_b, i0b = S.supertrend(atr, h, l, c)
        st_n, dir_n, i0n = S.supertrend(ada, h, l, c)
        tr_b, eq_b = S.run_arm(d, st_b, dir_b, spec[key]["mult"], spec[key]["tick"])
        tr_n, eq_n = S.run_arm(d, st_n, dir_n, spec[key]["mult"], spec[key]["tick"])

        rolls, rinfo = S.roll_days_of(key)
        wins = S.roll_confidence_windows(rolls)
        cover_last = rinfo["last"]

        def flagroll(e_day, x_day):
            w = None
            for a, b in wins:
                if a <= e_day and x_day <= b:
                    w = (a, b)
                    break
            if w is None:
                return "unknown"
            return bool(any(e_day < rd <= x_day for rd in rolls))

        for tr in (tr_b, tr_n):
            for t in tr:
                t["key"] = key
                t["is_month_roll"] = flagroll(t["entry_trade_day"], t["exit_trade_day"])

        # 区间内的交易日掩码
        mask = (d["trade_day"].dt.date >= lo) & (d["trade_day"].dt.date <= hi)
        n_years = float(mask.sum()) / 243.0

        store[key] = dict(d=d, atr=atr, ada=ada, lab=lab, lo_s=lo_s_, hi_s=hi_s_,
                          mid_s=mid_s_, st_b=st_b, dir_b=dir_b, st_n=st_n, dir_n=dir_n,
                          tr_b=tr_b, tr_n=tr_n, eq_b=eq_b, eq_n=eq_n,
                          mask=mask, n_years=n_years, rinfo=rinfo, rolls=rolls, wins=wins)
        log(f"  {key}: bars={len(c)}  区间bars={int(mask.sum())} ({n_years:.2f}yr)  "
            f"base段={len(tr_b)} bin段={len(tr_n)}  "
            f"换月={rinfo['n']}次 表末={cover_last} 回测末={d['trade_day'].max().date()}  "
            f"单合约天={rinfo['thin']}/{rinfo['rows']} 最大间隔={rinfo['max_gap']}天")

    # ---------------- 2) 区间统计 ----------------
    rows = []
    agg = {"base": [], "bin": []}
    log(f"\n[2] 分品种统计（{rng_name}）")
    hdr = f"{'key':4s} {'arm':5s} {'n':>4s} {'win%':>6s} {'gross':>10s} {'cost':>8s} {'net':>10s} " \
          f"{'ratio':>6s} {'t_seg':>6s} {'maxDD':>9s} {'hold':>6s} {'net/yr':>10s}"
    log(hdr)
    log("-" * len(hdr))
    for key, z in store.items():
        for arm in ("base", "bin"):
            tr = in_range(z[f"tr_{'b' if arm == 'base' else 'n'}"], lo, hi)
            s = summarize(tr, z["n_years"])
            s.update(key=key, arm=arm, insufficient=s["n_trades"] < 30)
            rows.append(s)
            agg[arm].extend(tr)
            log(f"{key:4s} {arm:5s} {s['n_trades']:4d} {s['win_rate']*100:6.1f} "
                f"{s['gross_pnl']:10.0f} {s['cost']:8.0f} {s['net_pnl']:10.0f} "
                f"{s['ratio']:6.2f} {s['t_seg']:6.2f} {s['max_dd']:9.0f} "
                f"{s['avg_hold_bars']:6.1f} {s['pnl_per_year']:10.0f}")

    # 合计（年化口径用「日历年数」，不是 Σ品种年，否则会低估 6 倍）
    total = {}
    cal_years = max(store[k]["n_years"] for k in store)
    for arm in ("base", "bin"):
        total[arm] = summarize(agg[arm], cal_years)
        total[arm]["n_years"] = cal_years
    log("-" * len(hdr))
    for arm in ("base", "bin"):
        s = total[arm]
        log(f"{'合计':4s} {arm:5s} {s['n_trades']:4d} {s['win_rate']*100:6.1f} "
            f"{s['gross_pnl']:10.0f} {s['cost']:8.0f} {s['net_pnl']:10.0f} "
            f"{s['ratio']:6.2f} {s['t_seg']:6.2f} {s['max_dd']:9.0f} "
            f"{s['avg_hold_bars']:6.1f} {s['pnl_per_year']:10.0f}")

    # ---------------- 3) 分档诊断（只报本区间） ----------------
    log(f"\n[3] 分档诊断（仅 {rng_name}，不含其他区间）")
    diag = []
    for key, z in store.items():
        m = z["mask"].to_numpy() & (z["lab"] >= 0)
        if m.sum() == 0:
            continue
        disp = (z["hi_s"][m] - z["lo_s"][m]) / np.maximum(z["mid_s"][m], 1e-12)
        share = [(z["lab"][m] == j).mean() for j in range(3)]
        ratio = z["ada"][m] / z["atr"][m]
        row = dict(key=key, n=int(m.sum()), disp_med=float(np.median(disp)),
                   low=float(share[0]), mid=float(share[1]), high=float(share[2]),
                   r_med=float(np.median(ratio)),
                   r_p5=float(np.percentile(ratio, 5)), r_p95=float(np.percentile(ratio, 95)),
                   dev5=float((np.abs(ratio - 1) > 0.05).mean()))
        diag.append(row)
        log(f"  {key:4s} n={row['n']:5d} disp中位={row['disp_med']:.3f} "
            f"bin_share 低{row['low']:.1%}/中{row['mid']:.1%}/高{row['high']:.1%}  "
            f"ada/atr 中位={row['r_med']:.3f} P5~P95={row['r_p5']:.2f}~{row['r_p95']:.2f} "
            f"|差|>5%占{row['dev5']:.0%}")
    if diag:
        log(f"  ⇒ 合计 disp 中位={np.median([r['disp_med'] for r in diag]):.3f}  "
            f"bin_share 低{np.mean([r['low'] for r in diag]):.1%}/"
            f"中{np.mean([r['mid'] for r in diag]):.1%}/高{np.mean([r['high'] for r in diag]):.1%}  "
            f"|ada/atr-1|>5% 占 {np.mean([r['dev5'] for r in diag]):.0%}")

    # ---------------- 4) Jaccard + 逐日权益差 ----------------
    log(f"\n[4] 配对比较（{rng_name}）")
    pair_rows = []
    inc_by_key = {}          # key -> Series(当日净盈亏增量, index=trade_day)
    log(f"{'key':4s} {'flips_b':>8s} {'flips_n':>8s} {'b/yr':>6s} {'n/yr':>6s} "
        f"{'Jaccard':>8s} {'Δnet':>10s} {'t_raw':>7s} {'t_HAC':>7s} {'t_boot':>7s} {'boot95':>18s}")
    log("-" * 108)
    for key, z in store.items():
        m = z["mask"].to_numpy()
        # 翻仓信号 bar（区间内）
        def flips(mask_, dirc_, st_):
            idx = np.where(mask_ & ~np.isnan(st_))[0]
            out = []
            for i in idx:
                if i == 0 or i < S.START_IDX:
                    continue
                if dirc_[i] != dirc_[i - 1]:
                    out.append(i)
            return set(out)
        fb, fn = flips(m, z["dir_b"], z["st_b"]), flips(m, z["dir_n"], z["st_n"])
        inter, uni = len(fb & fn), len(fb | fn)
        jac = inter / uni if uni else float("nan")

        # 逐日已实现权益差（区间内）
        sl = np.where(m)[0]
        if len(sl) < 3:
            continue
        eqb = z["eq_b"][sl] - z["eq_b"][sl[0]]
        eqn = z["eq_n"][sl] - z["eq_n"][sl[0]]
        diff = eqn - eqb
        mean_d, t_hac = nw_t(diff, lag=20)
        sd = diff.std(ddof=1)
        t_raw = float(diff.mean() / sd * math.sqrt(len(diff))) if sd > 0 else float("nan")

        # 组合层面用「当日净盈亏增量」按交易日对齐（不同品种 bar 数/起始日不同，禁止按位置对齐）
        days_sl = z["d"]["trade_day"].iloc[sl]
        inc = np.diff(np.r_[0.0, diff])          # 当日权益差增量
        inc_by_key[key] = pd.Series(inc, index=days_sl.dt.date.values)

        pair_rows.append(dict(key=key, flips_base=len(fb), flips_bin=len(fn),
                              b_yr=len(fb) / z["n_years"], n_yr=len(fn) / z["n_years"],
                              jaccard=float(jac), dnet=float(diff[-1]),
                              t_raw=t_raw, t_hac=t_hac, t_boot=float("nan"),
                              boot_lo=float("nan"), boot_hi=float("nan")))
        log(f"{key:4s} {len(fb):8d} {len(fn):8d} {len(fb)/z['n_years']:6.2f} "
            f"{len(fn)/z['n_years']:6.2f} {jac:8.3f} {diff[-1]:10.0f} "
            f"{t_raw:7.2f} {t_hac:7.2f}")

    # 组合：按交易日对齐求和，再对每日增量做 block bootstrap
    tj = tb = blo = bhi = float("nan")
    joint_dnet = float(sum(r["dnet"] for r in pair_rows)) if pair_rows else float("nan")
    if inc_by_key:
        grid = sorted(set().union(*[set(s.index) for s in inc_by_key.values()]))
        mat = np.vstack([inc_by_key[k].reindex(grid).fillna(0.0).to_numpy()
                         for k in inc_by_key])
        inc_all = mat.sum(axis=0)                 # 组合每日增量
        cum = np.cumsum(inc_all)                  # 组合累计权益差
        joint_dnet = float(cum[-1])
        _, tj = nw_t(cum, lag=20)                 # 与对 inc_all 的长期均值检验等价
        _, tb, blo, bhi = block_boot_t(inc_all, block=20)   # bootstrap 作用在增量上
        # 把「增量均值」的 CI 换算成「区间累计差」的 CI
        blo, bhi = blo * len(inc_all), bhi * len(inc_all)
        log("-" * 108)
        log(f"合计 Δnet = {joint_dnet:.0f}（按交易日对齐，{len(grid)} 个交易日）")
        log(f"  联合累计差 HAC t={tj:.2f}   block-bootstrap t={tb:.2f}   "
            f"95%CI=[{blo:.0f}, {bhi:.0f}] 元（区间累计）")

    # 合计 Jaccard = Σ交集 / Σ并集（由各品种 jaccard 反推交并集数）
    if pair_rows:
        inter_all = sum(r["jaccard"] * (r["flips_base"] + r["flips_bin"]) / (1 + r["jaccard"])
                        for r in pair_rows)
        uni_all = sum((r["flips_base"] + r["flips_bin"]) / (1 + r["jaccard"])
                      for r in pair_rows)
        log(f"合计翻仓 base={sum(r['flips_base'] for r in pair_rows)} "
            f"bin={sum(r['flips_bin'] for r in pair_rows)}  "
            f"合计 Jaccard={inter_all / uni_all:.3f}  "
            f"（分品种均值 {np.mean([r['jaccard'] for r in pair_rows]):.3f}）")

    # ---------------- 5) 换月段单独统计 ----------------
    log(f"\n[5] 换月段单独统计（{rng_name}）")
    roll_rows = []
    for key, z in store.items():
        for arm in ("base", "bin"):
            tr = in_range(z[f"tr_{'b' if arm == 'base' else 'n'}"], lo, hi)
            for state in (True, False, "unknown"):
                sub = [t for t in tr if t["is_month_roll"] == state]
                if not sub:
                    continue
                netv = sum(t["net_pnl"] for t in sub)
                roll_rows.append(dict(key=key, arm=arm, is_month_roll=state,
                                      n=len(sub), net=float(netv),
                                      win=float(np.mean([t["net_pnl"] > 0 for t in sub]))))
    for r in roll_rows:
        if r["is_month_roll"] is True:
            log(f"  {r['key']:4s} {r['arm']:5s} 跨换月 n={r['n']:3d} net={r['net']:9.0f} win={r['win']:.0%}")
    n_unk = sum(r["n"] for r in roll_rows if r["is_month_roll"] == "unknown")
    n_all = sum(r["n"] for r in roll_rows)
    log(f"  ⇒ is_month_roll=unknown 的段 {n_unk}/{n_all}（展期表缺口，见 §6.3 声明）")

    # ---------------- 6) 落盘 ----------------
    # ★ 只有 IS 用任务书 §7 的裸文件名；其余区间加后缀，避免把 IS 台账覆盖掉
    sfx = "" if rng_name == "IS" else f"_{rng_name}"
    log(f"\n[6] 落盘（后缀 '{sfx}'）")
    df_b = pd.DataFrame([{**t, "arm": "base"} for t in agg["base"]])
    df_n = pd.DataFrame([{**t, "arm": "bin"} for t in agg["bin"]])
    cols = ["key", "arm", "seg_id", "entry_time", "entry_price_raw", "entry_price",
            "exit_time", "exit_price_raw", "exit_price", "side", "bars_held",
            "gross_pnl", "cost", "net_pnl", "exit_reason", "is_month_roll",
            "entry_trade_day", "exit_trade_day", "entry_sig_i", "entry_fill_i",
            "exit_sig_i", "exit_fill_i"]
    for arm, df in (("base", df_b), ("bin", df_n)):
        p = os.path.join(OUT, f"trades_{arm}{sfx}.csv")
        df[cols].to_csv(p, index=False, encoding="utf-8-sig")
        log(f"  {p}  ({len(df)} 行)")

    for key, z in store.items():
        for arm, st, dd in (("base", z["st_b"], z["dir_b"]), ("bin", z["st_n"], z["dir_n"])):
            p = os.path.join(OUT, f"signals_{arm}_{key}{sfx}.csv")
            pd.DataFrame({
                "datetime": z["d"]["dt"], "open": z["d"]["open"], "high": z["d"]["high"],
                "low": z["d"]["low"], "close": z["d"]["close"],
                "atr": z["atr"], "adaptive_atr": z["ada"], "bin_label": z["lab"],
                "st": st, "dir": dd,
            }).to_csv(p, index=False, encoding="utf-8-sig")
    log(f"  signals_{{base,bin}}_{{...}}.csv  × {len(store) * 2}")

    for arm, z_key in (("base", "eq_b"), ("bin", "eq_n")):
        frames = []
        for key, z in store.items():
            frames.append(pd.DataFrame({"datetime": z["d"]["dt"], "key": key, "equity": z[z_key]}))
        p = os.path.join(OUT, f"equity_{arm}{sfx}.csv")
        pd.concat(frames, ignore_index=True).to_csv(p, index=False, encoding="utf-8-sig")
        log(f"  {p}")

    summary = {
        "range": rng_name, "span": [lo_s, hi_s],
        "params": dict(ATR_LEN=S.ATR_LEN, FACT=S.FACT, LOOKBACK=S.LOOKBACK, Q=S.Q,
                       PCT=S.PCT_METHOD, POS=S.POS_SIZE, SLIP=S.SLIP, COMM=S.COMM,
                       START_IDX=S.START_IDX),
        "total": total,
        "by_symbol": rows,
        "diagnostics": diag,
        "paired": pair_rows,
        "month_roll": roll_rows,
        "joint_dnet": joint_dnet,
        "joint_t_hac": float(tj), "joint_t_boot": float(tb),
        "elapsed_s": round(time.time() - t0, 1),
    }
    p = os.path.join(OUT, f"summary_{rng_name}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    log(f"  {p}")

    log(f"\n耗时 {time.time() - t0:.1f}s")
    with open(os.path.join(OUT, "run.log"), "a", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")

    if rng_name == "OOS":
        with open(flag, "w", encoding="utf-8") as f:
            f.write(f"OOS run at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        log(f"  [V9] 已写 OOS_DONE.flag")
    return 0


if __name__ == "__main__":
    sys.exit(main())
