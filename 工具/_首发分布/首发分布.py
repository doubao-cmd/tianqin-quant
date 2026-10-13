# -*- coding: utf-8 -*-
"""
首发分布解析 与 随机进场对照
==============================================================================
工具定位
--------
把论文《期货日内价格事件的条件概率与短周期交易研究》§5.2 / §6 的
「离散三跳边界 → 吸收马尔可夫链」框架，落到本项目的真实 5min 数据上，
回答一个具体问题：

    「12 根持有 + 2×ATR 止损」下 **46% 的止损率**，是策略缺陷，还是几何必然？

三块内容
--------
  A. 解析递推（论文 §5.2）
     给定 (u, d, p0, 上行格数 up, 下行格数 dn, 步数 H)，用动态规划算
     p_止盈 / p_止损 / 存活分布；再用同参数蒙特卡洛核验（论文 §12 的做法）。
     ⇒ 自检锚：论文示例 (u,d,p0)=(0.42,0.28,0.30)、±3 格、H=20
        应给出 p_止盈=0.666180235、p_止损=0.136413029（可逐位比对）。

  B. 随机进场对照（本工具的核心检验）
     把「进场体检」的 L1 逻辑**原样搬来**，分别跑
       ① 信号触发点（真实）  ② 随机进场点（对照，保留多空比例）
     若两者一致 ⇒ 信号没有改变「进场之后的路径结构」。
     这比「L1 净利 < 0」更进一步：它直接量化「信号时点」相对随机时点的增益。

  C. 边界敏感性（论文 §6 表 3 的对应物）
     扫 (止损倍数 × 持有步数)，给出理论止损率表 ⇒ 回答
     「要让止损率降到 X%，该放宽止损还是缩短持有期」。

口径（与 工具/_进场体检/进场体检.py **完全一致**，直接 import 复用）
--------------------------------------------------------------------
  HOLD=12 根(5min) · 止损=2.0×ATR · 滑点=1 跳 · 固定 1 手 · 成本含双边。
  数据复用 工具/_进场体检/数据/*.csv（同一份文件，保证可比）。

⚠️ 纪律
  1. 窗口 2021-2023 **已污染** ⇒ 本工具只能判死、不能判生。
  2. 本工具只做诊断：**不产出交易规则、不改任何策略参数**（规则 2）。
  3. 随机种子固定 ⇒ 结果可复现。

用法
----
  python 首发分布.py                    # 全 12 品种
  python 首发分布.py --syms JM,M        # 指定品种
  python 首发分布.py --delta 0.5        # 格宽（ATR 单位，默认 0.5）
  python 首发分布.py --selftest         # 只跑「论文复现 + 解析 vs MC」核验
"""
import os
import sys
import json
import math
import argparse
import importlib.util

import numpy as np
import pandas as pd

# ============================================================ 找项目根 + 复用进场体检
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_symbols as cs  # noqa: E402

_EP_PATH = os.path.join(_ROOT, "工具", "_进场体检", "进场体检.py")
if not os.path.exists(_EP_PATH):
    raise SystemExit(f"[FATAL] 找不到进场体检工具（同口径依赖）：{_EP_PATH}")
_spec = importlib.util.spec_from_file_location("entry_screen", _EP_PATH)
es = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(es)

# ============================================================ 冻结口径
DELTA = 0.5           # 格宽（ATR 单位）。止损 2.0ATR ⇒ 4 格（论文「三跳」的对应物）
STOP_ATR = 2.0        # 止损（ATR 倍数），与进场体检一致
HOLD = 12             # 持有步数（= 根），与进场体检一致
MC_N = 200_000        # 蒙特卡洛次数（论文 §12 用 20 万）
CTRL_REPEAT = 20      # 随机进场对照重复次数
SEED = 20261013

SYMS = ["EB", "JD", "JM", "LH", "MA", "M", "P", "RB", "RU", "SA", "SR", "V"]


# ============================================================ A. 解析递推
def dp_absorb(u, d, p0, up, dn, H):
    """双边吸收的离散三跳随机游走（论文 §5.2）。

    状态 k ∈ {−(dn−1), ..., up−1}（内部态），k=0 为入场点；
    每步：+1 概率 u / −1 概率 d / 不动概率 p0；
    触及 k=+up ⇒ 吸收（止盈）；触及 k=−dn ⇒ 吸收（止损）。

    返回 (p_up, p_dn, alive_final, pos)
      p_up / p_dn : H 步内「先到」上/下边界的概率（首次穿越）
      alive_final : H 步后仍存活（未触边界）的概率分布
      pos         : 对应的位置坐标（格）
    """
    lo, hi = -(dn - 1), (up - 1)
    n = hi - lo + 1
    alive = np.zeros(n)
    alive[0 - lo] = 1.0                      # k=0
    pos = np.arange(lo, hi + 1, dtype=float)
    p_up = p_dn = 0.0
    for _ in range(H):
        p_up += u * alive[-1]                # 从上边界内侧再上行 ⇒ 触上边界
        p_dn += d * alive[0]                 # 从下边界内侧再下行 ⇒ 触下边界
        nxt = p0 * alive
        nxt[1:] += u * alive[:-1]            # 上行：k-1 → k
        nxt[:-1] += d * alive[1:]            # 下行：k+1 → k
        alive = nxt                          # alive[0] 的下行已计入 p_dn，不再进入 nxt
    return p_up, p_dn, alive, pos


def mc_walk(u, d, p0, up, dn, H, n_sim, rng):
    """同参数蒙特卡洛（论文 §12 的核验做法）：验证 DP 的实现正确性。"""
    steps = rng.choice(np.array([1, -1, 0]), size=(n_sim, H), p=[u, d, p0])
    path = np.cumsum(steps, axis=1)
    up_hit = (path >= up).any(axis=1)
    dn_hit = (path <= -dn).any(axis=1)
    # 首次穿越优先（同时不可能同时触发，除非 H=0）
    first_up = np.where(up_hit, np.argmax(path >= up, axis=1), 10 ** 6)
    first_dn = np.where(dn_hit, np.argmax(path <= -dn, axis=1), 10 ** 6)
    p_up = float(np.mean(first_up < first_dn))
    p_dn = float(np.mean(first_dn < first_up))
    return p_up, p_dn


def estimate_uvd(r, delta):
    """三分类估计（论文 §5.1）：|Δ| < δ/2 记为「不动」。"""
    thr = delta / 2.0
    u = float(np.mean(r >= thr))
    d = float(np.mean(r <= -thr))
    return u, d, 1.0 - u - d


# ============================================================ B. L1 评估（与进场体检同口径）
def l1_eval(o, h, l, c, atr, idx, sgn, key, stop_mult, H):
    """逐笔 L1：进场在 open[i+1]，止损 = entry ∓ stop_mult×ATR[i]，持有 H 根。

    与 工具/_进场体检/进场体检.py 的 screen_one() 逐行同口径
    （止损成交价取 min(stop, open) / max(stop, open) —— 保守侧）。
    """
    if len(idx) == 0:
        return None
    entry = o[idx + 1]
    stop = np.where(sgn > 0, entry - stop_mult * atr[idx], entry + stop_mult * atr[idx])
    exit_px = o[idx + 1 + H].copy()
    hit = np.zeros(len(idx), dtype=bool)
    for j, (e_i, st, s) in enumerate(zip(idx, stop, sgn)):
        for jj in range(e_i + 1, e_i + 1 + H):
            if s > 0:
                if l[jj] <= st:
                    exit_px[j] = min(st, o[jj]); hit[j] = True; break
            else:
                if h[jj] >= st:
                    exit_px[j] = max(st, o[jj]); hit[j] = True; break
    gross = (exit_px - entry) * sgn
    return {
        "n": int(len(idx)),
        "gross": float(np.mean(gross)),
        "gross_sd": float(np.std(gross, ddof=1)) if len(gross) > 1 else 0.0,
        "stop_share": float(np.mean(hit)),
    }


# ============================================================ 单品种分析
def analyze_one(key, delta, stop_mult, H, mc_n, rng, verbose=True):
    df = es.load(key)
    if df is None or len(df) < 500:
        return None
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    N = len(c)
    atr = es._atr(h, l, c, es.ATR_N)
    ma = es._sma(c, es.MA_N)

    # ---- 每步位移（ATR 单位），用于解析模型的 (u,d,p0) 估计
    den = atr[:-1].copy()
    den[(~np.isfinite(den)) | (den <= 0)] = np.nan
    r = (c[1:] - c[:-1]) / den
    r = r[np.isfinite(r)]
    u, d, p0 = estimate_uvd(r, delta)

    # ---- 解析 + MC 核验（用真实估计的 u/d/p0；上行取 H+2 ⇒ H 步内永不触及上界）
    # ⚠️ dp_absorb 返回 (p_上界, p_下界, alive, pos)；单边止损场景必须取**第二个**。
    ell = max(1, int(round(stop_mult / delta)))
    _p_up_dp, p_stop_dp, alive_f, _pos_f = dp_absorb(u, d, p0, up=H + 2, dn=ell, H=H)
    _, p_dn_mc = mc_walk(u, d, p0, up=H + 2, dn=ell, H=H, n_sim=mc_n, rng=rng)
    # 模型失配度：一步位移 > 1 格的占比（越高 ⇒「每步 ±1 格」的简化越不准）
    frac_multi = float(np.mean(np.abs(r) > delta))

    # ---- B. 随机进场对照（复用进场体检的信号）
    sig_name = "retrace_ma13"          # 基准锚 = 06 的进场条件本身
    sig = es.SIGNALS[sig_name][1](o, h, l, c, atr, ma)
    bad = np.isnan(atr) | np.isnan(ma)
    sig = np.where(bad, 0.0, np.nan_to_num(sig, nan=0.0))
    valid = np.arange(0, N - H - 1)
    idx_real = valid[sig[valid] != 0]
    sgn_real = sig[idx_real]
    real = l1_eval(o, h, l, c, atr, idx_real, sgn_real, key, stop_mult, H)

    ctrl_g, ctrl_s = [], []
    if real is not None and len(idx_real) > 0:
        for _ in range(CTRL_REPEAT):
            idx_c = rng.choice(valid, size=len(idx_real), replace=False)
            sgn_c = rng.permutation(sgn_real)          # 保留多空比例，只随机化「时点」
            res = l1_eval(o, h, l, c, atr, idx_c, sgn_c, key, stop_mult, H)
            if res:
                ctrl_g.append(res["gross"]); ctrl_s.append(res["stop_share"])

    cp = es.cost_points(key)
    out = {
        "key": key, "name_cn": cs.get(key)[0],
        "n_bar": int(N),
        "u": round(u, 4), "d": round(d, 4), "p0": round(p0, 4),
        "skew_u_share": round(u / (u + d), 4) if (u + d) > 0 else None,
        "ell_grid": ell,
        "p_stop_dp": round(p_stop_dp, 6),
        "p_stop_mc": round(p_dn_mc, 6),
        "dp_mc_gap": round(abs(p_stop_dp - p_dn_mc), 6),
        "frac_gt_1grid": round(frac_multi, 4),
        "cost_pts": round(cp, 3),
        "real_n": (real or {}).get("n", 0),
        "real_gross": round(real["gross"], 4) if real else None,
        "real_stop": round(real["stop_share"], 4) if real else None,
        "ctrl_gross": round(float(np.mean(ctrl_g)), 4) if ctrl_g else None,
        "ctrl_stop": round(float(np.mean(ctrl_s)), 4) if ctrl_s else None,
        "ctrl_gross_sd": round(float(np.std(ctrl_g, ddof=1)), 4) if len(ctrl_g) > 1 else None,
    }
    out["edge_gross"] = (round(out["real_gross"] - out["ctrl_gross"], 4)
                         if (out["real_gross"] is not None and out["ctrl_gross"] is not None) else None)

    # ---- D. 检验功效（论文 §11.3）：给定现有样本量，能检测出多大的效应？
    _n = int((real or {}).get("n") or 0)
    _sd = float((real or {}).get("gross_sd") or 0.0)
    _t = (real["gross"] / (_sd / math.sqrt(_n)) if (_n > 1 and _sd > 0) else None)
    _mde = min_detectable_effect(_n, _sd) if (_n > 1 and _sd > 0) else None
    _tradable = 2.0 * cp          # 「值得交易」的毛效应门槛（毛/成本 ≥ 2）
    out["gross_sd"] = round(_sd, 4) if _sd > 0 else None
    out["t"] = round(_t, 2) if _t is not None else None
    out["mde_pts"] = round(_mde, 4) if _mde else None
    out["tradable_pts"] = round(_tradable, 3)
    out["mde_over_tradable"] = (round(_mde / _tradable, 4)
                                if (_mde and _tradable > 0) else None)
    if verbose:
        print(f"  {key:<3}{out['name_cn']:<6} "
              f"u/d/p0={out['u']:.3f}/{out['d']:.3f}/{out['p0']:.3f}  "
              f"解析p_stop={out['p_stop_dp']:.4f} MC={out['p_stop_mc']:.4f}(Δ{out['dp_mc_gap']:.5f})  "
              f"真实止损={out['real_stop']} 对照止损={out['ctrl_stop']}", flush=True)
    return out


# ============================================================ C. 边界敏感性
def boundary_table(u, d, p0, delta, stops, holds):
    rows = []
    for sm in stops:
        ell = max(1, int(round(sm / delta)))
        for H in holds:
            _, p_stop, _, _ = dp_absorb(u, d, p0, up=H + 2, dn=ell, H=H)
            rows.append({"stop_atr": sm, "hold": H, "grids": ell,
                         "p_stop": round(p_stop, 4)})
    return rows


# ============================================================ D. 检验功效与样本量（论文 §11.3）
def _norm_ppf(p):
    """标准正态分位数（Acklam 有理近似，绝对误差 < 1.2e-9，不依赖 scipy）。"""
    if not (0.0 < p < 1.0):
        raise ValueError("p 必须落在 (0,1)")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)


def required_n(delta, sigma, alpha=0.05, power=0.80):
    """论文 (84)：双侧 alpha、功效 power 下，识别均值优势 delta 所需样本量。

        n ≈ (z_{1−α/2} + z_power)² / (Δ/σ)²
    """
    if sigma <= 0 or delta == 0:
        return float("inf")
    z = _norm_ppf(1.0 - alpha / 2.0) + _norm_ppf(power)
    return (z * sigma / abs(delta)) ** 2


def min_detectable_effect(n, sigma, alpha=0.05, power=0.80):
    """反解：给定样本量 n，本检验能识别出来的最小效应（与原数据同单位）。

        Δ_min = (z_{1−α/2} + z_power) · σ / √n
    """
    if n <= 0:
        return float("inf")
    z = _norm_ppf(1.0 - alpha / 2.0) + _norm_ppf(power)
    return z * sigma / math.sqrt(n)


def effective_n(n, rho):
    """论文 (85)：AR(1) 相关系数 rho 下的有效样本量  n_eff ≈ n·(1−ρ)/(1+ρ)。"""
    if rho >= 1.0:
        return 0.0
    return n * (1.0 - rho) / (1.0 + rho)


# ============================================================ 自检
def selftest():
    """两层自检：①能否复现论文 §5.2 的解析解；②解析 DP 与蒙特卡洛是否一致。"""
    u, d, p0 = 0.42, 0.28, 0.30
    print("=" * 78)
    print(f"自检 1：复现论文 §5.2 示例  (u,d,p0)=({u},{d},{p0}), ±3 格, H=20")
    print("=" * 78)
    ok1 = True

    # (a) 解析锚：第 3 步「全上行 / 全下行」的发生率必须恰为 u³ / d³
    pu3, pd3, _, _ = dp_absorb(u, d, p0, up=3, dn=3, H=3)
    a1 = abs(pu3 - u ** 3) < 1e-12 and abs(pd3 - d ** 3) < 1e-12
    ok1 = ok1 and a1
    print(f"  (a) 3 步累计：止盈={pu3:.6f}（u³={u**3:.6f}） 止损={pd3:.6f}"
          f"（d³={d**3:.6f}）  {'✅' if a1 else '❌'}")

    # (b) 20 步 p_止盈 必须 9 位复现论文正文
    pu20, pd20, alive, _ = dp_absorb(u, d, p0, up=3, dn=3, H=20)
    surv = float(alive.sum())
    a2 = abs(pu20 - 0.666180235) < 1e-9
    ok1 = ok1 and a2
    print(f"  (b) 20 步 p_止盈 = {pu20:.9f}（论文正文 0.666180235）  {'✅' if a2 else '❌'}")

    # (c) 归一：止盈 + 止损 + 未触及 = 1
    a3 = abs(pu20 + pd20 + surv - 1.0) < 1e-12
    ok1 = ok1 and a3
    print(f"  (c) 20 步归一：{pu20:.6f} + {pd20:.6f} + {surv:.6f} = "
          f"{pu20 + pd20 + surv:.9f}  {'✅' if a3 else '❌'}")

    # (d) 累计概率序列单调不减（论文表 2 的基本性质）
    seq = [dp_absorb(u, d, p0, up=3, dn=3, H=h)[:2] for h in range(1, 21)]
    mono = all(seq[i][0] <= seq[i + 1][0] + 1e-15 and seq[i][1] <= seq[i + 1][1] + 1e-15
               for i in range(len(seq) - 1))
    ok1 = ok1 and mono
    print(f"  (d) 累计止盈/止损序列单调不减  {'✅' if mono else '❌'}")

    print(f"  ⚠️ 口径说明：论文正文写「20 步 p_R = 0.136413029」，但该值与论文自己表 2 的")
    print(f"     「累计止损」列（本工具 {pd20:.6f}）不符，反而与表 2「未触及」列（{surv:.6f}）")
    print(f"     同量级 ⇒ 判为**论文正文的列名标注问题**。本工具以表 2 的逐步递推为准")
    print(f"     （第 3 步 0.074088 / 0.021952 与论文逐位一致 ⇒ 实现正确）。")

    print()
    print("=" * 78)
    print("自检 2：解析 DP vs 蒙特卡洛（20 万次，固定种子）—— 阈值取 3 倍 MC 标准误")
    print("=" * 78)
    rng = np.random.default_rng(SEED)
    cases = [(0.42, 0.28, 0.30, 3, 20), (0.35, 0.35, 0.30, 4, 12),
             (0.30, 0.30, 0.40, 4, 12), (0.25, 0.45, 0.30, 4, 12)]
    ok2 = True
    for (uu, dd, pp, dn, H) in cases:
        _, pd_, _, _ = dp_absorb(uu, dd, pp, up=H + 2, dn=dn, H=H)
        _, pdm = mc_walk(uu, dd, pp, up=H + 2, dn=dn, H=H, n_sim=MC_N, rng=rng)
        se = math.sqrt(max(pdm * (1.0 - pdm), 1e-9) / MC_N)
        gap = abs(pd_ - pdm)
        good = gap < 3.0 * se + 1e-9
        ok2 = ok2 and good
        print(f"  u/d/p0=({uu},{dd},{pp}) 止损{dn}格 H={H:>2}: DP={pd_:.6f} MC={pdm:.6f} "
              f"Δ={gap:.6f}（3SE={3*se:.6f}）  {'✅' if good else '❌'}")

    print()
    print("=" * 78)
    print("自检 3：复现论文 §11.3 功效公式")
    print("=" * 78)
    n_ref = required_n(delta=0.1, sigma=2.6)
    a_n = abs(n_ref - 5306) < 3.0
    print(f"  (a) required_n(Δ=0.1, σ=2.6) = {n_ref:.1f}（论文 5306）  {'✅' if a_n else '❌'}")
    ratio_e = effective_n(1000.0, 0.30) / 1000.0
    a_e = abs(ratio_e - 0.5385) < 1e-4
    print(f"  (b) n_eff/n（ρ=0.3）= {ratio_e:.4f}（论文 0.5385）  {'✅' if a_e else '❌'}")
    return ok1 and ok2 and a_n and a_e


# ============================================================ 主程序
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--syms", default=",".join(SYMS))
    ap.add_argument("--delta", type=float, default=DELTA)
    ap.add_argument("--stop", type=float, default=STOP_ATR)
    ap.add_argument("--hold", type=int, default=HOLD)
    ap.add_argument("--mc", type=int, default=MC_N)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        ok = selftest()
        print(f"\n[selftest] {'全部通过 ✅' if ok else '存在失败 ❌'}")
        sys.exit(0 if ok else 1)

    ok = selftest()
    print()

    rng = np.random.default_rng(SEED)
    keys = [k.strip() for k in a.syms.split(",") if k.strip()]
    print("=" * 78)
    print(f"逐品种  ｜ δ={a.delta}ATR/格 ｜ 止损={a.stop}ATR（{int(round(a.stop/a.delta))}格）"
          f" ｜ 持有={a.hold} 根 ｜ 窗口 2021-2023（**已污染，只能判死**）")
    print("=" * 78)
    rows = []
    for k in keys:
        r = analyze_one(k, a.delta, a.stop, a.hold, a.mc, rng)
        if r:
            rows.append(r)

    # ---- C. 边界敏感性（用全样本平均 (u,d,p0) 作「典型品种」）
    if rows:
        u_avg = float(np.mean([r["u"] for r in rows]))
        d_avg = float(np.mean([r["d"] for r in rows]))
        p0_avg = 1.0 - u_avg - d_avg
        print()
        print("=" * 78)
        print(f"边界敏感性（典型品种 u/d/p0 = {u_avg:.3f}/{d_avg:.3f}/{p0_avg:.3f}）"
              f"：理论 p_止损")
        print("=" * 78)
        stops = [1.0, 1.5, 2.0, 3.0, 4.0]
        holds = [3, 6, 12, 24, 48]
        print("  止损\\持有 " + "".join(f"{h:>9}" for h in holds))
        tbl_rows = boundary_table(u_avg, d_avg, p0_avg, a.delta, stops, holds)
        for sm in stops:
            line = f"  {sm:>5.1f}ATR "
            for h in holds:
                v = [t for t in tbl_rows if t["stop_atr"] == sm and t["hold"] == h][0]["p_stop"]
                line += f"{v:>9.4f}"
            print(line)
        print("  ⇒ 止损率由「止损距离 ÷ √持有步数」的比值主导：放宽止损或缩短持有期都能降它。")
        avg_multi = float(np.mean([r["frac_gt_1grid"] for r in rows]))
        print(f"  ⚠️ 解析模型假设「每步至多 1 格（{a.delta} ATR）」，但真实 5min bar 有 "
              f"{avg_multi*100:.1f}% 的步位移 > 1 格，")
        print(f"     且 bar 内还有 high/low 波动 ⇒ 解析值系统性**低于**真实止损率。")
        print(f"     「路径结构」的真正检验看 B 列（真实 vs 对照）：两者差距才是信号的价值。")
        print()
        print("=" * 78)
        print("D 检验功效（论文 §11.3）：现有样本量能检测出多大的效应？")
        print("=" * 78)
        mdes = [r["mde_pts"] for r in rows if r["mde_pts"] is not None]
        ratios = [r["mde_over_tradable"] for r in rows if r["mde_over_tradable"] is not None]
        if mdes:
            print(f"  最小可检测效应 均值      : {np.mean(mdes):.4f} 点/笔")
            print(f"  「值得交易」门槛 均值     : "
                  f"{np.mean([r['tradable_pts'] for r in rows]):.3f} 点/笔（= 2×往返成本）")
            print(f"  mde ÷ 门槛 均值          : {np.mean(ratios):.4f}")
            verdict = ("**功效充足**：真有能覆盖成本的效应，这个样本量必然检出"
                       if np.mean(ratios) < 0.5 else "⚠️ 功效可能不足，结论需谨慎")
            print(f"  ⇒ {verdict}")

    # ---- 落盘
    out_md = os.path.join(_HERE, "首发分布结果.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("# 首发分布解析 与 随机进场对照\n\n")
        f.write(f"> δ={a.delta} ATR/格 ｜ 止损 {a.stop}×ATR（{int(round(a.stop/a.delta))} 格）"
                f" ｜ 持有 {a.hold} 根 ｜ 窗口 2021-2023（**已污染，只能判死**）\n")
        f.write("> 数据/口径与 `工具/_进场体检/` 完全一致（同一份 CSV、同一 `_atr/_sma`、同一 L1 逻辑）\n\n")
        f.write("## A+B 逐品种\n\n")
        f.write("| 品种 | n_bar | u | d | p0 | 上行占比 | 解析 p_止损 | MC p_止损 | Δ | 跨格占比 | "
                "真实止损 | 对照止损 | 真实毛 | 对照毛 | **edge(毛)** |\n")
        f.write("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
        for r in rows:
            f.write(f"| {r['name_cn']} | {r['n_bar']} | {r['u']} | {r['d']} | {r['p0']} | "
                    f"{r['skew_u_share']} | {r['p_stop_dp']} | {r['p_stop_mc']} | {r['dp_mc_gap']} | "
                    f"{r['frac_gt_1grid']} | "
                    f"{r['real_stop']} | {r['ctrl_stop']} | {r['real_gross']} | {r['ctrl_gross']} | "
                    f"**{r['edge_gross']}** |\n")
        if rows:
            f.write("\n## C 边界敏感性（典型品种）\n\n")
            f.write("| 止损\\持有 | " + " | ".join(str(h) for h in holds) + " |\n")
            f.write("|---|" + "---|" * len(holds) + "\n")
            for sm in stops:
                cells = []
                for h in holds:
                    v = [t for t in tbl_rows if t["stop_atr"] == sm and t["hold"] == h][0]["p_stop"]
                    cells.append(f"{v:.4f}")
                f.write(f"| {sm:.1f}ATR | " + " | ".join(cells) + " |\n")
            eg = [r["edge_gross"] for r in rows if r["edge_gross"] is not None]
            rs = [r["real_stop"] for r in rows if r["real_stop"] is not None]
            cs2 = [r["ctrl_stop"] for r in rows if r["ctrl_stop"] is not None]
            ps = [r["p_stop_dp"] for r in rows]
            skews = [r["skew_u_share"] for r in rows if r["skew_u_share"] is not None]
            if eg:
                sd = float(np.std(eg, ddof=1))
                t_e = float(np.mean(eg)) / (sd / math.sqrt(len(eg))) if sd > 0 else 0.0
                n_pos = sum(1 for x in eg if x > 0)
                f.write("\n## 汇总\n\n")
                f.write(f"- 解析 p_止损 均值（纯随机游走预测）：**{np.mean(ps):.4f}**\n")
                f.write(f"- 真实（信号）止损率均值：**{np.mean(rs):.4f}**\n")
                f.write(f"- 对照（随机时点）止损率均值：**{np.mean(cs2):.4f}**\n")
                f.write(f"- 信号相对随机时点的毛增益：**{np.mean(eg):+.4f} 点/笔**"
                        f"（跨品种 t={t_e:+.2f} ⇒ {'显著' if abs(t_e) >= 2 else '**不显著**'}）\n")
                f.write(f"- edge 符号：正 {n_pos} / 负 {len(eg) - n_pos} 个 ⇒ "
                        f"{'一致' if n_pos in (0, len(eg)) else '**不一致（噪声特征）**'}\n")
                f.write(f"- 上行占比 u/(u+d) 均值：**{np.mean(skews):.4f}**"
                        f"（≈0.5 ⇒ 5min 涨跌无方向偏差）\n")
            f.write("\n## D 检验功效（论文 §11.3）：现有样本量能检测出多大的效应\n\n")
            f.write("| 品种 | n | L1 毛均(点) | L1 毛 sd | t | **最小可检测效应**(点) | "
                    "「值得交易」门槛(点)=2×成本 | mde÷门槛 |\n")
            f.write("|---|---:|---:|---:|---:|---:|---:|---:|\n")
            for r in rows:
                f.write(f"| {r['name_cn']} | {r['real_n']} | {r['real_gross']} | {r['gross_sd']} | "
                        f"{r['t']} | **{r['mde_pts']}** | {r['tradable_pts']} | "
                        f"{r['mde_over_tradable']} |\n")
    print(f"\n[done] -> {out_md}")

    # ---- 汇总结论
    if rows:
        eg = [r["edge_gross"] for r in rows if r["edge_gross"] is not None]
        rs = [r["real_stop"] for r in rows if r["real_stop"] is not None]
        cs_ = [r["ctrl_stop"] for r in rows if r["ctrl_stop"] is not None]
        ps = [r["p_stop_dp"] for r in rows]
        print()
        print("=" * 78)
        print("汇总")
        print("=" * 78)
        print(f"  解析 p_止损 均值       : {np.mean(ps):.4f}  （纯随机游走预测）")
        print(f"  真实（信号）止损率均值  : {np.mean(rs):.4f}")
        print(f"  对照（随机）止损率均值  : {np.mean(cs_):.4f}")
        sd = float(np.std(eg, ddof=1))
        t_e = float(np.mean(eg)) / (sd / math.sqrt(len(eg))) if sd > 0 else 0.0
        n_pos = sum(1 for x in eg if x > 0)
        print(f"  信号相对随机时点的毛增益 : {np.mean(eg):+.4f} 点/笔"
              f"（跨品种 t={t_e:+.2f} ⇒ {'显著' if abs(t_e) >= 2 else '**不显著**'}）")
        print(f"  edge 符号一致性          : 正 {n_pos} / 负 {len(eg) - n_pos} 个 ⇒ "
              f"{'一致' if n_pos in (0, len(eg)) else '**不一致（噪声特征）**'}")
        print(f"  上行占比 u/(u+d) 均值    : "
              f"{np.mean([r['skew_u_share'] for r in rows]):.4f}"
              f"（≈0.5 ⇒ 5min 涨跌无方向偏差）")
        print(f"  自检（论文复现+DP/MC）  : {'通过 ✅' if ok else '失败 ❌'}")


if __name__ == "__main__":
    main()
