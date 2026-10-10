# -*- coding: utf-8 -*-
"""
A 方案：实证验证 2016-2019 是不是"震荡年"
==========================================
对 9 个老品种（2016 已上市）逐个【串行】取 2016-2019 日线，计算"趋势度"指标，
直接回答用户猜测："这几年是震荡年 → 双均线亏损正常"。

核心指标 —— 方向性比率 trend_ratio：
    = |期末价 - 期初价| / Σ|每日价格变动|
    取值 [0,1]：越接近 1 = 价格几乎一路单向（强趋势年）；
               越接近 0 = 价格来回缠绕、净位移相对总路程极小（震荡/无方向年）。
    （这是衡量"趋势跟随策略能否赚钱"的最直接代理：震荡市里双均线被反复打脸。）

辅助指标：
    total_return  区间/年度涨跌幅（方向）
    ann_vol       年化波动率（振幅）
    n_bars        样本根数

严守取数铁律：① 禁并发（一次一个品种，9 < 20 重开阈值）② 空数据不写盘。
"""
import sys, os, json, math, time
import numpy as np
import pandas as pd
from datetime import date

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 凭据来自本地 gitignore 的 config_account.py（绝不入库）。公开仓库 clone 后该文件不存在，
# 此时走免登录回测路径（TqSim + TqBacktest 不需要天勤账号）。
try:
    from config_account import USER, PASSWORD
    _HAS_ACCOUNT = bool(USER and PASSWORD and "填" not in str(USER))
except Exception:
    USER = PASSWORD = None
    _HAS_ACCOUNT = False

def _auth():
    """有凭据才带 TqAuth，否则返回 None（免登录回测）。"""
    return TqAuth(USER, PASSWORD) if _HAS_ACCOUNT else None

from config_symbols import SYMBOLS
from tqsdk import TqApi, TqSim, TqBacktest, TqAuth, BacktestFinished

# 9 个老品种：2016 年已上市，落在主样本窗口 2016-2019
OLD_KEYS = ["MA", "M", "RB", "V", "JM", "JD", "P", "RU", "SR"]

BACKTEST_START = date(2015, 12, 25)   # 含 1 周预热；指标只取 >= 2016-01-01
BACKTEST_END   = date(2019, 12, 31)
YEARS = [2016, 2017, 2018, 2019]


def _metrics(prices):
    """prices: 升序收盘价序列，返回趋势度指标字典。"""
    p = np.asarray(prices, dtype=float)
    if len(p) < 2:
        return None
    ret = p[1:] / p[:-1] - 1.0
    total_ret = p[-1] / p[0] - 1.0
    ann_vol = float(np.std(ret, ddof=1) * math.sqrt(252))
    net_disp = abs(p[-1] - p[0])
    path = float(np.sum(np.abs(np.diff(p))))
    trend_ratio = float(net_disp / path) if path > 0 else 0.0
    return {
        "n_bars": int(len(p)),
        "p_start": float(p[0]),
        "p_end": float(p[-1]),
        "total_return": round(float(total_ret), 4),
        "ann_vol": round(ann_vol, 4),
        "trend_ratio": round(trend_ratio, 4),
    }


def _fetch_daily(key):
    """取该品种 2016-2019 的 (datetime_index, 收盘价)，已过滤到窗口内。失败/空返回 None。"""
    name, main_code, mult, tick = SYMBOLS[key]
    api = TqApi(TqSim(200000),
                backtest=TqBacktest(start_dt=BACKTEST_START, end_dt=BACKTEST_END),
                auth=_auth(), web_gui=False)
    try:
        k = api.get_kline_serial(main_code, 24 * 60 * 60, data_length=2000)
        while True:
            try:
                api.wait_update(deadline=time.time() + 30)
            except BacktestFinished:
                break
        dt = pd.to_datetime(k["datetime"], unit="ns")
        close = np.asarray(k["close"].to_numpy(), dtype=float)
        valid = (~np.isnan(close)) & (dt.astype("int64") > 0)
        dt = dt[valid]
        close = close[valid]
        mask = (dt >= pd.Timestamp(2016, 1, 1)) & (dt <= pd.Timestamp(2019, 12, 31))
        dt = dt[mask]
        close = close[mask]
        if len(close) < 10:
            return None
        return pd.Series(close, index=dt)
    finally:
        try:
            api.close()
        except Exception:
            pass


def main():
    out = {}
    for key in OLD_KEYS:
        name, main_code, _, _ = SYMBOLS[key]
        print(f"[取数] {key} {name} ({main_code}) ...", flush=True)
        try:
            s = _fetch_daily(key)
        except Exception as e:
            print(f"    [!] 取数失败：{e}", flush=True)
            out[key] = {"name": name, "code": main_code, "error": str(e)}
            continue
        if s is None or len(s) < 10:
            print(f"    [!] 空数据，跳过不写盘", flush=True)
            out[key] = {"name": name, "code": main_code, "error": "empty"}
            continue

        whole = _metrics(s.values)
        yearly = {}
        for y in YEARS:
            sub = s[s.index.year == y]
            if len(sub) >= 2:
                yearly[y] = _metrics(sub.values)

        out[key] = {"name": name, "code": main_code, "whole": whole, "yearly": yearly}
        print(f"    整段 2016-2019: 涨{whole['total_return']*100:+.1f}% "
              f"波动{whole['ann_vol']*100:.1f}% 趋势度{whole['trend_ratio']:.3f}", flush=True)

    # 落盘
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "趋势指标.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # ---- 汇总判读 ----
    print("\n" + "=" * 78)
    print("A 方案 · 2016-2019 趋势度实测汇总")
    print("=" * 78)
    cells, trending, choppy, mixed = [], 0, 0, 0
    for key in OLD_KEYS:
        rec = out.get(key, {})
        if rec.get("error") or "whole" not in rec:
            print(f"{key:4s} {rec.get('name','?'):6s} 取数失败/空")
            continue
        w = rec["whole"]
        print(f"\n{key} {rec['name']}  整段(2016-2019): "
              f"涨{w['total_return']*100:+.1f}%  波动{w['ann_vol']*100:.1f}%  "
              f"趋势度{w['trend_ratio']:.3f}  (n={w['n_bars']})")
        for y in YEARS:
            yy = rec.get("yearly", {}).get(y)
            if not yy:
                print(f"    {y}  无数据")
                continue
            if yy["trend_ratio"] >= 0.40 and abs(yy["total_return"]) >= 0.15:
                tag, trending = "趋势年", trending + 1
            elif yy["trend_ratio"] < 0.25:
                tag, choppy = "震荡年", choppy + 1
            else:
                tag, mixed = "混合", mixed + 1
            cells.append(yy["trend_ratio"])
            print(f"    {y}  涨{yy['total_return']*100:+.1f}%  "
                  f"波动{yy['ann_vol']*100:5.1f}%  趋势度{yy['trend_ratio']:.3f}  [{tag}]")

    if cells:
        avg = sum(cells) / len(cells)
        print("\n" + "-" * 78)
        print(f"年度格子共 {len(cells)} 个 | 平均趋势度 {avg:.3f} | "
              f"趋势年 {trending} / 震荡年 {choppy} / 混合 {mixed}")
        if avg >= 0.35:
            verdict = "整体偏趋势（不是震荡年）"
        elif avg < 0.25:
            verdict = "整体偏震荡"
        else:
            verdict = "趋势与震荡混杂"
        print(f"判定：平均趋势度 {avg:.3f} → {verdict}")
    print(f"\n原始数据已写入：{out_path}")


if __name__ == "__main__":
    main()
