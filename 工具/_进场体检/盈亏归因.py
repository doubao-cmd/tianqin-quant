# -*- coding: utf-8 -*-
"""
盈亏归因 —— 回答「这套战法的盈亏到底在哪里产生」
==============================================================================
对任一进场信号跑 L1（进场 + 固定 ATR 止损 + 固定持有期），把**每一笔**的
毛盈亏 / 成本 / 净盈亏拆到几个维度上，回答「钱从哪来、亏在哪」：

  ① 总览      ：毛合计 vs 成本合计（`毛÷成本` 是判断「死了没有」的第一位数）
  ② 出场×方向 ：止损/到期 × 多/空 —— 四格各自贡献多少（最关键的一张表）
  ③ 集中度    ：盈利是否只靠极少数笔（尾部依赖 ⇒ 结论不可靠）
  ④ 年度分布  ：有没有「某一年特别好」（时间上的不均匀 = 另一个不稳定来源）

口径与 `工具/_进场体检/进场体检.py` **完全一致**（直接 import 复用同一份数据、
同一 `_atr/_sma`、同一 L1 逻辑、同一成本函数）⇒ 结果可直接与体检表对拍。

⚠️ 纪律
  1. 窗口 2021-2023 **已污染** ⇒ 只做诊断，不产出任何结论性「发现」。
  2. 本脚本只读、不产生任何交易规则。

用法
----
  python 盈亏归因.py --signal boll_rsi
  python 盈亏归因.py --signal boll_rsi --stop 1.5
  python 盈亏归因.py --signal retrace_ma13 --syms JM,M,P
"""
import os
import sys
import math
import argparse
import importlib.util

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_symbols as cs  # noqa: E402

_EP = os.path.join(_HERE, "进场体检.py")
_spec = importlib.util.spec_from_file_location("entry_screen", _EP)
es = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(es)


def collect(key, sig_name, stop_mult, H):
    """跑 L1 并返回逐笔明细 DataFrame（与 screen_one 逐行同口径）。"""
    df = es.load(key)
    if df is None or len(df) < 1000:
        return None
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    t = df["datetime"].to_numpy()
    N = len(c)
    atr = es._atr(h, l, c, es.ATR_N)
    ma = es._sma(c, es.MA_N)
    sig = es.SIGNALS[sig_name][1](o, h, l, c, atr, ma)
    bad = np.isnan(atr) | np.isnan(ma)
    sig = np.where(bad, 0.0, np.nan_to_num(sig, nan=0.0))

    valid = np.arange(0, N - H - 1)
    idx = valid[sig[valid] != 0]
    if len(idx) == 0:
        return None
    sgn = sig[idx]
    entry = o[idx + 1]
    stop = np.where(sgn > 0, entry - stop_mult * atr[idx], entry + stop_mult * atr[idx])
    exit_px = o[idx + 1 + H].copy()
    hit = np.zeros(len(idx), dtype=bool)
    bars = np.full(len(idx), H, dtype=int)
    for j, (e_i, st, s) in enumerate(zip(idx, stop, sgn)):
        for jj in range(e_i + 1, e_i + 1 + H):
            if s > 0:
                if l[jj] <= st:
                    exit_px[j] = min(st, o[jj]); hit[j] = True; bars[j] = jj - e_i; break
            else:
                if h[jj] >= st:
                    exit_px[j] = max(st, o[jj]); hit[j] = True; bars[j] = jj - e_i; break
    cp = es.cost_points(key)
    gross = (exit_px - entry) * sgn
    return pd.DataFrame({
        "品种": cs.get(key)[0], "key": key,
        "时间": t[idx + 1],
        "方向": np.where(sgn > 0, "多", "空"),
        "出场": np.where(hit, "止损", "到期"),
        "持有根数": bars,
        "毛盈亏": gross,
        "成本": cp,
        "净盈亏": gross - cp,
    })


def _agg(g):
    return {"笔数": len(g), "毛均": g["毛盈亏"].mean(),
            "毛合计": g["毛盈亏"].sum(), "净合计": g["净盈亏"].sum()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal", required=True)
    ap.add_argument("--syms", default=None)
    ap.add_argument("--stop", type=float, default=es.STOP_ATR_MULT)
    ap.add_argument("--hold", type=int, default=es.HOLD)
    a = ap.parse_args()

    keys = ([k.strip().upper() for k in a.syms.split(",")] if a.syms
            else ["EB", "JD", "JM", "LH", "MA", "M", "P", "RB", "RU", "SA", "SR", "V"])
    parts = [collect(k, a.signal, a.stop, a.hold) for k in keys]
    d = pd.concat([p for p in parts if p is not None], ignore_index=True)
    if len(d) == 0:
        raise SystemExit("无成交笔")

    L = []
    L.append("=" * 104)
    L.append(f"盈亏归因  ｜  信号={a.signal}  ｜  止损={a.stop:g}×ATR  ｜  持有={a.hold} 根"
             f"  ｜  窗口 2021-2023（**已污染，只做诊断**）")
    L.append("=" * 104)

    # ---------- ① 总览 ----------
    tot_g, tot_c, tot_n = d["毛盈亏"].sum(), d["成本"].sum(), d["净盈亏"].sum()
    L.append("")
    L.append("【① 总览】")
    L.append(f"  笔数 {len(d)}   毛合计 {tot_g:+,.1f}   成本合计 {tot_c:,.1f}   "
             f"净合计 {tot_n:+,.1f}")
    L.append(f"  ★ 毛 ÷ 成本 = {tot_g / tot_c:.4f}"
             f"   （<1 ⇒ 毛利本身就不够付成本；<0 ⇒ 毛利是负的）")
    L.append(f"  胜率 {100 * (d['净盈亏'] > 0).mean():.1f}%   "
             f"平均持有 {d['持有根数'].mean():.2f} 根")

    # ---------- ② 出场 × 方向 ----------
    L.append("")
    L.append("【② 出场原因 × 方向】—— 钱在哪一格里产生 / 消失")
    L.append(f"  {'格':<12}{'笔数':>7}{'占比':>8}{'毛均':>10}{'毛合计':>13}{'净合计':>13}"
             f"{'平均持有':>10}")
    for ex in ["止损", "到期"]:
        for dr in ["多", "空"]:
            g = d[(d["出场"] == ex) & (d["方向"] == dr)]
            if len(g) == 0:
                continue
            a_ = _agg(g)
            L.append(f"  {dr}·{ex:<9}{a_['笔数']:>7}{100*a_['笔数']/len(d):>7.1f}%"
                     f"{a_['毛均']:>10.3f}{a_['毛合计']:>13,.1f}{a_['净合计']:>13,.1f}"
                     f"{g['持有根数'].mean():>10.2f}")
    for ex in ["止损", "到期"]:
        g = d[d["出场"] == ex]
        a_ = _agg(g)
        L.append(f"  ── {ex}合计{'':<4}{a_['笔数']:>7}{100*a_['笔数']/len(d):>7.1f}%"
                 f"{a_['毛均']:>10.3f}{a_['毛合计']:>13,.1f}{a_['净合计']:>13,.1f}"
                 f"{g['持有根数'].mean():>10.2f}")

    # ---------- ③ 集中度 ----------
    L.append("")
    L.append("【③ 盈亏集中度】—— 结论是否只靠极少数笔")
    pos = d[d["毛盈亏"] > 0]["毛盈亏"].sort_values(ascending=False)
    neg = d[d["毛盈亏"] < 0]["毛盈亏"].sort_values()
    L.append(f"  盈利笔 {len(pos)} ({100*len(pos)/len(d):.1f}%)  合计 {pos.sum():+,.1f}   "
             f"均 {pos.mean():+.3f}" if len(pos) else "  无盈利笔")
    L.append(f"  亏损笔 {len(neg)} ({100*len(neg)/len(d):.1f}%)  合计 {neg.sum():+,.1f}   "
             f"均 {neg.mean():+.3f}" if len(neg) else "  无亏损笔")
    if len(pos) >= 20:
        k = max(1, int(len(pos) * 0.05))
        L.append(f"  盈利笔中最多的 5%（{k} 笔）合计 {pos.head(k).sum():+,.1f}"
                 f"  ⇒ 占全部盈利的 {100*pos.head(k).sum()/pos.sum():.1f}%")
    L.append(f"  最好 5 笔：{', '.join(f'{v:+.0f}' for v in pos.head(5))}")
    L.append(f"  最差 5 笔：{', '.join(f'{v:+.0f}' for v in neg.head(5))}")

    # ---------- ④ 年度 ----------
    L.append("")
    L.append("【④ 按年度】")
    d["年"] = pd.to_datetime(d["时间"]).dt.year
    L.append(f"  {'年':<8}{'笔数':>7}{'毛均':>10}{'毛合计':>13}{'净合计':>13}{'毛/成本':>10}")
    for y, g in d.groupby("年"):
        a_ = _agg(g)
        ratio = a_["毛合计"] / g["成本"].sum() if g["成本"].sum() else float("nan")
        L.append(f"  {y:<8}{a_['笔数']:>7}{a_['毛均']:>10.3f}{a_['毛合计']:>13,.1f}"
                 f"{a_['净合计']:>13,.1f}{ratio:>10.3f}")

    L.append("")
    L.append("⚠️ 窗口已污染 ⇒ 以上只用于「判死 / 定位亏损来源」，不构成任何 edge 证据。")
    txt = "\n".join(L)
    print(txt)
    sfx = "" if abs(a.stop - es.STOP_ATR_MULT) < 1e-9 else f"_stop{a.stop:g}"
    out = os.path.join(_HERE, f"盈亏归因_{a.signal}{sfx}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# 盈亏归因 · {a.signal}\n\n```\n{txt}\n```\n")
    print(f"\n[done] {out}")


if __name__ == "__main__":
    main()
