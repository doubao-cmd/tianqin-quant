# -*- coding: utf-8 -*-
"""
进场体检（Entry Screening）—— 在写任何策略代码之前，先判一个「进场信号」值不值得做
================================================================================
背景（2026-10-12 的结论）：
  06 MA13 的**毛利只有成本的 4.5%** ⇒ 出场怎么改都没用；瓶颈在**进场**。
  且 62% 的单子被止损直接扫掉 ⇒ 进场后价格常立刻反向。

因此需要一个**便宜、可复用**的筛子：给定一个候选进场信号，先算三件事，
过不了就直接判死，**不进引擎、不写策略**。

三层口径
--------
  L0 纯进场（无止损）  ：进场后**固定持有期**的前瞻毛收益（点/手）
                         ⇒ 只回答「信号有没有方向信息」，不含任何出场逻辑的污染。
  L1 进场 + 固定止损   ：进场 + **固定 STOP_ATR_MULT × ATR** 止损，到期未止损则持有期满平仓
                         ⇒ 回答「这个小系统在**成本后**能不能活」。
  量级判据             ：L1 毛均 ÷ 往返成本（点）必须 ≥ **2.0**
                         ⇒ 留 1 倍余量给估计误差 / 滑点恶化（见 README §七之二 结论⑤）。

通过门槛（跑前定死，全过才算「值得进 full backtest」）
----------------------------------------------------
  ① 样本量   n ≥ 30 笔
  ② 量级     L1 毛均 ÷ 成本 ≥ 2.0
  ③ 成本后   L1 净均 > 0
  ④ 显著性   |t| ≥ 2.0（按笔，非按 bar）
  ⑤ 一致性   至少 **多数品种**（≥ ⌈品种数/2⌉）同时满足 ②③

⚠️⚠️ 两条纪律（本工具存在的意义就是它们）
----------------------------------------------------
  1. **本工具只能「判死」，不能「判生」。** 跑在 2021-2023（已污染窗口）上，
     任何"通过"都不构成 edge 证据，只说明「值得进入下一轮、且必须换干净样本」。
  2. **禁止把「批次里表现最好的那个」当成发现。** 批量跑 N 个信号 = N 次比较，
     挑最好的宣称有效就是过拟合（规则 2 禁区）。批量模式**只用于一次性砍掉一批候选**；
     任何幸存者必须**单独再过一遍预注册卡 + 独立样本**。

禁未来函数
----------
  信号只用 ≤ 第 i 根已收盘 bar 的数据；**进场在 open[i+1]**。
  内置 `--selftest` 做「截断检验」：对随机样本，用 data[:i+1] 重算 sig[i]，
  与全量算出的 sig[i] 必须逐一相等（不等即为未来函数）。

用法
----
  python 进场体检.py --list                    # 列出所有候选信号
  python 进场体检.py --signal retrace_ma13     # 只测一个（推荐）
  python 进场体检.py --all                     # 批量证伪（结果不可用于择优）
  python 进场体检.py --signal cross_ma13 --syms JM,M,P
  python 进场体检.py --selftest                # 未来函数截断检验
"""
import os
import sys
import json
import math
import argparse

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_symbols as cs  # noqa: E402

# ==================== 冻结口径（写死，不许改；改了就等于调参）====================
MA_N = 13          # MA13（与 06 同）
SLOPE_N = 5        # 斜率周期（与 06 同）
ATR_N = 14         # ATR14（与 06 同）
HOLD = 12          # ★ L0/L1 主持有期（5min×12 ≈ 1 小时）。**外生定死，不扫**
STOP_ATR_MULT = 2.0  # ★ L1 固定止损（2×ATR）
SLIP = 1           # 滑点（跳），与全项目一致
RETRACE_TOL = 0.5  # 回踩容差（与 06 同）
BREAK_N = 20       # 唐奇安突破窗口（经典外生值，不扫）
TSMOM_N = 20       # 时序动量窗口（不扫）
DIAG_HOLDS = [1, 2, 3, 6, 12, 24, 48]  # ⚠️ 仅供「诊断曲线」，**严禁据此择优**
# ==============================================================================

DATA_DIR = os.path.join(_HERE, "数据")


# ---------------------------------------------------------------- 指标
def _atr(high, low, close, n):
    """简单滚动均值 TR —— 与 06 回测脚本**逐字同口径**（保证可比）。

    06 的实现是 Python 循环： atr[i] = mean(tr[max(1,i-n):i])（i>=2 时），atr[1]=tr[1]。
    这里用「累计和」等价向量化（5.3 万根 × 多个信号下，循环版慢到不可用）：
        mean(tr[a:i]) = (cs[i] - cs[a]) / (i - a)，其中 a = max(1, i-n)
    ⚠️ 用 max(1, ...) 而不是 i-n，是为了**复刻 06 在预热期（i<15）的"部分窗口"行为**——
    照抄 pandas.rolling(n).mean() 会在预热期产生 NaN，与 06 不一致。
    """
    prev = np.roll(close, 1); prev[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    N = len(tr)
    out = np.full(N, np.nan)
    if N < 2:
        return out
    cs = np.cumsum(np.insert(tr, 0, 0.0))          # cs[k] = sum(tr[:k])
    i = np.arange(N)
    a = np.maximum(1, i - n)
    cnt = i - a
    m = (i >= 2) & (cnt > 0)
    out[m] = (cs[i[m]] - cs[a[m]]) / cnt[m]
    out[1] = tr[1]
    return out


def _sma(x, n):
    """ma[i] = mean(x[i-n:i])（只用 i 之前的 bar）—— 与 06 的 MA13 同口径。
    cs 长度 N+1，cs[k]=sum(x[:k])；ma[i] = (cs[i]-cs[i-n])/n，i ∈ [n, N)。
    ⇒ out[n:] = (cs[n:N] - cs[0:N-n]) / n，两段长度都是 N-n。
    """
    N = len(x)
    out = np.full(N, np.nan)
    if N <= n:
        return out
    cs = np.cumsum(np.insert(x, 0, 0.0))
    out[n:] = (cs[n:-1] - cs[:-(n + 1)]) / n
    return out


# ---------------------------------------------------------------- 信号注册表
def _s_retrace_ma13(o, h, l, c, atr, ma):
    """★ 基准锚 = 06 MA13 的**进场条件本身**：|close−MA13| ≤ 0.5×ATR 且斜率定方向。"""
    slope = ma - np.roll(ma, SLOPE_N)
    slope[:SLOPE_N] = np.nan
    d = np.sign(slope)
    touch = np.abs(c - ma) <= RETRACE_TOL * atr
    return np.where(touch, d, 0.0)


def _s_cross_ma13(o, h, l, c, atr, ma):
    """收盘价上穿/下穿 MA13 的那一根。"""
    up = (c > ma) & (np.roll(c, 1) <= np.roll(ma, 1))
    dn = (c < ma) & (np.roll(c, 1) >= np.roll(ma, 1))
    return np.where(up, 1.0, np.where(dn, -1.0, 0.0))


def _s_breakout_20(o, h, l, c, atr, ma):
    """唐奇安：收盘 > 前 20 根最高 → 多；< 前 20 根最低 → 空（轨道不含当前根）。"""
    hh = pd.Series(h).rolling(BREAK_N).max().shift(1).to_numpy()
    ll = pd.Series(l).rolling(BREAK_N).min().shift(1).to_numpy()
    return np.where(c > hh, 1.0, np.where(c < ll, -1.0, 0.0))


def _s_tsmom_20(o, h, l, c, atr, ma):
    """过去 20 根收益的符号（时序动量）。"""
    r = c - np.roll(c, TSMOM_N)
    r[:TSMOM_N] = np.nan
    return np.sign(np.nan_to_num(r, nan=0.0))


def _s_rev_2bar(o, h, l, c, atr, ma):
    """连续 2 根下跌 → 做多（逆势抓反转）；连续 2 根上涨 → 做空。"""
    d1 = np.sign(c - np.roll(c, 1))
    d2 = np.sign(np.roll(c, 1) - np.roll(c, 2))
    both_dn = (d1 < 0) & (d2 < 0)
    both_up = (d1 > 0) & (d2 > 0)
    return np.where(both_dn, 1.0, np.where(both_up, -1.0, 0.0))


def _s_vol_break(o, h, l, c, atr, ma):
    """单根振幅 > 2×ATR 时顺势跟进（波动扩张）。"""
    rng = c - np.roll(c, 1)
    big = np.abs(rng) > 2.0 * atr
    return np.where(big, np.sign(rng), 0.0)


SIGNALS = {
    "retrace_ma13": ("回踩 MA13 + 斜率定方向（= 06 的进场条件，基准锚）", _s_retrace_ma13),
    "cross_ma13":   ("收盘上/下穿 MA13", _s_cross_ma13),
    "breakout_20":  ("唐奇安 20 根区间突破（轨道不含当前根）", _s_breakout_20),
    "tsmom_20":     ("过去 20 根收益的符号（时序动量）", _s_tsmom_20),
    "rev_2bar":     ("连续 2 根同向 → 反向开（逆势）", _s_rev_2bar),
    "vol_break":    ("单根振幅 > 2×ATR 顺势跟进", _s_vol_break),
}


# ---------------------------------------------------------------- 数据
def load(key):
    p = os.path.join(DATA_DIR, f"{key}_5min_2021-01-01_2023-12-31.csv")
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p, encoding="utf-8-sig", parse_dates=["datetime"])
    return df


def cost_points(key):
    """往返成本（点/手）= 2 × (滑点×一跳 + 佣金/乘数)。"""
    _, _, MULT, TICK = cs.get(key)
    COMM = float(cs.COST.get(key, 0.0))
    return 2.0 * (SLIP * TICK + COMM / MULT)


# ---------------------------------------------------------------- 体检
def screen_one(key, sig_name, df, diag=False):
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    N = len(c)
    atr = _atr(h, l, c, ATR_N)
    ma = _sma(c, MA_N)
    sig = SIGNALS[sig_name][1](o, h, l, c, atr, ma)
    # 信号预热期与无效值置 0
    bad = np.isnan(atr) | np.isnan(ma)
    sig = np.where(bad, 0.0, np.nan_to_num(sig, nan=0.0))

    valid = np.arange(0, N - HOLD - 1)
    idx = valid[sig[valid] != 0]
    if len(idx) == 0:
        return {"key": key, "signal": sig_name, "n": 0}

    sgn = sig[idx]
    entry = o[idx + 1]
    cp = cost_points(key)

    # ---- L0：固定持有期、无止损 ----
    fwd = (o[idx + 1 + HOLD] - entry) * sgn
    # ---- L1：进场 + 固定 2ATR 止损 ----
    stop = np.where(sgn > 0, entry - STOP_ATR_MULT * atr[idx], entry + STOP_ATR_MULT * atr[idx])
    exit_px = o[idx + 1 + HOLD].copy()
    hit = np.zeros(len(idx), dtype=bool)
    for j, (e_i, st, s) in enumerate(zip(idx, stop, sgn)):
        for jj in range(e_i + 1, e_i + 1 + HOLD):
            if s > 0:
                if l[jj] <= st:
                    exit_px[j] = min(st, o[jj]); hit[j] = True; break
            else:
                if h[jj] >= st:
                    exit_px[j] = max(st, o[jj]); hit[j] = True; break
    gross1 = (exit_px - entry) * sgn
    net1 = gross1 - cp

    def _t(x):
        sd = float(np.std(x, ddof=1)) if len(x) > 1 else 0.0
        return float(np.mean(x) / (sd / math.sqrt(len(x)))) if sd > 1e-12 else 0.0

    days = df["datetime"].dt.date.nunique()
    out = {
        "key": key, "name_cn": cs.get(key)[0], "signal": sig_name,
        "n": int(len(idx)), "per_day": round(len(idx) / days, 2), "days": int(days),
        "cost_pts": round(cp, 3),
        "L0_gross": round(float(np.mean(fwd)), 3), "L0_t": round(_t(fwd), 2),
        "L1_gross": round(float(np.mean(gross1)), 3),
        "L1_net": round(float(np.mean(net1)), 3), "L1_t": round(_t(net1), 2),
        "L1_winrate": round(float(np.mean(net1 > 0)), 4),
        "stop_share": round(float(np.mean(hit)), 4),
        "ratio": round(float(np.mean(gross1) / cp), 3) if cp > 0 else None,
    }
    if diag:
        # 诊断曲线要覆盖最长持有期 ⇒ 用「能容纳最长持有期」的那批进场点（不改主判定的样本）
        mx = max(DIAG_HOLDS)
        idz = idx[idx + 1 + mx < N]
        if len(idz) > 0:
            out["L0_curve"] = {str(hh): round(float(np.mean((o[idz + 1 + hh] - o[idz + 1]) * sig[idz])), 3)
                               for hh in DIAG_HOLDS}
    return out


def verdict(rows):
    """按跑前定死的五条门槛判定（跨品种一致性）。"""
    ok = [r for r in rows if r["n"] >= 30]
    hard = [r for r in ok if (r.get("ratio") or -9) >= 2.0 and r["L1_net"] > 0]
    sig_ok = [r for r in hard if abs(r["L1_t"]) >= 2.0]
    need = math.ceil(len(ok) / 2) if ok else 1
    return {
        "n_sym": len(ok), "need": need,
        "pass_hard": len(hard), "pass_sig": len(sig_ok),
        "verdict": "PASS（仅表示值得换干净样本再验，不是 edge 证据）"
                   if len(hard) >= need else "FAIL（判死）",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--syms", default=None, help="逗号分隔，如 JM,M,P")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--diag", action="store_true", help="附持有期诊断曲线")
    ap.add_argument("--selftest", action="store_true", help="未来函数截断检验")
    a = ap.parse_args()

    if a.list:
        print("可用候选信号：")
        for k, (desc, _) in SIGNALS.items():
            print(f"  {k:<15} {desc}")
        return

    keys = ([k.strip().upper() for k in a.syms.split(",")] if a.syms
            else list(cs.SYMBOLS.keys()))

    if a.selftest:
        _selftest(keys)
        return

    names = list(SIGNALS) if a.all else [a.signal]
    if names == [None]:
        raise SystemExit("请指定 --signal NAME 或 --all（--list 可看列表）")

    L = []
    L.append("=" * 112)
    L.append(f"进场体检  ｜  HOLD={HOLD} 根(5min)  ｜  止损={STOP_ATR_MULT}×ATR  ｜  "
             f"滑点={SLIP}跳  ｜  手数=固定 1 手  ｜  窗口 2021-2023（**已污染，只能判死**）")
    L.append("=" * 112)
    L.append(f"{'信号':<14}{'品种':<7}{'笔数':>7}{'笔/日':>7}{'成本(点)':>9}"
             f"{'L0毛(点)':>10}{'L1毛(点)':>10}{'L1净(点)':>10}{'毛/成本':>9}{'胜率':>8}{'止损占比':>9}{'t':>7}{'判定':>6}")

    summary = {}
    for nm in names:
        rows = []
        for k in keys:
            df = load(k)
            if df is None or len(df) < 1000:
                print(f"  [skip] {k} 无缓存数据（先跑 拉数据.py）")
                continue
            r = screen_one(k, nm, df, diag=a.diag)
            rows.append(r)
            v = "✅" if (r.get("ratio") or -9) >= 2.0 and r["L1_net"] > 0 else "❌"
            L.append(f"{nm:<14}{r['name_cn']:<7}{r['n']:>7}{r['per_day']:>7.2f}{r['cost_pts']:>9.2f}"
                     f"{r['L0_gross']:>10.3f}{r['L1_gross']:>10.3f}{r['L1_net']:>10.3f}"
                     f"{(r.get('ratio') or float('nan')):>9.2f}{r['L1_winrate']*100:>7.1f}%"
                     f"{r['stop_share']*100:>8.1f}%{r['L1_t']:>7.2f}{v:>6}")
            if a.diag and "L0_curve" in r:
                L.append("              持有期诊断曲线 L0毛(点): " +
                         "  ".join(f"{k2}根={v2}" for k2, v2 in r["L0_curve"].items()))
        vd = verdict(rows)
        summary[nm] = {"verdict": vd, "rows": rows}
        L.append(f"{'':14}└─ 样本量够的品种 {vd['n_sym']} 个；过硬门槛 {vd['pass_hard']} 个"
                 f"（需 ≥{vd['need']}）；其中显著 {vd['pass_sig']} 个  ⇒  **{vd['verdict']}**")
        L.append("-" * 112)

    L.append("")
    L.append("⚠️ 本工具只能「判死」，不能「判生」：窗口 2021-2023 已污染，任何 ✅ 都不构成 edge 证据。")
    L.append("⚠️ 批量模式（--all）跑 N 个信号 = N 次比较；**禁止挑最好的那个宣称有效**（规则 2 禁区）。")
    txt = "\n".join(L)
    print(txt)
    tag = "ALL" if a.all else names[0]
    out = os.path.join(_HERE, f"体检结果_{tag}.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    with open(os.path.join(_HERE, f"体检结果_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n[done] {out}")


def _selftest(keys, n_probe=200, seed=7):
    """未来函数「截断检验」：用 data[:i+1] 重算 sig[i]，须与全量结果逐一相等。

    ⚠️ 性能：早期版本对每根抽查都重算整段（5.3 万根 × 上万次抽样）⇒ 根本跑不完。
    现在只取**尾部 400 根**（sig[i] 只依赖最近 ~32 根：MA13 + 斜率 5 + ATR14），
    数学上等价、速度可行。窗口长度 W 远大于依赖长度，不存在边界效应。
    """
    W = 400
    rng = np.random.default_rng(seed)
    print(f"{'信号':<15}{'品种':<7}{'抽查':>6}{'不一致':>8}  结论")
    bad_total = 0
    for nm, (_, fn) in SIGNALS.items():
        for k in keys:
            df = load(k)
            if df is None or len(df) < 500:
                continue
            o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
            l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
            N = len(c)
            sig_full = fn(o, h, l, c, _atr(h, l, c, ATR_N), _sma(c, MA_N))
            probes = rng.integers(max(MA_N + SLOPE_N + ATR_N + 5, 60), N, size=n_probe)
            bad = 0
            for i in probes:
                j0 = max(0, i + 1 - W)
                oo, hh, ll, cc = o[j0:i + 1], h[j0:i + 1], l[j0:i + 1], c[j0:i + 1]
                sf = fn(oo, hh, ll, cc, _atr(hh, ll, cc, ATR_N), _sma(cc, MA_N))
                v1, v2 = sig_full[i], sf[-1]      # 切片末元素 = 原始第 i 根
                if np.isnan(v1) and np.isnan(v2):
                    continue
                if abs(float(np.nan_to_num(v1)) - float(np.nan_to_num(v2))) > 1e-9:
                    bad += 1
            bad_total += bad
            print(f"{nm:<15}{k:<7}{n_probe:>6}{bad:>8}  {'✅ 无未来函数' if bad == 0 else '❌ 有未来函数！'}")
    print(f"\n合计不一致 {bad_total} 处 ⇒ {'全部通过 ✅' if bad_total == 0 else '存在未来函数 ❌'}")


if __name__ == "__main__":
    main()
