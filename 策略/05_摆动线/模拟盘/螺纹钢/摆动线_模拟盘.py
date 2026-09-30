# -*- coding: utf-8 -*-
# ⚠️ 本文件由「PVC」版机械复制生成（2026-09-27 批量补品种，12 品种口径统一）。
#    品种相关处（SYMBOL_KEY / 报告路径 / 账本路径 / 滑点说明）已换成本品种；
#    docstring 里的版本沿革与实验数字记录的是**原品种「PVC」**的过程，本品种未单独复现。
#    核心买卖逻辑在 lib/engine.py，各品种共用同一份，不会走偏。
"""
摆动线策略 —— 模拟盘入口（30分钟 · 摆动点结构双向反手，零参数裸版）
==========================================================
逻辑与回测完全一致（swing_decision 同一状态机）。先观察、再真下单。
双向反手：收盘上穿最近确认摆动高点开多 / 收盘跌破最近确认摆动低点开空。
移动止盈线=持仓方向最近确认摆动点（只前进不后退）；出场与反手是同一事件。
"""
import sys
import os

# 把项目根目录（天勤量化/）加到搜索路径
_ROOT = os.path.dirname(os.path.abspath(__file__))  # 从脚本所在目录开始
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)  # 逐级向上，找到含 lib 的项目根目录为止
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib import engine

# ==================== 可调参数 ====================
SYMBOL_KEY = "RB"
VOLUME = 1
KLINE_SEC = 30 * 60                    # 30 分钟 K 线（与回测同口径）
ALLOW_TRADE = False                   # [!] 先观察：False = 只打印信号不下单；确认无误改 True
LEDGER_DIR = os.path.join(_ROOT, "实测记录", "账户")   # 账户流水单目录（2026-09-24 新增）
    # ★ 账户视角：不管哪个策略/哪个品种，账户里所有真实成交都记进这里
    #   · 成交明细.csv —— 每笔成交：成交价/委托价/滑点/手续费/平仓盈亏/成交额/拒单
    #   · 账户快照.csv —— 每 5 分钟一次账户全景：权益/可用/保证金/浮盈/持仓明细
    #   · 流水单事件.log —— 漏单、方向冲突等
    #   与「策略视角」的 _成交流水.csv 互补，互不替代。
WEB_GUI = True                        # 是否打开天勤 web_gui 面板（实时K线/信号/持仓/资金曲线）
# ---- v1（2026-09-23）：波动率预算开仓（与回测/01/03 同口径）----
#   手数 = 账户权益 × RISK_PCT% ÷ (2.0×ATR×乘数)，夹 [1, 8]；2.0×ATR 仅是风险刻度非止损线
RISK_PCT = 1.0
RISK_HAND_MIN = 1
RISK_HAND_MAX = 8
# ---- v2（2026-09-23）：固定止损（方案A，与回测同口径）----
#   开仓定死 信号根收盘价 ∓ STOP_ATR×信号根ATR；触发后空仓等下一次结构翻转
STOP_ATR = 2.0                        # 0=关闭

# ---- 权益口径（2026-09-27 补齐）----
#   ★ 原缺这一项 -> 手数按账户【实时权益】算（快期模拟户 100 万），
#     而回测按 init_balance=20 万 -> 两边手数不一致、对不上账。
#   传固定值即与回测同口径。0 = 用账户实时权益（旧行为，不推荐）。
RISK_EQUITY = 200000.0

# ---- 运行记录（2026-09-27 补齐，与 01/03/04 一致）----
#   把「程序有没有在跑」和「有没有开仓条件」分开落盘，供事后做台账：
#     实测记录/05_摆动线/螺纹钢/螺纹钢_成交流水.csv  （事件流）
#     实测记录/05_摆动线/螺纹钢/螺纹钢_运行心跳.csv  （每 5 分钟一行，**休市照写**）
#   想关掉记录：把 RECORD_DIR 改成 None。
RECORD_DIR = os.path.join(_ROOT, "实测记录", "05_摆动线", "螺纹钢")
RECORD_HEARTBEAT_SEC = 300   # 心跳落盘间隔（秒）
# =================================================


if __name__ == "__main__":
    engine.run_live_swing(
        SYMBOL_KEY, volume=VOLUME,
        kline_sec=KLINE_SEC, allow_trade=ALLOW_TRADE,
        web_gui=WEB_GUI,
        risk_pct=RISK_PCT, risk_hand_min=RISK_HAND_MIN,
        risk_hand_max=RISK_HAND_MAX,
        risk_equity=RISK_EQUITY,       # ★ 与回测 init_balance 对齐
        stop_atr=STOP_ATR,
        ledger_dir=LEDGER_DIR,
        record_dir=RECORD_DIR,
        record_heartbeat_sec=RECORD_HEARTBEAT_SEC,
    )
