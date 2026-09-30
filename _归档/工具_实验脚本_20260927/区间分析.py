# -*- coding: utf-8 -*-
"""
区间特征分析：把一段回测区间按「趋势 / 震荡」切分成两段
==========================================================
原理（客观指标，不靠肉眼）：
  ER  (Kaufman 效率比) = |区间净涨跌| / 逐日涨跌绝对值之和
        -> 越接近 1 = 价格一路走、几乎不回头 = 趋势
        -> 越接近 0 = 涨涨跌跌原地打转   = 震荡
  ADX (14日 Wilder)   = 趋势强度，>25 偏趋势，<20 偏震荡
  RANGE_RATIO         = 区间振幅 / 平均价，衡量波动大小（辅助参考）

做法：在区间内扫描每个候选切点，分别算左段/右段的 ER+ADX，
      找出「一段最像趋势、另一段最像震荡」的切点，并列出前几名供挑选。

用法：改下面 SYMBOL / START / END，然后跑：
  python 工具/区间分析.py
"""
import sys
import os
from datetime import date

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np
import pandas as pd

from config_account import USER, PASSWORD
from tqsdk import TqApi, TqAuth

# ==================== 可调参数 ====================
SYMBOL = "KQ.m@DCE.v"        # 主连；换品种改这里（指数用 KQ.i@DCE.v）
NAME = "PVC"
START = date(2026, 3, 13)
END = date(2026, 9, 13)
WARMUP_DAYS = 40             # 指标预热用的前置日K根数
# 自定义分段对比：每项 = (名称, 起, 止)，按交易日闭区间；[] = 不跑
SEGMENTS = [
    ("5-7月",        date(2026, 5, 1),  date(2026, 7, 31)),
    ("7-9月",        date(2026, 7, 1),  date(2026, 9, 13)),
    ("6月单月",      date(2026, 6, 1),  date(2026, 6, 30)),
    ("3-7月(上一版)", date(2026, 3, 13), date(2026, 7, 1)),
]
# =================================================


def adx_wilder(high, low, close, n=14):
    """标准 Wilder ADX(14)，返回数组（前 n 个为 nan）"""
    h, l, c = np.asarray(high, float), np.asarray(low, float), np.asarray(close, float)
    m = len(c)
    tr = np.zeros(m)
    pdm = np.zeros(m)
    ndm = np.zeros(m)
    for i in range(1, m):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0
    atr = np.full(m, np.nan)
    pdi = np.full(m, np.nan)
    ndi = np.full(m, np.nan)
    dx = np.full(m, np.nan)
    adx = np.full(m, np.nan)
    if m <= n:
        return adx
    atr[n] = tr[1:n + 1].sum()
    sp, sn = pdm[1:n + 1].sum(), ndm[1:n + 1].sum()
    for i in range(n + 1, m):
        atr[i] = atr[i - 1] - atr[i - 1] / n + tr[i]
        sp = sp - sp / n + pdm[i]
        sn = sn - sn / n + ndm[i]
        if atr[i] > 0:
            pdi[i] = 100 * sp / atr[i]
            ndi[i] = 100 * sn / atr[i]
            s = pdi[i] + ndi[i]
            if s > 0:
                dx[i] = 100 * abs(pdi[i] - ndi[i]) / s
    first = n * 2
    if m > first:
        adx[first] = np.nanmean(dx[n + 1:first + 1])
        for i in range(first + 1, m):
            if not np.isnan(dx[i]):
                adx[i] = (adx[i - 1] * (n - 1) + dx[i]) / n
    return adx


def trend_r2(close_seg):
    """收盘价 vs 时间的线性回归 R^2：越接近 1 = 价格越像一条单向直线 = 越趋势"""
    y = np.asarray(close_seg, float)
    if len(y) < 3:
        return 0.0
    x = np.arange(len(y), dtype=float)
    r = np.corrcoef(x, y)[0, 1]
    return 0.0 if np.isnan(r) else float(r * r)


def seg_stats(close_seg, high_seg, low_seg, adx_seg):
    """单段特征"""
    close_seg = np.asarray(close_seg, float)
    diff = np.diff(close_seg)
    path = float(np.sum(np.abs(diff)))
    net = float(close_seg[-1] - close_seg[0])
    er = abs(net) / path if path > 0 else 0.0
    hi, lo = float(np.max(high_seg)), float(np.min(low_seg))
    mid = float(np.mean(close_seg))
    rng = (hi - lo) / mid if mid else 0.0
    a = float(np.nanmean(adx_seg)) if np.any(~np.isnan(adx_seg)) else np.nan
    r2 = trend_r2(close_seg)
    a_ = 0.0 if np.isnan(a) else min(a / 50.0, 1.0)   # ADX 归一化(50 视为满)
    score = 0.45 * r2 + 0.35 * er + 0.20 * a_          # 趋势性综合分(0~1)
    return dict(net=net, pct=net / close_seg[0] * 100, er=er, adx=a, rng=rng * 100,
                r2=r2, score=score, hi=hi, lo=lo, bars=len(close_seg))


def main():
    api = TqApi(auth=TqAuth(USER, PASSWORD))
    try:
        need = 400
        ks = api.get_kline_serial(SYMBOL, 24 * 60 * 60, data_length=need)
        while not api.is_changing(ks.iloc[-1], "datetime"):
            api.wait_update(deadline=__import__("time").time() + 30)
    finally:
        api.close()
    df = pd.DataFrame(ks).copy()
    df["dt"] = pd.to_datetime(df["datetime"]).dt.date
    df = df[["dt", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    df = df[(df["dt"] >= START) | (df["dt"].index < 0)]
    df = df.reset_index(drop=True)

    # 取评估区间 + 前置预热
    idx = df.index[df["dt"] >= START]
    if len(idx) == 0:
        print("区间内没有K线，检查日期/合约"); return
    i0 = max(0, idx[0] - WARMUP_DAYS)
    i1 = df.index[df["dt"] <= END][-1]
    sub = df.iloc[i0:i1 + 1].reset_index(drop=True)

    h = sub["high"].values.astype(float)
    l = sub["low"].values.astype(float)
    c = sub["close"].values.astype(float)
    a = adx_wilder(h, l, c, 14)

    eval_len = i1 - idx[0] + 1
    off = idx[0] - i0          # 评估起点在 sub 中的下标
    print("=" * 78)
    print(f"{NAME}  {SYMBOL}   评估区间 {START} ~ {END}  共 {eval_len} 根日K")
    print("=" * 78)
    print("【全区间特征】")
    s = seg_stats(c[off:], h[off:], l[off:], a[off:])
    print(f"  净涨跌 {s['net']:+.0f} 点 ({s['pct']:+.2f}%)   最高 {s['hi']:.0f}  最低 {s['lo']:.0f}  "
          f"振幅 {s['rng']:.1f}%")
    print(f"  效率比 ER = {s['er']:.3f}   R² = {s['r2']:.3f}   平均ADX = {s['adx']:.1f}   趋势性总分 = {s['score']:.3f}")

    print()
    print(f"【逐日收盘】（评估区间 {eval_len} 根，格式：日期 收 涨跌%）")
    line = []
    for k in range(off, len(c)):
        chg = (c[k] - c[k - 1]) / c[k - 1] * 100 if k > off else 0.0
        line.append(f"{sub['dt'].iloc[k]} {c[k]:.0f} {chg:+.2f}%")
    for j in range(0, len(line), 4):
        print("  " + "   |   ".join(line[j:j + 4]))

    print()
    print("【候选切点扫描】趋势性综合分 = 0.45·R² + 0.35·ER + 0.20·归一ADX（0~1，越大越趋势）")
    print("             每段至少 30 根日K；只找「一段明显更趋势、另一段明显更震荡」的切点")
    MIN_BARS = 30
    lo_i = off + MIN_BARS - 1
    hi_i = off + eval_len - MIN_BARS - 1
    rows = []
    for k in range(lo_i, hi_i + 1):
        L = seg_stats(c[off:k + 1], h[off:k + 1], l[off:k + 1], a[off:k + 1])
        R = seg_stats(c[k:], h[k:], l[k:], a[k:])
        rows.append((k, sub["dt"].iloc[k], L, R, L["score"] - R["score"]))

    def show(rs, title):
        print(f"  -- {title} --")
        for k, dt, L, R, d in rs[:6]:
            print(f"    切点 {dt} | 左: {L['pct']:+7.2f}%  R²={L['r2']:.2f} ER={L['er']:.2f} "
                  f"ADX={L['adx']:5.1f} 分={L['score']:.2f} ({L['bars']:3d}根)"
                  f"  ‖ 右: {R['pct']:+7.2f}%  R²={R['r2']:.2f} ER={R['er']:.2f} "
                  f"ADX={R['adx']:5.1f} 分={R['score']:.2f} ({R['bars']:3d}根)  Δ={d:+.3f}")

    show(sorted(rows, key=lambda x: -x[4]), "方案A：左段=趋势 / 右段=震荡")
    show(sorted(rows, key=lambda x: x[4]), "方案B：左段=震荡 / 右段=趋势")

    print()
    print("【关键候选切点明细】（看看挑哪根K线切最顺眼）")
    ma20 = pd.Series(c).rolling(20).mean().values

    def cross_stats(k0, k1):
        """收盘在 MA20 上方的天数占比 / MA20 穿越次数（每百根）"""
        seg, m = c[k0:k1 + 1], ma20[k0:k1 + 1]
        v = ~np.isnan(m)
        if v.sum() < 5:
            return float("nan"), float("nan")
        s, mm = seg[v], m[v]
        up = s > mm
        return up.mean() * 100, float(np.sum(up[1:] != up[:-1])) / len(s) * 100

    for d0 in (date(2026, 5, 19), date(2026, 6, 1), date(2026, 6, 30),
               date(2026, 7, 1), date(2026, 7, 6), date(2026, 7, 13), date(2026, 7, 31)):
        kk = sub.index[sub["dt"] == d0]
        if len(kk) == 0:
            continue
        k = int(kk[0])
        L = seg_stats(c[off:k + 1], h[off:k + 1], l[off:k + 1], a[off:k + 1])
        R = seg_stats(c[k:], h[k:], l[k:], a[k:])
        lu, lc = cross_stats(off, k)
        ru, rc = cross_stats(k, len(c) - 1)
        print(f"  切点 {d0} | 左 {L['bars']:3d}根 {c[off]:.0f}->{c[k]:.0f} ({L['pct']:+6.2f}%) "
              f"R²={L['r2']:.2f} ER={L['er']:.2f} ADX={L['adx']:5.1f} 分={L['score']:.2f} "
              f"MA20上方={lu:4.0f}% 穿越={lc:4.0f}/百根"
              f"  ‖ 右 {R['bars']:3d}根 {c[k]:.0f}->{c[-1]:.0f} ({R['pct']:+6.2f}%) "
              f"R²={R['r2']:.2f} ER={R['er']:.2f} ADX={R['adx']:5.1f} 分={R['score']:.2f} "
              f"MA20上方={ru:4.0f}% 穿越={rc:4.0f}/百根  Δ={L['score']-R['score']:+.3f}")

    # 月度视角
    print()
    print("【月度视角】每月净涨跌 / 月内ER / 月内R² / 月内ADX")
    mon = sub.iloc[off:].copy()
    mon["ym"] = [d.strftime("%Y-%m") for d in mon["dt"]]
    for ym, g in mon.groupby("ym"):
        gi = g.index
        ss = seg_stats(c[gi[0]:gi[-1] + 1], h[gi[0]:gi[-1] + 1], l[gi[0]:gi[-1] + 1], a[gi[0]:gi[-1] + 1])
        print(f"  {ym}: 收盘 {c[gi[0]]:.0f}->{c[gi[-1]]:.0f} ({ss['pct']:+6.2f}%)  "
              f"ER={ss['er']:.2f}  R²={ss['r2']:.2f}  ADX={ss['adx']:5.1f}  "
              f"区间 {ss['lo']:.0f}~{ss['hi']:.0f}  趋势分={ss['score']:.2f}")

    if SEGMENTS:
        print()
        print("【自定义分段对比】")
        H = df["high"].values.astype(float)
        L = df["low"].values.astype(float)
        C = df["close"].values.astype(float)
        A = adx_wilder(H, L, C, 14)
        M = pd.Series(C).rolling(20).mean().values
        for nm, s0, s1 in SEGMENTS:
            ii = [i for i in range(len(df)) if s0 <= df["dt"].iloc[i] <= s1]
            if len(ii) < 3:
                print(f"  {nm}: 区间内无数据")
                continue
            i0, i1 = ii[0], ii[-1]
            cs, hs, ls, as_ = C[i0:i1 + 1], H[i0:i1 + 1], L[i0:i1 + 1], A[i0:i1 + 1]
            ss = seg_stats(cs, hs, ls, as_)
            mm = M[i0:i1 + 1]
            v = ~np.isnan(mm)
            up = cr = float("nan")
            if v.sum() > 2:
                ab = cs[v] > mm[v]
                up = float(ab.mean() * 100)
                cr = float(np.sum(ab[1:] != ab[:-1]) / max(int(v.sum()), 1) * 100)
            print(f"  {nm:14s} {df['dt'].iloc[i0]}~{df['dt'].iloc[i1]} ({ss['bars']:3d}根)  "
                  f"{C[i0]:.0f}->{C[i1]:.0f} {ss['pct']:+7.2f}%  R²={ss['r2']:.2f} ER={ss['er']:.2f} "
                  f"ADX={ss['adx']:5.1f} 分={ss['score']:.2f} | MA20上方={up:4.0f}% "
                  f"穿越={cr:4.0f}/百根 | 轴 {ss['lo']:.0f}~{ss['hi']:.0f} 振幅{ss['rng']:.1f}%")

    # ==================== 导出分区走势图（SVG） ====================
    CUT = date(2026, 7, 1)                     # 推荐切点
    kk = sub.index[sub["dt"] == CUT]
    kc = int(kk[0]) if len(kk) else off + int(eval_len * 0.6)
    ma20 = pd.Series(c).rolling(20).mean().values
    N = len(c) - off
    pmin, pmax = float(np.min(l[off:])), float(np.max(h[off:]))
    pad = (pmax - pmin) * 0.07
    pmin -= pad
    pmax += pad
    X0, X1, Y0, Y1 = 46.0, 634.0, 66.0, 320.0

    def X(i):
        return X0 + (i - off) / (N - 1) * (X1 - X0)

    def Y(p):
        return Y1 - (p - pmin) / (pmax - pmin) * (Y1 - Y0)

    pts = " ".join(f"{X(i):.1f},{Y(c[i]):.1f}" for i in range(off, len(c)))
    mpts = " ".join(f"{X(i):.1f},{Y(ma20[i]):.1f}" for i in range(off, len(c)) if not np.isnan(ma20[i]))
    Ls, Rs = seg_stats(c[off:kc + 1], h[off:kc + 1], l[off:kc + 1], a[off:kc + 1]), \
             seg_stats(c[kc:], h[kc:], l[kc:], a[kc:])
    s = []
    s.append('<svg viewBox="0 0 680 380" width="100%" xmlns="http://www.w3.org/2000/svg" '
             'role="img" font-family="var(--font-sans)">')
    s.append('<title>PVC主力日K分区图</title>')
    s.append('<desc>2026年3月13日至9月13日PVC主力日K收盘价走势，以7月1日为界分为左侧下跌趋势段和右侧底部震荡段。</desc>')
    s.append('<defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" '
             'markerHeight="6" orient="auto-start-reverse"><path d="M2 1L8 5L2 9" fill="none" '
             'stroke="context-stroke" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'
             '</marker></defs>')
    # 分区底色
    s.append(f'<rect x="{X0}" y="{Y0}" width="{X(kc)-X0:.1f}" height="{Y1-Y0}" '
             f'fill="var(--color-background-danger)" opacity="0.25"/>')
    s.append(f'<rect x="{X(kc):.1f}" y="{Y0}" width="{X1-X(kc):.1f}" height="{Y1-Y0}" '
             f'fill="var(--color-background-warning)" opacity="0.25"/>')
    # 网格 + 价格刻度
    for gv in np.linspace(pmin + pad, pmax - pad, 4):
        s.append(f'<line x1="{X0}" y1="{Y(gv):.1f}" x2="{X1}" y2="{Y(gv):.1f}" '
                 f'stroke="var(--color-border-tertiary)" stroke-width="0.5"/>')
        s.append(f'<text x="{X0+4}" y="{Y(gv)-4:.1f}" font-size="11" '
                 f'fill="var(--color-text-tertiary)">{gv:.0f}</text>')
    # 月份刻度
    prev = None
    for i in range(off, len(c)):
        ym = sub["dt"].iloc[i].strftime("%Y-%m")
        if ym != prev:
            s.append(f'<line x1="{X(i):.1f}" y1="{Y0}" x2="{X(i):.1f}" y2="{Y1}" '
                     f'stroke="var(--color-border-tertiary)" stroke-width="0.5"/>')
            s.append(f'<text x="{X(i):.1f}" y="{Y1+16}" font-size="11" text-anchor="middle" '
                     f'fill="var(--color-text-tertiary)">{sub["dt"].iloc[i].strftime("%m月")}</text>')
            prev = ym
    # 价格线 / 均线
    s.append(f'<polyline points="{pts}" fill="none" stroke="var(--color-text-primary)" stroke-width="1.6"/>')
    s.append(f'<polyline points="{mpts}" fill="none" stroke="var(--color-text-secondary)" '
             f'stroke-width="1" stroke-dasharray="4 3"/>')
    # 切点竖线
    s.append(f'<line x1="{X(kc):.1f}" y1="{Y0}" x2="{X(kc):.1f}" y2="{Y1}" '
             f'stroke="var(--color-text-secondary)" stroke-width="1" stroke-dasharray="3 3"/>')
    s.append(f'<text x="{X(kc)-6:.1f}" y="{Y0+14}" font-size="11" text-anchor="end" '
             f'fill="var(--color-text-secondary)">切点 07-01</text>')
    # 极值点
    imin = int(np.argmin(c[off:])) + off
    s.append(f'<circle cx="{X(imin):.1f}" cy="{Y(c[imin]):.1f}" r="3" fill="none" '
             f'stroke="var(--color-text-primary)" stroke-width="1"/>')
    s.append(f'<text x="{X(imin)+6:.1f}" y="{Y(c[imin])+14:.1f}" font-size="11" '
             f'fill="var(--color-text-secondary)">低点 {c[imin]:.0f}</text>')
    # 标题与图例
    s.append(f'<text x="{X0}" y="24" font-size="14" font-weight="500" '
             f'fill="var(--color-text-primary)">{NAME} 主力日K · {START} → {END}</text>')
    s.append(f'<text x="{X0}" y="46" font-size="12" fill="var(--color-text-secondary)">'
             f'① 趋势段 {Ls["bars"]}根 {Ls["pct"]:+.1f}% · R²={Ls["r2"]:.2f} · ER={Ls["er"]:.2f}'
             f'   ｜   ② 震荡段 {Rs["bars"]}根 {Rs["pct"]:+.1f}% · R²={Rs["r2"]:.2f} · ER={Rs["er"]:.2f}</text>')
    s.append(f'<text x="{X0}" y="{Y1+34}" font-size="11" fill="var(--color-text-tertiary)">'
             f'实线=日收盘 · 虚线=MA20 · 红底=趋势段 · 黄底=震荡段</text>')
    s.append('</svg>')
    out_dir = os.path.join(_ROOT, "工具", "_输出")
    os.makedirs(out_dir, exist_ok=True)
    out_svg = os.path.join(out_dir, f"{NAME}_日K分区.svg")
    with open(out_svg, "w", encoding="utf-8") as f:
        f.write("\n".join(s))
    print()
    print(f"[图] 已导出 {out_svg}")


if __name__ == "__main__":
    main()
