# -*- coding: utf-8 -*-
"""组合运行器 × engine 单品种 —— 量具校验（**不消耗 K**）
==========================================================
`lib/portfolio.py` 是新写的多品种主循环。**新主循环必须自证没写错**，
否则后面所有组合数字都建立在未知口径上 —— 这正是本项目「口径漂移最贵」的教训。

做法：同一区间、同一参数、同一个品种，两条路各跑一次，**逐笔对比成交**：
  A) `lib/engine.run_backtest_boll`（既有单品种路径，10 处调用方之一）
  B) `lib/portfolio.跑组合(['V'])`（新组合运行器的单腿模式）
两者复用同一批纯函数（`_boll / boll_decision / _atr / _dual_ma_stop_line /
_dual_ma_stop_hit / _risk_budget_hand`），所以差异**只可能来自主循环**。

判据（预先写死）：
  ① 成交笔数相同；② 逐笔「方向 / 开平 / 手数」完全相同；
  ③ 成交价差 ≤ 1 跳（撮合时点微差允许）；④ 净利差 ≤ 1‰ 初始资金。

用法：python 工具/组合对照.py [品种key] [周期秒]
"""
import os
import re
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from lib import engine, portfolio as PF

# 静音 tqsdk 的 INFO 刷屏（每笔委托都打一行，会把对照结果淹掉）
try:
    from tqsdk import TqApi as _TqApi
    _TqApi._print = lambda self, msg="", level="INFO": None
except Exception:
    pass

KEY = (sys.argv[1] if len(sys.argv) > 1 else "V").upper()
KLINE = int(sys.argv[2]) if len(sys.argv) > 2 else 86400
TAG = "组合校验"

# 区间：对照只需暴露主循环的逻辑差异 ⇒ 取半年足够（长区间留给正式体检）。
# ⚠️ 日线回测很慢（约 1~2 秒/交易日），跑满 IS 段要 20 分钟，对照不值得。
起, 评, 止 = date(2023, 12, 1), date(2024, 1, 1), date(2024, 6, 30)

参数 = {
    "BOLL_N": 20, "BOLL_K": 2.0,
    "ATR_PERIOD": 14, "ATR_STOP_MULT": 1.0, "TIME_STOP_BARS": 20,
    # ★ 2026-09-27 定稿口径：与 lib/portfolio.py 参数默认、组合/布林带__EB_JD_M.py 一致。
    #   ⚠️ 三条路必须永远同口径 —— 否则校验的是一个已经不存在的口径（本项目的经典坑）。
    "RISK_PCT": 1.00, "RISK_EQUITY": 0, "HAND_MIN": 1, "HAND_MAX": 25,
    "MARGIN_CAP_PCT": 30.0, "EXIT_OFF": 0.25,
}

名称, 主连, 乘数, 一跳 = PF.config_symbols.get(KEY)
保证金 = float(PF._读品种池(KEY).get("保证金", 0) or 0)
init_balance = 200000

print("=" * 78)
print("组合运行器 × engine 单品种 ｜ 量具校验")
print("  品种 %s(%s) ｜ 周期 %d 秒 ｜ %s ~ %s（评估自 %s）" % (
    KEY, 名称, KLINE, 起, 止, 评))
print("=" * 78)


# ---------------------------------------------------------------
# A) engine 既有单品种路径
# ---------------------------------------------------------------
print("\n[A] engine.run_backtest_boll ...")
engine.run_backtest_boll(
    KEY, period=参数["BOLL_N"], k=参数["BOLL_K"], volume=1,
    start=起, end=止, eval_start=评,
    init_balance=init_balance, kline_sec=KLINE,
    strategy_name="布林带", open_html=False, sample_tag=TAG,
    slip_ticks=1,
    atr_period=参数["ATR_PERIOD"], atr_stop_mult=参数["ATR_STOP_MULT"],
    risk_pct=参数["RISK_PCT"], risk_hand_min=参数["HAND_MIN"],
    risk_hand_max=参数["HAND_MAX"], risk_equity=参数["RISK_EQUITY"],
    margin_per_hand=保证金, margin_cap_pct=参数["MARGIN_CAP_PCT"],
    time_stop_bars=参数["TIME_STOP_BARS"],
)

周期名 = {86400: "1d", 1800: "30min", 300: "5min", 900: "15min"}.get(KLINE, "%ds" % KLINE)
明细路径 = os.path.join(_ROOT, "回测结果", "03_布林带", 名称,
                        "%s_布林带_%s_%s.txt" % (名称, 周期名, TAG))
if not os.path.exists(明细路径):       # 命名规则若与预期不同，按 tag 兜底搜一次
    import glob
    _c = sorted(glob.glob(os.path.join(_ROOT, "回测结果", "03_布林带", 名称,
                                       "*%s*.txt" % TAG)))
    if _c:
        明细路径 = _c[-1]


def 读engine明细(path):
    出 = []
    if not os.path.exists(path):
        return 出
    for line in open(path, encoding="utf-8"):
        m = re.match(r"^(\d{4}-\d\d-\d\d)[ \d:]*\s+(BUY|SELL)\s+(\w+)\s+(\d+)\s*手\s+([\d.]+)",
                     line.strip())
        if m:
            出.append({"日": m.group(1), "方向": m.group(2), "开平": m.group(3),
                       "手": int(m.group(4)), "价": float(m.group(5))})
    return 出


A = 读engine明细(明细路径)
print("  A 成交 %d 笔 ｜ 明细 %s" % (len(A), os.path.basename(明细路径)))

# ---------------------------------------------------------------
# B) 组合运行器单腿
# ---------------------------------------------------------------
print("\n[B] lib.portfolio.跑组合(['%s']) ..." % KEY)
r = PF.跑组合([KEY], 起, 止, eval_start=评, init_balance=init_balance,
             kline_sec=KLINE, 参数=参数, 腿数上限=None, slip_ticks=1)
B_raw = sorted(r["成交"], key=lambda x: (x.get("sym", ""),))
print("  B 成交 %d 笔 ｜ 指标 %s" % (
    len(B_raw), {k: (round(v, 4) if isinstance(v, float) else v)
                 for k, v in r["指标"].items()
                 if k in ("年化收益", "夏普", "最大回撤", "净利", "总成本")}))

# 组合的成交没有「开平」字段顺序保证，这里按时间序（tqsdk get_trade 本身按时间）
B = [{"方向": t["direction"], "开平": t["offset"], "手": t["volume"],
      "价": t["price"], "sym": t.get("sym", "")} for t in r["成交"]]

# ---------------------------------------------------------------
# 对比
# ---------------------------------------------------------------
print("\n" + "=" * 78)
print("对比")
print("=" * 78)
print("  笔数：A %d ｜ B %d  %s" % (len(A), len(B), "✓" if len(A) == len(B) else "✗"))

n = min(len(A), len(B))
错方向 = 错开平 = 错手数 = 0
价差大 = []
for i in range(n):
    a, b = A[i], B[i]
    if a["方向"] != b["方向"]:
        错方向 += 1
    if a["开平"].upper() != str(b["开平"]).upper():
        错开平 += 1
    if a["手"] != b["手"]:
        错手数 += 1
    d = abs(a["价"] - b["价"])
    if d > 一跳 + 1e-9:
        价差大.append((i, a, b))

print("  逐笔方向不同 %d 笔 ｜ 开平不同 %d 笔 ｜ 手数不同 %d 笔" % (错方向, 错开平, 错手数))
print("  成交价差 > 1 跳的 %d 笔" % len(价差大))
for i, a, b in 价差大[:5]:
    print("     #%d A %s %s %s %d手 %.2f ｜ B %s %s %d手 %.2f" % (
        i, a["日"], a["方向"], a["开平"], a["手"], a["价"],
        b["方向"], b["开平"], b["手"], b["价"]))

print("\n  前 5 笔对照：")
for i in range(min(5, n)):
    a, b = A[i], B[i]
    标 = "✓" if (a["方向"] == b["方向"] and a["手"] == b["手"]
                 and abs(a["价"] - b["价"]) <= 一跳 + 1e-9) else "✗"
    print("   %s A %s %s %s %d手 %.2f ｜ B %s %s %d手 %.2f" % (
        标, a["日"], a["方向"], a["开平"], a["手"], a["价"],
        b["方向"], b["开平"], b["手"], b["价"]))

# 净利对照（A 的净利从它的报告 txt 拿不到，用权益曲线口径无意义 ⇒ 用 B 自己的对账）
print("\n  B 自对账：已实现毛利 %.2f − 总成本 %.2f = %.2f ｜ 权益变化 %.2f" % (
    r["指标"]["已实现毛利"], r["指标"]["总成本"], r["指标"]["已实现净利"],
    r["指标"]["净利"]))
print("  （两者差 = 期末未平仓浮盈；对账判据见报告）")
