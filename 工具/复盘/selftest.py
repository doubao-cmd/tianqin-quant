# -*- coding: utf-8 -*-
"""
复盘工具 · 自检脚本（阶段 4 起）
================================================================
★ 硬门槛（简报 §4 阶段 4）：
    「把 `df[upto_idx+1:]` 的 OHLC 全部改成随机乱码，重画一次，图必须完全不变。」
  本文件把这条断言自动化：`python selftest.py --replay`

为什么这条断言能成立（不是"看起来对"，是结构性保证）：
  1. **物理截断**：回放只把 `v.iloc[:upto_idx+1]` 交给 `charts`，未来那几根**根本不在图里**
     ——不是画成半透明（§8 红线 7）。
  2. **指标因果**：MA13 / ATR14 / 布林 / 振幅 / 影比 / 连续同向 全部只回看，指标层过了
     `features.py` 的 T2 涂改未来自检。
  两条路都测：
    A 路线 = app 实际路径：**全序列**算指标（`@st.cache_data`）→ 切片。涂改未来只会污染
      被丢掉的那截 ⇒ 图不变。
    B 路线 = 更强路径：**先截断原始 df 再算指标**。未来数据连"参与计算"的机会都没有 ⇒ 图不变。

用法：
  .venv_viz\\Scripts\\python.exe 工具\\复盘\\selftest.py --replay
  ... --replay --sym M --period 15 --upto 3000 --win 300 --seed 7
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import charts                      # noqa: E402
import data                        # noqa: E402
import features                    # noqa: E402
import sessions                    # noqa: E402

OHLC = ["open", "high", "low", "close"]


def _sig(fig) -> str:
    """图的指纹：序列化成 JSON 后取 sha1（两次输入完全一致 ⇒ 指纹一致）。"""
    return hashlib.sha1(fig.to_json().encode("utf-8")).hexdigest()[:16]


def _same_series(a, b) -> bool:
    """逐值比较：NaN 位置也必须一致（§6·5）。"""
    if len(a) != len(b):
        return False
    aa = np.asarray(a, dtype=float)
    bb = np.asarray(b, dtype=float)
    if not np.array_equal(np.isnan(aa), np.isnan(bb)):
        return False
    m = ~np.isnan(aa)
    return bool(np.array_equal(aa[m], bb[m]))


def replay_invariance(sym="MA", period=5, upto=None, win=300, seed=20261017) -> int:
    ok = True
    print("=" * 68)
    df, meta = data.load_kline(sym, period, use_cache=True)
    n = len(df)
    upto = (n - 100) if upto is None else int(upto)
    upto = max(1, min(upto, n - 2))              # 两边都留出余量
    win = max(1, min(int(win), upto + 1))
    print(f"数据：{sym} {period}min  n={n}  source={meta['source']}  "
          f"回放位置 upto_idx={upto}（0-based）  未来 {n - upto - 1} 根将被涂改成乱码")

    # 造乱码（只动 future 的 OHLC，时间戳不动）
    rng = np.random.default_rng(seed)
    tampered = df.copy()
    tampered.loc[upto + 1:, OHLC] = rng.normal(100_000.0, 20_000.0, size=(n - upto - 1, 4))

    # ---- A 路线：全序列指标 → 物理截断（app 实际路径）----
    full_clean = features.build_all(df)
    full_tamp = features.build_all(tampered)
    lo = max(0, upto + 1 - win)
    va, vb = full_clean.iloc[lo:upto + 1], full_tamp.iloc[lo:upto + 1]
    fa, fb = charts.build_kline_atr(va), charts.build_kline_atr(vb)

    same_ma_a = _same_series(va["ma13"], vb["ma13"])
    same_atr_a = _same_series(va["atr14"], vb["atr14"])
    same_fig_a = _sig(fa) == _sig(fb)
    print(f"[A 全序列→切片]  窗口 {len(va)} 根 ｜ 指标逐值一致 ma13={same_ma_a} atr14={same_atr_a} "
          f"｜ 图指纹 {_sig(fa)}=={_sig(fb)} ⇒ {same_fig_a}")
    ok &= same_ma_a and same_atr_a and same_fig_a

    # ---- B 路线：先截断原始 df，再算指标（更强）----
    tc = features.build_all(df.iloc[:upto + 1])
    tt = features.build_all(tampered.iloc[:upto + 1])
    vc = tc.iloc[max(0, len(tc) - win):]
    vt = tt.iloc[max(0, len(tt) - win):]
    fc, fd = charts.build_kline_atr(vc), charts.build_kline_atr(vt)
    same_ma_b = _same_series(vc["ma13"], vt["ma13"])
    same_atr_b = _same_series(vc["atr14"], vt["atr14"])
    same_fig_b = _sig(fc) == _sig(fd)
    print(f"[B 先截断→再算指标] 窗口 {len(vc)} 根 ｜ 指标逐值一致 ma13={same_ma_b} atr14={same_atr_b} "
          f"｜ 图指纹 {_sig(fc)}=={_sig(fd)} ⇒ {same_fig_b}")
    ok &= same_ma_b and same_atr_b and same_fig_b

    # ---- 未来确实不在图里（"物理截断"这半句的独立证据）----
    x_end = max(t for tr in fa.data if tr.x is not None for t in list(tr.x)[-1:])
    bar_max = df["datetime"].iloc[upto]
    no_future = (x_end <= bar_max) and (len(va) <= win)
    print(f"[物理截断] 图内最大时间 {x_end} <= 当前 bar {bar_max} ⇒ {no_future}"
          f"（窗口 {len(va)} ≤ {win}，未来 {n - upto - 1} 根一根都没进来）")
    ok &= bool(no_future)

    # ---- 反证：如果**不**截断，涂改未来必须让图变（否则本自检是空转）----
    fa_all = charts.build_kline_atr(full_clean)
    fb_all = charts.build_kline_atr(full_tamp)
    detect = _sig(fa_all) != _sig(fb_all)
    print(f"[反证/灵敏度] 不截断时涂改未来 → 图指纹变化 = {detect}（应为 True，证明本自检不是空转）")
    ok &= bool(detect)

    print("=" * 68)
    print("回放不变性（硬门槛）：" + ("通过 ✅ 图对未来完全不可见" if ok else "失败 ❌"))
    return 0 if ok else 1


def terminal_invariance(sym="M", period=5, win=300, seed=20261018) -> int:
    """单屏终端主图（买卖点/止损/时段底色/画线）的回放不变性。

    与 `replay_invariance` 的区别：走的是**2026 近期数据 + build_terminal_figure**，
    并且叠了真实成交事件，验证「事件标记也不越过回放位置」。
    """
    import trades as T

    ok = True
    print("=" * 68)
    key = sym.upper()
    if key not in data.RECENT_SPECS:
        print(f"未知品种 {key}，可用：{list(data.RECENT_SPECS)}")
        return 1
    cn = data.RECENT_SPECS[key][0]
    df, meta = data.load_recent(key, period)
    if int(period) == 1440:
        df = df.assign(x=df["datetime"].dt.normalize() + pd.Timedelta(days=1))
    else:
        df = df.assign(x=data.to_beijing(df["datetime"]))
    feats = features.build_custom(df)
    n = len(feats)
    upto = max(1, min(n - 2, n - 100))
    win = max(1, min(int(win), upto + 1))

    ev = T.load_events(cn)
    tr = T.load_trades(cn)
    print(f"数据：{key}（{cn}）{period}min  n={n}  upto={upto}  未来 {n - upto - 1} 根涂乱码")

    rng = np.random.default_rng(seed)
    tamp = df.copy()
    tamp.loc[upto + 1:, OHLC] = rng.normal(100_000.0, 20_000.0, size=(n - upto - 1, 4))
    feats2 = features.build_custom(tamp)

    lo = max(0, upto + 1 - win)
    va, vb = feats.iloc[lo:upto + 1], feats2.iloc[lo:upto + 1]
    x1 = va["x"].iloc[-1]

    ev_a = ev[ev["时间"] <= x1]
    tr_a = tr[(tr["开仓时间"] <= x1) & (tr["平仓时间"].isna() | (tr["平仓时间"] >= va["x"].iloc[0]))]
    rb = sessions.rangebreaks(int(period))

    fa = charts.build_terminal_figure(va, events=ev_a, trades=tr_a, height=420, rangebreaks=rb)
    fb = charts.build_terminal_figure(vb, events=ev_a, trades=tr_a, height=420, rangebreaks=rb)
    same_fig = _sig(fa) == _sig(fb)
    print(f"[① 图指纹] 涂改未来后 {_sig(fa)}=={_sig(fb)} ⇒ {same_fig}")
    ok &= same_fig

    bad = 0
    cnt = 0
    for t in fa.data:
        if getattr(t, "type", "") == "scatter" and getattr(t, "mode", "") == "markers" and t.x is not None:
            cnt += len(t.x)
            bad += int((pd.to_datetime(pd.Series(t.x)) > x1).sum())
    print(f"[② 事件截断] 买卖点 {cnt} 个，越过回放位置 {bad} 个（应 0）⇒ {bad == 0}")
    ok &= bad == 0

    plan_leak = 0
    for _, r in tr_a.iterrows():
        if pd.notna(r.get("计划止损")) and pd.isna(r.get("止损")):
            plan_leak += 1
    print(f"[③ 止损口径] 图上止损用『计划止损』（开仓已知），出场止损线不进图 ⇒ {plan_leak == 0}")
    ok &= plan_leak == 0

    print("=" * 68)
    print("终端主图回放不变性：" + ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


def _main() -> int:
    ap = argparse.ArgumentParser(description="复盘工具自检（阶段 4+）")
    ap.add_argument("--replay", action="store_true", help="跑回放不变性硬门槛（默认也跑）")
    ap.add_argument("--terminal", action="store_true", help="跑单屏终端主图（含买卖点）回放不变性")
    ap.add_argument("--sessions", action="store_true", help="跑阶段 5 时段/交易日/分位自检（转发 sessions.py）")
    ap.add_argument("--sym", default="MA")
    ap.add_argument("--period", type=int, default=5)
    ap.add_argument("--upto", type=int, default=None, help="回放位置（默认 n-100）")
    ap.add_argument("--win", type=int, default=300, help="窗口根数（300~800，§6·7）")
    ap.add_argument("--seed", type=int, default=20261017)
    a = ap.parse_args()

    rc = 0
    if a.sessions:                       # 阶段 5：时段口径 + 夜盘归属日 + 只输出分位
        import sessions as _s
        rc |= _s._selftest()
    if a.terminal:                       # 单屏终端（2026 数据 + 买卖点）
        rc |= terminal_invariance("M", 5, a.win, a.seed)
    if a.replay or not (a.sessions or a.terminal):   # 阶段 4 硬门槛
        rc |= replay_invariance(a.sym, a.period, a.upto, a.win, a.seed)
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_main())
