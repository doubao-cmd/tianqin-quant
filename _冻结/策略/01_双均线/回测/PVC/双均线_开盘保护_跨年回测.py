# -*- coding: utf-8 -*-
"""
双均线 开盘保护 跨年 A/B（2026-09-21，用户要求）
================================================
对 4 个历史区间，各自顺序跑两种模式，确保 A/B 严格同窗：
    原版 forward : no_open_window_min=0
    开盘保护 ow  : no_open_window_min=30
（其余 v6 参数逐字一致：30min / MA5,20,60 / 2xATR / 波动率预算1% / 手数[1,8] / 滑点1跳）
区间与 04 forward / 时间止损跨年实验一致（均避开 2025-03~09 唯一公共 OOS）：
    2024-2025 : 2024-03-13 ~ 2025-02-28
    2023      : 2023-03-13 ~ 2024-02-29
    2022      : 2022-03-13 ~ 2023-02-28
    2021      : 2021-03-13 ~ 2022-02-28
2026 IS(2026-03-13~09-13) 已单独跑过：v6=+1.80% / 开盘保护=+5.92%，此处不再重跑。

★ 按用户 2026-09-21 指示：往年不写新报告文件，只留 26 年报告对比、减少工作量。
  故此处临时把引擎的 _save_backtest_report 换成空操作（不改 engine.py 本身），
  往年运行只打印关键指标，不落 html/txt。
"""
import sys
import os
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib import engine

# 往年不写报告：把引擎的报告落盘函数临时换成空操作
engine._save_backtest_report = lambda *a, **k: (None, None)

SYMBOL_KEY = "V"
N1, N2, N3 = 5, 20, 60
VOLUME = 1
TARGET_RISK_PCT = 1.0
HAND_MIN, HAND_MAX = 1, 8
INIT_BALANCE = 200000
KLINE_SEC = 30 * 60
ATR_STOP_MULT = 2.0
BREAKEVEN_ATR = 0.0
ATR_PERIOD = 14

WINDOWS = [
    ("2024-2025", date(2024, 3, 13), date(2025, 2, 28)),
    ("2023",      date(2023, 3, 13), date(2024, 2, 29)),
    ("2022",      date(2022, 3, 13), date(2023, 2, 28)),
    ("2021",      date(2021, 3, 13), date(2022, 2, 28)),
]


def _val(obj, key):
    try:
        return obj[key]
    except Exception:
        try:
            return getattr(obj, key)
        except Exception:
            return None


def run_one(label, ev_start, ev_end, win_min):
    start = ev_start - timedelta(days=7)
    ret = engine.run_backtest(
        SYMBOL_KEY, engine.trend_reverse_signal, N1, N2, VOLUME,
        start, ev_end, INIT_BALANCE, KLINE_SEC,
        strategy_name="双均线", open_html=False, eval_start=ev_start,
        sample_tag=f"ow{win_min}{label}",
        pass_dir=True,
        ma_filter=N3,
        slip_ticks=1,
        atr_stop_mult=ATR_STOP_MULT,
        breakeven_atr=BREAKEVEN_ATR,
        atr_period=ATR_PERIOD,
        risk_pct=TARGET_RISK_PCT,
        risk_hand_min=HAND_MIN,
        risk_hand_max=HAND_MAX,
        no_open_window_min=win_min,
    )
    mode = "OW(30)" if win_min > 0 else "FWD"
    ror = _val(ret, "ror")
    mdd = _val(ret, "max_drawdown")
    win = _val(ret, "winning_rate")
    shr = _val(ret, "sharpe_ratio")
    plr = _val(ret, "profit_loss_ratio")
    def pct(x):
        return f"{x:.2%}" if isinstance(x, (int, float)) else str(x)
    def num(x):
        return f"{x:.2f}" if isinstance(x, (int, float)) else str(x)
    print(f"  >> {label} {mode} : 收益={pct(ror)} 回撤={pct(mdd)} "
          f"胜率={pct(win)} 盈亏比={num(plr)} 夏普={num(shr)}")
    return ret


if __name__ == "__main__":
    print("=" * 64)
    print("双均线 开盘保护 跨年 A/B（FWD vs OW30），往年不写报告")
    print("=" * 64)
    summary = []
    for label, es, ee in WINDOWS:
        print(f"\n########## 区间 {label} : {es} ~ {ee} ##########")
        print("--- 原版 forward ---")
        fwd = run_one(label, es, ee, 0)
        print("--- 开盘保护 ow30 ---")
        ow = run_one(label, es, ee, 30)
        fr = _val(fwd, "ror"); orr = _val(ow, "ror")
        delta = (orr - fr) if isinstance(fr, (int, float)) and isinstance(orr, (int, float)) else None
        summary.append((label, fr, orr, delta))
    print("\n" + "=" * 64)
    print("跨年 A/B 汇总（收益率对比；FWD=原版 / OW=开盘保护30min）")
    print(f"{'区间':<12}{'FWD':>10}{'OW30':>10}{'Δ':>10}")
    for label, fr, orr, delta in summary:
        def p(x):
            return f"{x:+.2%}" if isinstance(x, (int, float)) else str(x)
        print(f"{label:<12}{p(fr):>10}{p(orr):>10}{p(delta):>10}")
    print("=" * 64)
    print("DONE（往年报告已抑制，仅打印指标；2026 IS 报告保留作对比）")
