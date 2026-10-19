# -*- coding: utf-8 -*-
"""
复盘工具 · 指标层（阶段 3）
================================================================
纯函数：**不读文件、不连网、无全局状态**（简报 §4 四层硬约束）。

★ 口径红线：MA13 / ATR14 必须与策略**逐字对齐**（§4 阶段 3 验收：差 < 1e-9）。
  单一真相源 = `策略/06_MA13超短线/工作台/signals.py::Ma13Signal`
  （ma13_live_multi.py 已改为调用该接口，不再内联副本；其内联公式与之一致）：
    prev_c = np.roll(close,1); prev_c[0]=close[0]
    tr = max(H-L, |H-prev_c|, |L-prev_c|)
    atr[j] = mean(tr[max(1, j-ATR_N):j])   if j>=2 else tr[j]      # 右端**不含 tr[j]**，非 Wilder
    ma[j]  = mean(close[j-MA_N:j])          for j>=13，否则 NaN      # 右端**不含 close[j]**
  参数（signals.py:72-73）：MA_N, SLOPE_N, ATR_N = 13, 5, 14

★ 展示层指标（布林/振幅/上下影/连续同向/距MA13的ATR倍数）为 L1，只画不判（§6.4）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ---- 06 参数（与 signals.py 完全一致）----
MA_N, SLOPE_N, ATR_N = 13, 5, 14
# ---- L1 展示参数 ----
BOLL_N, BOLL_K = 20, 2


def _cols(df: pd.DataFrame):
    return (df["open"].to_numpy(float), df["high"].to_numpy(float),
            df["low"].to_numpy(float), df["close"].to_numpy(float))


# ---------------------------------------------------------------------------
# 06 核心（逐字对齐）
# ---------------------------------------------------------------------------
def true_range(high, low, close) -> np.ndarray:
    """TR，prev_c 首值用 close[0] 兜底（与 signals.py:81-82 一致）。"""
    prev_c = np.roll(close, 1)
    prev_c[0] = close[0]
    return np.maximum(high - low, np.maximum(np.abs(high - prev_c), np.abs(low - prev_c)))


def atr(df: pd.DataFrame, n: int = ATR_N) -> pd.Series:
    """ATR：14 根 TR 的**简单均值**，窗口 [max(1,j-n), j)，右端不含 tr[j]（§5·3，非 Wilder）。"""
    _, high, low, close = _cols(df)
    tr = true_range(high, low, close)
    arr = np.array([np.mean(tr[max(1, j - n):j]) if j >= 2 else tr[j]
                    for j in range(len(close))], dtype=float)
    return pd.Series(arr, index=df.index, name=f"atr{n}")


def ma(df: pd.DataFrame, n: int = MA_N) -> pd.Series:
    """MA：mean(close[j-n:j])，右端不含 close[j]；不足 n 根为 NaN（signals.py:87-88）。"""
    _, _, _, close = _cols(df)
    m = len(close)
    if m < n:
        return pd.Series(np.full(m, np.nan), index=df.index, name=f"ma{n}")
    a = np.array([np.mean(close[j - n:j]) for j in range(n, m)], dtype=float)
    a = np.concatenate([np.full(n, np.nan), a])
    return pd.Series(a, index=df.index, name=f"ma{n}")


def slope(ma_s: pd.Series, n: int = SLOPE_N) -> pd.Series:
    """斜率 = MA13 − MA13.shift(SLOPE_N)（signals.py:101）。"""
    return (ma_s - ma_s.shift(n)).rename(f"slope{n}")


# ---------------------------------------------------------------------------
# L1 展示层（只画，不参与任何判断）
# ---------------------------------------------------------------------------
def boll(df: pd.DataFrame, n: int = BOLL_N, k: float = BOLL_K) -> pd.DataFrame:
    """布林(20,2)：mid=MA20、上下轨 ±kσ。**展示口径，非 06 口径**（06 不用布林）。σ 用总体标准差 ddof=0。"""
    _, _, _, close = _cols(df)
    s = pd.Series(close, index=df.index)
    mid = s.rolling(n).mean()
    sd = s.rolling(n).std(ddof=0)
    return pd.DataFrame({"boll_mid": mid, "boll_upper": mid + k * sd, "boll_lower": mid - k * sd})


def amplitude(df: pd.DataFrame) -> pd.Series:
    """振幅 =(H−L)/C（当天"有多急"的标量）。"""
    _, high, low, close = _cols(df)
    with np.errstate(divide="ignore", invalid="ignore"):
        return pd.Series(np.where(close > 0, (high - low) / close, np.nan),
                         index=df.index, name="amplitude")


def shadow_ratio(df: pd.DataFrame) -> pd.Series:
    """上下影比 =(H−max(O,C)) / (min(O,C)−L)（多空拉锯强度；下影为 0 → inf，置 NaN）。"""
    o, h, l, c = _cols(df)
    up = h - np.maximum(o, c)
    dn = np.minimum(o, c) - l
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(dn > 0, up / dn, np.nan)
    return pd.Series(r, index=df.index, name="shadow_ratio")


def run_length(df: pd.DataFrame) -> pd.Series:
    """连续同向根数（按收盘价相邻方向；反向或平盘则重置）。"""
    _, _, _, close = _cols(df)
    n = len(close)
    sign = np.sign(np.diff(close, prepend=close[:1]))
    run = np.zeros(n, dtype=int)
    for j in range(1, n):
        run[j] = (run[j - 1] + 1) if (sign[j] != 0 and sign[j] == sign[j - 1]) else (1 if sign[j] != 0 else 0)
    return pd.Series(run, index=df.index, name="run_len")


def atr_mult_dist(df: pd.DataFrame, ma_s: pd.Series, atr_s: pd.Series) -> pd.Series:
    """距 MA13 的 ATR 倍数 =(C−MA13)/ATR —— 06 的进场条件本体，必须画（§6.4）。"""
    _, _, _, close = _cols(df)
    a = np.asarray(atr_s, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        d = np.where(a > 0, (close - np.asarray(ma_s, dtype=float)) / a, np.nan)
    return pd.Series(d, index=df.index, name="atr_dist")


def build_custom(df: pd.DataFrame, ma_n: int = MA_N, slope_n: int = SLOPE_N,
                 atr_n: int = ATR_N, boll_n: int = BOLL_N, boll_k: float = BOLL_K) -> pd.DataFrame:
    """可调参数的指标表（复盘终端「修改指标」用）。

    与 `build_all` 的唯一区别：参数可传。**默认值仍是 13/5/14/20/2**，
    即默认输出与策略口径逐值一致；改了参数只是「换个角度看图」，不是扫参优化。
    列名用通用名（ma / slope / atr / boll_*），避免把 13/14 写死在列名里。
    """
    out = df.copy()
    out["ma"] = ma(df, ma_n)
    out["slope"] = slope(out["ma"], slope_n)
    out["atr"] = atr(df, atr_n)
    out = pd.concat([out, boll(df, boll_n, boll_k)], axis=1)
    out["amplitude"] = amplitude(df)
    out["shadow_ratio"] = shadow_ratio(df)
    out["atr_dist"] = atr_mult_dist(df, out["ma"], out["atr"])
    return out


def build_all(df: pd.DataFrame) -> pd.DataFrame:
    """把全序列指标算成一张表（供 @st.cache_data 缓存全序列、upto_idx 只切片，§6·6）。"""
    out = df.copy()
    out["ma13"] = ma(df, MA_N)
    out["slope13_5"] = slope(out["ma13"], SLOPE_N)
    out["atr14"] = atr(df, ATR_N)
    out = pd.concat([out, boll(df)], axis=1)
    out["amplitude"] = amplitude(df)
    out["shadow_ratio"] = shadow_ratio(df)
    out["run_len"] = run_length(df)
    out["atr_dist"] = atr_mult_dist(df, out["ma13"], out["atr14"])
    return out


# ---------------------------------------------------------------------------
# 自检：与 signals.Ma13Signal 逐值比对（<1e-9）＋ 因果 T2 ＋ 500 根渲染计时
# ---------------------------------------------------------------------------
def _strategy_signals_module():
    """dev-time 交叉验证用：加载策略的单一真相源 signals.py（只 numpy/pandas，安全）。"""
    import importlib.util
    p = r"C:\Users\cc\Desktop\天勤量化\策略\06_MA13超短线\工作台\signals.py"
    spec = importlib.util.spec_from_file_location("_strat_signals", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _selftest() -> int:
    import sys, time
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    import data
    import charts

    ok = True
    print("=" * 64)
    df, meta = data.load_kline("MA", 5, use_cache=True)
    print(f"数据：MA 5min  n={meta['n']}  source={meta['source']}")

    # ① MA13 / ATR14 与策略逐值一致
    ref = _strategy_signals_module().Ma13Signal().indicators(df)
    ours_atr = atr(df, ATR_N).to_numpy()
    ours_ma = ma(df, MA_N).to_numpy()
    d_atr = float(np.nanmax(np.abs(ours_atr - ref["atr"])))
    d_ma = float(np.nanmax(np.abs(ours_ma - ref["ma"])))
    nan_atr = bool(np.array_equal(np.isnan(ours_atr), np.isnan(ref["atr"])))
    nan_ma = bool(np.array_equal(np.isnan(ours_ma), np.isnan(ref["ma"])))
    print(f"[① 逐值] max|ΔATR|={d_atr:.3e}  max|ΔMA13|={d_ma:.3e}  NaN位置一致 atr/ma={nan_atr}/{nan_ma}（阈值 1e-9）")
    ok &= d_atr < 1e-9 and d_ma < 1e-9 and nan_atr and nan_ma

    # ② ATR 不是 Wilder（与 Wilder 结果必然不同）
    wilder = true_range(*[df[c].to_numpy(float) for c in ("high", "low", "close")])
    w = pd.Series(wilder, index=df.index).ewm(alpha=1 / ATR_N, adjust=False).mean().to_numpy()
    diff_wilder = float(np.nanmax(np.abs(ours_atr - w)))
    print(f"[② 口径] 与 Wilder(alpha=1/14) 的最大差={diff_wilder:.4f}（应显著>0，证明非 Wilder）")
    ok &= diff_wilder > 1e-6

    # ③ 因果 T2：把未来涂改成乱码，第 i 根指标必须一动不动（§6.3）
    i = 5000
    a1 = build_all(df.iloc[: i + 1])["atr14"].iloc[i]
    m1 = build_all(df.iloc[: i + 1])["ma13"].iloc[i]
    tampered = df.copy()
    tampered.loc[i + 1:, ["open", "high", "low", "close"]] = 999999.0
    a2 = build_all(tampered.iloc[: i + 1])["atr14"].iloc[i]
    m2 = build_all(tampered.iloc[: i + 1])["ma13"].iloc[i]
    same = (a1 == a2) and (m1 == m2)
    print(f"[③ 因果T2] 涂改未来后第{i}根 atr/ma 不变={same}")
    ok &= same

    # ④ 500 根渲染 < 1 秒
    v = build_all(df.iloc[1000:1500])
    t0 = time.perf_counter()
    fig = charts.build_kline_atr(v)
    dt = time.perf_counter() - t0
    print(f"[④ 性能] 500 根 build_kline_atr 用时 {dt * 1000:.0f} ms（阈值 1000ms），traces={len(fig.data)}")
    ok &= dt < 1.0

    print("=" * 64)
    print("阶段 3 自检：" + ("全部通过 ✅" if ok else "存在失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
