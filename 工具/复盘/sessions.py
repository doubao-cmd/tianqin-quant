# -*- coding: utf-8 -*-
"""
复盘工具 · 交易时段（阶段 5）
================================================================
口径 = **用户定义**（与策略工作台 `工作台/universe.py:109-130` 一致，不另立一套）：
    早盘 09:00–11:30 ｜ 午盘 13:30–15:00 ｜ 夜盘 21:00–23:00（**均为北京时间**）
（实测数据里 早盘 内部还有 **10:15–10:30 休市**断档 —— 那是交易所规则，不是数据缺失；
 它不影响"早盘"作为一个时段聚合，但画图要把它当空档去掉，见 `rangebreaks()`。）

两条硬口径
-----------
1. **时间戳要先换算**：CSV 原始 datetime 是 UTC（比北京时间慢 8h，`data.py` 有实测证据）。
   本模块所有对外函数**吃原始 df / 原始时间戳**，内部统一 `data.to_beijing()` —— 避免调用方漏换。
2. **夜盘归属日（§6·9）**：21:00–23:00 的 bar 属于**下一交易日**，不能按自然日分组。
   交易日历用「**跳过周末**」近似（周五夜盘 → 下周一）；真实节假日历属已知欠账，已标注。

无未来函数：所有统计只按已发生的 bar 聚合。
只输出**分布与分位**，不输出"开/不开"结论（§8 红线 5）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import data
import features

# ---- 时段窗口（北京时间，单位=小时的小数；右端不含）----
SESSIONS: tuple[tuple[str, float, float], ...] = (
    ("早盘", 9.0, 11.5),
    ("午盘", 13.5, 15.0),
    ("夜盘", 21.0, 23.0),
)
SEG_NAMES = tuple(s[0] for s in SESSIONS)
CLOSED = "休市"

# 画图用：日内空档（§6·8，`rangebreaks` 处理不了"自动"，必须自己给）
_GAPS_HOUR = (15.0, 21.0), (23.0, 9.0), (11.5, 13.5), (10.25, 10.5)


# ---------------------------------------------------------------------------
# 时段判定（纯函数，输入一律为**北京时间**）
# ---------------------------------------------------------------------------
def seg_of_bj(ts) -> str:
    """北京时间时刻 → '早盘' / '午盘' / '夜盘' / '休市'。"""
    if ts is None or pd.isna(ts):
        return CLOSED
    h = pd.Timestamp(ts).hour + pd.Timestamp(ts).minute / 60.0
    for name, lo, hi in SESSIONS:
        if lo <= h < hi:
            return name
    return CLOSED


def _next_biz_day(d) -> pd.Timestamp:
    """下一个工作日（跳过周六周日）。**近似**交易日历：不含节假日。"""
    d = pd.Timestamp(d).normalize() + pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d += pd.Timedelta(days=1)
    return d


def trading_day_of_bj(ts) -> pd.Timestamp:
    """北京时间时刻 → **交易日**：夜盘（21:00–23:00）归下一交易日，其余归当日（§6·9）。"""
    t = pd.Timestamp(ts).normalize()
    return _next_biz_day(t) if seg_of_bj(ts) == "夜盘" else t


# ---------------------------------------------------------------------------
# 给 DataFrame 打时段/交易日列（吃原始 datetime，内部换算）
# ---------------------------------------------------------------------------
def label_sessions(df: pd.DataFrame, time_col: str = "datetime") -> pd.DataFrame:
    """返回新增 `北京时间` / `时段` / `交易日` 三列的副本。原始 df 不动。"""
    bj = data.to_beijing(df[time_col])
    seg = bj.map(seg_of_bj)
    tday = pd.Series([trading_day_of_bj(t) for t in bj], index=df.index)
    out = df.copy()
    out["北京时间"] = bj.to_numpy()
    out["时段"] = seg.to_numpy()
    out["交易日"] = tday.to_numpy()
    return out


# ---------------------------------------------------------------------------
# session 画像：每个 (交易日, 时段) 一行
# ---------------------------------------------------------------------------
def session_table(df: pd.DataFrame, feats: pd.DataFrame | None = None) -> pd.DataFrame:
    """按 (交易日, 时段) 聚合的 session 画像表。

    列：交易日 / 时段 / 起点 / 终点 / 根数 / 开 / 高 / 低 / 收 / 振幅ATR / 收益率 / 最长连续同向
    口径：
      · 振幅ATR = (session 最高 − session 最低) / **session 末根的 ATR14**（因果、只用到收盘）
      · 收益率  = (session 末收 / session 首开 − 1) × 100（%）
      · 最长连续同向 = 会话内最长同向根数（用全序列 run_len 聚合，会话首根承接前一根）
    """
    lab = label_sessions(df)
    f = feats if feats is not None else features.build_all(df)
    lab = lab.assign(atr14=np.asarray(f["atr14"], dtype=float),
                     run_len=np.asarray(f["run_len"], dtype=int))
    use = lab[lab["时段"] != CLOSED]
    if use.empty:
        return pd.DataFrame(columns=["交易日", "时段", "起点", "终点", "根数", "开", "高",
                                     "低", "收", "振幅ATR", "收益率", "最长连续同向"])
    g = use.groupby(["交易日", "时段"], sort=True)
    out = g.agg(起点=("北京时间", "first"), 终点=("北京时间", "last"), 根数=("close", "size"),
                开=("open", "first"), 高=("high", "max"), 低=("low", "min"), 收=("close", "last"),
                ATR末=("atr14", "last"), 连续=("run_len", "max")).reset_index()
    with np.errstate(divide="ignore", invalid="ignore"):
        out["振幅ATR"] = np.where(out["ATR末"] > 0, (out["高"] - out["低"]) / out["ATR末"], np.nan)
        out["收益率"] = np.where(out["开"] > 0, (out["收"] / out["开"] - 1.0) * 100.0, np.nan)
    out = out.rename(columns={"连续": "最长连续同向"})
    out["最长连续同向"] = out["最长连续同向"].astype(int)
    return out[["交易日", "时段", "起点", "终点", "根数", "开", "高", "低", "收",
                "振幅ATR", "收益率", "最长连续同向"]]


def session_profile(st: pd.DataFrame) -> pd.DataFrame:
    """三组分布的中位数/P25/P75/n（按 时段）。"""
    rows = []
    for seg in SEG_NAMES:
        sub = st[st["时段"] == seg]
        if sub.empty:
            continue
        for col in ("振幅ATR", "收益率", "最长连续同向"):
            v = pd.to_numeric(sub[col], errors="coerce").dropna()
            rows.append({"时段": seg, "指标": col, "n": int(len(v)),
                         "中位": float(v.median()) if len(v) else np.nan,
                         "P25": float(v.quantile(0.25)) if len(v) else np.nan,
                         "P75": float(v.quantile(0.75)) if len(v) else np.nan})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 截断盘：percentile_rank(今日同时段振幅, 近 N 个同时段)
# ---------------------------------------------------------------------------
def percentile_rank(x: float, series) -> float:
    """x 在 series 中的分位（0–1，含并列取'≤'口径）。series 为空或全 NaN 返回 NaN。"""
    s = pd.to_numeric(pd.Series(series), errors="coerce").dropna()
    if len(s) == 0 or pd.isna(x):
        return float("nan")
    return float((s <= x).sum() / len(s))


def truncation(df: pd.DataFrame, seg: str = "早盘", lookback: int = 60,
               feats: pd.DataFrame | None = None) -> dict:
    """截断盘：**只给分位与分布**，不给"能不能开"的结论（§8 红线 5）。

    返回 dict：seg / 交易日 / 今日振幅ATR / 分位 / n_hist / 历史(Series) / 今日收益率 / 今日连续
    """
    st = session_table(df, feats)
    sub = st[st["时段"] == seg].sort_values("交易日").reset_index(drop=True)
    if sub.empty:
        return {"seg": seg, "交易日": None, "今日振幅ATR": float("nan"),
                "分位": float("nan"), "n_hist": 0, "历史": pd.Series(dtype=float),
                "今日收益率": float("nan"), "今日连续": 0}
    today = sub.iloc[-1]
    hist = sub["振幅ATR"].iloc[max(0, len(sub) - 1 - lookback): len(sub) - 1]
    return {
        "seg": seg,
        "交易日": today["交易日"],
        "今日振幅ATR": float(today["振幅ATR"]),
        "分位": percentile_rank(float(today["振幅ATR"]), hist),
        "n_hist": int(pd.to_numeric(hist, errors="coerce").dropna().shape[0]),
        "历史": pd.to_numeric(hist, errors="coerce"),
        "今日收益率": float(today["收益率"]),
        "今日连续": int(today["最长连续同向"]),
    }


# ---------------------------------------------------------------------------
# 画图：日内空档（§6·8：rangebreaks 不会自己算日内休市，必须显式给）
# ---------------------------------------------------------------------------
def rangebreaks(period: int | None = None) -> list[dict]:
    """Plotly rangebreaks：周末 + 日内三段空档（15:00–21:00 / 23:00–09:00 / 11:30–13:30
    + 10:15–10:30 休市）。**前提：x 轴已是北京时间**。

    ★ 日线（`period == 1440`）**只去周末，不做日内小时剔除**：
      日线 bar 的原始时刻恒为 16:00（见 data.py），恰好落在 [15,21) 这个小时内，
      若照搬日内空档，整张日线图会被隐藏成全空（仅坐标轴）。
    """
    rb = [dict(bounds=["sat", "mon"])]
    if period == 1440:
        return rb
    rb += [dict(bounds=[lo, hi], pattern="hour") for lo, hi in _GAPS_HOUR]
    return rb


# ---------------------------------------------------------------------------
# 自检：阶段 5 验收（夜盘归下一交易日 / 只有分位没有结论）
# ---------------------------------------------------------------------------
def _selftest() -> int:
    ok = True
    print("=" * 68)

    # ① 夜盘 21:00 归**下一交易日**（§6·9，阶段 5 硬验收）
    cases = [
        ("2026-10-19 21:05", "夜盘", "2026-10-20", "周一晚 → 周二"),
        ("2026-10-23 21:05", "夜盘", "2026-10-26", "周五晚 → 下周一（跳周末）"),
        ("2026-10-20 09:05", "早盘", "2026-10-20", "周二早 → 当日"),
        ("2026-10-20 13:45", "午盘", "2026-10-20", "周二午 → 当日"),
        ("2026-10-20 12:00", CLOSED, "2026-10-20", "午休 → 休市"),
        ("2026-10-20 23:30", CLOSED, "2026-10-20", "23:30 已收盘 → 休市"),
    ]
    for ts, seg_exp, day_exp, note in cases:
        seg = seg_of_bj(ts)
        day = trading_day_of_bj(ts).strftime("%Y-%m-%d")
        good = (seg == seg_exp) and (day == day_exp)
        print(f"[① 时段/交易日] {ts} → {seg} / 交易日 {day}（期望 {seg_exp} / {day_exp}）{note} ⇒ {good}")
        ok &= good

    # ② 原始（UTC）→ 北京时间 +8h：raw 13:00 必须是夜盘 21:00
    raw = pd.Timestamp("2026-10-19 13:00")
    bj = data.to_beijing(raw)
    good = (bj.strftime("%H:%M") == "21:00") and (seg_of_bj(bj) == "夜盘")
    print(f"[② 时区换算] raw {raw} → 北京 {bj} → {seg_of_bj(bj)} ⇒ {good}")
    ok &= good

    # ③ 真实数据：MA（有夜盘）/ JD（无夜盘）
    for sym, want_night in [("MA", True), ("JD", False)]:
        df, meta = data.load_kline(sym, 5, use_cache=True)
        st = session_table(df)
        segs = set(st["时段"].unique())
        n_night = int((st["时段"] == "夜盘").sum())
        lab = label_sessions(df)
        n_closed = int((lab["时段"] == CLOSED).sum())
        # 夜盘行的交易日必须 = 起点北京日 + 1 个工作日
        n_bad = 0
        for _, r in st[st["时段"] == "夜盘"].head(200).iterrows():
            if trading_day_of_bj(r["起点"]) != pd.Timestamp(r["交易日"]):
                n_bad += 1
        good = (segs == set(SEG_NAMES)) if want_night else (segs == {"早盘", "午盘"})
        good &= (n_night > 0) if want_night else (n_night == 0)
        good &= (n_closed == 0) and (n_bad == 0)
        print(f"[③ 真实数据] {sym} 5min n={len(df)}：时段={sorted(segs)} 夜盘session={n_night} "
              f"未归类 bar={n_closed} 夜盘交易日错配={n_bad} ⇒ {good}")
        ok &= bool(good)

    # ④ 截断盘：**只有分位与分布，没有"开/不开"结论**（§8 红线 5）
    df, meta = data.load_kline("MA", 5, use_cache=True)
    tr = truncation(df, seg="早盘", lookback=60)
    allow_keys = {"seg", "交易日", "今日振幅ATR", "分位", "n_hist", "历史", "今日收益率", "今日连续"}
    extra = set(tr.keys()) - allow_keys
    banned = {"开仓", "不开", "建议", "结论", "信号", "gate"}
    good = (not (extra & banned)) and (0.0 <= tr["分位"] <= 1.0)
    print(f"[④ 截断盘] MA 早盘 交易日={tr['交易日']} 今日振幅/ATR={tr['今日振幅ATR']:.2f} "
          f"分位={tr['分位']:.1%}（近 {tr['n_hist']} 个同时段）⇒ 无结论字段={good}")
    ok &= bool(good)

    # 附：session 画像抽样
    st = session_table(df)
    prof = session_profile(st)
    print("[附] MA session 样本数："
          + "｜".join(f"{s}={int((st['时段'] == s).sum())}" for s in SEG_NAMES))
    for _, r in prof.iterrows():
        print(f"     {r['时段']} {r['指标']}: n={r['n']} 中位={r['中位']:.3f} "
              f"P25={r['P25']:.3f} P75={r['P75']:.3f}")

    print("=" * 68)
    print("阶段 5 自检：" + ("全部通过 ✅" if ok else "存在失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
