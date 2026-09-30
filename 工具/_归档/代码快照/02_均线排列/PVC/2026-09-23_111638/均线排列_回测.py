# -*- coding: utf-8 -*-
"""
均线多空排列策略 —— 回测入口（30分钟 · 双向排列 + MACD 交叉，PVC 专用版本）
==========================================================
策略逻辑（核心共用 lib/engine.align_signal_dual_macd）：
  · 周期：30 分钟 K 线（均线/MACD 参数保持不变）
  · 均线：MA5 > MA10 > MA20 > MA40 多头排列 / MA5 < MA10 < MA20 < MA40 空头排列
  · 开多（空仓时）：多头排列 且 MACD(8,17,9) 水上金叉（DIF>0 且 DEA>0 且 DIF 上穿 DEA）
  · 开空（空仓时）：空头排列 且 MACD 水下死叉（DIF<0 且 DEA<0 且 DIF 下穿 DEA）——完全镜像
  · 平多（持多时）：收盘跌破最近确认摆动低点（v2 —— 替换原来的「MACD 死叉」）
  · 平空（持空时）：收盘上穿最近确认摆动高点（v2 —— 替换原来的「MACD 金叉」，完全镜像）
  · 止损：开仓时定死 2.0×ATR 固定线（多空对称，不移动；与摆动线**并存**，不是替代）
  · 双向。出场 = 摆动线结构穿越（收盘确认） 或 固定 ATR 止损（盘中触及）
  · ⚠️ v2 起「死叉/金叉平仓」停用 —— 连带原本靠它实现的
    「死叉+空头排列+水下 → 平多反手开空」这条反手路径也一并停用（归因见账本 v2 卡）

怎么用：
  1. 改下面「可调参数」
  2. 双击同目录 回测.bat
  3. 浏览器自动出报告（资金曲线 + 逐笔成交 + 指标卡片）

注：30 分钟 K 线下 MA40 需约 40 根（≈20 小时，约 3 个交易日）即完成预热，
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
SYMBOL_KEY = "V"
MA_PERIODS = (5, 10, 20, 40)          # 四根均线，判断多/空排列（短->长）
VOLUME = 1                            # 每次开仓手数（新手先 1 手）
STOP_ATR = 2.0                        # 固定 ATR 止损倍数（开仓时定死不移动；0=关闭）
# ---- v2（2026-09-23）：出场换成摆动线结构线（方案A：替换 MACD 死叉/金叉平仓）----
#   持多：收盘跌破最近确认摆动低点 -> 平多；持空：收盘上穿最近确认摆动高点 -> 平空。
#   收盘确认（盘中刺破不算）；摆动点确认根数 2 = 05 摆动线源指标口径（滞后 2 根，防重绘）。
#   与 STOP_ATR 的关系：两条线**独立并存**（2ATR 盘中触及即平、定死不动；摆动线收盘确认）。
SWING_EXIT = True                     # True=摆动线出场（v2） | False=回到 v1 的交叉出场
SWING_CONFIRM_BARS = 2                # 摆动点确认根数（2=源指标口径）
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
START = EVAL_START - timedelta(days=7)   # 预热：30分钟 MA40 约需 40 根(≈3个交易日)，前移 1 周足够
END   = EVAL_END
INIT_BALANCE = 200000                # 初始资金（元）
KLINE_SEC = 30 * 60                     # 30 分钟 K 线（v0 2026-09-19：15分钟 → 30分钟；均线/MACD 数值不变）
STOP_PCT = 0.0                        # 百分比止损（0=关闭）
WEB_GUI = True                        # 跑完用浏览器打开报告
# =================================================


alignment_signal = engine.align_signal_dual_macd  # 双向统一信号（空头侧完全镜像，见 lib/engine.py）

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
        stop_atr=STOP_ATR,
        swing_exit=SWING_EXIT, swing_confirm_bars=SWING_CONFIRM_BARS,
    )

    # 回测跑完 -> 自动刷新「修改记录汇总报告」
    #   文字说明在 工具/_账本/PVC_均线排列.json（人工维护）
    #   数字取自刚生成的报告，自动写入并重渲染 回测结果/02_均线排列/PVC/PVC_均线排列_汇总.html
    try:
        sys.path.insert(0, os.path.join(_ROOT, "工具"))
        import 汇总报告
        汇总报告.更新(SYMBOL_KEY, "均线排列", SAMPLE)
    except Exception as _e:
        print(f"[汇总] 刷新失败（不影响回测结果）：{_e}")
