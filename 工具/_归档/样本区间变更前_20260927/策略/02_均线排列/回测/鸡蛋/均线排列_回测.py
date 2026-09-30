# -*- coding: utf-8 -*-
# ⚠️ 本文件由「豆粕」版机械复制生成（2026-09-27 批量补品种，12 品种口径统一）。
#    品种相关处（SYMBOL_KEY / 报告路径 / 账本路径 / 滑点说明）已换成本品种；
#    docstring 里的版本沿革与实验数字记录的是**原品种「豆粕」**的过程，本品种未单独复现。
#    核心买卖逻辑在 lib/engine.py，各品种共用同一份，不会走偏。
"""
均线多空排列策略 —— 回测入口（15分钟 · 多头排列 + MACD 水上金叉）
==========================================================
策略逻辑（核心共用 lib/engine.align_signal_long_macd）：
  · 周期：15 分钟 K 线
  · 均线：MA5 > MA10 > MA20 > MA40 多头排列（四线依次向上）
  · 开仓（空仓时）：多头排列 且 MACD(8,17,9) 水上金叉（DIF>0 且 DEA>0 且 DIF 上穿 DEA）同时成立 -> 做多
  · 平仓（持多时）：MACD 死叉（DIF 下穿 DEA）-> 平多
  · 仅做多。出场只看 MACD 死叉，无止盈、无止损

怎么用：
  1. 改下面「可调参数」
  2. 双击同目录 回测.bat
  3. 浏览器自动出报告（资金曲线 + 逐笔成交 + 指标卡片）

注：15 分钟 K 线下 MA40 需约 40 根（≈10 小时，约 2 个交易日）即完成预热，
START 已前移到评估区间前 1 周；EVAL_START 之前只预热、不交易。
样本内(练习)/样本外(期末考试) 切分见下方「可调参数」里的 SAMPLE / IS_* / OOS_*。
"""
import sys
import os
from datetime import date, timedelta

# 把项目根目录（天勤量化/）加到搜索路径，import lib / config 才找得到
_ROOT = os.path.dirname(os.path.abspath(__file__))  # 从脚本所在目录开始
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)  # 逐级向上，找到含 lib 的项目根目录为止
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib import engine

# ==================== 可调参数 ====================
# 本文件夹对应的品种（已写死在下方 SYMBOL_KEY）
SYMBOL_KEY = "JD"
MA_PERIODS = (5, 10, 20, 40)          # 四根均线，判断多头排列（短->长）
VOLUME = 1                            # 每次开仓手数（新手先 1 手）
# ---- 样本内(练习) / 样本外(期末考试) 切分（规则3：样本外仅用于最终验证，看过一次即作废）----
# 练习(样本内)     = 2026-03-13 ~ 2026-09-13   一直用来开发/调参的那段，可反复跑
# 期末考试(样本外) = 2025-03-13 ~ 2025-09-13   去年同期，从没看过，只跑一次
# 切分由用户 2026-09-17 决定（"今年3-9是练习、去年3-9是期末考试"），定稿后不得再改。
IS_START  = date(2026, 3, 13)   # 练习(样本内)开始
IS_END    = date(2026, 9, 13)   # 练习(样本内)结束
OOS_START = date(2025, 3, 13)   # 期末考试(样本外)开始
OOS_END   = date(2025, 9, 13)   # 期末考试(样本外)结束
SAMPLE = "in"                   # "in"=练习(可反复跑) | "out"=期末考试(只跑一次)
assert SAMPLE in ("in", "out"), f"SAMPLE 只能填 'in' 或 'out'，现在是 {SAMPLE!r}"
EVAL_START = IS_START if SAMPLE == "in" else OOS_START
EVAL_END   = IS_END   if SAMPLE == "in" else OOS_END
START = EVAL_START - timedelta(days=7)   # 预热：15分钟 MA40 约需 40 根(≈2.5个交易日)，前移 1 周足够
END   = EVAL_END
INIT_BALANCE = 200000                # 初始资金（元）
KLINE_SEC = 15 * 60                    # 15 分钟 K 线
STOP_PCT = 0.0                        # 百分比止损（0=关闭）
WEB_GUI = True                        # 跑完用浏览器打开报告
# =================================================


alignment_signal = engine.align_signal_long_macd  # 共用统一信号（见 lib/engine.py）

if __name__ == "__main__":
    _label = {"in": "练习(样本内，可反复跑)", "out": "期末考试(样本外，只跑一次)"}[SAMPLE]
    print(f"[样本模式] {_label} | 回测区间 {START} ~ {END}")
    if SAMPLE == "out":
        print("!! 期末考试提示：样本外区间只应跑一次，跑完即作废，此后不得再据此修改策略 !!")
    engine.run_backtest_align(
        SYMBOL_KEY, alignment_signal, MA_PERIODS, VOLUME,
        START, END, eval_start=EVAL_START, init_balance=INIT_BALANCE,
        kline_sec=KLINE_SEC, strategy_name="均线排列",
        open_html=WEB_GUI, stop_pct=STOP_PCT, sample_tag=SAMPLE,
    )
