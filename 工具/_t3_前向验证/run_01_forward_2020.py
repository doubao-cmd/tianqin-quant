# -*- coding: utf-8 -*-
"""
01 双均线 v7 · T3 前向验证运行器（隔离版，不改动任何冻结脚本）
==========================================================
- 窗口：2020-01-01 ~ 2020-12-31（9 老品种唯一未看过的连贯整块）
- 9 老品种：MA M RB V JM JD RU P SR
- 串行、禁并发（严守取数铁律）
- 参数逐字取自 策略/01_双均线/回测/焦煤/双均线_回测.py 的 v7 冻结值
- 只捕获每品种 stdout 的 夏普率/收益率/年化收益/最大回撤/胜率，写 JSON
"""
import sys, os, io, re, json, contextlib
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib import engine

# ---- v7 冻结参数（与焦煤脚本逐字一致）----
N1, N2, N3 = 5, 20, 60
VOLUME = 1
TARGET_RISK_PCT = 1.0
HAND_MIN, HAND_MAX = 1, 8
KLINE_SEC = 30 * 60
INIT_BALANCE = 200000
ATR_STOP_MULT = 2.0
BREAKEVEN_ATR = 0.0
ATR_PERIOD = 14
NO_OPEN_WINDOW_MIN = 30
SLIP_TICKS = 1

# ---- 前向窗口：2020（预热 7 天）----
EVAL_START = date(2020, 1, 1)
EVAL_END   = date(2020, 12, 31)
START = EVAL_START - timedelta(days=7)

OLD9 = ["MA", "M", "RB", "V", "JM", "JD", "RU", "P", "SR"]

# 匹配 engine.py:5974-5979 的 stdout 摘要行
_PAT = {
    "ror":        re.compile(r"收益率\s+(-?\d+\.\d+)%"),
    "annual":     re.compile(r"年化收益\s+(-?\d+\.\d+)%"),
    "max_dd":     re.compile(r"最大回撤\s+(-?\d+\.\d+)%"),
    "win_rate":   re.compile(r"胜率\s+(-?\d+\.\d+)%"),
    "sharpe":     re.compile(r"夏普率\s+(-?\d+\.\d+)"),
}

def parse_summary(text):
    out = {}
    for k, p in _PAT.items():
        m = p.search(text)
        out[k] = float(m.group(1)) if m else None
    return out

def run_one(sym):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        engine.run_backtest(
            sym, engine.trend_reverse_signal, N1, N2, VOLUME,
            START, EVAL_END, INIT_BALANCE, KLINE_SEC,
            strategy_name="双均线", open_html=False, eval_start=EVAL_START,
            sample_tag="fwd2020",
            pass_dir=True, ma_filter=N3, slip_ticks=SLIP_TICKS,
            atr_stop_mult=ATR_STOP_MULT, breakeven_atr=BREAKEVEN_ATR,
            atr_period=ATR_PERIOD,
            risk_pct=TARGET_RISK_PCT, risk_hand_min=HAND_MIN, risk_hand_max=HAND_MAX,
            no_open_window_min=NO_OPEN_WINDOW_MIN,
        )
    return parse_summary(buf.getvalue())

def main():
    results = {}
    for sym in OLD9:
        print(f"[前向2020] 跑 {sym} ...", flush=True)
        try:
            r = run_one(sym)
            results[sym] = r
            print(f"          夏普={r.get('sharpe')}  年化={r.get('annual')}%  回撤={r.get('max_dd')}%", flush=True)
        except Exception as e:
            results[sym] = {"error": str(e)}
            print(f"          !! {sym} 失败: {e}", flush=True)

    # 汇总统计
    sharpes = [v["sharpe"] for v in results.values() if isinstance(v, dict) and v.get("sharpe") is not None]
    annuals = [v["annual"] for v in results.values() if isinstance(v, dict) and v.get("annual") is not None]
    n = len(sharpes)
    mean_sharpe = sum(sharpes) / n if n else None
    mean_annual = sum(annuals) / n if n else None
    pos_sharpe = sum(1 for s in sharpes if s > 0)

    MDES = 1.96  # K=1, T=1.0yr
    verdict = "PASS(支持H1)" if (mean_sharpe is not None and mean_sharpe >= MDES) else "FAIL(支持H0/无法区分)"

    summary = {
        "window": "2020-01-01~2020-12-31",
        "k": 1,
        "mdes": MDES,
        "n_commodities": n,
        "mean_sharpe": mean_sharpe,
        "mean_annual_pct": mean_annual,
        "positive_sharpe_count": pos_sharpe,
        "verdict": verdict,
        "per_commodity": results,
    }
    out_path = os.path.join(_ROOT, "前向2020_结果.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("\n================ 前向2020 汇总 ================")
    print(f"样本数 n={n}  K=1  MDES={MDES}")
    print(f"平均年化夏普 = {mean_sharpe}")
    print(f"平均年化收益 = {mean_annual}%")
    print(f"正夏普品种数 = {pos_sharpe}/{n}")
    print(f"判定 = {verdict}")
    print(f"结果已写: {out_path}")

if __name__ == "__main__":
    main()
