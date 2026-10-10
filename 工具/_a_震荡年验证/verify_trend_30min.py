# -*- coding: utf-8 -*-
"""
A' 方案：在【双均线实际交易的 30 分钟尺度】重测方向性
========================================================
A 方案只在「日线 / 4 年」尺度测了方向性比率，但双均线是在 30 分钟、MA5/20
（数日）尺度交易的——尺度错配。本脚本在正确尺度上实测：

对 9 个老品种逐个【串行】取 2016-2019 的 30 分钟 K 线，计算：
    trend_ratio = |期末-期初| / Σ|每段价格变动|
        → 越接近 1 越有单向趋势；越接近 0 越是纯噪声缠绕。
    total_return / ann_vol / n_bars

目的：看在双均线真正博弈的尺度上，市场到底有没有"可捕的短趋势"。
如果 30 分钟方向性比率整段都 ≈ 0.01~0.05（近似随机游走），
说明双均线是在近乎纯噪声的尺度上找信号——这本身就能解释为何难赚钱，
但仍需结合双均线逐年盈亏交叉对照（结论里手写映射）。

严守取数铁律：① 禁并发（一次一个品种）② 空数据不写盘。
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

OLD_KEYS = ["MA", "M", "RB", "V", "JM", "JD", "P", "RU", "SR"]
BACKTEST_START = date(2015, 12, 25)
BACKTEST_END   = date(2019, 12, 31)
YEARS = [2016, 2017, 2018, 2019]
DURATION = 30 * 60                      # 30 分钟
ANN_FACTOR = math.sqrt(252 * 8)         # 30min 年化近似（日线 252 × 每日约 8 根 30min）


def _metrics(prices):
    p = np.asarray(prices, dtype=float)
    if len(p) < 2:
        return None
    ret = p[1:] / p[:-1] - 1.0
    total_ret = p[-1] / p[0] - 1.0
    ann_vol = float(np.std(ret, ddof=1) * ANN_FACTOR)
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


def _fetch_30min(key):
    name, main_code, mult, tick = SYMBOLS[key]
    api = TqApi(TqSim(200000),
                backtest=TqBacktest(start_dt=BACKTEST_START, end_dt=BACKTEST_END),
                auth=_auth(), web_gui=False)
    try:
        k = api.get_kline_serial(main_code, DURATION, data_length=9000)
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
        print(f"[取数] {key} {name} ({main_code}) 30min ...", flush=True)
        try:
            s = _fetch_30min(key)
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
        print(f"    30min 整段: 涨{whole['total_return']*100:+.1f}% "
              f"波动{whole['ann_vol']*100:.1f}% 趋势度{whole['trend_ratio']:.4f} (n={whole['n_bars']})", flush=True)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "趋势指标_30min.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 78)
    print("A' 方案 · 30 分钟尺度方向性实测汇总（双均线真实博弈尺度）")
    print("=" * 78)
    cells = []
    for key in OLD_KEYS:
        rec = out.get(key, {})
        if rec.get("error") or "whole" not in rec:
            print(f"{key:4s} {rec.get('name','?'):6s} 取数失败/空")
            continue
        w = rec["whole"]
        print(f"\n{key} {rec['name']}  30min整段(2016-2019): "
              f"涨{w['total_return']*100:+.1f}%  波动{w['ann_vol']*100:.1f}%  "
              f"趋势度{w['trend_ratio']:.4f}  (n={w['n_bars']})")
        for y in YEARS:
            yy = rec.get("yearly", {}).get(y)
            if not yy:
                print(f"    {y}  无数据")
                continue
            cells.append(yy["trend_ratio"])
            print(f"    {y}  涨{yy['total_return']*100:+.1f}%  "
                  f"波动{yy['ann_vol']*100:5.1f}%  趋势度{yy['trend_ratio']:.4f}")
    if cells:
        avg = sum(cells) / len(cells)
        print("\n" + "-" * 78)
        print(f"30min 年度格子共 {len(cells)} 个 | 平均趋势度 {avg:.4f}")
        print(f"→ 若平均趋势度 << 日线尺度(0.02~0.04)，说明在交易尺度上市场接近纯噪声缠绕")
    print(f"\n原始数据已写入：{out_path}")


if __name__ == "__main__":
    main()
