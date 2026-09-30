# -*- coding: utf-8 -*-
"""
==========================================================
 公共引擎：回测 / 模拟盘 通用逻辑
==========================================================

每个策略只需要做两件事：
  1. 写一个信号函数  cross_signal(now_f, now_s, pre_f, pre_s) -> int
       返回 +1 表示做多、-1 表示做空、0 表示不动
  2. 调本文件的函数运行：
       engine.run_backtest(...)   跑历史回测（自动生成按品种命名的 HTML 报告）
       engine.run_live(...)       跑模拟盘（快期模拟账户）

换月、下单、持仓、统计、HTML 报告这些脏活都在这处理，
策略文件里只写"什么时候该买该卖"这一句逻辑就够了。

目录约定（回测结果自动落盘，2026-09-17 起分「策略 -> 品种」两层）：
  回测结果/<策略目录>/<品种中文名>/<品种中文名>_<短名>_<周期>[_in|_out].html
  回测结果/<策略目录>/<品种中文名>/<品种中文名>_<短名>_<周期>[_in|_out].txt
  （<策略目录> = 01_双均线 / 02_均线排列 / 03_布林带，与 策略/ 下的编号目录一一对应，
    映射表在 lib/strategy_dirs.py，改编号只改那一处；
    <短名> = 双均线 / 均线排列 / 布林带，只出现在报告文件名和报告标题里；
    <周期> 由 kline_sec 推导：5min / 15min / 1d；in=练习区间，out=期末考试区间）
实测（模拟盘）单独放，同样两层：
  实测记录/<策略目录>/<品种中文名>/      -> 占位空文件夹，运行时再填

注意：本文件依赖根目录的 config_account.py / config_symbols.py，
所以调用方（策略入口 .py）必须先把项目根目录加到 sys.path。
"""

import sys
import os
import csv
import atexit
import webbrowser
import re
import time
import numpy as np

# 把项目根目录（天勤量化/）加入搜索路径，这样 import config_account 才能找到
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_account
import config_symbols

# 策略短名 -> 落盘目录名（带 01_/02_/03_ 编号），唯一出处见 lib/strategy_dirs.py
from lib.strategy_dirs import 策略目录名

from datetime import date, datetime, timedelta
import pandas as pd

from tqsdk import (
    TqApi, TqAuth, TqBacktest, TqSim, TqKq, TargetPosTask, BacktestFinished
)

PY = "C:/Users/cc/.workbuddy/binaries/python/envs/tqsdk/Scripts/python.exe"

# ===================================================================
# web_gui 面板地址捕获（tqsdk 3.10.2 把随机端口只打印到 stdout，不挂属性）
# ===================================================================
_captured_gui_url = {"url": None}


def _tqapi_print_with_gui_capture(self, msg="", level="INFO"):
    """包裹 TqApi._print：截获 '您可以访问 http://127.0.0.1:PORT' 这行，
    把面板地址存到 _captured_gui_url，再照常打印。"""
    if msg and "您可以访问 http://" in msg:
        m = re.search(r"http://127\.0\.0\.1:\d+", msg)
        if m:
            _captured_gui_url["url"] = m.group(0)
    return _orig_tqapi_print(self, msg, level)


_orig_tqapi_print = TqApi._print
TqApi._print = _tqapi_print_with_gui_capture




# ===================================================================
# 回测
# ===================================================================
# ===================================================================
# 双均线 + 趋势过滤（01）—— 回测与模拟盘共用同一份决策函数
#   金叉/死叉只决定「方向意图」，MA20 与 MA60 的关系决定「是否允许开反向仓」
# ===================================================================
def trend_reverse_signal(cur_dir, now_f, now_s, pre_f, pre_s, now_t, pre_t):
    """双均线交叉 + MA60 趋势过滤（回测 / 模拟盘共用，保证两侧逻辑不走偏）。

    输入全部取自「刚刚收盘的那根 K 线」：
      now_f/pre_f = 快线 MA(n1) 当前/上一根，now_s/pre_s = 慢线 MA(n2)，
      now_t/pre_t = 趋势过滤线 MA(ma_filter，通常 60)。
    不看未收盘数据，不含未来函数。

    规则（cur_dir = 当前持仓方向：+1 多 / -1 空 / 0 空仓）：
      金叉（快线上穿慢线）且 MA20 >  MA60  -> 目标 +1（做多；持空则反手做多）
      金叉            且 MA20 <= MA60      -> 目标  0（只平空，空仓等待，不反手）
      死叉（快线下穿慢线）且 MA20 <  MA60  -> 目标 -1（做空；持多则反手做空）
      死叉            且 MA20 >= MA60      -> 目标  0（只平多，空仓等待，不反手）
      无交叉：维持当前方向

    返回目标方向 -1 / 0 / +1；与 cur_dir 相同 = 本根不动。
    """
    crossed_up = pre_f <= pre_s and now_f > now_s      # 金叉
    crossed_dn = pre_f >= pre_s and now_f < now_s      # 死叉
    if crossed_up:
        if cur_dir > 0:
            return cur_dir                             # 已持多，不重复动作
        return 1 if now_s > now_t else 0               # 多头环境才做多，否则平空等待
    if crossed_dn:
        if cur_dir < 0:
            return cur_dir                             # 已持空，不重复动作
        return -1 if now_s < now_t else 0              # 空头环境才做空，否则平多等待
    return cur_dir


def _dual_ma_stop_line(entry, entry_atr, direction, atr_stop_mult,
                       best_fav=0.0, breakeven_atr=0.0):
    """双均线策略的「止损线」—— 纯函数，便于单测。

    两个机制，可以单独开、也可以同时开：
      ① 初始止损（atr_stop_mult > 0，2026-09-18 阶段A）
           多单：开仓基准价 - atr_stop_mult × 开仓时ATR
           空单：开仓基准价 + atr_stop_mult × 开仓时ATR
      ② 保本（breakeven_atr > 0，2026-09-18 阶段B）
           持仓期间最大有利偏移 ≥ breakeven_atr × 开仓时ATR 后，
           把止损线移到开仓基准价（多单上抬、空单下压），此后不再动。
    两个都开时取「更有利」的那一条（多单取 max、空单取 min），即谁更近用谁。

    只到「移到开仓基准价」为止，不含移动/追踪止损。
    幅度一律用「开仓那一刻的 ATR」锁定，持仓期间不随 ATR 漂移 ——
    否则止损线会自己走动，出场原因就说不清了。

    参数：
      entry     : 开仓基准价（= 发出信号那根 K 线的收盘价，与均线排列同口径）
      entry_atr : 开仓那一刻锁定的 ATR
      best_fav  : 持仓期间最大有利偏移（点）；调用方用【已收盘】K 线的极值维护
      atr_stop_mult / breakeven_atr : <= 0 表示该机制关闭

    返回止损价；两个机制都没启用、或 ATR 无效时返回 None。
    """
    # 判正而不是判负：float('nan') 是 truthy、且 nan <= 0 恒为 False，
    # 用 `not entry_atr or entry_atr <= 0` 会让 NaN 漏过去，最后返回一条 NaN 止损线
    # —— 后果不是报错而是「永不触发」的静默失效，最难查。故写成 not (x > 0)。
    if direction == 0 or not (entry_atr and entry_atr > 0):
        return None
    line = None
    if atr_stop_mult > 0:
        line = (entry - atr_stop_mult * entry_atr if direction > 0
                else entry + atr_stop_mult * entry_atr)
    # 保本：浮盈够了就把线挪到开仓基准价。best_fav 为 NaN 时比较恒为 False，安全。
    if breakeven_atr > 0 and best_fav >= breakeven_atr * entry_atr:
        if line is None:
            line = entry
        elif direction > 0:
            line = max(line, entry)
        else:
            line = min(line, entry)
    return line


def _dual_ma_stop_hit(stop_line, direction, bar_high, bar_low):
    """判断【已收盘】那根 K 线有没有碰到止损线 —— 纯函数，便于单测。

    多单：最低价 <= 止损线 视为触及
    空单：最高价 >= 止损线 视为触及
    止损线为 None（未启用/ATR 无效）或方向为 0 时一律返回 False。

    用 high/low 而不是 close，语义是「盘中触价即出场」。注意成交时点：
    本函数只负责判定，实际下单发生在该根收盘后、成交落在下一根 K 线 ——
    这个「判定用当根极值、成交跳一根」的口径与「均线排列」策略一致。
    """
    if stop_line is None or direction == 0:
        return False
    if direction > 0:
        return bar_low <= stop_line
    return bar_high >= stop_line


def _risk_budget_hand(equity, risk_pct, atr_val, stop_mult, mult,
                      hand_min=1, hand_max=8):
    """按「单笔目标风险 = 权益 × risk_pct」反推开仓手数 —— 纯函数，便于单测。

    v6 波动率预算（2026-09-19）。口径：
        单笔风险(元) = 手数 × (stop_mult × ATR) × 合约乘数
        => 手数 = 权益 × risk_pct / (stop_mult × ATR × 乘数)

    为什么分母用 stop_mult × ATR：止损线就挂在「开仓基准价 ∓ stop_mult × ATR」
    处，所以「被止损打掉时亏多少」正好是 手数 × stop_mult × ATR × 乘数 ——
    风险预算与实际风险是同一把尺子，不引入任何额外假设，也不用估波动率倍数。

    ⚠️ 它改的只是「开多少手」，不含任何出场逻辑：
       不加移动止损、不加保本、不加分级/分批平仓（规则4：每次只改一处）。
    ⚠️ 它是纯杠杆刻度：每笔盈亏与每笔风险的比值（R）不变，
       所以收益率、回撤按同一倍数整体放大，夏普/卡玛不会因此改善。
       看到收益率变大先回来确认手数，别当成策略变好（这也是本函数存在的意义）。

    边界（吸取结论5「NaN 静默失效」的教训，一律判正而不是判负）：
      · 权益 / risk_pct / stop_mult / 乘数 任一非正，或 ATR 无效（NaN、<=0）
        -> 退回 hand_min（不放大、不静默失效）
      · 结果四舍五入后夹在 [hand_min, hand_max]
    """
    if not (equity and equity > 0) or not (risk_pct and risk_pct > 0) \
            or not (stop_mult and stop_mult > 0) \
            or not (atr_val and atr_val > 0) or not (mult and mult > 0):
        return hand_min
    risk_money = equity * risk_pct / 100.0        # 单笔允许亏掉多少钱
    per_hand = stop_mult * atr_val * mult         # 每手被打止损亏多少钱
    n = int(round(risk_money / per_hand))
    return max(hand_min, min(hand_max, n))


def live_reconcile_action(want, pos_now, persisted_sec, timeout_sec=180.0):
    """【纯函数】模拟盘「目标仓位 want 与实际持仓 pos_now 对不上」时该怎么办。

    为什么需要它（2026-09-20 新增，只服务模拟盘/实盘，回测不用）：
      run_live 里 `want` 只在启动时同步过一次，之后全由信号写。一旦开仓
      「一点没成交」或「只成交一部分」，want 仍停在原目标 —— 而主循环里
      cur_dir = sign(want)，信号函数会认为「已经在场」，于是 sig == cur_dir
      -> acted=False：**既不补单、也不会再开，策略就这么空仓看着行情走完**，
      直到下一次反向交叉才醒过来。回测踩不到（TqSim 一定会成交）。

    设计原则：**不能一发现不一致就改 want**。TargetPosTask 是异步的 ——
      set_target_volume 之后要等后续 wait_update 才下单、才撮合，正常有几秒
      延迟。立刻回写会把刚下的单子当成「没成交」而重复触发。所以先用时间
      窗口把「正常撮合延迟」和「真的开不出来」分开。

    ★ 顺带与既有裁决对齐：结论 20 已定「限价挂不上就放弃」，所以这里超时后
      也选择**放弃这一笔**（清零、等下一个信号），而不是无限等待成交。

    Args:
        want         : 目标仓位（正=多，负=空，0=空仓）
        pos_now      : 实际持仓手数（同符号约定）
        persisted_sec: 这次「不一致」已经持续了多少秒
        timeout_sec  : 容忍阈值，小于它一律视为「还在撮合」

    Returns:
        (动作, 新的 want)
          ("ok", want)        一致 -> 无需处理（调用方把计时器清掉）
          ("wait", want)      未超时 -> 正常撮合延迟，什么都不做
          ("accept", pos_now) 超时、同向但**手数不够**（部分成交/开不满）
                              -> 接受实际持仓，别无限等（风险比目标小，安全侧）
          ("trim", want)      超时、同向但**手数反而多了** -> 重发目标把它收回
                              （风险比目标大，必须收；也覆盖「有人在快期3
                                手动下了同一合约」的情况 —— 本程序假定自己独占
                                该合约持仓，会把它拉回目标）
          ("retry", 0)        超时、want=0 却仍有持仓（平仓单没成交）
                              -> 重发平仓
          ("flush", 0)        超时、want≠0 却完全空仓（一点没成交）
                              -> 清零 + 关止损监控
          ("conflict", 0)     超时、方向相反（正常流程不该出现）
                              -> 清零 + 关止损监控 + 告警

    ⚠️ 它只对齐「目标 vs 实际」这一件事，**不产生任何新的开仓意图** ——
       清零之后要等下一个真信号才会再开（规则2：不加条件、不择优）。
    """
    if want == pos_now:
        return "ok", want
    if persisted_sec < timeout_sec:
        return "wait", want
    if want == 0:                     # 想空仓却还有持仓 -> 平仓单没成交
        return "retry", 0
    if pos_now == 0:                  # 想开仓却一手没有 -> 一点都没成交
        return "flush", 0
    if (want > 0) == (pos_now > 0):   # 同方向、手数不符
        if abs(pos_now) < abs(want):
            return "accept", pos_now
        return "trim", want
    return "conflict", 0              # 方向相反


# ===================================================================
# 通道突破（04）—— 回测与模拟盘共用同一份决策函数
#   入场：收盘价突破「前 n1 根」的最高价 / 最低价 -> 开仓
#   出场：收盘价反向突破「前 n2 根」的另一侧     -> 平仓（出场即空仓，不反手）
#   两套轨道各管一件事：n1 决定「要不要进场」，n2 决定「要不要离场」。
#   这是海龟交易法 System 1 的原始结构（20 根进 / 10 根出）；参数是外生给定的
#   经典值，不是在本项目数据上扫出来的 —— 因此不扫描 n1/n2（规则2：不遍历参数择优）。
# ===================================================================
def channel_signal(cur_dir, en_up, en_dn, ex_up, ex_dn, close_now):
    """通道突破（唐奇安）目标仓位决策 —— 纯函数，便于单测。

    输入全部取自「刚刚收盘的那根 K 线」：
      en_up / en_dn = 入场轨道：前 n1 根（不含当前根）的最高价 / 最低价
      ex_up / ex_dn = 出场轨道：前 n2 根（不含当前根）的最高价 / 最低价
      close_now     = 刚收盘那根的收盘价
    调用方负责 shift(1)，本函数不做任何时间偏移；不看未收盘数据，不含未来函数。

    规则（cur_dir = 当前持仓方向：+1 多 / -1 空 / 0 空仓）：
      持多：close_now < ex_dn -> 目标 0（平多离场）
      持空：close_now > ex_up -> 目标 0（平空离场）
      空仓：close_now > en_up -> 目标 +1（做多）
            close_now < en_dn -> 目标 -1（做空）
      其余：维持当前方向

    ★ 为什么「出场即空仓、不反手」：n1 与 n2 是两个独立参数、对应两条独立规则
      （进场规则 / 离场规则），这正是海龟原版的结构。若跌破出场轨道就直接反手，
      出场轨道实际上变成了「反手轨道」，两个参数的分工被抹掉，事后也就说不清
      到底是哪一条在起作用。出场之后若价格继续走，会再由 en_up/en_dn 触发新入场。
      副作用：会出现较长的空仓等待期（与 01 的「只平仓、不反手」是同一形态）。

    ★ 为什么用「收盘价确认」而不是盘中触价：同一根 K 线里 high 与 low 谁先到
      在回测层是不知道的（没有 tick 级先后），用触价会出现「先止损还是先开仓」
      的歧义。收盘确认比原版唐奇安晚约 1 根，属有意的口径选择，不是缺陷。

    ★ 为什么不必单独判断「反向突破入场轨道」：n2 < n1 时
      min(low 前 n1 根) <= min(low 前 n2 根)、max(high 前 n1 根) >= max(high 前 n2 根)，
      所以价格跌破入场下轨时必然已经先跌破了出场下轨 -> 早已离场，逻辑自洽。

    轨道无效（NaN，预热不足）时：入场不成立（开不了仓）、出场不成立（不动），
    一律返回 cur_dir —— 与结论 5「NaN 静默失效」的教训一致：判正而不是判负。
    """
    if not (close_now > 0):                    # NaN / 非法价格 -> 什么都不做
        return cur_dir
    if cur_dir > 0:
        if ex_dn == ex_dn and close_now < ex_dn:      # ex_dn == ex_dn 过滤 NaN
            return 0
        return cur_dir
    if cur_dir < 0:
        if ex_up == ex_up and close_now > ex_up:
            return 0
        return cur_dir
    # 空仓：两条入场轨道都有效才允许开仓
    if en_up == en_up and en_dn == en_dn:
        if close_now > en_up:
            return 1
        if close_now < en_dn:
            return -1
    return cur_dir


def channel_gate_ok(sig, gate_now, ma_slow):
    """通道突破的 MA 趋势闸门 —— 纯函数，便于单测。

    只回答一个问题：空仓前提下，通道给出的入场信号 sig 是否**允许**执行。
      MA(gate_fast) 在 MA(ma_filter) 之上 -> 只许做多（sig = +1 放行，−1 拦下）
      MA(gate_fast) 在 MA(ma_filter) 之下 -> 只许做空（sig = −1 放行，+1 拦下）

    这是 01 双均线那条闸门的原样复刻（结论 19：闸门贡献 92% 的差异），
    不是另造口径 —— 「闸门」在两处必须指同一件事，先验才能迁移。

    ★ 只管「开仓许可」：平仓（sig = 0）恒为 True，出场规则自己负责离场。
    ★ NaN 一律返回 False（不许可）—— 判正不判负，见结论 5：
      `not x or x <= 0` 对 NaN 会漏网，必须写成 `not (x and x > 0)`。
    """
    if sig == 0:
        return True
    if not (gate_now == gate_now and ma_slow == ma_slow):   # NaN -> 不许可
        return False
    return (gate_now > ma_slow) if sig > 0 else (gate_now < ma_slow)


def turtle_failsafe_after_close(float_profit):
    """海龟 System 1 的 failsafe 过滤器 —— 平仓后更新「下一次要不要跳过」。

    原版规则（《海龟交易法则》System 1）：「如果上一次突破信号是赚钱的，
    就跳过下一个突破信号」。System 2（55 日，无过滤）没有这条。

    ★ 口径（写死，可复现；项目首次实现，故把自由度全钉住）：
      · 判据 = 该笔交易的**盈亏符号**（float_profit > 0 视为盈利）。
        ⚠️ 用 TqSim 的 float_profit（价格差 × 手数 × 乘数，**不含手续费/滑点**），
           所以「毛价差只有 1~2 点的边界单」会被判成盈利、而实际净亏。
           这是有意保留的近似：原版海龟也只问「这笔赚没赚」，不细扣成本。
      · 盈利 -> 跳过下一个突破信号（**one-shot**：用一次即恢复）；
        未盈利 -> 不跳，下一个突破照常接受。
      · 读不到（None）一律按「未盈利」处理 —— 判正不判负，见结论 5。

    返回 (last_win, skip_next)；last_win = None 表示无法判断。
    """
    if float_profit is None:
        return None, False
    win = float_profit > 0
    return win, win


def turtle_failsafe_blocks(cur_dir, sig, skip_next):
    """本次开仓信号是否被 failsafe 跳过。True = 跳过（不入场）。

    只在「空仓 + 有开仓信号」时才可能被跳；平仓信号（sig = 0）一律放行 ——
    过滤器只管「能不能开」，出场规则管「要不要走」，两条独立（同闸门的设计）。
    """
    return bool(skip_next) and cur_dir == 0 and sig != 0


def _read_float_profit(api, sym):
    """读当前持仓的浮动盈亏（元）；读不到（或 NaN）返回 None。

    ⚠️ 必须在「下平仓单之前」调用 —— 一旦 set_target_volume 把仓位清掉，
       这个值就归零，failsafe 的判据也就丢了。
    ⚠️ 无持仓时 TqSim 会给 NaN（本项目的**幽灵止损**：开仓被「最小开仓手数」
       拒掉，但引擎仍按 `want` 挂了止损线，触及时照样打印并下发平仓单）。
       NaN 一律归一成 None —— 判正不判负（结论 5），让调用方按「未盈利」处理。
    """
    try:
        v = float(api.get_position(sym).float_profit)
    except Exception:
        return None
    return None if v != v else v          # NaN -> None


def _snap_code(strategy_name, name):
    """回测开跑前，把当前策略脚本 + engine.py 存一份代码快照。

    目的：改动逻辑后若回测是负收益、要靠手改退回去，很容易漏。
    存好快照后，一条命令 工具/回滚.py 就能还原代码 + 删掉这次修改的汇总记录。
    快照与本次回测一一对应（同一份代码只存一次），失败也绝不影响回测。
    """
    try:
        from lib import code_snapshot
        code_snapshot.存(strategy_name, name)
    except Exception as e:
        print(f"    （代码快照失败，不影响回测：{e}）")


def run_backtest(symbol_key, signal_fn, n1, n2, volume, start, end,
                 init_balance=200000, kline_sec=24 * 60 * 60,
                 strategy_name="双均线", open_html=True, eval_start=None,
                 sample_tag="", slip_ticks=0, commission_per_hand=None,
                 pass_dir=False, ma_filter=0,
                 atr_stop_mult=0.0, atr_period=14, breakeven_atr=0.0,
                 exec_price="ACTIVE",
                 risk_pct=0.0, risk_hand_min=1, risk_hand_max=8,
                 signal_kind="ma", gate_fast=0, turtle_failsafe=0,
                 no_open_window_min=0):
    """
    跑一个品种的历史回测，结束自动生成按品种命名的 HTML 报告 + 成交明细 txt。

    报告落盘位置（两层：先策略、后品种）：
        回测结果/<策略目录>/<品种中文名>/<品种中文名>_<策略>_<周期>[_in|_out].html
        <策略目录> 带编号（01_双均线 / 02_均线排列 / 03_布林带），见 lib/strategy_dirs.py

    成本模型（2026-09-17 新增，默认 0 = 保持老行为）：
        slip_ticks        : 每笔成交滑点（跳）。折进 TqSim 手续费，等价于
                            每手每笔成交多付 slip_ticks × 乘数 × 最小变动价位 元。
        commission_per_hand: 每手手续费（元，单边）。
                             None = 保留 TqSim 交易所默认费率（只叠加 slip_ticks）。
    信号模式（2026-09-17 新增）：
        pass_dir=False（默认）: signal_fn(now_f, now_s, pre_f, pre_s) -> ±1/0，
                               0 = 不动（边界触发式，老策略行为）。
        pass_dir=True         : signal_fn(cur_dir, now_f, now_s, pre_f, pre_s)
                               -> 目标方向 -1/0/+1（0 = 清仓），每次比较目标与
                               当前仓位，不同才下单；None = 维持现状。
                               用于「平仓后不反手」这类需要知道持仓状态的逻辑。
    趋势过滤（2026-09-17 新增，默认 0 = 关闭）：
        ma_filter > 0 : 在 n1/n2 之外多算一条 MA(ma_filter)，把该线在信号 K 线上的
                        (now, pre) 两个值**追加到 signal_fn 参数末尾**：
                          pass_dir=False: signal_fn(now_f, now_s, pre_f, pre_s, now_t, pre_t)
                          pass_dir=True : signal_fn(cur_dir, now_f, now_s, pre_f, pre_s, now_t, pre_t)
                        典型用法 = MA60 趋势过滤（详见 trend_reverse_signal）。
        ma_filter = 0 : 不计算、不传参，严格保持老行为。
    止损（2026-09-18 新增；两个参数都默认 0 = 关闭，严格向后兼容）：
        atr_stop_mult > 0 : 阶段A · 初始止损。开仓时按「开仓基准价 ∓
                            atr_stop_mult × 开仓时ATR」挂一条固定止损，触及即平仓。
        breakeven_atr > 0 : 阶段B · 保本。持仓期间浮盈达到
                            breakeven_atr × 开仓时ATR 后，把止损线移到开仓基准价。
                            两个参数可单开、可同开（同开取更有利的那条）。
        两者都与 run_live 同口径；只做到「移到开仓基准价」为止，不含移动/追踪。
        基准价 = 发出开仓信号那根 K 线的收盘价（与均线排列同口径）。
        atr_period        : ATR 周期，默认 14。
    执行方式（2026-09-19 新增，默认 "ACTIVE" = 严格保持老行为）：
        exec_price        : TargetPosTask 的下单方式，直接透传给 tqsdk。
            "ACTIVE"  = 对价下单：买吃卖一、卖吃买一 → 每笔成交天然付 1 跳价差。
            "PASSIVE" = 排队价下单：买挂买一、卖挂卖一 → 挂单排队，省下这 1 跳，
                        但 tqsdk 会在价格变化时撤单重挂（追价），成交时点会后移。
        ⚠️ 重要：TqSim 撮合本来就按对手价成交（实测 60 笔成交价 = 所在 bar 开盘价
           ±1 跳，且与 slip_ticks 取值无关）。所以 slip_ticks 是在「价格层滑点」
           之上**再叠加一层费用层滑点**，两者会重复计数。
           B1「执行改造」实验要同时动这两处才完整：
             · exec_price 控制价格层（ACTIVE ↔ PASSIVE）
             · slip_ticks 控制费用层（1 ↔ 0）
    仓位（2026-09-19 新增 v6，默认 risk_pct=0 = 严格保持老行为）：
        risk_pct > 0 : 波动率预算。每次开仓的手数不再固定，而是按
                       「单笔目标风险 = init_balance × risk_pct%」反推：
                         手数 = 权益 × risk_pct% ÷ (atr_stop_mult × 信号根ATR × 乘数)
                       并夹在 [risk_hand_min, risk_hand_max]。
                       手数在开仓那一根锁定，持仓期间不变（不加仓、不递减）。
                       ⚠️ 仅在 pass_dir=True 分支生效（PVC 走的那条）；
                          需要 atr_stop_mult > 0 才有意义（否则退化为 risk_hand_min）。
                       ⚠️ 这是纯杠杆刻度：收益率/回撤整体放大，夏普/卡玛不变。
        risk_pct = 0 : 完全走老路径 want = sig × volume，逐字未变（其余 4 品种、
                       以及 02/03 与全部历史实验都不受影响）。
    信号源（2026-09-20 新增 04 通道突破；默认 "ma" = 严格保持老行为）：
        signal_kind = "ma"      : 老路径。n1/n2 是快慢均线，信号函数收
                                  signal_fn(cur_dir, now_f, now_s, pre_f, pre_s, *extra)。
        signal_kind = "channel" : 04 通道突破（唐奇安）。n1 = **入场通道**根数、
                                  n2 = **出场通道**根数（本项目取 20 / 10）；信号函数改收
                                  signal_fn(cur_dir, 入场上轨, 入场下轨,
                                            出场上轨, 出场下轨, 收盘价)
                                  = channel_signal。轨道一律 shift(1) 取「前 n 根」极值，
                                  不含当前根 —— 否则等于用未来极值判断当下。
        ⚠️ 只改「信号值从哪来」这一处：止损、仓位、成本、报告、换月处理全部共用，
           与 signal_kind 无关。默认值保证 01/02/03 与全部历史实验逐字不变。
    通道闸门（2026-09-20 新增，仅 signal_kind="channel"；默认 0 = 关闭）：
        gate_fast > 0 且 ma_filter > 0 时启用。把 01 双均线那条**已验证有正贡献**
        的趋势闸门（结论19：闸门贡献 92% 的差异）原样搬到通道突破上：
            MA(gate_fast) > MA(ma_filter) -> 只放行做多
            MA(gate_fast) < MA(ma_filter) -> 只放行做空
        ★ 只管「开仓许可」：平仓信号（sig=0）一律放行，与 01 一致 ——
          闸门决定「能不能开」，出场规则决定「要不要走」，两条独立。
        ★ 拦下的入场会打印 [闸门] 并在结束时汇总次数，便于归因
          「止损次数下降是闸门挡掉了逆势单，还是通道本身变好了」。
        gate_fast = 0 : 不计算、不拦任何信号，严格保持 04 v0 行为。
    海龟 failsafe 过滤（2026-09-20 新增，仅 signal_kind="channel"；默认 0 = 关闭）：
        turtle_failsafe > 0 时启用海龟**原版 System 1** 的过滤器：
            上一笔【已入场的突破交易】盈利 -> 跳过下一个突破信号（one-shot）
            上一笔未盈利                    -> 不跳，下一个突破照常接受
        ★ 这是原版《海龟交易法则》System 1「有过滤」的那条 —— 与均线闸门
          毫无关系（闸门是我们从 01 搬来的，结论 39 已证其在通道上为负贡献）。
        ★ 只管「开仓许可」：平仓信号（sig = 0）一律放行。
        ★ 判据用「平仓那一刻的持仓浮盈符号」（TqSim float_profit，不含成本）；
          口径写死于 turtle_failsafe_after_close()，可单测。
        ★ 止损平仓同样计入「上一笔」的结果（止损必然是亏损 -> 下一次照常接受）。
        ★ 跳过的入场会打印 [failsafe]（含时间戳），结束时汇总跳过次数 ——
          便于把被跳过的信号与无过滤口径的成交按「时间 + 方向」配对归因。
        turtle_failsafe = 0 : 不启用，严格保持 04 v2 行为（其余策略更不受影响）。
    """
    name, main_code, mult, tick = config_symbols.get(symbol_key)
    user, pwd = config_account.check()
    _snap_code(strategy_name, name)   # 回测前存档当前代码，供 工具/回滚.py 还原
    # ---- 成本口径（2026-09-17 修，为「只加滑点」这类单变量实验服务）----
    #   commission_per_hand 显式传入 -> 用它覆盖交易所默认费率
    #   未传(None) 且只加滑点        -> 保留 TqSim 的交易所默认费率，只在其上叠加滑点
    #   旧行为是「一传 slip_ticks 就把手续费也换成 config 的估算值」，
    #   等于一次改动两个变量、无法归因，故改为按需在运行时解析。
    _cost_specified = commission_per_hand is not None
    _cfg_cost = float(getattr(config_symbols, "COST", {}).get(symbol_key, 0.0) or 0.0)
    slip_money = slip_ticks * mult * tick          # 1 手吃 1 跳 = 乘数 × 最小变动价位 元
    # 既没传手续费也没传滑点 -> 完全不碰 TqSim 费率，严格保持历史行为（口径可归因）
    apply_cost = _cost_specified or slip_ticks > 0
    cost_per_hand = (commission_per_hand + slip_money) if _cost_specified else 0.0
    _cost_base = commission_per_hand if _cost_specified else 0.0   # 其中手续费部分，供打印/报告用

    def _resolve_cost(sym):
        """解析该合约实际采用的每手成本 = 手续费 + 滑点（元/手/单边）。

        未显式指定手续费时取 TqSim 的交易所默认值（sim.get_commission），
        取不到（NaN 或异常）才退回 config_symbols.COST 的估算值。
        """
        nonlocal _cost_base
        if _cost_specified:
            _cost_base = commission_per_hand
            return _cost_base + slip_money
        try:
            b = float(sim.get_commission(sym))
            if b != b or b <= 0:          # NaN / 未取到 -> 退回估算值
                b = _cfg_cost
        except Exception:
            b = _cfg_cost
        _cost_base = b
        return _cost_base + slip_money
    sim = TqSim(init_balance)
    stat = None
    equity_curve = []   # [(bar_datetime_str, account.balance), ...]
    kline_history = []  # 每根已完成 K 线的 OHLCV，供报告画 K 线图
    signal_events = []  # 信号事件 {dt, dir, price, reason?}，供报告标注买卖点
    # 止损状态（atr_stop_mult 与 breakeven_atr 都为 0 时全程不用）
    #   entry / entry_atr 在「开新仓 或 反手」那一刻锁定，平仓（含止损出场）时下线
    #   best_fav = 持仓期间最大有利偏移（点）—— 保本的触发依据
    stop_st = {"active": False, "entry": 0.0, "entry_atr": 0.0, "best_fav": 0.0}
    eval_ts = pd.Timestamp(eval_start) if eval_start else None
    try:
        api = TqApi(
            sim,
            backtest=TqBacktest(start_dt=start, end_dt=end),
            auth=TqAuth(user, pwd),
            web_gui=False,          # 我们用自己的 HTML 报告，不依赖 tqsdk 内置浏览器
        )
        klines = api.get_kline_serial(main_code, kline_sec,
                                      data_length=max(n1, n2, ma_filter, gate_fast) + 80)
        quote = api.get_quote(main_code)
        account = api.get_account()
        api.wait_update(deadline=time.time() + 30)

        cur_sym = quote.underlying_symbol
        tp = TargetPosTask(api, cur_sym, price=exec_price, support_open_min_volume=True)
        if apply_cost:
            cost_per_hand = _resolve_cost(cur_sym)
            try:
                sim.set_commission(cur_sym, cost_per_hand)
                sim.set_commission(main_code, cost_per_hand)
            except Exception as e:
                print(f"  [!] 成本设置失败（不影响信号，但报告不含成本）: {e}")
        want = 0  # 目标仓位（正=多，负=空）
        _gate_blocked = [0]   # 通道闸门拦下的入场次数（仅诊断用，不参与任何交易逻辑）
        _open_blocked = [0]  # 开盘保护拦下的开仓次数（仅诊断用，不参与任何交易逻辑）

        # ---- 海龟 failsafe 状态（turtle_failsafe=0 时全程不读不写）----
        _fs_skip_next = [False]   # 下一次开仓是否要跳过（平仓时按上一笔盈亏设定）
        _fs_last_win = [None]     # 上一笔已入场交易是否盈利（诊断用）
        _fs_skipped = [0]         # 被 failsafe 跳过的入场次数（诊断用）

        print(f"\n>>> {name} {main_code} 开始回测 {start} ~ {end}")
        if apply_cost:
            _src = "显式指定" if _cost_specified else "交易所默认"
            _sl = f" + 滑点 {slip_ticks} 跳({slip_money:g} 元/手)" if slip_ticks else ""
            print(f"    成本模型：手续费 {_cost_base:g} 元/手(单边,{_src}){_sl}"
                  f" = {cost_per_hand:g} 元/手/单边")
        if pass_dir:
            print("    信号模式：目标仓位式（signal_fn 收 cur_dir，可返回 0 清仓）")

        while True:
            api.wait_update(deadline=time.time() + 30)

            # ---- 换月：主连背后的合约变了，先平旧的再下新的 ----
            if api.is_changing(quote, "underlying_symbol"):
                new_sym = quote.underlying_symbol
                if new_sym != cur_sym:
                    print(f"    换月 {cur_sym} -> {new_sym}")
                    tp.set_target_volume(0)
                    _guard = 0
                    while api.get_position(cur_sym).pos != 0 and _guard < 2000:
                        api.wait_update(deadline=time.time() + 30)
                        _guard += 1
                    if api.get_position(cur_sym).pos != 0:
                        print(f"    [!] 换月平仓等待超限(exec_price={exec_price})，"
                              f"残留 {api.get_position(cur_sym).pos} 手，继续")
                    cur_sym = new_sym
                    tp = TargetPosTask(api, cur_sym, price=exec_price,
                                       support_open_min_volume=True)
                    if apply_cost:
                        try:
                            cost_per_hand = _resolve_cost(cur_sym)
                            sim.set_commission(cur_sym, cost_per_hand)
                        except Exception:
                            pass
                    tp.set_target_volume(want)

            # ---- 每根新 K 线：记一次权益 + 判断一次信号 ----
            if api.is_changing(klines.iloc[-1], "datetime"):
                # 2026-09-16 修：原为 str()，拿到的是纳秒整数串（如 "1773100800000000000"）
                # -> 月度收益表取 [:7] 当月份，显示成 "1773100" 乱码；
                #    与同段的 _dt_str(bar_dt) 混用两种格式。统一走 _dt_str。
                bar_dt = _dt_str(klines.iloc[-2]["datetime"])
                try:
                    equity_curve.append((bar_dt, float(account.balance)))
                except Exception:
                    pass

                # 预热期（eval_start 之前）：只累积权益曲线，
                # 不在 K 线图标注买卖点、也不触发信号（与 run_backtest_align 一致）
                if eval_ts is not None and pd.Timestamp(bar_dt) < eval_ts:
                    continue

                # 记录已完成 K 线，供报告画 K 线图
                try:
                    b = klines.iloc[-2]
                    kline_history.append({
                        # 与 signal_events 的 dt 同格式：_kline_svg 用字符串比较定位买卖点
                        "dt": _dt_str(b["datetime"]),
                        "open": float(b["open"]), "high": float(b["high"]),
                        "low": float(b["low"]), "close": float(b["close"]),
                        "volume": float(b["volume"]),
                    })
                except Exception:
                    pass

                close = klines.close
                # ---- 信号值：按 signal_kind 决定用均线还是用通道轨道 ----
                #   "ma"（默认，逐字保持老行为）：快线 / 慢线在「刚收盘那根」与「再前一根」的值
                #   "channel"（04 通道突破）：入场轨道 n1 根、出场轨道 n2 根的极值。
                #       ★ .shift(1) 是必须的 —— 少了它就把「当前这根」的 high/low 算进轨道，
                #         等于拿本根的极值判断本根，属于未来函数（规则1）。
                ch_en_up = ch_en_dn = ch_ex_up = ch_ex_dn = float("nan")
                if signal_kind == "channel":
                    ch_en_up = float(klines.high.rolling(n1).max().shift(1).iloc[-2])
                    ch_en_dn = float(klines.low.rolling(n1).min().shift(1).iloc[-2])
                    ch_ex_up = float(klines.high.rolling(n2).max().shift(1).iloc[-2])
                    ch_ex_dn = float(klines.low.rolling(n2).min().shift(1).iloc[-2])
                ma_f = close.rolling(n1).mean()
                ma_s = close.rolling(n2).mean()
                now_f, now_s = ma_f.iloc[-2], ma_s.iloc[-2]
                pre_f, pre_s = ma_f.iloc[-3], ma_s.iloc[-3]
                # 可选的趋势过滤线（如 MA60）：多算一条，值追加到信号函数参数末尾
                extra = ()
                if ma_filter:
                    ma_t = close.rolling(ma_filter).mean()
                    extra = (ma_t.iloc[-2], ma_t.iloc[-3])
                # 通道突破专用闸门：只算快线；慢线直接复用上面那条（extra[0]）
                #   gate_fast=0 或 ma_filter=0 -> 不计算，gate_now 保持 NaN -> 闸门自动不生效
                gate_now = float("nan")
                if signal_kind == "channel" and ma_filter and gate_fast:
                    gate_now = float(close.rolling(gate_fast).mean().iloc[-2])

                # ---- 止损（阶段A 初始止损 / 阶段B 保本，两个参数都默认关闭）----
                #  口径与「均线排列」策略一致：用【已收盘】那根的 high/low 判断是否
                #  触及，触发后在这根收盘时发单、成交在下一根 K 线开盘 -> 无未来函数。
                #  幅度用「开仓那一刻的 ATR」锁定，持仓期间不随 ATR 漂移。
                #  优先级高于均线信号：一旦被打掉，本根不再看金叉/死叉。
                atr_v = 0.0
                if atr_stop_mult > 0 or breakeven_atr > 0 or risk_pct > 0:
                    try:
                        atr_v = float(_atr(klines.high, klines.low, klines.close,
                                           atr_period).iloc[-2])
                    except Exception:
                        atr_v = 0.0

                if (atr_stop_mult > 0 or breakeven_atr > 0) \
                        and stop_st["active"] and want != 0:
                    _sdir = 1 if want > 0 else -1
                    _satr = stop_st["entry_atr"]
                    _barl = float(klines.low.iloc[-2])
                    _barh = float(klines.high.iloc[-2])
                    # 浮盈峰值：只用【已收盘】那根的极值更新，不看未收盘 K 线
                    _fav = (_barh - stop_st["entry"] if _sdir > 0
                            else stop_st["entry"] - _barl)
                    if _fav > stop_st["best_fav"]:
                        stop_st["best_fav"] = _fav
                    _peak = stop_st["best_fav"]
                    _sline = _dual_ma_stop_line(stop_st["entry"], _satr, _sdir,
                                                atr_stop_mult, _peak, breakeven_atr)
                    # 这次被触发的是哪条线：浮盈够 -> 保本线，否则 -> 初始止损线
                    _be = (breakeven_atr > 0 and _satr > 0
                           and _peak >= breakeven_atr * _satr)
                    _why = "保本止损" if _be else "ATR止损"
                    if _dual_ma_stop_hit(_sline, _sdir, _barh, _barl):
                        # 先落状态再打印（打印要用清零前的值，故用局部量 _satr/_peak）
                        want = 0
                        stop_st["active"] = False
                        stop_st["entry_atr"] = 0.0
                        stop_st["best_fav"] = 0.0
                        print("=" * 60)
                        print(f"  [止损] 触及{_why} -> "
                              f"平{'多' if _sdir > 0 else '空'}")
                        print(f"  时间 {_dt_str(bar_dt)} | 止损线 {_sline:.2f}"
                              f"（基准 {stop_st['entry']:.2f}"
                              + (f" {'-' if _sdir > 0 else '+'}"
                                 f" {atr_stop_mult:g}×ATR{_satr:.2f}"
                                 if atr_stop_mult > 0 and not _be else "")
                              + f"，浮盈峰值 {_peak:.2f} 点）")
                        print(f"  本根 高 {_barh:.2f} / 低 {_barl:.2f}")
                        print("=" * 60)
                        signal_events.append({
                            "dt": bar_dt,
                            "dir": "SELL" if _sdir > 0 else "BUY",
                            "price": float(_sline),
                            "reason": f"{_why} {_sline:.2f}",
                        })
                        # ---- 海龟 failsafe：止损平仓同样是一笔交易的结束 ----
                        #   止损必然是亏损 -> 记为「未盈利」-> 下一个突破照常接受。
                        #   ⚠️ 同样必须在 set_target_volume(0) 之前读浮盈。
                        if turtle_failsafe:
                            _fp = _read_float_profit(api, cur_sym)
                            _fs_last_win[0], _fs_skip_next[0] = \
                                turtle_failsafe_after_close(_fp)
                            _fptxt = "读不到" if _fp is None else f"{_fp:+,.0f} 元"
                            print(f"  [failsafe] 止损平仓 | 时间 {_dt_str(bar_dt)}"
                                  f" | 本笔浮盈 {_fptxt} -> 下一个突破照常接受")
                        tp.set_target_volume(0)
                        continue

                acted = False
                if pass_dir:
                    # 目标仓位式：把当前持仓方向喂给信号函数，返回 0 = 清仓（不反手）
                    cur_dir = 1 if want > 0 else (-1 if want < 0 else 0)
                    if signal_kind == "channel":
                        # 04 通道突破：入场轨道 / 出场轨道 / 刚收盘那根的收盘价
                        sig = signal_fn(cur_dir, ch_en_up, ch_en_dn, ch_ex_up,
                                        ch_ex_dn, float(close.iloc[-2]))
                        # ---- MA 闸门（可选）：严格复刻 01 双均线那条闸门口径 ----
                        #   MA(gate_fast) 在 MA(ma_filter) 之上 -> 只放行做多，反之只放行做空。
                        #   ★ 只在「空仓要开新仓」时才拦（cur_dir == 0 且 sig != 0）：
                        #     平仓信号一律放行 —— 闸门管开仓许可，出场规则管离场，两条独立。
                        if gate_fast and sig != 0 and cur_dir == 0:
                            _gt = extra[0] if extra else float("nan")
                            if not channel_gate_ok(sig, gate_now, _gt):
                                _gdir = "多" if sig > 0 else "空"
                                sig = cur_dir
                                _gate_blocked[0] += 1
                                # ★ 必须带时间：被拦下的信号要能与 v0（无闸门）的成交
                                #   按「时间 + 方向」逐笔配对，才能回答"闸门拦掉的单在
                                #   v0 里是赚是亏" —— 这是本次实验唯一有价值的归因数据。
                                print(f"  [闸门] 拦下{_gdir}单 | 时间 {_dt_str(bar_dt)}"
                                      f" | 不开仓，继续等待"
                                      f"（MA{gate_fast}={gate_now:.2f} / "
                                      f"MA{ma_filter}={_gt:.2f}）")
                    else:
                        sig = signal_fn(cur_dir, now_f, now_s, pre_f, pre_s, *extra)
                    if sig is None:
                        sig = cur_dir
                    # ---- 海龟 failsafe（System 1 过滤器；默认关闭）----
                    #   两个动作的顺序不能颠倒：
                    #     ① 平仓信号先给「上一笔」记账 —— 此刻持仓还在，才读得到浮盈；
                    #        一旦下面 set_target_volume 把仓位清掉，浮盈就归零了。
                    #     ② 开仓信号再判断要不要被跳过。
                    if turtle_failsafe:
                        if cur_dir != 0 and sig == 0:
                            _fp = _read_float_profit(api, cur_sym)
                            _fs_last_win[0], _fs_skip_next[0] = \
                                turtle_failsafe_after_close(_fp)
                            if _fs_skip_next[0]:
                                print(f"  [failsafe] 平仓 | 时间 {_dt_str(bar_dt)}"
                                      f" | 本笔浮盈 {_fp:+,.0f} 元 = 盈利"
                                      f" -> 跳过下一个突破信号")
                            else:
                                _fptxt = "读不到" if _fp is None else f"{_fp:+,.0f} 元"
                                print(f"  [failsafe] 平仓 | 时间 {_dt_str(bar_dt)}"
                                      f" | 本笔浮盈 {_fptxt} = 未盈利"
                                      f" -> 下一个突破照常接受")
                        elif turtle_failsafe_blocks(cur_dir, sig, _fs_skip_next[0]):
                            _fs_skip_next[0] = False        # one-shot：只跳一次
                            _fs_skipped[0] += 1
                            _fdir = "多" if sig > 0 else "空"
                            print(f"  [failsafe] 跳过{_fdir}单 | 时间 {_dt_str(bar_dt)}"
                                  f" | 上一笔突破交易盈利 -> 本次不开仓（仅跳一次）")
                            sig = cur_dir
                    if sig != cur_dir:
                        # 手数：v6 波动率预算 —— 开几手由「信号根已收盘那根的 ATR」决定，
                        # 与同一根锁定的止损线共用同一个 atr_v（同一把尺子）。
                        # risk_pct=0 时 _hand 恒等于 volume，老行为逐字不变。
                        _hand = volume
                        if risk_pct > 0 and sig != 0:
                            _hand = _risk_budget_hand(
                                init_balance, risk_pct, atr_v, atr_stop_mult,
                                mult, risk_hand_min, risk_hand_max)
                        if no_open_window_min > 0 and sig != 0 and cur_dir == 0:
                            if _in_no_open_window(klines.iloc[-2]["datetime"],
                                                  no_open_window_min):
                                print(f"  [开盘保护] 跳过开仓 | 时间 {_dt_str(bar_dt)}"
                                      f" | 开盘后 {no_open_window_min} 分钟内不开仓")
                                _open_blocked[0] += 1
                                sig = cur_dir
                        want = sig * _hand
                        # ---- 止损状态：开新仓/反手时锁基准价，平仓时下线 ----
                        if atr_stop_mult > 0 or breakeven_atr > 0:
                            if sig == 0:
                                stop_st["active"] = False
                                stop_st["entry_atr"] = 0.0
                                stop_st["best_fav"] = 0.0
                            else:
                                stop_st.update({
                                    "active": True,
                                    "entry": float(klines.iloc[-2]["close"]),
                                    "entry_atr": atr_v,
                                    "best_fav": 0.0})
                        tp.set_target_volume(want)
                        acted = True
                else:
                    # 边界触发式（老策略路径，目前无调用者）：
                    # 波动率预算 v6 只接在 pass_dir 模式上，此处保持 want = sig × volume。
                    sig = signal_fn(now_f, now_s, pre_f, pre_s, *extra)
                    if sig != 0:
                        want = sig * volume
                        tp.set_target_volume(want)
                        acted = True

                # 只有「目标仓位真的变了」才打印（平仓信号不标 ▲▼，成交明细里能看到）
                if acted:
                    # 手数一律取 want（= sig × 手数）；v6 前手数恒为 1，sig 与 want 同值，
                    # 多手后必须显示 want，否则打印的「目标手数」与实际下单不符。
                    if sig != 0:
                        _txt = f"目标 {int(want):+d} 手（{'多' if sig > 0 else '空'}）"
                        if risk_pct > 0:
                            _txt += (f"｜波动率预算 {risk_pct:g}%×{init_balance:g}"
                                     f" ÷ ({atr_stop_mult:g}×ATR{atr_v:.2f}"
                                     f"×{mult:g})")
                    else:
                        _txt = "目标 0 手（平仓，等待新信号）"
                    print("=" * 60)
                    print(f"  [信号] {_txt}")
                    print(f"  时间 {_dt_str(bar_dt)} | 触发价 {quote.last_price:.2f}")
                    if signal_kind == "channel":
                        _maline = (f"  入场轨道 {n1}根 {ch_en_dn:.2f} ~ {ch_en_up:.2f}"
                                   f" ｜ 出场轨道 {n2}根 {ch_ex_dn:.2f} ~ {ch_ex_up:.2f}"
                                   f" ｜ 收盘 {float(close.iloc[-2]):.2f}")
                        if gate_fast and ma_filter:
                            _maline += (f" ｜ 闸门 MA{gate_fast}={gate_now:.2f}"
                                        f" / MA{ma_filter}={extra[0]:.2f}")
                    else:
                        _maline = f"  均线 MA{n1}={now_f:.2f} / MA{n2}={now_s:.2f}"
                        if extra:
                            _maline += f" / MA{ma_filter}={extra[0]:.2f}"
                    print(_maline)
                    print("=" * 60)
                    if sig != 0:
                        signal_events.append({
                            "dt": bar_dt,
                            "dir": "BUY" if sig > 0 else "SELL",
                            "price": float(klines.iloc[-2]["close"]),
                        })

    except BacktestFinished:
        stat = sim.tqsdk_stat
        sdict = _stat_to_dict(stat)
        trades = _collect_trades(api)
        _print_stat(name, sdict)
        if gate_fast and ma_filter:
            print(f"    [闸门] MA{gate_fast}/MA{ma_filter} 共拦下 {_gate_blocked[0]} "
                  f"次逆势入场（这些单未开仓，不在成交明细中）")
        if signal_kind == "channel" and turtle_failsafe:
            print(f"    [failsafe] 海龟 System 1 过滤共跳过 {_fs_skipped[0]} 次入场"
                  f"（这些单未开仓，不在成交明细中）")
        if no_open_window_min > 0:
            print(f"    [开盘保护] 共跳过 {_open_blocked[0]} 次开盘窗口内的开仓"
                  f"（这些单未开仓，不在成交明细中）")
        html_path, txt_path = _save_backtest_report(
            strategy_name, symbol_key, name, sdict, equity_curve, trades,
            start, end,
            (f"入场通道 {n1} 根 / 出场通道 {n2} 根（收盘价确认，不用均线）"
             + (f" + MA{gate_fast}/MA{ma_filter} 趋势闸门（仅顺趋势开仓）"
                if (gate_fast and ma_filter) else "")
             + (f" + 海龟 failsafe 过滤（跳过上一次突破盈利后的下一个信号）"
                if (signal_kind == "channel" and turtle_failsafe) else "")
             if signal_kind == "channel"
             else f"MA{n1}/MA{n2}" + (f" + MA{ma_filter}趋势过滤" if ma_filter else "")
             + (f" + 开盘{no_open_window_min}分钟保护" if no_open_window_min > 0 else "")),
            volume, kline_sec, init_balance, main_code,
            kline_history=kline_history, signal_events=signal_events, mult=mult,
            extra_note=" ｜ ".join(x for x in [
                (f"信号评估区间：{eval_start} ~ {end}" if eval_start else ""),
                (f"成本模型：手续费 {_cost_base:g} 元/手"
                 f"({'显式指定' if _cost_specified else '交易所默认'})"
                 + (f" + 费用层滑点 {slip_ticks} 跳" if slip_ticks else "")
                 + f" = {cost_per_hand:g} 元/手/单边" if apply_cost else ""),
                (("止损："
                  + (f"初始 {atr_stop_mult:g}×ATR({atr_period})" if atr_stop_mult > 0 else "")
                  + (" + " if (atr_stop_mult > 0 and breakeven_atr > 0) else "")
                  + (f"保本（浮盈≥{breakeven_atr:g}×ATR 后移到开仓基准价）"
                     if breakeven_atr > 0 else "")
                  + "，基准=信号K线收盘价，无移动止损")
                 if (atr_stop_mult > 0 or breakeven_atr > 0) else ""),
                (("仓位：波动率预算 —— 单笔目标风险 "
                  + f"{risk_pct:g}% × 权益 {init_balance:g} 元 = "
                  + f"{init_balance * risk_pct / 100:g} 元；"
                  + f"手数 = 该金额 ÷ ({atr_stop_mult:g}×信号根ATR×{mult:g})，"
                  + f"夹在 {risk_hand_min}~{risk_hand_max} 手，开仓时锁定")
                 + " ⚠️ 纯杠杆刻度：收益率/回撤按同一倍数放大，"
                   "夏普/卡玛不因此改善 —— 别把数字变大读成策略变好"
                 if risk_pct > 0 else ""),

            ] if x),
            sample_tag=sample_tag,
            vol_text=(f"{risk_hand_min}~{risk_hand_max} 手（波动率预算）"
                      if risk_pct > 0 else None),
            cost_note=_cost_note_text(_cost_base, _cost_specified, slip_ticks,
                                      exec_price, apply_cost))
        print(f"\n  回测记录已保存：{txt_path}")
        print(f"  回测报告已生成：{html_path}")
        if open_html:
            try:
                os.startfile(html_path)
            except Exception:
                pass
        api.close()
    return stat



# ===================================================================
# 模拟盘
# ===================================================================
def _get_web_gui_url(api):
    """从 TqApi 内部获取 web_gui 的真实端口，返回完整 URL 或 None。

    tqsdk 3.10.2：TqWebHelper 用随机端口起 HTTP server，只把地址打印到 stdout
    （'您可以访问 http://127.0.0.1:PORT ...'），不挂到任何属性。这里优先用
    运行时截获到的地址；老版本若把端口挂在 _tq_web_helper 上也能兼容。
    """
    # 1) 优先：运行时截获到的面板地址（最可靠）
    if _captured_gui_url.get("url"):
        return _captured_gui_url["url"]
    # 2) 兜底：老版本可能挂在属性上
    try:
        wh = getattr(api, '_tq_web_helper', None)
        if wh is not None:
            server = getattr(wh, '_http_server', None) or getattr(wh, 'server', None)
            if server is not None:
                port = getattr(server, 'server_port', None) or getattr(server, 'port', None)
                if port:
                    return f"http://127.0.0.1:{port}"
    except Exception:
        pass
    return None


def _paint_ma(kdata, periods, colors=None):
    """在 web_gui 主图上叠加均线。放在主循环里每次调用，可随新 K 线向右延伸。"""
    if colors is None:
        colors = [0xFFFF9900, 0xFF3399FF, 0xFF9933CC, 0xFF33CC99]
    for p, c in zip(periods, colors):
        kdata[f"MA{p}"] = kdata.close.rolling(p).mean()
        kdata[f"MA{p}.board"] = "MAIN"
        kdata[f"MA{p}.color"] = c


def _draw_signal(api, kdata, is_buy, idx=-2):
    """在 web_gui 主图上标买卖点：红▲=做多/平空，绿▼=做空/平多。

    idx 是 K 线位置偏移（-2 = 刚收盘的那根信号 K 线）。纯显示，失败静默、不影响策略。
    """
    try:
        row = kdata.iloc[idx]
        y = float(row["low"]) if is_buy else float(row["high"])
        api.draw_text(kdata, "▲" if is_buy else "▼", x=idx, y=y,
                      color=0xFFFF0000 if is_buy else 0xFF00AA00,
                      id="sig%d" % int(row["id"]), board="MAIN")
    except Exception:
        pass


def _pvc_bar_close_dt(bar_start_ns, kline_sec):
    """由 K 线起始时间推算「本根收盘」的本地时刻。

    PVC(郑商所 V) 交易时段：夜盘 21:00-23:00 / 日盘 9:00-10:15, 10:30-11:30,
    13:30-15:00。30 分钟 K 线里唯一非整 30 分钟的收盘是 10:00 开始那根
    （10:15 收盘，因为 10:15-10:30 休市）。其余根一律 起始+kline_sec。
    """
    start = datetime.fromtimestamp(int(bar_start_ns) / 1e9)
    if kline_sec == 1800 and start.hour == 10 and start.minute == 0:
        return start + timedelta(minutes=15)
    return start + timedelta(seconds=kline_sec)


def _paint_countdown(api, kdata, kline_sec):
    """在 web_gui 主图左下角画「距本根K线收盘」倒计时，同步期货软件的显示习惯。

    纯显示：用固定 id 反复 draw_text 覆盖（tqsdk 同 id 后一次覆盖前一次），
    主循环每 tick 刷新一次；任何异常静默吞掉，绝不影响交易逻辑。
    非交易时段（本根收盘时刻已过）显示「休市」。
    """
    try:
        if kdata is None or len(kdata) == 0:
            return
        close_dt = _pvc_bar_close_dt(kdata.iloc[-1]["datetime"], kline_sec)
        remain = int((close_dt - datetime.now()).total_seconds())
        if remain > 0:
            text = f"距收盘 {remain // 60:02d}:{remain % 60:02d}"
        else:
            text = "休市"
        _low = float(kdata["low"].min())
        _rng = float(kdata["high"].max()) - _low
        y = _low - max(_rng * 0.03, 1.0)     # 最低价下方 3% 处 ≈ K线区左下角
        # ★ 注意：tqsdk draw_text 的 x 必须是「K线序列号(id 列)」，不是从 0 起的
        #   整数位置。用最老一根的 id 锚定到窗口左端，每 tick 用新 id 重画，
        #   文字始终钉在左下角（同 id 覆盖重画，不会叠字）。
        x_id = int(kdata.iloc[0]["id"])
        api.draw_text(kdata, text, x=x_id, y=y, id="bar_countdown",
                      board="MAIN", color=0xFFFFFF00)
    except Exception:
        pass


# ===================================================================
# 模拟盘运行记录器（2026-09-20）
#   只落盘、不参与任何交易判断。record_dir=None（默认）时整体不启用，
#   此时 run_live 的行为与加这个类之前逐字一致。
# ===================================================================
class _LiveRecorder:
    """把「程序有没有在跑」和「有没有开仓条件」分开落盘，供事后做台账。

    record_dir=None（默认）-> 完全不记录。
    给出目录后落两个 CSV（utf-8-sig + CRLF，Excel 双击即可看）：
      <品种>_成交流水.csv —— 事件流：START / STOP / SIGNAL / FILL / STOPLOSS
      <品种>_运行心跳.csv —— 每 record_heartbeat_sec 秒一行，**休市照写**

    ★ 为什么要单独一份心跳（这是整个设计的关键）：
      只要程序开着，心跳就一直写，不看行情开不开市。所以台账里出现的
      「时间断层」只可能有一个原因 —— 程序当时没在跑；反过来，
      「心跳连续、成交流水却没有 SIGNAL 行」= 程序在跑、条件不够。
      两者由此可以明确区分，不会再混成同一团「数据空洞」。
      再加一条判据可以分辨「关机有没有代价」：对比断层前后的 K 线时间，
      没推进 = 当时根本没新 K 线收盘（休市/无行情）-> 不会漏信号；
      推进了 = 期间有新 K 线收盘 -> 可能漏掉信号（台账里会标出来）。

    记录失败绝不允许拖垮交易：所有写盘都包在 try/except 里。
    """

    EVENT_COLS = ["类型", "记录时间", "K线时间", "方向", "手数",
                  "成交价", "参考价", "备注"]
    HEART_COLS = ["记录时间", "K线时间", "现价", "持仓手数", "目标手数"]

    def __init__(self, record_dir, name, heartbeat_sec=300):
        self.on = bool(record_dir)
        self.name = name
        self.heartbeat_sec = max(30, int(heartbeat_sec or 300))
        self._ev = None
        self._hv = None
        self._last_h = None
        if not self.on:
            return
        try:
            os.makedirs(record_dir, exist_ok=True)
            self._ev = self._open(
                os.path.join(record_dir, name + "_成交流水.csv"), self.EVENT_COLS)
            self._hv = self._open(
                os.path.join(record_dir, name + "_运行心跳.csv"), self.HEART_COLS)
        except Exception as e:                     # 记录失败不影响交易
            print(f"  [!] 运行记录初始化失败（不影响交易）：{e}", flush=True)
            self.on = False

    @staticmethod
    def _open(path, cols):
        """追加打开；文件新建时写表头。csv 默认行尾 \\r\\n，与项目惯例一致。"""
        new = (not os.path.exists(path)) or os.path.getsize(path) == 0
        fh = open(path, "a", encoding="utf-8-sig", newline="")
        w = csv.writer(fh)
        if new:
            w.writerow(cols)
            fh.flush()
        return (fh, w)

    def _w(self, pair, row):
        if not self.on or pair is None:
            return
        fh, w = pair
        try:
            w.writerow(row)
            fh.flush()          # 必须立刻落盘：程序随时可能被强杀
        except Exception:
            pass

    @staticmethod
    def _now():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def start(self, note):
        self._w(self._ev, ["START", self._now(), "", "", "", "", "", note])

    def stop(self, note="程序退出"):
        self._w(self._ev, ["STOP", self._now(), "", "", "", "", "", note])

    def signal(self, dir_cn, hand, ref_price, bar_time, trigger, atr_v, note):
        """ref_price = 信号根【已收盘】那根的收盘价（与回测决策基准同一个价）。"""
        self._w(self._ev, ["SIGNAL", self._now(), bar_time, dir_cn, int(hand), "",
                           f"{ref_price:.2f}",
                           f"信号根收盘 {ref_price:.2f} | 触发价 {trigger:.2f} | "
                           f"ATR {atr_v:.2f} | {note}"])

    def fill(self, dir_cn, offset_cn, vol, price, symbol):
        self._w(self._ev, ["FILL", self._now(), "", f"{dir_cn}{offset_cn}",
                           int(vol), f"{price:.2f}", "", symbol])

    def stoploss(self, dir_cn, line_price, trigger, note):
        self._w(self._ev, ["STOPLOSS", self._now(), "", dir_cn, "", "",
                           f"{line_price:.2f}",
                           f"止损线 {line_price:.2f} | 触发价 {trigger:.2f} | {note}"])

    def event(self, kind, note, dir_cn="", ref_price=""):
        """落一条 SIGNAL/FILL/STOPLOSS 之外的事件。

        目前用到两类（2026-09-20 新增）：
          · "DESYNC"     —— 目标仓位与实际持仓对不上（未成交/部分成交/平仓未成）
          · "STOPATTACH" —— 程序启动时已有持仓，补挂了一条止损

        ★ 为什么必须能落这两类：台账是收口阶段唯一的产出。它必须能看见
          「程序以为在做多、其实一手没有」的那段时间 —— 否则台账会把那段
          静默空仓当成正常持仓，把整套实测滑点/成交率算歪。
          这一点与「幽灵止损」是同一个病：判定看意图、不看实际持仓。

        台账工具（工具/模拟盘台账.py）只识别 START/SIGNAL/FILL/STOPLOSS，
        多余类型不影响它解析（实测确认）。
        """
        _rp = ("" if ref_price == "" else
               (f"{ref_price:.2f}" if isinstance(ref_price, (int, float))
                else str(ref_price)))
        self._w(self._ev, [kind, self._now(), "", dir_cn, "", "", _rp, note])

    def heartbeat(self, bar_time, price, pos, want, force=False):
        if not self.on:
            return
        now = datetime.now()
        if not force and self._last_h is not None \
                and (now - self._last_h).total_seconds() < self.heartbeat_sec:
            return
        self._last_h = now
        self._w(self._hv, [self._now(), bar_time, f"{price:.2f}",
                           int(pos), int(want)])

    def close(self):
        for pair in (self._ev, self._hv):
            try:
                if pair:
                    pair[0].close()
            except Exception:
                pass
        self._ev = None
        self._hv = None


def run_live(symbol_key, signal_fn, n1, n2, volume,
             kline_sec=24 * 60 * 60, allow_trade=True, web_gui=True,
             heartbeat=30, log_trades=True, ma_filter=0, pass_dir=False,
             atr_stop_mult=0.0, atr_period=14, breakeven_atr=0.0,
             risk_pct=0.0, risk_hand_min=1, risk_hand_max=8,
             risk_equity=0.0, record_dir=None, record_heartbeat_sec=300,
             attach_stop_on_start=True, reconcile_sec=180.0,
             no_open_window_min=0):
    """连快期模拟账户跑实时信号（程序要一直开着）。

    盯盘能力：
      - 实时成交监听：开仓/平仓一旦真实撮合，立刻在窗口打印确认
      - 盘面心跳：每 heartbeat 秒打印一次现价 / 双均线 / 持仓 / 目标仓位
      - web_gui 面板：浏览器自动打开，可视化看 K线+信号+持仓+资金曲线

    仓位（2026-09-19 v6 波动率预算 —— 与 run_backtest 同一份口径）：
        risk_pct=0（默认）: 固定 volume 手，老行为逐字不变。
        risk_pct>0        : 手数 = 权益 × risk_pct ÷ (止损倍数 × 信号根ATR × 乘数)，
                            夹在 [risk_hand_min, risk_hand_max]，开仓那一根锁定、
                            持仓期内不改（不加仓、不递减）。
                            risk_equity=0 -> 取账户实时权益 account.balance；
                            risk_equity 传固定值 -> 与回测 init_balance 完全对齐，
                            便于同一根 K 线两边算出相同手数、直接对账。
        ⚠️ 模拟盘不具备「止损被打掉亏的钱 ≈ 目标额」的核对能力，也不模拟冲击成本；
           多手（7-8 手）的真实滑点与挂单成交率只能在实盘或更真实的撮合环境里观察。

    运行记录（2026-09-20）：
        record_dir=None（默认）-> 不记录，老行为逐字不变。
        record_dir=目录      -> 落两个 CSV（成交流水 + 运行心跳），只为事后做台账，
                                不参与任何交易判断。台账用 工具/模拟盘台账.py 生成。

    ★★ 模拟盘正确性三个修正（2026-09-20；只动 run_live，run_backtest 一字未改）★
    这三条都不是「优化」，是 bug —— 它们污染的正是收口阶段唯一的产出（台账）：

      ① 止损守卫原来只看 `want`（意图），不看实际持仓 -> 未成交时照样下发
         平仓单、照样往台账写 STOPLOSS 行（幽灵）。现在加实际持仓守卫。
      ② `want` 与真实持仓脱钩 -> 未成交后策略「以为自己在场」，既不补单也
         不再开，静默空仓。现在按 reconcile_sec 定期对账（见
         live_reconcile_action），超时以实际为准并按结论 20 的「挂不上就放弃」
         清零，同时往台账写 DESYNC 事件。
      ③ 程序是止损的唯一执行者（交易所侧没有条件单）-> 重启/关机/崩溃/换月
         之后已有持仓裸奔。现在 attach_stop_on_start=True 时按
         「最近一根已收盘K线收盘价 ± stop_mult×ATR」补挂一条，并写 STOPATTACH。
         ⚠️ 真实开仓价不在本程序上下文里，所以这条线**不等于**回测口径的止损线，
            被触发时亏损不必然 ≈ 目标风险额；它只保证「从现在起有保护」。

    这两个参数只管上面 ②③，都不改变任何信号/参数/手数口径：
        attach_stop_on_start=True  启动时若已有持仓，补挂止损（False = 老行为）
        reconcile_sec=180.0        目标与实际不一致多久才算「真的没成交」
                                   （小于 2 个 heartbeat 没意义；设很大 = 几乎不对账）
    """
    name, main_code, mult, tick = config_symbols.get(symbol_key)
    user, pwd = config_account.check()

    # 让天勤 web_gui 面板标题显示品种名（面板标题取 sys.argv[0] 的文件名）
    sys.argv[0] = name

    print("[诊断] 正在连接快期模拟账户 TqKq ...", flush=True)
    api = TqApi(TqKq(), auth=TqAuth(user, pwd), web_gui=web_gui)
    print("[诊断] TqApi 已创建，正在拉取行情数据 ...", flush=True)
    klines = api.get_kline_serial(main_code, kline_sec,
                                  data_length=max(n1, n2, ma_filter) + 80)
    quote = api.get_quote(main_code)
    account = api.get_account()
    trade_log = api.get_trade()          # 真实成交记录
    api.wait_update(deadline=time.time() + 30)
    print("[诊断] 初始行情已就绪，进入主循环", flush=True)

    # ---- 在 web_gui 面板上画技术指标 ----
    # tqsdk 3.10.2 没有 api.create_chart()：指标以「附加列」写进 klines DataFrame，
    # web_gui 会自动识别并画在主图上（.board 指定图板，.color 指定颜色）。
    # ★ 2026-09-21（用户指示）：砍掉 5 分钟/3 分钟图板，均线直接画在信号用的
    #   30 分钟主图上（与 02 均线排列同款写法，已验证安全）；另在主图左下角
    #   画 30 分钟收盘倒计时（见 _paint_countdown）。
    gui_serials = []          # [(K线序列, 均线周期)]，主循环里每根新K线重算以向右延伸
    if web_gui:
        try:
            _periods = [n1, n2] + ([ma_filter] if ma_filter else [])
            _colors = [0xFFFF9900, 0xFF3399FF, 0xFF9933CC][:len(_periods)]
            _paint_ma(klines, _periods, _colors)
            gui_serials.append((klines, _periods))
            _desc = f"MA{n1}(橙)/MA{n2}(蓝)" + (f"/MA{ma_filter}(紫)" if ma_filter else "")
            print(f"  [图表] 均线画在 {kline_sec // 60} 分钟主图：{_desc}", flush=True)
        except Exception as e:
            print(f"  [!] 主图均线绑定失败（不影响策略运行）: {e}", flush=True)

    cur_sym = quote.underlying_symbol
    tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
    want = int(api.get_position(cur_sym).pos)   # 沿用已有持仓，不强行平掉
    seen = set(trade_log.keys())           # 启动前的历史成交不打，只盯新成交
    # 止损状态（atr_stop_mult 与 breakeven_atr 都为 0 时全程不用）
    #   注意：启动时若已有持仓，止损要等「下一次开仓/反手」才会挂上（历史持仓的
    #   开仓基准价不在本程序上下文里），下面会提示一句。
    stop_st = {"active": False, "entry": 0.0, "entry_atr": 0.0, "best_fav": 0.0}
    # 对账计时器（修正②）：want 与实际持仓不一致的起始时刻；一致时清空
    _dsync_since = None

    DIR_CN = {"BUY": "多", "SELL": "空"}
    OFF_CN = {"OPEN": "开仓", "CLOSE": "平仓", "CLOSETODAY": "平今"}

    print("=" * 60)
    print(f" 模拟盘启动：{name}  {main_code}")
    print(f" 当前主力合约：{cur_sym}")
    print(f" 账户可用资金：{account.available:,.2f} 元")
    print(f" 下单开关：{'开（会真发单到模拟账户）' if allow_trade else '关（只打印信号不下单）'}")
    # 启动横幅必须反映真实手数口径：v6 起 risk_pct>0 时手数由波动率预算决定，
    # volume 常量已不生效 —— 若仍打印 "{volume} 手" 会误导成「固定 1 手」。
    if risk_pct > 0:
        _eq_txt = (f"固定 {risk_equity:,.0f} 元（与回测 init_balance 对齐）"
                   if (risk_equity and risk_equity > 0) else "账户实时权益")
        print(f" 参数：MA{n1} / MA{n2} / 波动率预算 {risk_pct:g}% "
              f"-> {risk_hand_min}~{risk_hand_max} 手（按信号根 ATR 算，开仓那根锁定）")
        print(f" 算手数用的权益：{_eq_txt}")
    else:
        print(f" 参数：MA{n1} / MA{n2} / {volume} 手（固定）")
    if atr_stop_mult > 0 or breakeven_atr > 0:
        _st = []
        if atr_stop_mult > 0:
            _st.append(f"初始 {atr_stop_mult:g}×ATR({atr_period})")
        if breakeven_atr > 0:
            _st.append(f"保本（浮盈≥{breakeven_atr:g}×ATR 移到开仓基准价）")
        print(f" 止损：{' + '.join(_st)}（无移动/追踪）")
        if want != 0:
            if attach_stop_on_start:
                print(" [i] 启动时已有持仓 -> 按「最近已收盘K线收盘价 ± "
                      f"{atr_stop_mult:g}×ATR({atr_period})」补挂一条止损（见下方）")
            else:
                print(" [!] 启动时已有持仓，本次止损要等下一次开仓/反手才挂上"
                      "（attach_stop_on_start=False）")
    if no_open_window_min > 0:
        print(f" 开盘保护：开盘后 {no_open_window_min} 分钟内不开新仓（与回测 v7 同口径）")
    print("=" * 60)
    if web_gui:
        # 天勤 web_gui 每次启动分配随机端口，从内部获取真实地址
        gui_url = _get_web_gui_url(api)
        if gui_url:
            print(f" 浏览器监控面板 -> {gui_url}")
            try:
                import platform
                if platform.system() == "Windows":
                    os.startfile(gui_url)   # Windows 最可靠
                else:
                    webbrowser.open(gui_url)
            except Exception:
                pass
        else:
            print("   (web_gui 面板端口获取失败，查看天勤启动日志中的端口号)")
        print("   （可看实时K线、信号点、持仓、资金曲线；关掉窗口程序仍运行）")
    if not allow_trade:
        print("  [!] 当前只是观察模式：信号会打印，但不会真正下单")
    print("  盘中盯盘看这里：每笔真实成交会标 *，并定时刷新盘面快照\n")

    # ---- 运行记录（2026-09-20）：只落盘，不参与任何交易判断 ----
    #   record_dir=None 时 _rec.on 为 False，下面所有 _rec.xxx() 全是空操作。
    _rec = _LiveRecorder(record_dir, name, record_heartbeat_sec)
    if _rec.on:
        # 手续费：优先用 tqsdk 报的值（TqSim 实际就按它扣），拿不到再退回配置估值
        try:
            _comm = float(quote.commission)
        except Exception:
            _comm = -1.0
        if not (_comm == _comm) or _comm < 0:      # NaN / 非法
            _comm = float(config_symbols.COST.get(symbol_key, 0.0))
        if risk_pct > 0:
            _ptxt = (f"波动率预算 {risk_pct:g}% -> {risk_hand_min}~{risk_hand_max} 手"
                     f"（算手数权益 {risk_equity:,.0f}）")
        else:
            _ptxt = f"固定 {volume} 手"
        _rec.start(f"合约 {cur_sym} | 下单开关 {'开' if allow_trade else '关'} | "
                   f"MA{n1}/MA{n2}" + (f"/MA{ma_filter}" if ma_filter else "")
                   + f" | {_ptxt} | 手续费 {_comm:g} 元/手/单边")
        _rec.heartbeat(_dt_str(klines.iloc[-2]["datetime"]), float(quote.last_price),
                       int(api.get_position(cur_sym).pos), int(want), force=True)

        def _rec_bye(_r=_rec):
            _r.stop("程序退出（正常结束或 Ctrl+C）")
            _r.close()
        atexit.register(_rec_bye)
        print(f" 运行记录 -> {record_dir}")
        print("   （成交流水 + 运行心跳；台账用 工具/模拟盘台账.py 生成）")

    # ---- 启动补挂止损（2026-09-20 · 修正③）----
    #   止损是由**本程序**在 while 循环里判定的，交易所侧没有条件单。所以
    #   「Ctrl+C 重启 / 收盘关机第二天再开 / 崩溃后重启 / 主力换月」之后，
    #   已有持仓在下一根 K 线之前是**完全没有保护**的。原来只打印一句提示。
    #   做法：用【最近一根已收盘 K 线的收盘价】当基准挂线。
    #     · 真实开仓价不在本程序上下文里 -> 这条线**不是**回测口径的止损线，
    #       也**不能**拿去核对「单笔风险 ≈ 目标风险额」（risk_pct 的核对基准）。
    #     · 以当前价当基准，可保证止损线永远距现价 stop_mult×ATR，
    #       所以补挂本身不会立刻触发（不会一开机就被自己的止损打掉）。
    if attach_stop_on_start and want != 0 \
            and (atr_stop_mult > 0 or breakeven_atr > 0):
        _s0 = 1 if want > 0 else -1
        _base0 = float(klines.iloc[-2]["close"])
        try:
            _a0 = float(_atr(klines.high, klines.low, klines.close,
                             atr_period).iloc[-2])
        except Exception:
            _a0 = 0.0
        if _a0 and _a0 > 0:
            stop_st.update({"active": True, "entry": _base0,
                            "entry_atr": _a0, "best_fav": 0.0})
            # ⚠️ 这条线可能是 None —— 当某一档**只开保本、关掉初始止损**（历史上的
            #    「阶段B」配置：atr_stop_mult=0 而 breakeven_atr>0）时，因为这里
            #    传的浮盈峰值是 0，`_dual_ma_stop_line` 会返回 None。
            #    那不是错误，是那一档自己的语义：现在没有线，浮盈够了才出现。
            #    ⚠️ 当前 5 个品种的配置全是阶段A（2.0/0.0），所以这条分支
            #       **暂时走不到** —— 它是防御，防止将来切回阶段B 时被
            #       f"{None:.2f}" 直接抛 TypeError 把模拟盘打挂。
            #       （2026-09-20 复核过：5 个回测 + 5 个模拟盘脚本均为 2.0/0.0）
            _l0 = _dual_ma_stop_line(_base0, _a0, _s0, atr_stop_mult, 0.0,
                                     breakeven_atr)
            _sdir0 = "多" if _s0 > 0 else "空"
            print("=" * 60)
            print(f" [i] 启动补挂止损：已有 {int(want):+d} 手持仓（{_sdir0}）")
            print(f"     基准 {_base0:.2f}（最近已收盘K线收盘价，非真实开仓价）"
                  f" | ATR({atr_period}) {_a0:.2f}")
            if _l0 is None:
                print(f"     起始无止损线：本档未启用初始止损，只开了保本 —— "
                      f"浮盈达到 {breakeven_atr:g}×ATR 后保本线才会出现")
                print("     ⚠️ 也就是说：从现在到那一刻，这段持仓**没有止损保护**。")
                _rec.event("STOPATTACH",
                           f"启动补挂止损 基准 {_base0:.2f}(非真实开仓价) "
                           f"ATR({atr_period}) {_a0:.2f} 持仓 {int(want):+d}"
                           f" -> 起始无止损线（本档只开保本，"
                           f"浮盈达 {breakeven_atr:g}×ATR 后才出现）",
                           dir_cn=_sdir0)
            else:
                print(f"     -> 止损线 {_l0:.2f}")
                print("     注：真实开仓价不在本程序里，这条线不等于回测口径的止损线，")
                print("         被触发时亏损不必然 ≈ 目标风险额；它只保证「从现在起有保护」。")
                _rec.event("STOPATTACH",
                           f"启动补挂止损 基准 {_base0:.2f}(非真实开仓价) "
                           f"ATR({atr_period}) {_a0:.2f} 线 {_l0:.2f} "
                           f"持仓 {int(want):+d}",
                           dir_cn=_sdir0, ref_price=_l0)
            print("=" * 60, flush=True)
        else:
            print(f" [!] 启动补挂止损失败：ATR({atr_period}) 无效（{_a0}），"
                  f"本段持仓无止损保护", flush=True)
            _rec.event("STOPATTACH",
                       f"启动补挂止损失败 ATR({atr_period}) 无效（{_a0}）"
                       f" 持仓 {int(want):+d} -> 无止损保护",
                       dir_cn=("多" if _s0 > 0 else "空"))

    last_bar_dt = klines.iloc[-1]["datetime"]
    last_heart = datetime.now()
    # 盘面心跳的「状态指纹」（2026-09-21）：现价 / 持仓 / 目标 与上一次打印时
    # 完全一样，就不再打印。休市（午休、收盘后）没有新 tick，last_price 一动不动，
    # 原来会每 30 秒重复刷同一行，屏幕很快被刷满。指纹一变就立刻恢复打印。

    def ma_now():
        close = klines.close
        mf = close.rolling(n1).mean()
        ms = close.rolling(n2).mean()
        mt = close.rolling(ma_filter).mean() if ma_filter else None
        return mf.iloc[-1], ms.iloc[-1], (mt.iloc[-1] if ma_filter else None)

    while True:
        api.wait_update(deadline=time.time() + 30)

        # ---- 1) 真实成交监听：开仓 / 平仓一旦撮合立刻确认 ----
        if log_trades and api.is_changing(trade_log):
            for tid in trade_log:
                if tid in seen:
                    continue
                seen.add(tid)
                t = trade_log[tid]
                d = DIR_CN.get(t.direction, t.direction)
                o = OFF_CN.get(t.offset, t.offset)
                print(f"[{datetime.now():%H:%M:%S}] * 成交 {d}{o} {int(t.volume)}手 "
                      f"@ {float(t.price):.2f}  合约 {t.symbol}")
                _rec.fill(d, o, int(t.volume), float(t.price), t.symbol)
                if allow_trade:
                    print(f"            （已真实发送到模拟账户，可在快期3核对）")

        # ---- 2) 换月处理 ----
        if api.is_changing(quote, "underlying_symbol"):
            new_sym = quote.underlying_symbol
            if new_sym != cur_sym:
                print(f"[{datetime.now():%H:%M:%S}] 主力换月 {cur_sym} -> {new_sym}")
                tp.set_target_volume(0)
                while api.get_position(cur_sym).pos != 0:
                    api.wait_update(deadline=time.time() + 30)
                cur_sym = new_sym
                tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
                if allow_trade:
                    tp.set_target_volume(want)

        # ---- 3) 新 K 线：做一次判断 ----
        if api.is_changing(klines.iloc[-1], "datetime") and \
           klines.iloc[-1]["datetime"] != last_bar_dt:
            last_bar_dt = klines.iloc[-1]["datetime"]

            for _gk, _gps in gui_serials:      # 重算均线，让面板上的线随新K线延伸
                _paint_ma(_gk, _gps)

            close = klines.close
            ma_f = close.rolling(n1).mean()
            ma_s = close.rolling(n2).mean()
            now_f, now_s = ma_f.iloc[-2], ma_s.iloc[-2]
            pre_f, pre_s = ma_f.iloc[-3], ma_s.iloc[-3]
            extra = ()
            if ma_filter:
                ma_t = close.rolling(ma_filter).mean()
                extra = (ma_t.iloc[-2], ma_t.iloc[-3])

            # ---- 止损（与 run_backtest 同口径；阶段A 初始止损 / 阶段B 保本）----
            atr_v = 0.0
            if atr_stop_mult > 0 or breakeven_atr > 0 or risk_pct > 0:
                try:
                    atr_v = float(_atr(klines.high, klines.low, klines.close,
                                       atr_period).iloc[-2])
                except Exception:
                    atr_v = 0.0

            # ★ 修正①（2026-09-20）：守卫必须看**实际持仓**，不能只看 want（意图）。
            #   回测里 意图==实际，看不出区别；模拟盘/实盘会出现「下单没成交」——
            #   那时 want 仍是原目标，于是照样吃掉信号、照样下发平仓单、照样往台账
            #   写 STOPLOSS 行（幽灵止损），把「实测滑点」栏算歪。
            #   ⚠️ 回测那份守卫（run_backtest 里同名字的两行）**故意不改** ——
            #      改了会动 v1~v6 的信号序列，整本账的对照就废了。这是单独一次改动。
            _pos_guard = int(api.get_position(cur_sym).pos)
            if (atr_stop_mult > 0 or breakeven_atr > 0) \
                    and stop_st["active"] and want != 0 and _pos_guard != 0:
                _sdir = 1 if want > 0 else -1
                _satr = stop_st["entry_atr"]
                _barl = float(klines.low.iloc[-2])
                _barh = float(klines.high.iloc[-2])
                # 浮盈峰值：只用【已收盘】那根的极值更新，不看未收盘 K 线
                _fav = (_barh - stop_st["entry"] if _sdir > 0
                        else stop_st["entry"] - _barl)
                if _fav > stop_st["best_fav"]:
                    stop_st["best_fav"] = _fav
                _peak = stop_st["best_fav"]
                _sline = _dual_ma_stop_line(stop_st["entry"], _satr, _sdir,
                                            atr_stop_mult, _peak, breakeven_atr)
                _be = (breakeven_atr > 0 and _satr > 0
                       and _peak >= breakeven_atr * _satr)
                _why = "保本止损" if _be else "ATR止损"
                if _dual_ma_stop_hit(_sline, _sdir, _barh, _barl):
                    want = 0
                    stop_st["active"] = False
                    stop_st["entry_atr"] = 0.0
                    stop_st["best_fav"] = 0.0
                    print("=" * 60)
                    print(f"[{datetime.now():%H:%M:%S}] [止损] 触及{_why} -> "
                          f"平{'多' if _sdir > 0 else '空'}")
                    print(f"  止损线 {_sline:.2f}（基准 {stop_st['entry']:.2f}"
                          + (f" {'-' if _sdir > 0 else '+'}"
                             f" {atr_stop_mult:g}×ATR{_satr:.2f}"
                             if atr_stop_mult > 0 and not _be else "")
                          + f"，浮盈峰值 {_peak:.2f} 点）"
                          f"| 触发价 {quote.last_price:.2f}")
                    print("=" * 60)
                    if allow_trade:
                        tp.set_target_volume(0)
                        print("            > 已下达目标仓位 0 手（止损平仓），等待撮合...")
                    else:
                        print("            （观察模式，未下单）")
                    _rec.stoploss("多" if _sdir > 0 else "空", float(_sline),
                                  float(quote.last_price), _why)
                    continue

            acted = False
            if pass_dir:
                # 目标仓位式：把当前持仓方向喂给信号函数，返回 0 = 清仓（不反手）
                cur_dir = 1 if want > 0 else (-1 if want < 0 else 0)
                sig = signal_fn(cur_dir, now_f, now_s, pre_f, pre_s, *extra)
                if sig is None:
                    sig = cur_dir
                if sig != cur_dir:
                    acted = True
            else:
                sig = signal_fn(now_f, now_s, pre_f, pre_s, *extra)
                if sig != 0:
                    acted = True

            # ---- 开盘保护（2026-09-21 v7 同步，与 run_backtest 同一份逻辑）----
            # 只拦「空仓要开新仓」（want==0 且 sig!=0）；平仓/反手不受限。
            # 被拦的信号丢弃（不是推迟到窗口外），与回测口径一致。
            if acted and no_open_window_min > 0 and sig != 0 and want == 0 \
                    and _in_no_open_window(klines.iloc[-2]["datetime"],
                                           no_open_window_min):
                print(f"  [开盘保护] 跳过开仓 | 时间 "
                      f"{_dt_str(klines.iloc[-2]['datetime'])}"
                      f" | 开盘后 {no_open_window_min} 分钟内不开新仓", flush=True)
                acted = False

            if acted:
                # 手数：v6 波动率预算 —— 与 run_backtest 同一份口径：
                # 开几手由「信号根已收盘那根的 ATR」决定，与同一根锁定的止损线
                # 共用同一个 atr_v（同一把尺子）；开仓那一根锁定、持仓期不变。
                # risk_pct=0 时 _hand 恒等于 volume，老行为逐字不变。
                _hand = volume
                _eq = 0.0
                if risk_pct > 0 and sig != 0:
                    _eq = risk_equity
                    if not (_eq and _eq > 0):     # 0 / NaN / 非法 -> 账户实时权益
                        try:
                            _eq = float(account.balance)
                        except Exception:
                            _eq = 0.0
                    _hand = _risk_budget_hand(
                        _eq, risk_pct, atr_v, atr_stop_mult,
                        mult, risk_hand_min, risk_hand_max)
                want = sig * _hand
                # ---- 止损状态：开新仓/反手时锁基准价，平仓时下线 ----
                if atr_stop_mult > 0 or breakeven_atr > 0:
                    if sig == 0:
                        stop_st["active"] = False
                        stop_st["entry_atr"] = 0.0
                        stop_st["best_fav"] = 0.0
                    else:
                        stop_st.update({
                            "active": True,
                            "entry": float(klines.iloc[-2]["close"]),
                            "entry_atr": atr_v,
                            "best_fav": 0.0})
                # 手数一律取 want（= sig × 手数）；risk_pct=0 时 want 与 sig 同值，
                # 多手后必须显示 want，否则打印的「目标手数」与实际下单不符。
                if sig != 0:
                    _txt = f"目标 {int(want):+d} 手（{'多' if sig > 0 else '空'}）"
                    if risk_pct > 0:
                        _txt += (f"｜波动率预算 {risk_pct:g}% × 权益 {_eq:,.0f}"
                                 f" ÷ ({atr_stop_mult:g}×ATR{atr_v:.2f}×{mult:g})")
                else:
                    _txt = "目标 0 手（平仓，等待新信号）"
                # 运行记录：信号本身落盘。参考价取「信号根已收盘那根的收盘价」——
                # 与回测决策基准是同一个价，事后才能用它算真实滑点。
                # 平仓信号的手数取当时实际持仓（want 此时为 0，不能拿来记）。
                _rec.signal(
                    {1: "多", -1: "空"}.get(sig, "平"),
                    int(abs(want)) if sig != 0 else int(abs(api.get_position(cur_sym).pos)),
                    float(klines.iloc[-2]["close"]),
                    _dt_str(klines.iloc[-2]["datetime"]),
                    float(quote.last_price), atr_v,
                    "已下单" if allow_trade else "观察模式未下单")
                print("=" * 60)
                print(f"  [信号] {_txt}")
                print(f"  时间 {datetime.now():%Y-%m-%d %H:%M:%S} | 触发价 {quote.last_price:.2f}")
                _maline = f"  均线 MA{n1}={now_f:.2f} / MA{n2}={now_s:.2f}"
                if extra:
                    _maline += f" / MA{ma_filter}={extra[0]:.2f}"
                print(_maline)
                print("=" * 60)
                if web_gui and sig != 0:
                    _draw_signal(api, klines, is_buy=(sig > 0))
                if allow_trade:
                    tp.set_target_volume(want)
                    print(f"            > 已下达目标仓位 {want} 手，等待撮合...")
                else:
                    print(f"            （观察模式，未下单）")

        # ---- 3b) 30 分钟收盘倒计时（纯显示，每 tick 覆盖重画；异常静默）----
        if web_gui:
            _paint_countdown(api, klines, kline_sec)

        # ---- 4) 盘面心跳：定时刷新快照，确认程序没卡死 ----
        #   ★ 2026-09-21：每 heartbeat 周期无条件打印一行（静默守卫已去除），
        #   便于肉眼判断程序是否中断；价格/持仓不变时仍照打（间隔=heartbeat，PVC=300s）。
        #   下方 _rec.heartbeat 仍「休市照写」写入 CSV —— 台账和巡检靠 CSV 区分
        #   「程序没跑」与「在跑但条件不够」，那一层不动。
        if (datetime.now() - last_heart).total_seconds() >= heartbeat:
            last_heart = datetime.now()
            mf, ms, mt = ma_now()
            pos = api.get_position(cur_sym).pos
            _px_h = float(quote.last_price)
            _mtxt = f"MA{n1}={mf:.2f} MA{n2}={ms:.2f}"
            if mt is not None:
                _mtxt += f" MA{ma_filter}={mt:.2f}"
            print(f"[{datetime.now():%H:%M:%S}] 盘面 现价 {_px_h:.2f} "
                  f"| {_mtxt} "
                  f"| 持仓 {int(pos)}手 目标 {want} "
                  f"| 可用 {account.available:,.0f}")
            _rec.heartbeat(_dt_str(klines.iloc[-2]["datetime"]),
                           _px_h, pos, want)

        # ---- 5) 目标仓位 vs 实际持仓 对账（2026-09-20 · 修正②）----
        #   为什么需要：want 只在信号里被写。一旦「一点没成交 / 只成交一部分 /
        #   平仓单没成交」，want 就与真实持仓脱钩；而主循环里 cur_dir = sign(want)，
        #   信号函数会认为「已经在场」-> sig == cur_dir -> acted=False ->
        #   **既不补单、也不再开，静默空仓看着行情走完**（见 live_reconcile_action）。
        #   这里只对齐「目标 vs 实际」，不产生任何新的开仓意图：
        #   清零之后要等下一个真信号才会再开（规则2：不加条件、不择优）。
        _pos_now = int(api.get_position(cur_sym).pos)
        if want == _pos_now:
            _dsync_since = None
        else:
            if _dsync_since is None:
                _dsync_since = datetime.now()
            _el = (datetime.now() - _dsync_since).total_seconds()
            _act, _new = live_reconcile_action(want, _pos_now, _el,
                                               reconcile_sec)
            if _act == "wait":
                pass                      # 正常撮合延迟，什么都不做
            elif _act == "accept":
                print(f"[{datetime.now():%H:%M:%S}] [对账] 开仓只成交 "
                      f"{int(_pos_now):+d}/{int(want):+d} 手（{_el:.0f}s 未补齐）"
                      f" -> 以实际持仓为准")
                _rec.event("DESYNC",
                           f"部分成交 {int(_pos_now):+d}/{int(want):+d} 手"
                           f" 等待 {_el:.0f}s -> 接受实际持仓（手数不符）")
                want = _new
                _dsync_since = None
            elif _act == "trim":
                print(f"[{datetime.now():%H:%M:%S}] [对账] 实际持仓 "
                      f"{int(_pos_now):+d} 手**多于**目标 {int(want):+d} 手"
                      f"（{_el:.0f}s）-> 重发目标把它收回（超预算风险必须收）")
                _rec.event("DESYNC",
                           f"实际多于目标 {int(_pos_now):+d}/{int(want):+d} 手"
                           f" 等待 {_el:.0f}s -> 重发目标收回"
                           f"（含手动下单的情况）")
                if allow_trade:
                    tp.set_target_volume(want)
                _dsync_since = None
            elif _act == "retry":
                print(f"[{datetime.now():%H:%M:%S}] [对账] 平仓 {_el:.0f}s 未成交"
                      f"（仍持 {int(_pos_now):+d} 手）-> 重发平仓")
                _rec.event("DESYNC",
                           f"平仓未成交 仍持 {int(_pos_now):+d} 手"
                           f" 等待 {_el:.0f}s -> 重发平仓")
                if allow_trade:
                    tp.set_target_volume(0)
                _dsync_since = None
            else:                         # "flush" / "conflict"
                _why = ("完全未成交（一点都没开出来）" if _act == "flush"
                        else "方向冲突（目标与实际相反）")
                print("=" * 60)
                print(f"[{datetime.now():%H:%M:%S}] [对账] {_why}"
                      f" | 目标 {int(want):+d} 手 / 实际 {int(_pos_now):+d} 手"
                      f"（等待 {_el:.0f}s）-> 目标清零，等下一个信号")
                print("=" * 60, flush=True)
                _rec.event("DESYNC",
                           f"{_why} 目标 {int(want):+d} / 实际 {int(_pos_now):+d}"
                           f" 等待 {_el:.0f}s -> 目标清零并关闭止损监控")
                want = 0
                stop_st["active"] = False
                stop_st["entry_atr"] = 0.0
                stop_st["best_fav"] = 0.0
                if allow_trade:
                    tp.set_target_volume(0)
                _dsync_since = None


# ===================================================================
# 布林带策略（极简底层逻辑）
#   下轨做多 / 上轨做空 / 回到中轨平仓 / 越轨 stop_pct 硬止损
# ===================================================================
def _boll(close, period=20, k=2.0):
    """布林带三线：中轨 = MA(period)，上/下轨 = 中轨 ± k × 标准差。

    标准差用 pandas 默认的样本标准差（ddof=1），与国内行情软件的 STD 口径一致。
    返回 (mid, upper, lower) 三个 Series。
    """
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    return mid, mid + k * std, mid - k * std


def boll_decision(cur_pos, close_now, mid, upper, lower, volume=1):
    """布林带策略 v0 决策函数（回测 / 模拟盘共用，保证两侧逻辑完全一致）。

    v0 均值回归裸版（2026-09-21 定稿，参数全部先验值，不扫描）：
      空仓：收盘 <= 下轨 -> 开多（赌回归中轨）；收盘 >= 上轨 -> 开空
      v1 出场（2026-09-22，用户拍板定死 0.5，单变量实验）：
        持仓：收盘越过「中轨另一侧 0.5×半带宽」-> 平仓（赌回归超调；
        half_band=(upper-lower)/2=kσ，k=2 时即中轨外 1σ）。失败即回滚。
      止损：2×ATR 固定线，在引擎侧单独判定且优先级更高（见 run_backtest_boll /
      run_live_boll），本函数不再负责止损。
    只对已收盘的 K 线做判断，不看未收盘数据，因此不含未来函数。

    返回 (目标仓位手数, 动作说明)；目标仓位与当前相同 = 本根不动。
    """
    if cur_pos > 0:
        # 持多：收盘越过「中轨另一侧 0.5×半带宽」-> 平多（v1 超调出场）
        #   0.5×half_band = (upper-lower)×0.25；k=2 时 = 中轨外 1σ
        #   upper/lower 为 nan 时 _off 为 nan，比较恒 False -> 不动（与 v0 nan 行为一致）
        _off = (upper - lower) * 0.25
        if close_now >= mid + _off:
            return 0, "收盘越过中轨另一侧0.5×半带宽 -> 平多"
        return cur_pos, ""

    if cur_pos < 0:
        # 持空：收盘跌破「中轨另一侧 0.5×半带宽」-> 平空（v1 镜像）
        _off = (upper - lower) * 0.25
        if close_now <= mid - _off:
            return 0, "收盘跌破中轨另一侧0.5×半带宽 -> 平空"
        return cur_pos, ""

    # 空仓：收盘触及下轨 -> 开多；触及上轨 -> 开空
    if close_now <= lower:
        return volume, "收盘触及下轨 -> 开多"
    if close_now >= upper:
        return -volume, "收盘触及上轨 -> 开空"
    return 0, ""

def _paint_boll(kdata, period=20, k=2.0,
                colors=(0xFFFF3366, 0xFFFFCC00, 0xFF3366FF)):
    """在 web_gui 主图上叠加布林带三条线（上轨/中轨/下轨），纯显示。"""
    try:
        mid, up, lo = _boll(kdata.close, period, k)
        for col, ser, c in (("BOLLUP", up, colors[0]),
                            ("BOLLMID", mid, colors[1]),
                            ("BOLLLOW", lo, colors[2])):
            kdata[col] = ser
            kdata[col + ".board"] = "MAIN"
            kdata[col + ".color"] = c
    except Exception:
        pass


def run_backtest_boll(symbol_key, period=20, k=2.0, volume=1,
                      start=None, end=None, eval_start=None,
                      init_balance=200000, kline_sec=30 * 60,
                      strategy_name="布林带", open_html=True, sample_tag="",
                      slip_ticks=1, commission_per_hand=None,
                      atr_period=14, atr_stop_mult=2.0,
                      risk_pct=1.0, risk_hand_min=1, risk_hand_max=8,
                      time_stop_bars=None):
    """布林带策略 v0 回测（均值回归裸版，零过滤）。

    交易规则（决策见 boll_decision；止损在引擎侧，口径与 01 双均线完全一致）：
      1) 已收盘K线 收盘 <= 下轨 -> 下一根开盘开多；收盘 >= 上轨 -> 开空
      2) 持仓中，收盘回到中轨 -> 下一根开盘平仓
      2b) 时间止损（v2 2026-09-22）：持仓满 time_stop_bars 根仍未出场 -> 假设失效离场
          （K=20 复用布林周期；优先级低于 ATR 止损、高于决策出场）
      3) 止损：锚=信号根收盘价，宽度 = atr_stop_mult×ATR(信号根)，固定线不移动；
         已收盘K线的 high/low 触线 -> 该根收盘发单、成交在下一根开盘（与 01 同口径）
      4) 手数：波动率预算 risk_pct%（= 权益×risk% ÷ (atr_stop_mult×ATR×乘数)，
         夹在 [risk_hand_min, risk_hand_max]）
      5) 同一时间只持一个方向，不锁仓；信号在收盘后产生、下一根开盘成交 -> 无未来函数
    eval_start：此前只预热不交易。
    落盘：回测结果/03_布林带/<品种>/<品种>_布林带_<周期>[_in|_out].html / .txt
    """
    name, main_code, mult, tick = config_symbols.get(symbol_key)
    user, pwd = config_account.check()
    _snap_code(strategy_name, name)   # 回测前存档当前代码，供 工具/回滚.py 还原
    # ---- 成本口径（与 run_backtest 同一套：滑点折进 TqSim 手续费）----
    _cost_specified = commission_per_hand is not None
    _cfg_cost = float(getattr(config_symbols, "COST", {}).get(symbol_key, 0.0) or 0.0)
    slip_money = slip_ticks * mult * tick          # 1 手吃 1 跳 = 乘数 × 最小变动价位 元
    apply_cost = _cost_specified or slip_ticks > 0
    cost_per_hand = (commission_per_hand + slip_money) if _cost_specified else 0.0
    _cost_base = commission_per_hand if _cost_specified else 0.0

    sim = TqSim(init_balance)
    stat = None
    equity_curve = []   # [(bar_datetime_str, account.balance), ...]
    kline_history = []  # 每根已完成 K 线的 OHLCV，供报告画 K 线图
    trade_marks = []    # 买卖点明细（开/平仓），供在 K 线图上标注
    # 止损状态：基准价=信号根收盘价，ATR=信号根ATR，持仓期间不漂移（与 01 同口径）
    stop_st = {"active": False, "entry": 0.0, "entry_atr": 0.0}
    _bars_held = 0      # v2 时间止损计数：开仓信号根之后的已收盘根数
    eval_ts = pd.Timestamp(eval_start) if eval_start else None
    try:
        api = TqApi(
            sim,
            backtest=TqBacktest(start_dt=start, end_dt=end),
            auth=TqAuth(user, pwd),
            web_gui=False,      # 我们用自己的 HTML 报告
        )
        klines = api.get_kline_serial(main_code, kline_sec,
                                     data_length=period + 200)
        quote = api.get_quote(main_code)
        account = api.get_account()
        api.wait_update(deadline=time.time() + 30)

        cur_sym = quote.underlying_symbol
        tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
        if apply_cost:
            try:
                b = float(sim.get_commission(cur_sym))
                _cost_base = commission_per_hand if _cost_specified else (
                    b if b == b and b > 0 else _cfg_cost)
            except Exception:
                _cost_base = commission_per_hand if _cost_specified else _cfg_cost
            cost_per_hand = _cost_base + slip_money
            try:
                sim.set_commission(cur_sym, cost_per_hand)
                sim.set_commission(main_code, cost_per_hand)
            except Exception as e:
                print(f"  [!] 成本设置失败（不影响信号，但报告不含成本）: {e}")
        want = 0  # 目标仓位（正=多，负=空，0=空仓）
        print(f"\n>>> {name} {main_code} 布林带 v0 回测 {start} ~ {end}")
        print(f"    参数：BOLL({period},{k:g}) {kline_sec // 60}分钟 | 止损 "
              f"{atr_stop_mult:g}×ATR{atr_period}（锚=信号根收盘价） | "
              f"波动率预算 {risk_pct:g}% 手数[{risk_hand_min},{risk_hand_max}] | "
              f"滑点 {slip_ticks} 跳")
        if eval_ts is not None:
            print(f"   评估区间（实际交易）：{eval_start} 起；此前仅预热，不交易")

        while True:
            api.wait_update(deadline=time.time() + 30)

            # ---- 换月：主连背后的合约变了，先平旧的再切新的 ----
            if api.is_changing(quote, "underlying_symbol"):
                new_sym = quote.underlying_symbol
                if new_sym != cur_sym:
                    print(f"    换月 {cur_sym} -> {new_sym}")
                    tp.set_target_volume(0)
                    while api.get_position(cur_sym).pos != 0:
                        api.wait_update(deadline=time.time() + 30)
                    cur_sym = new_sym
                    tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
                    tp.set_target_volume(want)
                    # 2026-09-22 修：换月后新合约必须重设费率，否则该合约成交按交易所默认
                    # 手续费扣（实测 V 默认 1 元/手），slip_ticks 与指定手续费全部落空
                    # ——鲁棒测试 C 与基线逐位相同即此 bug 暴露；探针实证 set_commission
                    # 只对其后该合约的成交生效。01 run_backtest 换月分支本就有重设，不受影响。
                    if apply_cost:
                        try:
                            sim.set_commission(cur_sym, cost_per_hand)
                        except Exception:
                            pass

            # ---- 每根新 K 线：记权益 + 判断一次 ----
            if api.is_changing(klines.iloc[-1], "datetime"):
                bar_dt = klines.iloc[-2]["datetime"]
                try:
                    equity_curve.append((_dt_str(bar_dt), float(account.balance)))
                except Exception:
                    pass

                # 布林带三条线（用已收盘K线，含当前这根；iloc[-1] 未收盘不参与）
                mid_s, up_s, lo_s = _boll(klines.close, period, k)
                m = float(mid_s.iloc[-2])
                u = float(up_s.iloc[-2])
                l = float(lo_s.iloc[-2])
                c = float(klines.close.iloc[-2])
                if m != m:          # 预热不足，布林带尚未算出（NaN）
                    continue

                # 预热期：只累积权益曲线，不交易、不标注
                if eval_ts is not None and pd.Timestamp(bar_dt) < eval_ts:
                    continue

                try:
                    kline_history.append({
                        "dt": _dt_str(bar_dt),
                        "open": float(klines.open.iloc[-2]),
                        "high": float(klines.high.iloc[-2]),
                        "low": float(klines.low.iloc[-2]),
                        "close": c,
                        "volume": float(klines.volume.iloc[-2]),
                    })
                except Exception:
                    pass

                # ATR（信号根）：止损线与手数共用同一把尺子（与 01 同口径）
                atr_v = 0.0
                if atr_stop_mult > 0 or risk_pct > 0:
                    try:
                        atr_v = float(_atr(klines.high, klines.low, klines.close,
                                           atr_period).iloc[-2])
                    except Exception:
                        atr_v = 0.0

                # ---- 止损（优先级高于中轨平仓）：已收盘K线极值触线，收盘发单 ----
                if atr_stop_mult > 0 and stop_st["active"] and want != 0:
                    _sdir = 1 if want > 0 else -1
                    _sline = _dual_ma_stop_line(stop_st["entry"],
                                                stop_st["entry_atr"],
                                                _sdir, atr_stop_mult)
                    _barl = float(klines.low.iloc[-2])
                    _barh = float(klines.high.iloc[-2])
                    if _dual_ma_stop_hit(_sline, _sdir, _barh, _barl):
                        _sentry, _satr = stop_st["entry"], stop_st["entry_atr"]
                        want = 0
                        stop_st = {"active": False, "entry": 0.0, "entry_atr": 0.0}
                        print(f"    [止损] {_dt_str(bar_dt)} 触及 "
                              f"{atr_stop_mult:g}×ATR 止损线 {_sline:.2f}"
                              f"（基准 {_sentry:.2f} "
                              f"{'-' if _sdir > 0 else '+'} "
                              f"{atr_stop_mult:g}×ATR{_satr:.2f}）"
                              f" | 本根 高 {_barh:.2f} / 低 {_barl:.2f}")
                        trade_marks.append({"dt": _dt_str(bar_dt),
                                            "dir": "SELL" if _sdir > 0 else "BUY",
                                            "price": float(_sline),
                                            "reason": "ATR止损"})
                        tp.set_target_volume(0)
                        continue

                # ---- 时间止损（v2 2026-09-22）：持仓满 time_stop_bars 根仍未出场 -> 假设失效 ----
                #   口径与 ATR 止损一致：已收盘根判定，收盘发单、下根开盘成交；
                #   计数 = 开仓信号根之后的已收盘根数（开仓当根记 0）；K=20 复用布林周期
                if time_stop_bars and want != 0:
                    _bars_held += 1
                    if _bars_held >= time_stop_bars:
                        _tdir = want
                        want = 0
                        stop_st = {"active": False, "entry": 0.0, "entry_atr": 0.0}
                        print(f"    [时间止损] {_dt_str(bar_dt)} 持仓 {_bars_held} 根"
                              f"（>= {time_stop_bars}）仍未触及出场线 -> 假设失效离场")
                        trade_marks.append({"dt": _dt_str(bar_dt),
                                            "dir": "SELL" if _tdir > 0 else "BUY",
                                            "price": float(c),
                                            "reason": "时间止损"})
                        tp.set_target_volume(0)
                        continue

                target, reason = boll_decision(want, c, m, u, l, 1)
                if target != want:
                    if target == 0:
                        # 平仓：止损状态下线
                        stop_st = {"active": False, "entry": 0.0, "entry_atr": 0.0}
                    else:
                        # 开新仓：手数 = 波动率预算（信号根 ATR 同一把尺子）
                        _hand = volume
                        if risk_pct > 0:
                            _hand = _risk_budget_hand(
                                init_balance, risk_pct, atr_v, atr_stop_mult,
                                mult, risk_hand_min, risk_hand_max)
                        target = int(_hand) if target > 0 else -int(_hand)
                        stop_st = {"active": True, "entry": c, "entry_atr": atr_v}
                        _bars_held = 0
                    _extra = ""
                    if target != 0 and atr_stop_mult > 0 and stop_st["active"]:
                        _sl = (stop_st["entry"] - atr_stop_mult * stop_st["entry_atr"]
                               if target > 0 else
                               stop_st["entry"] + atr_stop_mult * stop_st["entry_atr"])
                        _extra = f" | 止损线 {_sl:.2f}"
                    d = "BUY" if target > want else "SELL"
                    print(f"    {_dt_str(bar_dt)} {reason} {abs(target)}手  "
                          f"收盘 {c:.2f} | 上轨 {u:.2f} 中轨 {m:.2f} 下轨 {l:.2f}"
                          f"{_extra}")
                    trade_marks.append({"dt": _dt_str(bar_dt), "dir": d,
                                        "price": c, "reason": reason})
                    want = target
                    tp.set_target_volume(want)

    except BacktestFinished:
        stat = sim.tqsdk_stat
        sdict = _stat_to_dict(stat)
        trades = _collect_trades(api)
        _print_stat(name, sdict)
        # ---- 成本诊断（v0 预注册判据：笔均毛利 vs 成本）----
        try:
            _net = float(account.balance) - init_balance
            _fills = sum(int(t.get("volume", 0)) for t in trades)
            _cost = _fills * cost_per_hand
            _gross = _net + _cost
            if _fills > 0 and abs(_gross) > 1e-9:
                print(f"  [成本诊断] 单边成交 {_fills} 笔 | 总成本(手续费+滑点) "
                      f"{_cost:,.0f} 元 | 净利 {_net:+,.0f} 元 | 毛利约 {_gross:+,.0f} 元"
                      f" | 成本/|毛利| {abs(_cost) / abs(_gross) * 100:.0f}%")
        except Exception as _e:
            print(f"  [成本诊断] 失败：{_e}")
        param_text = f"BOLL({period},{k:g}) v0"
        extra = (f"周期：{kline_sec // 60}分钟K线 | 信号评估区间：{eval_start} ~ {end}"
                 f"（{start} 起仅预热不交易）| "
                 f"开仓：收盘触及下轨 -> 开多 / 收盘触及上轨 -> 开空 | "
                 f"平仓：收盘回到中轨 或 触及 {atr_stop_mult:g}×ATR{atr_period} 止损线"
                 f"（锚=信号根收盘价，固定不移动）| "
                 f"手数：波动率预算 {risk_pct:g}% 夹[{risk_hand_min},{risk_hand_max}] | "
                 f"滑点 {slip_ticks} 跳 | 同一时间仅持一个方向，不锁仓")
        # 买卖点 -> K线图标记（BUY=开多/平空，SELL=开空/平多）；最多显示 50 个
        signal_events = [{"dt": _x["dt"], "dir": _x["dir"]} for _x in trade_marks]
        html_path, txt_path = _save_backtest_report(
            strategy_name, symbol_key, name, sdict, equity_curve, trades,
            start, end, param_text, volume, kline_sec, init_balance, main_code,
            extra_note=extra, kline_history=kline_history, signal_events=signal_events,
            mult=mult, trade_marks=trade_marks, sample_tag=sample_tag)
        print(f"\n  回测记录已保存：{txt_path}")
        print(f"  回测报告已生成：{html_path}")
        if open_html:
            try:
                os.startfile(html_path)
            except Exception:
                pass
        api.close()
    return stat

def run_live_boll(symbol_key, period=20, k=2.0, volume=1,
                  kline_sec=30 * 60, allow_trade=True, web_gui=True,
                  heartbeat=30, log_trades=True,
                  atr_period=14, atr_stop_mult=2.0,
                  risk_pct=1.0, risk_hand_min=1, risk_hand_max=8,
                  time_stop_bars=None,
                  attach_stop_on_start=True, reconcile_sec=180.0,
                  state_path=None):
    """布林带模拟盘（连快期模拟账户跑实时信号，程序要一直开着）。

    逻辑与 run_backtest_boll 完全一致 —— 两侧共用同一个 boll_decision，
    改规则只需改那一处，回测与模拟盘不会走偏。

    盯盘能力：
      - 实时成交监听：开仓/平仓一旦真实撮合，立刻在窗口打印确认
      - 盘面心跳：每 heartbeat 秒打印一次现价 / 布林带三轨 / 持仓 / 目标仓位
      - web_gui 面板：自动打开，主图上叠加布林带三线 + 买卖点标记

    allow_trade=False 时只打印信号、不下单（观察模式，建议先跑这个）。
    """
    name, main_code, mult, tick = config_symbols.get(symbol_key)
    user, pwd = config_account.check()

    # 让天勤 web_gui 面板标题显示品种名（面板标题取 sys.argv[0] 的文件名）
    sys.argv[0] = name

    print("[诊断] 正在连接快期模拟账户 TqKq ...", flush=True)
    api = TqApi(TqKq(), auth=TqAuth(user, pwd), web_gui=web_gui)
    print("[诊断] TqApi 已创建，正在拉取行情数据 ...", flush=True)
    klines = api.get_kline_serial(main_code, kline_sec, data_length=period + 200)
    quote = api.get_quote(main_code)
    account = api.get_account()
    trade_log = api.get_trade()          # 真实成交记录
    api.wait_update(deadline=time.time() + 30)
    print("[诊断] 初始行情已就绪，进入主循环", flush=True)

    # ---- 在 web_gui 面板上画布林带（附加列写进 klines，web_gui 自动画在主图）----
    if web_gui:
        try:
            _paint_boll(klines, period, k)
            print(f"  [图表] 已添加布林带 BOLL({period},{k:g}) 上轨(粉红)/中轨(金黄)/下轨(蓝)", flush=True)
        except Exception as e:
            print(f"  [!] 布林带绑定失败（不影响策略运行）: {e}", flush=True)

    cur_sym = quote.underlying_symbol
    tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
    want = api.get_position(cur_sym).pos   # 沿用已有持仓，不强行平掉
    seen = set(trade_log.keys())           # 启动前的历史成交不打，只盯新成交

    # 止损状态（v0：锚=信号根收盘价，固定不移动；与 run_backtest_boll 同口径）
    stop_st = {"active": False, "entry": 0.0, "entry_atr": 0.0}
    _bars_held = 0      # v2 时间止损计数：开仓信号根之后的已收盘根数

    # ---- 三修正移植（2026-09-22，自 run_live ②③）+ 状态文件持久化 ----
    #   状态文件记录 entry/entry_atr/bars_held/方向/合约：重启后若方向与实际持仓
    #   一致则**精确恢复**回测口径的止损线与时间止损计数（优于 01 的现价重锚）；
    #   无状态文件时退回 01 口径（最近已收盘K线收盘价 ± mult×ATR 补挂）。
    import json as _json
    if state_path is None:
        state_path = os.path.join(os.getcwd(), "_boll_state.json")

    def _save_state():
        try:
            _json.dump({"symbol": cur_sym, "dir": 1 if want > 0 else -1,
                        "entry": stop_st["entry"], "entry_atr": stop_st["entry_atr"],
                        "bars_held": _bars_held,
                        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                       io.open(state_path, "w", encoding="utf-8"))
        except Exception as _e:
            print(f"  [!] 状态文件写入失败（不影响交易，仅影响重启恢复）: {_e}",
                  flush=True)

    def _clear_state():
        try:
            if os.path.exists(state_path):
                os.remove(state_path)
        except Exception:
            pass

    # 对账计时器（修正②）
    _dsync_since = None

    DIR_CN = {"BUY": "多", "SELL": "空"}
    OFF_CN = {"OPEN": "开仓", "CLOSE": "平仓", "CLOSETODAY": "平今"}

    print("=" * 60)
    print(f" 模拟盘启动：{name}  {main_code}")
    print(f" 当前主力合约：{cur_sym}")
    print(f" 账户可用资金：{account.available:,.2f} 元")
    print(f" 下单开关：{'开（会真发单到模拟账户）' if allow_trade else '关（只打印信号不下单）'}")
    print(f" 参数：BOLL({period},{k:g}) {kline_sec // 60}分钟 | 止损 {atr_stop_mult:g}×ATR{atr_period}（锚=信号根收盘价） | 波动率预算 {risk_pct:g}% | 时间止损 {time_stop_bars if time_stop_bars else '关'}根")
    # ---- 启动补挂止损（修正③）+ 状态文件精确恢复 ----
    if want != 0 and atr_stop_mult > 0:
        _sdir0 = 1 if want > 0 else -1
        _restored = False
        try:
            if os.path.exists(state_path):
                _st0 = _json.load(io.open(state_path, encoding="utf-8"))
                if _st0.get("dir") == _sdir0 and _st0.get("symbol") == cur_sym \
                        and float(_st0.get("entry_atr", 0)) > 0:
                    stop_st = {"active": True,
                               "entry": float(_st0["entry"]),
                               "entry_atr": float(_st0["entry_atr"])}
                    _bars_held = int(_st0.get("bars_held", 0))
                    _l0 = _dual_ma_stop_line(stop_st["entry"], stop_st["entry_atr"],
                                             _sdir0, atr_stop_mult)
                    print(f" [i] 状态文件精确恢复：{'多' if _sdir0 > 0 else '空'}仓 "
                          f"{int(want):+d} 手 | 基准(开仓信号根收盘) {stop_st['entry']:.2f} "
                          f"| ATR {stop_st['entry_atr']:.2f} | 已持 {_bars_held} 根"
                          + (f" | 止损线 {_l0:.2f}（回测口径）" if _l0 else ""))
                    _restored = True
        except Exception as _e:
            print(f"  [!] 状态文件读取失败，转普通补挂: {_e}", flush=True)
        if not _restored:
            _base0 = float(klines.iloc[-2]["close"])
            try:
                _a0 = float(_atr(klines.high, klines.low, klines.close,
                                 atr_period).iloc[-2])
            except Exception:
                _a0 = 0.0
            if _a0 and _a0 > 0:
                stop_st = {"active": True, "entry": _base0, "entry_atr": _a0}
                _l0 = _dual_ma_stop_line(_base0, _a0, _sdir0, atr_stop_mult)
                print(f" [i] 启动补挂止损（无状态文件，01 口径现价重锚）："
                      f"{'多' if _sdir0 > 0 else '空'}仓 {int(want):+d} 手 | "
                      f"基准 {_base0:.2f} | ATR {_a0:.2f}"
                      + (f" | 止损线 {_l0:.2f}" if _l0 else ""))
                print("     ⚠️ 此线非回测口径（真实开仓价不在本程序上下文），"
                      "只保证「从现在起有保护」")
            else:
                print(f" [!] 启动补挂止损失败：ATR 无效（{_a0}），本段持仓无保护")
    else:
        _clear_state()          # 无持仓却残留状态文件（盘外手动平仓等）-> 清掉
    print("=" * 60)
    if web_gui:
        # 天勤 web_gui 每次启动分配随机端口，从内部获取真实地址
        gui_url = _get_web_gui_url(api)
        if gui_url:
            print(f" 浏览器监控面板 -> {gui_url}")
            try:
                import platform
                if platform.system() == "Windows":
                    os.startfile(gui_url)   # Windows 最可靠
                else:
                    webbrowser.open(gui_url)
            except Exception:
                pass
        else:
            print("   (web_gui 面板端口获取失败，查看天勤启动日志中的端口号)")
        print("   （可看实时K线、布林带、买卖点、持仓、资金曲线；关掉窗口程序仍运行）")
    if not allow_trade:
        print("  [!] 当前只是观察模式：信号会打印，但不会真正下单")
    print("  盘中盯盘看这里：每笔真实成交会标 *，并定时刷新盘面快照\n")

    last_bar_dt = klines.iloc[-1]["datetime"]
    last_heart = datetime.now()
    # 盘面心跳的「状态指纹」（2026-09-21）：现价/持仓/目标 与上次打印时完全一样
    # 就不再打印，休市期间不再重复刷屏。指纹一变立刻恢复。

    def boll_now():
        mid_s, up_s, lo_s = _boll(klines.close, period, k)
        return float(mid_s.iloc[-1]), float(up_s.iloc[-1]), float(lo_s.iloc[-1])

    while True:
        api.wait_update(deadline=time.time() + 30)

        # ---- 1) 真实成交监听：开仓 / 平仓一旦撮合立刻确认 ----
        if log_trades and api.is_changing(trade_log):
            for tid in trade_log:
                if tid in seen:
                    continue
                seen.add(tid)
                t = trade_log[tid]
                d = DIR_CN.get(t.direction, t.direction)
                o = OFF_CN.get(t.offset, t.offset)
                print(f"[{datetime.now():%H:%M:%S}] * 成交 {d}{o} {int(t.volume)}手 "
                      f"@ {float(t.price):.2f}  合约 {t.symbol}")
                if allow_trade:
                    print("            （已真实发送到模拟账户，可在快期3核对）")

        # ---- 2) 换月处理 ----
        if api.is_changing(quote, "underlying_symbol"):
            new_sym = quote.underlying_symbol
            if new_sym != cur_sym:
                print(f"[{datetime.now():%H:%M:%S}] 主力换月 {cur_sym} -> {new_sym}")
                tp.set_target_volume(0)
                while api.get_position(cur_sym).pos != 0:
                    api.wait_update(deadline=time.time() + 30)
                cur_sym = new_sym
                tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
                if allow_trade:
                    tp.set_target_volume(want)

        # ---- 3) 新 K 线：做一次判断（只用已收盘的那根）----
        if api.is_changing(klines.iloc[-1], "datetime") and \
           klines.iloc[-1]["datetime"] != last_bar_dt:
            last_bar_dt = klines.iloc[-1]["datetime"]

            if web_gui:
                _paint_boll(klines, period, k)   # 重算，让三条线随新K线延伸

            mid_s, up_s, lo_s = _boll(klines.close, period, k)
            m = float(mid_s.iloc[-2])
            u = float(up_s.iloc[-2])
            l = float(lo_s.iloc[-2])
            c = float(klines.close.iloc[-2])
            if m != m:                            # 预热不足，布林带还没算出来
                print(f"[{datetime.now():%H:%M:%S}] 布林带预热中（不足 {period} 根），暂不判断")
            else:
                # ATR（信号根）：止损线与手数共用同一把尺子（与 01 同口径）
                atr_v = 0.0
                if atr_stop_mult > 0 or risk_pct > 0:
                    try:
                        atr_v = float(_atr(klines.high, klines.low, klines.close,
                                           atr_period).iloc[-2])
                    except Exception:
                        atr_v = 0.0
                # ---- 止损（优先级最高）：已收盘K线极值触线，收盘发单、下一根成交 ----
                _stopped = False
                if atr_stop_mult > 0 and stop_st["active"] and want != 0:
                    _sdir = 1 if want > 0 else -1
                    _sline = _dual_ma_stop_line(stop_st["entry"],
                                                stop_st["entry_atr"],
                                                _sdir, atr_stop_mult)
                    _barl = float(klines.low.iloc[-2])
                    _barh = float(klines.high.iloc[-2])
                    if _dual_ma_stop_hit(_sline, _sdir, _barh, _barl):
                        _stopped = True
                        print("=" * 60)
                        print(f"  [止损] 触及 {atr_stop_mult:g}×ATR 止损线 {_sline:.2f}"
                              f"（基准 {stop_st['entry']:.2f}）| 目标 0 手")
                        print(f"  时间 {datetime.now():%Y-%m-%d %H:%M:%S} | "
                              f"触发价 {quote.last_price:.2f}")
                        print(f"  本根 高 {_barh:.2f} / 低 {_barl:.2f}")
                        print("=" * 60)
                        want = 0
                        stop_st = {"active": False, "entry": 0.0, "entry_atr": 0.0}
                        _clear_state()
                        if web_gui:
                            _draw_signal(api, klines, is_buy=(_sdir < 0))
                        if allow_trade:
                            tp.set_target_volume(0)
                            print("            > 已下达目标仓位 0 手，等待撮合...")
                        else:
                            print("            （观察模式，未下单）")
                if not _stopped:
                    # ---- 时间止损（v2 2026-09-22）：持仓满 time_stop_bars 根未出场 -> 假设失效
                    #      与回测同口径：每根新已收盘K线计数，收盘发单、下根成交；K=20 复用布林周期
                    _ts_fired = False
                    if time_stop_bars and want != 0:
                        _bars_held += 1
                        _save_state()          # 计数推进即落盘，重启可续算
                        if _bars_held >= time_stop_bars:
                            _ts_fired = True
                            _tdir = want
                            want = 0
                            stop_st = {"active": False, "entry": 0.0,
                                       "entry_atr": 0.0}
                            _clear_state()
                            print("=" * 60)
                            print(f"  [时间止损] 持仓 {_bars_held} 根"
                                  f"（>= {time_stop_bars}）仍未出场 -> 假设失效离场")
                            print(f"  时间 {datetime.now():%Y-%m-%d %H:%M:%S} | "
                                  f"现价 {quote.last_price:.2f}")
                            print("=" * 60)
                            if web_gui:
                                _draw_signal(api, klines, is_buy=(_tdir < 0))
                            if allow_trade:
                                tp.set_target_volume(0)
                                print("            > 已下达目标仓位 0 手，等待撮合...")
                            else:
                                print("            （观察模式，未下单）")
                    if _ts_fired:
                        target, reason = 0, ""
                    else:
                        target, reason = boll_decision(want, c, m, u, l, 1)
                    if target != want:
                        if target == 0:
                            stop_st = {"active": False, "entry": 0.0,
                                       "entry_atr": 0.0}
                            _clear_state()
                        else:
                            _hand = volume
                            if risk_pct > 0:
                                _hand = _risk_budget_hand(
                                    float(account.balance), risk_pct, atr_v,
                                    atr_stop_mult, mult,
                                    risk_hand_min, risk_hand_max)
                            target = int(_hand) if target > 0 else -int(_hand)
                            stop_st = {"active": True, "entry": c,
                                       "entry_atr": atr_v}
                            _bars_held = 0
                            _save_state()
                        print("=" * 60)
                        print(f"  [信号] {reason} | 目标仓位 {target} 手")
                        print(f"  时间 {datetime.now():%Y-%m-%d %H:%M:%S} | "
                              f"触发价 {quote.last_price:.2f}")
                        print(f"  收盘 {c:.2f} | 上轨 {u:.2f} 中轨 {m:.2f} "
                              f"下轨 {l:.2f}")
                        print("=" * 60)
                        if web_gui:
                            _draw_signal(api, klines, is_buy=(target > want))
                        want = target
                        if allow_trade:
                            tp.set_target_volume(want)
                            print(f"            > 已下达目标仓位 {want} 手，等待撮合...")
                        else:
                            print("            （观察模式，未下单）")

        # ---- 4) 盘面心跳：定时刷新快照，确认程序没卡死 ----
        #   ★ 2026-09-21：每 heartbeat 周期无条件打印一行（静默守卫已去除），便于肉眼判断中断。
        if (datetime.now() - last_heart).total_seconds() >= heartbeat:
            last_heart = datetime.now()
            m, u, l = boll_now()
            pos = api.get_position(cur_sym).pos
            _px_h = float(quote.last_price)
            print(f"[{datetime.now():%H:%M:%S}] 盘面 现价 {_px_h:.2f} "
                  f"| 上轨 {u:.2f} 中轨 {m:.2f} 下轨 {l:.2f} "
                      f"| 持仓 {int(pos)}手 目标 {want} "
                      f"| 可用 {account.available:,.0f}")

        # ---- 5) 目标仓位 vs 实际持仓 对账（修正②，自 run_live 移植）----
        #   只对齐「目标 vs 实际」，不产生任何新的开仓意图；清零后等下一个真信号。
        _pos_now = int(api.get_position(cur_sym).pos)
        if want == _pos_now:
            _dsync_since = None
        else:
            if _dsync_since is None:
                _dsync_since = datetime.now()
            _el = (datetime.now() - _dsync_since).total_seconds()
            _act, _new = live_reconcile_action(want, _pos_now, _el,
                                               reconcile_sec)
            if _act == "wait":
                pass                      # 正常撮合延迟
            elif _act == "accept":
                print(f"[{datetime.now():%H:%M:%S}] [对账] 开仓只成交 "
                      f"{int(_pos_now):+d}/{int(want):+d} 手（{_el:.0f}s 未补齐）"
                      f" -> 以实际持仓为准")
                want = _new
                _save_state()             # 手数变了但方向/锚不变，刷新存档
                _dsync_since = None
            elif _act == "trim":
                print(f"[{datetime.now():%H:%M:%S}] [对账] 实际持仓 "
                      f"{int(_pos_now):+d} 手多于目标 {int(want):+d} 手"
                      f"（{_el:.0f}s）-> 重发目标收回")
                if allow_trade:
                    tp.set_target_volume(want)
                _dsync_since = None
            elif _act == "retry":
                print(f"[{datetime.now():%H:%M:%S}] [对账] 平仓 {_el:.0f}s 未成交"
                      f"（仍持 {int(_pos_now):+d} 手）-> 重发平仓")
                if allow_trade:
                    tp.set_target_volume(0)
                _dsync_since = None
            else:                         # "flush" / "conflict"
                _why = ("完全未成交（一点都没开出来）" if _act == "flush"
                        else "方向冲突（目标与实际相反）")
                print("=" * 60)
                print(f"[{datetime.now():%H:%M:%S}] [对账] {_why}"
                      f" | 目标 {int(want):+d} 手 / 实际 {int(_pos_now):+d} 手"
                      f"（等待 {_el:.0f}s）-> 目标清零，等下一个信号")
                print("=" * 60, flush=True)
                want = 0
                stop_st = {"active": False, "entry": 0.0, "entry_atr": 0.0}
                _bars_held = 0
                _clear_state()
                if allow_trade:
                    tp.set_target_volume(0)
                _dsync_since = None


# ===================================================================
# 均线多空排列策略（四均线：多头排列做多 / 空头排列做空 / 震荡不进场）
# ===================================================================
def _macd(close, fast=8, slow=17, signal=9):
    """计算 MACD，返回 (dif, dea) 两个 pandas Series。MACD 柱 = (dif - dea)。"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea


def align_signal_long_macd(ma1, ma2, ma3, ma4, close,
                           dif, dea, pdif, pdea, in_long,
                           atr=0.0, adx=999.0, adx_min=0.0):
    """均线排列·仅做多（MACD 8/17/9）信号函数，所有品种共用。

    入参（引擎自动计算并传入）：
        ma1..ma4 : 短->长 四根均线当前值（MA5/MA10/MA20/MA40）
        close    : 当前收盘价
        dif/dea  : MACD 当前 DIF / DEA
        pdif/pdea: 上一根 K 线的 DIF / DEA（用于判断金叉/死叉）
        in_long  : 当前是否已持多仓
        atr      : 当前 ATR（引擎侧回调/追高判定用，本函数不直接使用）
        adx      : 当前 ADX 趋势强度（999 = 未启用过滤）
        adx_min  : ADX 门槛；>0 时趋势强度不达标不开新仓（震荡市过滤）

    规则：
        开仓（空仓时返回 +1）：MA5>MA10>MA20>MA40 多头排列 且
                            MACD 水上金叉（DIF>0 且 DEA>0 且 DIF 上穿 DEA）三者同时成立，
                            且 ADX >= adx_min（adx_min>0 时）。
        持仓中（持多时）：   出现 MACD 死叉（DIF 下穿 DEA）返回 0 平仓；否则返回 +1 继续持有。
        其余情况返回 0（空仓观望）。

    返回 +1 = 做多/持有多，0 = 空仓/平多；本策略永不返回 -1（仅做多）。
    """
    bull = (ma1 > ma2 > ma3 > ma4)                       # 多头排列
    above_water = (dif > 0) and (dea > 0)                # 水上（零轴之上）
    golden = (pdif <= pdea) and (dif > dea)              # 金叉：DIF 上穿 DEA
    death = (pdif >= pdea) and (dif < dea)               # 死叉：DIF 下穿 DEA
    if in_long:
        return 0 if death else 1                         # 死叉平多，否则继续持有
    if adx_min > 0 and adx < adx_min:
        return 0                                         # 趋势强度不足（震荡市）不新开
    return 1 if (bull and above_water and golden) else 0


def align_signal_dual_macd(ma1, ma2, ma3, ma4, close,
                           dif, dea, pdif, pdea, in_pos,
                           atr=0.0, adx=999.0, adx_min=0.0):
    """均线排列·双向（MACD 8/17/9）信号函数 —— 空头侧与多头侧完全镜像。

    入参与 align_signal_long_macd 完全一致，仅 in_pos 语义扩展：
        in_pos = +1 持多 / -1 持空 / 0 空仓
        （旧的长-only 信号函数把非 0 当作持多，与 +1 传入行为一致，互不影响）

    规则（多头侧与 align_signal_long_macd 逐字相同；空头侧为完全镜像）：
        多头排列 = MA5>MA10>MA20>MA40；空头排列 = MA5<MA10<MA20<MA40
        水上 = DIF>0 且 DEA>0；水下 = DIF<0 且 DEA<0
        金叉 = DIF 上穿 DEA；死叉 = DIF 下穿 DEA

        持多时：  死叉 -> 0 平多；否则 +1 继续持有。
        持空时：  金叉 -> 0 平空；否则 -1 继续持有（镜像）。
        空仓时：  多头排列 且 水上金叉 -> +1 开多；
                  空头排列 且 水下死叉 -> -1 开空（镜像）；否则 0 观望。

    返回 +1 = 做多/持有多，-1 = 做空/持有空，0 = 空仓/平仓。
    """
    bull = (ma1 > ma2 > ma3 > ma4)                       # 多头排列
    bear = (ma1 < ma2 < ma3 < ma4)                       # 空头排列（镜像）
    above_water = (dif > 0) and (dea > 0)                # 水上（零轴之上）
    below_water = (dif < 0) and (dea < 0)                # 水下（零轴之下，镜像）
    golden = (pdif <= pdea) and (dif > dea)              # 金叉：DIF 上穿 DEA
    death = (pdif >= pdea) and (dif < dea)               # 死叉：DIF 下穿 DEA
    if in_pos > 0:
        return 0 if death else 1                         # 死叉平多，否则继续持有
    if in_pos < 0:
        return 0 if golden else -1                       # 金叉平空，否则继续持有（镜像）
    if adx_min > 0 and adx < adx_min:
        return 0                                         # 趋势强度不足（震荡市）不新开
    if bull and above_water and golden:
        return 1
    if bear and below_water and death:                   # 镜像开空
        return -1
    return 0


def _align_trail_stop_long(entry, entry_atr, ext_high, cur_atr, close,
                           atr_stop_mult, breakeven_atr=2.0, lock_atr=3.0,
                           trail_atr_mult=2.5, min_stop_atr=2.0,
                           use_ma_stop=False, ma40=None, slip_price=0.0):
    """均线排列（仅做多）动态 ATR 止损线 —— 纯函数，便于单测。

    阶梯口径与升级版 _pro_bar_decision 的 long 分支完全一致：
      ① 初始止损 = 开仓价 - atr_stop_mult x 开仓时ATR
      ② 可选结构止损：跌破 MA40（use_ma_stop=True 时）
      ③ 止损至少距开仓价 min_stop_atr x ATR（防被结构止损顶得太近）
      ④ 浮盈 > breakeven_atr x ATR  -> 止损上移到开仓价（保本）
      ⑤ 浮盈 > lock_atr x ATR       -> 止损锁在 开仓价 + 1 x ATR
      ⑥ 峰值回撤追踪：止损上移到 持仓期最高价 - trail_atr_mult x 当前ATR
         （仅当该价位高于开仓价时才启用，否则由初始止损主导）
    返回止损价；未启用（atr_stop_mult<=0 或 entry_atr<=0）时返回 None。
    """
    if atr_stop_mult <= 0 or not entry_atr or entry_atr <= 0:
        return None
    profit = close - entry
    stop = entry - atr_stop_mult * entry_atr
    if use_ma_stop and ma40 is not None:
        stop = max(stop, ma40 - slip_price)
    stop = min(stop, entry - min_stop_atr * entry_atr)
    if profit > breakeven_atr * entry_atr:
        stop = max(stop, entry)
    if profit > lock_atr * entry_atr:
        stop = max(stop, entry + 1.0 * entry_atr)
    if ext_high is not None and cur_atr and cur_atr > 0:
        soft = ext_high - trail_atr_mult * cur_atr
        if soft > entry:
            # 追踪线必须站上开仓价才纳入。否则开仓初期(ext_high≈开仓价)追踪线
            # 会比初始止损更近、被 max() 选中，导致 atr_stop_mult 形同虚设
            # （设 3.5ATR 实际只给到 2.5ATR）。
            stop = max(stop, soft)
    return stop


def run_backtest_align(symbol_key, signal_fn, ma_periods=(5, 10, 20, 40),
                       volume=1, start=date(2026, 1, 1), end=date(2026, 9, 13),
                       eval_start=None, init_balance=200000,
                       kline_sec=5 * 60, strategy_name="均线排列",
                       open_html=True, stop_pct=0.0, macd=(8, 17, 9),
                       atr_stop_mult=0.0, breakeven_atr=2.0, lock_atr=3.0,
                       trail_atr_mult=2.5, min_stop_atr=2.0,
                       use_ma_stop=False, atr_period=14,
                       adx_filter=False, adx_min=20.0, adx_period=14,
                       entry_ext_atr=0.0, pullback_wait_bars=12,
                       pullback_tol_atr=0.5, sample_tag="", stop_atr=0.0,
                       swing_exit=False, swing_confirm_bars=2):
    """均线排列回测：signal_fn(ma1,ma2,ma3,ma4,close,dif,dea,pdif,pdea,in_pos)-> +1/0/-1。

    信号约定（K 线周期由脚本 KLINE_SEC 传入）：
      · 空仓(in_pos=0)返回 +1：开多；返回 -1：开空（双向信号，如 align_signal_dual_macd）
      · 持多返回 0：平多；持空返回 0：平空
      · 持多返回 -1 / 持空返回 +1：当日反向（平仓同时反手开仓）
      · 仅返回 +1/0 的旧长-only 信号函数（如 align_signal_long_macd）行为不变
    落盘：回测结果/02_均线排列/<品种>/<品种>_均线排列_<周期>[_in|_out].html
    eval_start：在此之前只做均线预热、不交易。macd=(fast, slow, signal) 默认 8/17/9。

    固定 ATR 止损（stop_atr>0 时启用；多空对称、开仓时定死不移动）：
      持多：止损线 = 开仓价 - stop_atr x 开仓时ATR，跌破即平多。
      持空：止损线 = 开仓价 + stop_atr x 开仓时ATR，升破即平空（镜像）。

    动态 ATR 止损（atr_stop_mult>0 时启用；旧阶梯口径，与 stop_atr 互斥，stop_atr 优先）：
      初始 atr_stop_mult x 开仓时ATR；浮盈 > breakeven_atr 时止损移到开仓价（保本）；
      浮盈 > lock_atr 时锁在开仓价+1ATR；之后按 持仓期最高价 - trail_atr_mult x 当前ATR
      实时上移追踪；可选 use_ma_stop 追加「跌破 MA40」结构止损。

    信号质量过滤（2026-09-16 新增，默认全关以保持原行为）：
      · adx_filter/adx_min：ADX >= adx_min 才允许开新仓，过滤震荡市假信号。
      · entry_ext_atr：金叉时若价格已高于 MA10 超过 N x ATR，则不追高，改为登记
        「等回踩」，最多等 pullback_wait_bars 根；价格回到 MA10 + pullback_tol_atr x ATR
        以内即进场；等待期间多头排列破坏或 MACD 掉回水下则取消本次入场。

    摆动线出场（v2，swing_exit=True 时启用；2026-09-23 用户拍板「方案A：替换」）：
      把「MACD 死叉平多 / 金叉平空」整体换成 05 摆动线的结构出场，开仓条件一字不动。
        · 持多：收盘价 < 最近确认摆动低点 -> 平多（收盘确认，盘中刺破不算）
        · 持空：收盘价 > 最近确认摆动高点 -> 平空（镜像）
        · 摆动点确认滞后 swing_confirm_bars 根（默认 2=05 源指标口径），用已收盘K线，防重绘
        · 持仓期间不再看死叉/金叉 —— 因此原「死叉+空头排列+水下 -> 平多反手开空」
          这条**反手路径也一并停用**（交叉是瞬时事件，摆动线平仓通常晚好几根，
          等它触发时那个交叉早已翻页）。配套的 shadow_events 影子统计会把
          「原出场信号若生效会在哪根触发」记下来但不执行，用于事后判断
          「保留死叉」的 B 方案还值不值得做。
        · 出场后空仓，等下一次「排列+交叉」重新开仓；本根不再开新仓。
      与 stop_atr 的关系：两者独立并存 —— stop_atr 是盘中触及即平的固定线（不移动），
      摆动线是收盘确认的结构线，互不替代（与 05 摆动线 v2 口径一致）。
    """
    name, main_code, mult, tick = config_symbols.get(symbol_key)
    user, pwd = config_account.check()
    _snap_code(strategy_name, name)   # 回测前存档当前代码，供 工具/回滚.py 还原
    sim = TqSim(init_balance)
    stat = None
    equity_curve = []
    kline_history = []  # 每根已完成 K 线的 OHLCV，供报告画 K 线图
    eval_ts = pd.Timestamp(eval_start) if eval_start else None
    try:
        api = TqApi(
            sim,
            backtest=TqBacktest(start_dt=start, end_dt=end),
            auth=TqAuth(user, pwd),
            web_gui=False,
        )
        klines = api.get_kline_serial(main_code, kline_sec,
                                     data_length=max(max(ma_periods), macd[1] * 3) + 150)
        quote = api.get_quote(main_code)
        account = api.get_account()
        api.wait_update(deadline=time.time() + 30)

        cur_sym = quote.underlying_symbol
        tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
        want = 0
        trade_marks = []  # 买卖点明细（开/平仓），供在 K 线图上标注
        stop_st = {"active": False, "entry": 0.0, "entry_atr": 0.0,
                   "ext_high": 0.0, "bars": 0, "side": 0}   # ATR 止损状态（side +1多/-1空）
        pend_st = {"bars": 0}                    # 「金叉后等回踩 MA10」等待状态
        # ---- v2 摆动线出场（swing_exit）状态 ----
        swing_hh2 = None                 # 最近确认摆动高点（持空时的出场线）
        swing_ll2 = None                 # 最近确认摆动低点（持多时的出场线）
        swing_entry_i = 0                # 本笔开仓时的影子事件计数（判断「死叉是否先到」）
        shadow_events = []               # 影子统计：原死叉/金叉出场若生效会在此触发（不执行）
        swing_pos_log = []               # 影子统计：每笔摆动线平仓时的对照记录
        n_swing_close = 0                # 摆动线平仓笔数
        print(f"\n>>> {name} {main_code} 均线排列回测 {start} ~ {end}")
        if eval_ts is not None:
            print(f"   评估区间（实际交易）：{eval_start} 起；此前为均线预热，不交易")

        while True:
            api.wait_update(deadline=time.time() + 30)

            if api.is_changing(quote, "underlying_symbol"):
                new_sym = quote.underlying_symbol
                if new_sym != cur_sym:
                    print(f"    换月 {cur_sym} -> {new_sym}")
                    tp.set_target_volume(0)
                    while api.get_position(cur_sym).pos != 0:
                        api.wait_update(deadline=time.time() + 30)
                    cur_sym = new_sym
                    tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
                    tp.set_target_volume(want)

            if api.is_changing(klines.iloc[-1], "datetime"):
                bar_dt = klines.iloc[-2]["datetime"]
                try:
                    equity_curve.append((_dt_str(bar_dt), float(account.balance)))
                except Exception:
                    pass

                close = klines.close
                vals = [float(close.rolling(p).mean().iloc[-2]) for p in ma_periods]
                last_close = float(close.iloc[-2])

                # ---- v2 摆动点确认：预热期照常累积（保证评估起点结构状态正确）----
                if swing_exit:
                    _nh, _nl = _swing_update_levels(klines, swing_confirm_bars)
                    if _nh is not None:
                        swing_hh2 = _nh
                    if _nl is not None:
                        swing_ll2 = _nl

                if eval_ts is not None and pd.Timestamp(bar_dt) < eval_ts:
                    continue

                try:
                    kline_history.append({
                        "dt": _dt_str(bar_dt),
                        "open": float(klines.open.iloc[-2]),
                        "high": float(klines.high.iloc[-2]),
                        "low": float(klines.low.iloc[-2]),
                        "close": last_close,
                        "volume": float(klines.volume.iloc[-2]),
                    })
                except Exception:
                    pass

                # 止损：基于当前持仓均价（tqsdk 3.10+ 用 open_price_long/short）
                pos = api.get_position(cur_sym)
                net_pos = pos.pos_long_his + pos.pos_long_today - pos.pos_short_his - pos.pos_short_today
                if stop_pct > 0 and net_pos != 0:
                    avg = float(pos.open_price_long) if net_pos > 0 else float(pos.open_price_short)
                    if (net_pos > 0 and last_close < avg * (1 - stop_pct)) or \
                       (net_pos < 0 and last_close > avg * (1 + stop_pct)):
                        want = 0
                        tp.set_target_volume(0)
                        print(f"    {_dt_str(bar_dt)} 触及 {stop_pct*100:.0f}% 止损，平仓")
                        continue

                # ---- ATR 止损：先算 ATR，再判本根是否被止损打掉 ----
                atr_v = 0.0
                if atr_stop_mult > 0 or entry_ext_atr > 0 or stop_atr > 0:
                    try:
                        atr_v = float(_atr(klines.high, klines.low, klines.close,
                                           atr_period).iloc[-2])
                    except Exception:
                        atr_v = 0.0
                last_high = float(klines.high.iloc[-2])
                last_low = float(klines.low.iloc[-2])
                if stop_st["active"] and want != 0:
                    if stop_atr > 0:
                        # 固定 ATR 止损线（开仓时定死、不移动），多空对称
                        _hit_side = 1 if stop_st["side"] > 0 else -1
                        if _hit_side > 0:
                            stop_line = stop_st["entry"] - stop_atr * stop_st["entry_atr"]
                            _hit = last_low <= stop_line
                        else:
                            stop_line = stop_st["entry"] + stop_atr * stop_st["entry_atr"]
                            _hit = last_high >= stop_line
                        if _hit:
                            want = 0
                            stop_st["active"] = False
                            tp.set_target_volume(0)
                            trade_marks.append({"dt": _dt_str(bar_dt),
                                                "dir": "SELL" if _hit_side > 0 else "BUY",
                                                "price": stop_line,
                                                "reason": f"{stop_atr:g}ATR固定止损(触发价{stop_line:.2f})"})
                            print(f"    {_dt_str(bar_dt)} {stop_atr:g}ATR固定止损 -> "
                                  f"{'平多' if _hit_side > 0 else '平空'} {volume}手"
                                  f" | 触发价 {stop_line:.2f} | 开仓 {stop_st['entry']:.2f}"
                                  f" 开仓ATR {stop_st['entry_atr']:.2f}")
                            continue
                    elif want > 0 and atr_stop_mult > 0:
                        stop_st["bars"] += 1
                        stop_st["ext_high"] = max(stop_st["ext_high"], last_high)
                        stop_line = _align_trail_stop_long(
                            stop_st["entry"], stop_st["entry_atr"], stop_st["ext_high"],
                            atr_v, last_close, atr_stop_mult, breakeven_atr, lock_atr,
                            trail_atr_mult, min_stop_atr, use_ma_stop, vals[3], 0.0)
                        if stop_line is not None and last_low <= stop_line:
                            want = 0
                            stop_st["active"] = False
                            tp.set_target_volume(0)
                            trade_marks.append({"dt": _dt_str(bar_dt), "dir": "SELL",
                                                "price": stop_line,
                                                "reason": f"ATR动态止损(触发价{stop_line:.2f})"})
                            print(f"    {_dt_str(bar_dt)} ATR动态止损 -> 平多 {volume}手"
                                  f" | 触发价 {stop_line:.2f} | 开仓 {stop_st['entry']:.2f}"
                                  f" 最高 {stop_st['ext_high']:.2f} ATR {atr_v:.2f}")
                            continue

                # ---- v2 摆动线出场（swing_exit）：收盘确认，盘中刺破不算 ----
                if swing_exit and want != 0:
                    _sw_side = 1 if want > 0 else -1
                    _sw_line = swing_ll2 if _sw_side > 0 else swing_hh2
                    _sw_hit = (_sw_line is not None and
                               (last_close < _sw_line if _sw_side > 0
                                else last_close > _sw_line))
                    if _sw_hit:
                        _n_shadow_pre = len(shadow_events) - swing_entry_i
                        n_swing_close += 1
                        swing_pos_log.append({"dt": _dt_str(bar_dt),
                                              "n_shadow_before": _n_shadow_pre})
                        _sw_cn = "平多" if _sw_side > 0 else "平空"
                        _sw_tag = (f"收盘{'跌破' if _sw_side > 0 else '上穿'}最近确认摆动"
                                   f"{'低' if _sw_side > 0 else '高'}点{_sw_line:.2f} -> {_sw_cn}")
                        want = 0
                        stop_st["active"] = False
                        tp.set_target_volume(0)
                        trade_marks.append({"dt": _dt_str(bar_dt),
                                            "dir": "SELL" if _sw_side > 0 else "BUY",
                                            "price": _sw_line, "reason": _sw_tag})
                        print(f"    {_dt_str(bar_dt)} {_sw_tag} {volume}手"
                              f" | 收盘 {last_close:.1f} 摆动线 {_sw_line:.1f}"
                              f"（本笔持仓期间原死叉/金叉信号 {_n_shadow_pre} 次，"
                              f"按方案A 未执行）")
                        continue

                dif_s, dea_s = _macd(close, *macd)
                dif_v = float(dif_s.iloc[-2]); dea_v = float(dea_s.iloc[-2])
                pdif = float(dif_s.iloc[-3]); pdea = float(dea_s.iloc[-3])

                # ADX 趋势强度（adx_filter 关闭时传 999 = 不过滤）
                adx_v = 999.0
                if adx_filter:
                    try:
                        adx_s, _pdi, _mdi = _adx(klines.high, klines.low,
                                                 klines.close, adx_period)
                        adx_v = float(adx_s.iloc[-2])
                    except Exception:
                        adx_v = 999.0

                bull_now = (vals[0] > vals[1] > vals[2] > vals[3])
                bear_now = (vals[0] < vals[1] < vals[2] < vals[3])
                in_pos = 1 if want > 0 else (-1 if want < 0 else 0)
                sig_orig = signal_fn(*vals, last_close, dif_v, dea_v, pdif, pdea, in_pos,
                                     atr=atr_v, adx=adx_v,
                                     adx_min=(adx_min if adx_filter else 0.0))
                sig = sig_orig
                if swing_exit:
                    # 影子统计：原死叉/金叉出场若仍然生效，会在本根触发（只记录、不执行）
                    if in_pos != 0 and sig_orig != in_pos:
                        _sh_kind = "平仓" if sig_orig == 0 else "反手"
                        shadow_events.append({"dt": _dt_str(bar_dt), "side": in_pos,
                                              "kind": _sh_kind, "close": last_close})
                        print(f"    〔影子〕{_dt_str(bar_dt)} 原"
                              f"{'死叉平多' if in_pos > 0 else '金叉平空'}"
                              f"（{_sh_kind}）若生效会在此动仓 —— 摆动线未触发，方案A 继续持有")
                    sig = in_pos          # 持仓期间不看死叉/金叉，出场交给摆动线

                # ---- 开仓/平仓（含「金叉后不追高、等回踩 MA10」状态机）----
                do_open = False
                do_close = False
                open_side = 0          # +1 开多 / -1 开空
                open_tag = ""
                close_tag = ""
                close_dir = "SELL"     # 平仓成交方向：平多=SELL / 平空=BUY
                if want == 0:
                    if pend_st["bars"] > 0:
                        if (not bull_now) or dif_v <= 0:
                            print(f"    {_dt_str(bar_dt)} 等回踩期间多头排列破坏/掉回水下，"
                                  f"取消本次开仓")
                            pend_st["bars"] = 0
                        else:
                            pend_st["bars"] -= 1
                            if last_close <= vals[1] + pullback_tol_atr * atr_v:
                                do_open = True
                                open_side = 1
                                open_tag = "金叉后回踩MA10 -> 开多"
                                pend_st["bars"] = 0
                            elif pend_st["bars"] == 0:
                                print(f"    {_dt_str(bar_dt)} 等回踩超时"
                                      f"（{pullback_wait_bars} 根），放弃本次开仓")
                    elif sig == 1:
                        if entry_ext_atr > 0 and atr_v > 0 and \
                           (last_close - vals[1]) > entry_ext_atr * atr_v:
                            pend_st["bars"] = pullback_wait_bars
                            print(f"    {_dt_str(bar_dt)} 金叉但已高于 MA10 "
                                  f"{last_close - vals[1]:.1f} 点（>{entry_ext_atr:g}ATR），"
                                  f"不追高 -> 等回踩，最多等 {pullback_wait_bars} 根")
                        else:
                            do_open = True
                            open_side = 1
                            open_tag = "金叉+多头排列+水上 -> 开多"
                    elif sig == -1:
                        do_open = True
                        open_side = -1
                        open_tag = "死叉+空头排列+水下 -> 开空"
                elif want > 0:
                    # 持多：0=平多；-1=平多且反手开空（多头侧信号函数永不返回 -1，行为不变）
                    if sig == 0:
                        do_close = True
                        close_tag = "MACD 死叉 -> 平多"
                        close_dir = "SELL"
                    elif sig == -1:
                        do_close = True
                        do_open = True
                        open_side = -1
                        open_tag = "死叉+空头排列+水下 -> 反手开空"
                else:
                    # 持空（镜像）：0=平空；+1=平空且反手开多
                    if sig == 0:
                        do_close = True
                        close_tag = "MACD 金叉 -> 平空"
                        close_dir = "BUY"
                    elif sig == 1:
                        do_close = True
                        do_open = True
                        open_side = 1
                        open_tag = "金叉+多头排列+水上 -> 反手开多"

                if do_open:
                    want = volume * open_side
                    swing_entry_i = len(shadow_events)   # v2 影子统计：本笔起点的影子计数
                    _side_cn = "开多" if open_side > 0 else "开空"
                    print(f"    {_dt_str(bar_dt)} {open_tag} {volume}手  "
                          f"MA5={vals[0]:.1f} MA10={vals[1]:.1f} "
                          f"MA20={vals[2]:.1f} MA40={vals[3]:.1f} "
                          f"| DIF={dif_v:.2f} DEA={dea_v:.2f} ADX={adx_v:.1f}")
                    trade_marks.append({"dt": _dt_str(bar_dt),
                                        "dir": "BUY" if open_side > 0 else "SELL",
                                        "price": last_close, "reason": open_tag})
                    if stop_atr > 0:
                        # 固定 ATR 止损线：开仓时定死，多空对称，不移动
                        stop_st.update({"active": True, "entry": last_close,
                                        "entry_atr": atr_v, "side": open_side,
                                        "ext_high": last_high, "ext_low": last_low,
                                        "bars": 0})
                        _sl = (last_close - stop_atr * atr_v if open_side > 0
                               else last_close + stop_atr * atr_v)
                        print(f"           初始止损 {_sl:.2f}"
                              f"（{stop_atr:g}ATR = {stop_atr * atr_v:.2f} 点，固定不动）")
                    elif atr_stop_mult > 0 and open_side > 0:
                        stop_st.update({"active": True, "entry": last_close,
                                        "entry_atr": atr_v, "side": 1,
                                        "ext_high": last_high, "bars": 0})
                        _sl = _align_trail_stop_long(
                            last_close, atr_v, last_high, atr_v, last_close,
                            atr_stop_mult, breakeven_atr, lock_atr,
                            trail_atr_mult, min_stop_atr, use_ma_stop,
                            vals[3], 0.0)
                        if _sl is not None:
                            print(f"           初始止损 {_sl:.2f}"
                                  f"（{atr_stop_mult:g}ATR = "
                                  f"{atr_stop_mult * atr_v:.2f} 点）")
                    tp.set_target_volume(want)
                elif do_close:
                    want = 0
                    stop_st["active"] = False
                    print(f"    {_dt_str(bar_dt)} {close_tag} {volume}手  "
                          f"MA5={vals[0]:.1f} MA10={vals[1]:.1f} "
                          f"MA20={vals[2]:.1f} MA40={vals[3]:.1f} "
                          f"| DIF={dif_v:.2f} DEA={dea_v:.2f}")
                    trade_marks.append({"dt": _dt_str(bar_dt), "dir": close_dir,
                                        "price": last_close,
                                        "reason": close_tag})
                    tp.set_target_volume(want)

    except BacktestFinished:
        stat = sim.tqsdk_stat
        sdict = _stat_to_dict(stat)
        trades = _collect_trades(api)
        _print_stat(name, sdict)
        if swing_exit:
            _n_pre = sum(1 for x in swing_pos_log if x["n_shadow_before"] > 0)
            _n_dc = sum(1 for x in shadow_events if x["side"] > 0)
            print(f"\n  [影子统计] 摆动线出场 vs 原死叉/金叉出场（方案A：只记录、不执行）")
            print(f"    摆动线平仓 {n_swing_close} 笔；持仓期间原出场信号共出现 "
                  f"{len(shadow_events)} 次（死叉 {_n_dc} / 金叉 "
                  f"{len(shadow_events) - _n_dc}）")
            if n_swing_close:
                print(f"    其中 {_n_pre} 笔在摆动线平仓之前就已出现过原出场信号"
                      f"（{_n_pre / n_swing_close * 100:.0f}%）"
                      f" —— 这部分是「方案B 保留死叉」才会提前平的仓位")
        ma_text = "/".join(f"MA{p}" for p in ma_periods) + \
                  f" + MACD({macd[0]},{macd[1]},{macd[2]})"
        _is_dual = "dual" in getattr(signal_fn, "__name__", "")
        if stop_atr > 0:
            stop_txt = f"{stop_atr:g}ATR 固定止损（开仓时定死不移动，多空对称）"
        elif atr_stop_mult > 0:
            stop_txt = (f"动态ATR止损(初始{atr_stop_mult:g}ATR / 保本{breakeven_atr:g}ATR / "
                        f"锁定{lock_atr:g}ATR / 峰值回撤{trail_atr_mult:g}ATR追踪"
                        f"{' / 跌破MA40结构止损' if use_ma_stop else ''})")
        else:
            stop_txt = "未启用止损"
        if _is_dual:
            _exit_txt = ((f"平多/平空：摆动线（持多收盘跌破最近确认摆动低点 / "
                          f"持空收盘上穿最近确认摆动高点；{swing_confirm_bars}根确认，收盘确认）")
                         if swing_exit else "平多：MACD 死叉 | 平空（镜像）：MACD 金叉")
            open_txt = (f"开多：{ma_text} 多头排列 且 MACD 水上金叉 三者同时成立 | "
                        f"开空（镜像）：{ma_text} 空头排列 且 MACD 水下死叉 三者同时成立 | "
                        f"{_exit_txt}")
            side_txt = "双向（多空对称）"
        else:
            open_txt = (f"开仓：{ma_text} 多头排列 且 MACD 水上金叉 三者同时成立 | "
                        f"平仓：MACD 死叉 或")
            side_txt = "仅做多"
        filt_txt = ""
        if adx_filter:
            filt_txt += f"ADX({adx_period})>={adx_min:g} 才开仓 "
        if entry_ext_atr > 0:
            filt_txt += (f"不追高（高于 MA10 超 {entry_ext_atr:g}ATR 则等回踩 "
                         f"MA10+{pullback_tol_atr:g}ATR，最多 {pullback_wait_bars} 根） ")
        if filt_txt:
            filt_txt = "过滤：" + filt_txt + "| "
        if swing_exit:
            _exit_line = (f"出场：① 止损({stop_txt}) ② 摆动线结构出场（收盘确认）"
                          f" —— MACD 死叉/金叉平仓已停用")
        else:
            _exit_line = f"出场：止损({stop_txt}) 或 反向交叉平仓"
        extra = (f"周期：{kline_sec // 60}分钟K线 | 信号评估区间：{eval_start} ~ {end}"
                 f"（{start} 起仅预热不交易）| "
                 f"{open_txt} | {filt_txt}"
                 f"{_exit_line} | {side_txt}")
        # 买卖点 -> K线图标记（align 仅做多：BUY=开多，SELL=平多）；最多显示 50 个
        signal_events = [{"dt": _m["dt"], "dir": _m["dir"]} for _m in trade_marks]
        html_path, txt_path = _save_backtest_report(
            strategy_name, symbol_key, name, sdict, equity_curve, trades,
            start, end, ma_text, volume, kline_sec, init_balance, main_code,
            extra_note=extra, kline_history=kline_history, signal_events=signal_events,
            mult=mult, trade_marks=trade_marks, sample_tag=sample_tag)
        print(f"\n  回测记录已保存：{txt_path}")
        print(f"  回测报告已生成：{html_path}")
        if open_html:
            try:
                os.startfile(html_path)
            except Exception:
                pass
        api.close()
    return stat


def run_live_align(symbol_key, signal_fn, ma_periods=(5, 10, 20, 40),
                   volume=1, kline_sec=5 * 60, allow_trade=True,
                   web_gui=True, heartbeat=30, stop_pct=0.0, macd=(8, 17, 9),
                   atr_stop_mult=0.0, breakeven_atr=2.0, lock_atr=3.0,
                   trail_atr_mult=2.5, min_stop_atr=2.0,
                   use_ma_stop=False, atr_period=14,
                   adx_filter=False, adx_min=20.0, adx_period=14,
                   entry_ext_atr=0.0, pullback_wait_bars=12,
                   pullback_tol_atr=0.5, stop_atr=0.0):
    """均线排列模拟盘：signal_fn(ma1..ma4,close,dif,dea,pdif,pdea,in_pos)->+1/0/-1（双向）。
    盯盘能力与 run_live 一致（*成交 / 盘面心跳 / web_gui）。macd 默认 8/17/9。
    固定 ATR 止损与 run_backtest_align 同口径（stop_atr>0 启用：开仓时定死、多空对称、不移动）。
    动态 ATR 止损（atr_stop_mult>0，旧阶梯口径，仅多头）与 run_backtest_align 同口径，默认 0=关闭。
    信号质量过滤（adx_filter / entry_ext_atr）同样与回测同口径，默认全关。"""
    name, main_code, mult, tick = config_symbols.get(symbol_key)
    user, pwd = config_account.check()

    # 让天勤 web_gui 面板标题显示品种名（面板标题取 sys.argv[0] 的文件名）
    sys.argv[0] = name

    api = TqApi(TqKq(), auth=TqAuth(user, pwd), web_gui=web_gui)
    klines = api.get_kline_serial(main_code, kline_sec,
                                 data_length=max(max(ma_periods), macd[1] * 3) + 150)
    quote = api.get_quote(main_code)
    account = api.get_account()
    trade_log = api.get_trade()
    api.wait_update(deadline=time.time() + 30)

    # ---- 在 web_gui 面板上画技术指标（附加列写进 klines，web_gui 自动画在主图）----
    gui_serials = []          # [(K线序列, 均线周期)]，主循环里每根新K线重算以向右延伸
    if web_gui:
        try:
            _paint_ma(klines, list(ma_periods))
            gui_serials.append((klines, list(ma_periods)))
            print(f"  [图表] 已添加均线 {'/'.join('MA' + str(p) for p in ma_periods)}", flush=True)
        except Exception as e:
            print(f"  [!] 均线绑定失败（不影响策略运行）: {e}", flush=True)

    cur_sym = quote.underlying_symbol
    tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
    want = api.get_position(cur_sym).pos
    seen = set(trade_log.keys())
    # 修 BUG（2026-09-16）：原实现漏了 stop_st 初始化，只要跑过一根 K 线就 NameError
    stop_st = {"active": False, "entry": 0.0, "entry_atr": 0.0,
               "ext_high": 0.0, "bars": 0, "side": 0}
    pend_st = {"bars": 0}   # 「金叉后等回踩 MA10」等待状态

    DIR_CN = {"BUY": "多", "SELL": "空"}
    OFF_CN = {"OPEN": "开仓", "CLOSE": "平仓", "CLOSETODAY": "平今"}

    print("=" * 60)
    print(f" 模拟盘启动：{name}  {main_code}")
    print(f" 当前主力合约：{cur_sym}")
    print(f" 账户可用资金：{account.available:,.2f} 元")
    print(f" 下单开关：{'开（会真发单到模拟账户）' if allow_trade else '关（只打印信号不下单）'}")
    print(f" 参数：均线 MA{ma_periods[0]}/{ma_periods[1]}/"
          f"{ma_periods[2]}/{ma_periods[3]} / MACD({macd[0]},{macd[1]},{macd[2]}) / {volume} 手")
    print("=" * 60)
    if web_gui:
        # 天勤 web_gui 每次启动分配随机端口，从内部获取真实地址
        gui_url = _get_web_gui_url(api)
        if gui_url:
            print(f" 浏览器监控面板 -> {gui_url}")
            try:
                import platform
                if platform.system() == "Windows":
                    os.startfile(gui_url)
                else:
                    webbrowser.open(gui_url)
            except Exception:
                pass
        else:
            print("   (web_gui 面板端口获取失败，查看天勤启动日志中的端口号)")
        print("   （可看实时K线、均线、买卖点、持仓、资金曲线；关掉窗口程序仍运行）")
    if not allow_trade:
        print("  [!] 当前只是观察模式：信号会打印，但不会真正下单")
    print("  盘中盯盘看这里：每笔真实成交会标 *，并定时刷新盘面快照\n")

    last_bar_dt = klines.iloc[-1]["datetime"]
    last_heart = datetime.now()
    # 盘面心跳的「状态指纹」（2026-09-21）：现价/持仓/目标 与上次打印时完全一样
    # 就不再打印，休市期间不再重复刷屏。指纹一变立刻恢复。

    def mas_now():
        close = klines.close
        return [float(close.rolling(p).mean().iloc[-1]) for p in ma_periods]

    while True:
        api.wait_update(deadline=time.time() + 30)

        if allow_trade and api.is_changing(trade_log):
            for tid in trade_log:
                if tid in seen:
                    continue
                seen.add(tid)
                t = trade_log[tid]
                d = DIR_CN.get(t.direction, t.direction)
                o = OFF_CN.get(t.offset, t.offset)
                print(f"[{datetime.now():%H:%M:%S}] * 成交 {d}{o} {int(t.volume)}手 "
                      f"@ {float(t.price):.2f}  合约 {t.symbol}")
                if allow_trade:
                    print(f"            （已真实发送到模拟账户，可在快期3核对）")

        if api.is_changing(quote, "underlying_symbol"):
            new_sym = quote.underlying_symbol
            if new_sym != cur_sym:
                print(f"[{datetime.now():%H:%M:%S}] 主力换月 {cur_sym} -> {new_sym}")
                tp.set_target_volume(0)
                while api.get_position(cur_sym).pos != 0:
                    api.wait_update(deadline=time.time() + 30)
                cur_sym = new_sym
                tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
                if allow_trade:
                    tp.set_target_volume(want)

        if api.is_changing(klines.iloc[-1], "datetime") and \
           klines.iloc[-1]["datetime"] != last_bar_dt:
            last_bar_dt = klines.iloc[-1]["datetime"]

            for _gk, _gps in gui_serials:      # 重算均线，让面板上的线随新K线延伸
                _paint_ma(_gk, _gps)

            close = klines.close
            vals = [float(close.rolling(p).mean().iloc[-2]) for p in ma_periods]
            last_close = float(close.iloc[-2])

            pos = api.get_position(cur_sym)
            net_pos = pos.pos_long_his + pos.pos_long_today - pos.pos_short_his - pos.pos_short_today
            if stop_pct > 0 and net_pos != 0:
                avg = float(pos.open_price_long) if net_pos > 0 else float(pos.open_price_short)
                if (net_pos > 0 and last_close < avg * (1 - stop_pct)) or \
                   (net_pos < 0 and last_close > avg * (1 + stop_pct)):
                    want = 0
                    if allow_trade:
                        tp.set_target_volume(0)
                    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 触及 "
                          f"{stop_pct*100:.0f}% 止损，平仓")
                    continue

            # ---- ATR 止损 ----
            atr_v = 0.0
            if atr_stop_mult > 0 or entry_ext_atr > 0 or stop_atr > 0:
                try:
                    atr_v = float(_atr(klines.high, klines.low, klines.close,
                                       atr_period).iloc[-2])
                except Exception:
                    atr_v = 0.0
            last_high = float(klines.high.iloc[-2])
            last_low = float(klines.low.iloc[-2])
            if stop_st["active"] and stop_st["entry_atr"] <= 0 and atr_v > 0:
                stop_st["entry_atr"] = atr_v     # 启动时已有持仓：用当前 ATR 补齐基准
            if stop_st["active"] and want != 0:
                if stop_atr > 0:
                    # 固定 ATR 止损线（开仓时定死、不移动），多空对称
                    _hit_side = 1 if stop_st["side"] > 0 else -1
                    if _hit_side > 0:
                        stop_line = stop_st["entry"] - stop_atr * stop_st["entry_atr"]
                        _hit = last_low <= stop_line
                    else:
                        stop_line = stop_st["entry"] + stop_atr * stop_st["entry_atr"]
                        _hit = last_high >= stop_line
                    if _hit:
                        want = 0
                        stop_st["active"] = False
                        if allow_trade:
                            tp.set_target_volume(0)
                        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {stop_atr:g}ATR固定止损 -> "
                              f"{'平多' if _hit_side > 0 else '平空'}"
                              f" | 触发价 {stop_line:.2f} | 开仓 {stop_st['entry']:.2f}"
                              f" 开仓ATR {stop_st['entry_atr']:.2f}")
                        continue
                elif want > 0 and atr_stop_mult > 0:
                    stop_st["bars"] += 1
                    stop_st["ext_high"] = max(stop_st["ext_high"], last_high)
                    stop_line = _align_trail_stop_long(
                        stop_st["entry"], stop_st["entry_atr"], stop_st["ext_high"],
                        atr_v, last_close, atr_stop_mult, breakeven_atr, lock_atr,
                        trail_atr_mult, min_stop_atr, use_ma_stop, vals[3], 0.0)
                    if stop_line is not None and last_low <= stop_line:
                        want = 0
                        stop_st["active"] = False
                        if allow_trade:
                            tp.set_target_volume(0)
                        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ATR动态止损 -> 平多"
                              f" | 触发价 {stop_line:.2f} | 开仓 {stop_st['entry']:.2f}"
                              f" 最高 {stop_st['ext_high']:.2f} ATR {atr_v:.2f}")
                        continue

            dif_s, dea_s = _macd(close, *macd)
            dif_v = float(dif_s.iloc[-2]); dea_v = float(dea_s.iloc[-2])
            pdif = float(dif_s.iloc[-3]); pdea = float(dea_s.iloc[-3])

            adx_v = 999.0
            if adx_filter:
                try:
                    adx_s, _pdi, _mdi = _adx(klines.high, klines.low,
                                             klines.close, adx_period)
                    adx_v = float(adx_s.iloc[-2])
                except Exception:
                    adx_v = 999.0

            bull_now = (vals[0] > vals[1] > vals[2] > vals[3])
            in_pos = 1 if want > 0 else (-1 if want < 0 else 0)
            sig = signal_fn(*vals, last_close, dif_v, dea_v, pdif, pdea, in_pos,
                            atr=atr_v, adx=adx_v,
                            adx_min=(adx_min if adx_filter else 0.0))

            do_open = False
            do_close = False
            open_side = 0          # +1 开多 / -1 开空
            open_tag = ""
            close_tag = ""
            close_dir = "SELL"     # 平仓成交方向：平多=SELL / 平空=BUY
            now_s = f"[{datetime.now():%Y-%m-%d %H:%M:%S}]"
            if want == 0:
                if pend_st["bars"] > 0:
                    if (not bull_now) or dif_v <= 0:
                        print(f"{now_s} 等回踩期间多头排列破坏/掉回水下，取消本次开仓")
                        pend_st["bars"] = 0
                    else:
                        pend_st["bars"] -= 1
                        if last_close <= vals[1] + pullback_tol_atr * atr_v:
                            do_open = True
                            open_side = 1
                            open_tag = "金叉后回踩MA10 -> 开多"
                            pend_st["bars"] = 0
                        elif pend_st["bars"] == 0:
                            print(f"{now_s} 等回踩超时（{pullback_wait_bars} 根），放弃本次开仓")
                elif sig == 1:
                    if entry_ext_atr > 0 and atr_v > 0 and \
                       (last_close - vals[1]) > entry_ext_atr * atr_v:
                        pend_st["bars"] = pullback_wait_bars
                        print(f"{now_s} 金叉但已高于 MA10 {last_close - vals[1]:.1f} 点"
                              f"（>{entry_ext_atr:g}ATR），不追高 -> 等回踩，最多等 "
                              f"{pullback_wait_bars} 根")
                    else:
                        do_open = True
                        open_side = 1
                        open_tag = "金叉+多头排列+水上 -> 开多"
                elif sig == -1:
                    do_open = True
                    open_side = -1
                    open_tag = "死叉+空头排列+水下 -> 开空"
            elif want > 0:
                # 持多：0=平多；-1=平多且反手开空（多头侧信号函数永不返回 -1，行为不变）
                if sig == 0:
                    do_close = True
                    close_tag = "MACD 死叉 -> 平多"
                    close_dir = "SELL"
                elif sig == -1:
                    do_close = True
                    do_open = True
                    open_side = -1
                    open_tag = "死叉+空头排列+水下 -> 反手开空"
            else:
                # 持空（镜像）：0=平空；+1=平空且反手开多
                if sig == 0:
                    do_close = True
                    close_tag = "MACD 金叉 -> 平空"
                    close_dir = "BUY"
                elif sig == 1:
                    do_close = True
                    do_open = True
                    open_side = 1
                    open_tag = "金叉+多头排列+水上 -> 反手开多"

            if do_open or do_close:
                want = volume * open_side if do_open else 0
                tag = open_tag if do_open else close_tag
                print(f"{now_s} {tag} {volume}手"
                      f" | 触发价 {last_close:.2f}"
                      f" | MA5={vals[0]:.2f} MA10={vals[1]:.2f} "
                      f"MA20={vals[2]:.2f} MA40={vals[3]:.2f}"
                      f" | DIF={dif_v:.2f} DEA={dea_v:.2f} ADX={adx_v:.1f}")
                if web_gui:
                    # 买卖点标记按真实成交方向：开多/平空=买，开空/平多=卖
                    _draw_signal(api, klines, is_buy=(open_side > 0) if do_open
                                 else (close_dir == "BUY"))
                if want > 0 and stop_atr > 0:
                    # 固定 ATR 止损线：开仓时定死，多空对称，不移动
                    stop_st.update({"active": True, "entry": last_close,
                                    "entry_atr": atr_v, "side": open_side,
                                    "ext_high": last_high, "ext_low": last_low,
                                    "bars": 0})
                    _sl = (last_close - stop_atr * atr_v if open_side > 0
                           else last_close + stop_atr * atr_v)
                    print(f"            初始止损 {_sl:.2f}"
                          f"（{stop_atr:g}ATR = {stop_atr * atr_v:.2f} 点，固定不动）")
                elif want > 0 and atr_stop_mult > 0:
                    stop_st.update({"active": True, "entry": last_close,
                                    "entry_atr": atr_v, "side": 1,
                                    "ext_high": last_high, "bars": 0})
                elif want <= 0:
                    stop_st["active"] = False
                if allow_trade:
                    tp.set_target_volume(want)
                    print(f"            > 已下达目标仓位 {want} 手，等待撮合...")
                else:
                    print(f"            （观察模式，未下单）")

        # ---- 4) 盘面心跳：定时刷新快照，确认程序没卡死 ----
        #   ★ 2026-09-21：每 heartbeat 周期无条件打印一行（静默守卫已去除），便于肉眼判断中断。
        if (datetime.now() - last_heart).total_seconds() >= heartbeat:
            last_heart = datetime.now()
            mv = mas_now()
            pos = api.get_position(cur_sym).pos
            _px_h = float(quote.last_price)
            print(f"[{datetime.now():%H:%M:%S}] 盘面 现价 {_px_h:.2f} "
                  f"| MA{ma_periods[0]}={mv[0]:.2f} "
                      f"MA{ma_periods[-1]}={mv[-1]:.2f} "
                      f"| 持仓 {int(pos)}手 目标 {want} "
                      f"| 可用 {account.available:,.0f}")


# ===================================================================
# 05 摆动线（麦语言「移动止损止盈线」2022 自编指标 1:1 转化）
# ===================================================================
def swing_decision(state, close, hh2, ll2):
    """摆动线状态机（纯函数，便于单测）——与麦语言 K1/K2 逐句对应。

    入参：
        state : 当前状态，+1=多头 / -1=空头 / 0=未定（麦语言 K2 用 -3/1 记号，此处映射 ±1）
        close : 本根已收盘 K 线收盘价
        hh2   : 最近一次确认的摆动高点（连续 2 根更低高点确认；None=尚未出现）
        ll2   : 最近一次确认的摆动低点（连续 2 根更高低点确认；None=尚未出现）

    规则（与麦语言同优先级：先判上穿、再判下穿）：
        close > hh2 -> 状态翻多；close < ll2 -> 状态翻空；否则维持原状态。
    返回 (new_state, action, line)：
        action: 'open_long' / 'open_short'（状态翻转）/ 'hold'（含观望与续持）
        line  : 当前移动止盈线（多头=ll2，空头=hh2，未定=None）——仅展示/记录用，
                出场与翻手是同一事件（结构反向穿越），不另设止损。
    """
    new_state = state
    if hh2 is not None and close > hh2:
        new_state = 1
    elif ll2 is not None and close < ll2:
        new_state = -1
    action = 'hold'
    if new_state != state:
        action = 'open_long' if new_state == 1 else 'open_short'
    line = None
    if new_state == 1:
        line = ll2
    elif new_state == -1:
        line = hh2
    return new_state, action, line


def _swing_update_levels(klines):
    """用最近 3 根已收盘 K 线确认摆动点（麦语言 HH1/LL1 的等价实现）。

    麦语言在 bar t 判：H[t]<H[t-1]<H[t-2] -> 摆动高点=H[t-2]（滞后 2 根确认，防重绘）。
    这里 iloc[-2] 为最后一根已收盘 K 线（t），iloc[-3]=t-1，iloc[-4]=t-2。
    返回 (new_hh2, new_ll2)：本根新确认的摆动高/低点，没有则为 None。
    """
    try:
        h2 = float(klines.high.iloc[-2]); h3 = float(klines.high.iloc[-3])
        h4 = float(klines.high.iloc[-4])
        l2 = float(klines.low.iloc[-2]);  l3 = float(klines.low.iloc[-3])
        l4 = float(klines.low.iloc[-4])
    except Exception:
        return None, None
    if h2 != h2 or h3 != h3 or h4 != h4 or l2 != l2 or l3 != l3 or l4 != l4:
        return None, None          # 预热不足（NaN）
    new_hh2 = h4 if (h2 < h3 < h4) else None   # 连续 2 根更低高点 -> 前第 2 根高点
    new_ll2 = l4 if (l2 > l3 > l4) else None   # 连续 2 根更高低点 -> 前第 2 根低点
    return new_hh2, new_ll2


def run_backtest_swing(symbol_key, volume=1, start=None, end=None,
                       eval_start=None, init_balance=200000,
                       kline_sec=30 * 60, strategy_name="摆动线",
                       open_html=True, sample_tag="",
                       slip_ticks=1, commission_per_hand=None,
                       risk_pct=0.0, risk_hand_min=1, risk_hand_max=8,
                       atr_period=14, stop_atr=0.0):
    """摆动线策略 v0 回测 —— 麦语言「移动止损止盈线」1:1 转化（零参数、零过滤裸版）。

    交易规则（决策见 swing_decision / _swing_update_levels；与源指标逐句对应）：
      1) 摆动高点：连续 2 根更低高点 -> 前第 2 根高点确认；摆动低点镜像（滞后 2 根，防重绘）
      2) 状态机：收盘 > 最近确认摆动高点 -> 翻多；收盘 < 最近确认摆动低点 -> 翻空；
         其余维持原状态（未翻转前不交易）。同一根先判上穿再判下穿（与源码 IFELSE 优先级一致）
      3) 执行：信号在已收盘 K 线产生、下一根开盘成交 -> 无未来函数；状态翻转 = 平旧仓 + 反手
      4) 移动止盈线：多头=最近确认摆动低点、空头=最近确认摆动高点，只前进不后退
         （出场与翻手是同一事件，不另设止损）
      5) 手数：risk_pct=0 时固定 volume 手（v0 裸版）；risk_pct>0 时波动率预算
         （手数 = 权益×risk_pct% ÷ (2.0×ATR×乘数)，夹 [risk_hand_min, risk_hand_max]）。
         ⚠️ 分母的 2.0×ATR 只是「风险刻度」（与 01/03 同一把尺子，保证 1~8 手的量级一致），
         不是止损线 —— 摆动线的出场仍是结构反向穿越。
      6) 固定止损（v2，stop_atr>0 启用，默认 0=关闭）：开仓时按
         信号根收盘价 ∓ stop_atr×信号根ATR 定死止损线，此后不移动、不上移。
         多：最低价跌破止损线 -> 平多；空：最高价升破 -> 平空。
         ⚠️ 触发后【空仓等待下一次结构翻转】（方案A，2026-09-23 用户拍板）：
         状态机照常记忆，不反向开仓、不回补；若触发当根恰好发生结构翻转，
         视为新的翻转事件、允许按信号开新方向（止损只管出场，入场仍归结构）。
    成本口径与 01/03 同一套：TqSim 对价成交（价格层 1 跳）+ slip_ticks 费用层（默认 1 跳），
    换月后新合约重设费率（2026-09-22 修复的坑，这里从第一天就不犯）。
    eval_start：此前只预热不交易，但摆动点与状态机照常累积（保证评估起点状态正确）。
    落盘：回测结果/05_摆动线/<品种>/<品种>_摆动线_<周期>[_in|_out].html / .txt
    """
    name, main_code, mult, tick = config_symbols.get(symbol_key)
    user, pwd = config_account.check()
    _snap_code(strategy_name, name)   # 回测前存档当前代码，供 工具/回滚.py 还原
    # ---- 成本口径（与 run_backtest_boll 同一套：滑点折进 TqSim 手续费）----
    _cost_specified = commission_per_hand is not None
    _cfg_cost = float(getattr(config_symbols, "COST", {}).get(symbol_key, 0.0) or 0.0)
    slip_money = slip_ticks * mult * tick
    apply_cost = _cost_specified or slip_ticks > 0
    cost_per_hand = (commission_per_hand + slip_money) if _cost_specified else 0.0
    _cost_base = commission_per_hand if _cost_specified else 0.0

    sim = TqSim(init_balance)
    stat = None
    equity_curve = []
    kline_history = []
    trade_marks = []
    eval_ts = pd.Timestamp(eval_start) if eval_start else None
    try:
        api = TqApi(
            sim,
            backtest=TqBacktest(start_dt=start, end_dt=end),
            auth=TqAuth(user, pwd),
            web_gui=False,
        )
        klines = api.get_kline_serial(main_code, kline_sec, data_length=150)
        quote = api.get_quote(main_code)
        account = api.get_account()
        api.wait_update(deadline=time.time() + 30)

        cur_sym = quote.underlying_symbol
        tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
        if apply_cost:
            try:
                b = float(sim.get_commission(cur_sym))
                _cost_base = commission_per_hand if _cost_specified else (
                    b if b == b and b > 0 else _cfg_cost)
            except Exception:
                _cost_base = commission_per_hand if _cost_specified else _cfg_cost
            cost_per_hand = _cost_base + slip_money
            try:
                sim.set_commission(cur_sym, cost_per_hand)
                sim.set_commission(main_code, cost_per_hand)
            except Exception as e:
                print(f"  [!] 成本设置失败（不影响信号，但报告不含成本）: {e}")
        want = 0            # 目标仓位（正=多，负=空，0=空仓）
        state = 0           # 摆动线状态：+1 多 / -1 空 / 0 未定
        hh2 = None          # 最近确认摆动高点
        ll2 = None          # 最近确认摆动低点
        entry_px = 0.0      # v2 固定止损基准：开仓信号根收盘价
        entry_atr_s = 0.0   # v2 固定止损基准：开仓信号根 ATR
        print(f"\n>>> {name} {main_code} 摆动线回测 {start} ~ {end}")
        print(f"    参数：{kline_sec // 60}分钟K线 | 零参数裸版（无止损叠加/无过滤） | "
              f"滑点 {slip_ticks} 跳（费用层）")
        if risk_pct > 0:
            print(f"    仓位：波动率预算 {risk_pct:g}% "
                  f"-> {risk_hand_min}~{risk_hand_max} 手（风险刻度 2.0×ATR，开仓那根锁定）")
        if stop_atr > 0:
            print(f"    止损：固定 {stop_atr:g}×ATR（开仓定死不移动）；"
                  f"触发后空仓等下一次结构翻转（方案A）")
        if eval_ts is not None:
            print(f"   评估区间（实际交易）：{eval_start} 起；此前仅预热（状态机照常累积），不交易")

        while True:
            api.wait_update(deadline=time.time() + 30)

            # ---- 换月：先平旧仓，切新合约并重设费率（03 踩过的坑）----
            if api.is_changing(quote, "underlying_symbol"):
                new_sym = quote.underlying_symbol
                if new_sym != cur_sym:
                    print(f"    换月 {cur_sym} -> {new_sym}")
                    tp.set_target_volume(0)
                    while api.get_position(cur_sym).pos != 0:
                        api.wait_update(deadline=time.time() + 30)
                    cur_sym = new_sym
                    tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
                    tp.set_target_volume(want)
                    if apply_cost:
                        try:
                            sim.set_commission(cur_sym, cost_per_hand)
                        except Exception:
                            pass

            if api.is_changing(klines.iloc[-1], "datetime"):
                bar_dt = klines.iloc[-2]["datetime"]
                try:
                    equity_curve.append((_dt_str(bar_dt), float(account.balance)))
                except Exception:
                    pass

                # ---- 摆动点确认（用已收盘K线；预热不足时跳过本根）----
                new_hh, new_ll = _swing_update_levels(klines)
                if new_hh is None and new_ll is None and \
                   float(klines.close.iloc[-2]) != float(klines.close.iloc[-2]):
                    continue
                if new_hh is not None:
                    hh2 = new_hh
                if new_ll is not None:
                    ll2 = new_ll

                close = float(klines.close.iloc[-2])
                state, action, line = swing_decision(state, close, hh2, ll2)

                # 预热期：状态照常累积，但不交易、不记录
                if eval_ts is not None and pd.Timestamp(bar_dt) < eval_ts:
                    continue

                # ---- v2 固定止损（信号根=已收盘K线；触发 -> 目标0，状态机不动）----
                if stop_atr > 0 and want != 0 and entry_atr_s > 0:
                    last_low = float(klines.low.iloc[-2])
                    last_high = float(klines.high.iloc[-2])
                    if want > 0:
                        stop_line = entry_px - stop_atr * entry_atr_s
                        hit = last_low <= stop_line
                    else:
                        stop_line = entry_px + stop_atr * entry_atr_s
                        hit = last_high >= stop_line
                    if hit:
                        _dir_cn = "多" if want > 0 else "空"
                        print(f"    {_dt_str(bar_dt)} {stop_atr:g}×ATR固定止损 -> 平{_dir_cn}"
                              f" | 触发价 {stop_line:.1f} | 开仓基准 {entry_px:.1f}"
                              f" 开仓ATR {entry_atr_s:.2f}"
                              f" | 空仓等下一次结构翻转（方案A）")
                        trade_marks.append({"dt": _dt_str(bar_dt),
                                            "dir": "SELL" if want > 0 else "BUY",
                                            "price": stop_line,
                                            "reason": f"{stop_atr:g}×ATR固定止损(触发价{stop_line:.1f})"})
                        want = 0
                        tp.set_target_volume(0)
                        # 不 continue：本根若恰好结构翻转，视为新事件、允许开新方向

                try:
                    kline_history.append({
                        "dt": _dt_str(bar_dt),
                        "open": float(klines.open.iloc[-2]),
                        "high": float(klines.high.iloc[-2]),
                        "low": float(klines.low.iloc[-2]),
                        "close": close,
                        "volume": float(klines.volume.iloc[-2]),
                    })
                except Exception:
                    pass

                # ---- 执行：状态翻转才动仓（下一根开盘成交）----
                new_want = want
                tag = ""
                _hand = volume
                atr_v = 0.0
                if action in ('open_long', 'open_short') and (risk_pct > 0 or stop_atr > 0):
                    # 信号根已收盘那根的 ATR：risk_pct>0 用来算手数（风险刻度 2.0×ATR），
                    # stop_atr>0 用来定固定止损线（同一根锁定，同一把尺）。
                    try:
                        atr_v = float(_atr(klines.high, klines.low, klines.close,
                                           atr_period).iloc[-2])
                    except Exception:
                        atr_v = 0.0
                    if risk_pct > 0:
                        _hand = _risk_budget_hand(
                            init_balance, risk_pct, atr_v, 2.0,
                            mult, risk_hand_min, risk_hand_max)
                if action == 'open_long':
                    new_want = _hand
                    tag = ("收盘上穿摆动高点 -> 平空反手开多" if want < 0
                           else "收盘上穿摆动高点 -> 开多")
                elif action == 'open_short':
                    new_want = -_hand
                    tag = ("收盘跌破摆动低点 -> 平多反手开空" if want > 0
                           else "收盘跌破摆动低点 -> 开空")
                if new_want != want:
                    want = new_want
                    if want != 0:
                        # 开新仓/反手：锁定止损基准（信号根收盘价 + 信号根ATR）
                        entry_px = close
                        entry_atr_s = atr_v if atr_v > 0 else 0.0
                    _btxt = ""
                    if risk_pct > 0:
                        _btxt = (f" | 波动率预算 {risk_pct:g}%×{init_balance:g}"
                                 f" ÷ (2.0×ATR{atr_v:.2f}×{mult:g}) -> {abs(want)} 手")
                    print(f"    {_dt_str(bar_dt)} {tag} {abs(want)}手 | 收盘 {close:.1f}"
                          f"{_btxt}"
                          f" | 摆动高点 {hh2 if hh2 else float('nan'):.1f}"
                          f" 摆动低点 {ll2 if ll2 else float('nan'):.1f}"
                          f" | 止盈线 {line if line else float('nan'):.1f}")
                    trade_marks.append({"dt": _dt_str(bar_dt),
                                        "dir": "BUY" if want > 0 else "SELL",
                                        "price": close, "reason": tag})
                    tp.set_target_volume(want)

    except BacktestFinished:
        stat = sim.tqsdk_stat
        sdict = _stat_to_dict(stat)
        trades = _collect_trades(api)
        _print_stat(name, sdict)
        desc = "摆动点结构（HH2/LL2，2根K线确认）双向反手 + 结构移动止盈线"
        pos_txt = (f"仓位：波动率预算 {risk_pct:g}% —— 手数=权益×{risk_pct:g}% ÷ "
                   f"(2.0×ATR×乘数) 夹 {risk_hand_min}~{risk_hand_max} 手"
                   f"（2.0×ATR 仅是风险刻度，与 01/03 同尺，非止损线）"
                   if risk_pct > 0 else f"仓位：固定 {volume} 手（裸版）")
        stop_txt = (f" | 止损：固定 {stop_atr:g}×ATR（开仓基准=信号根收盘价，定死不移动），"
                    f"触发后空仓等下一次结构翻转（方案A，不反手不回补）"
                    if stop_atr > 0 else "")
        extra = (f"周期：{kline_sec // 60}分钟K线 | 信号评估区间：{eval_start} ~ {end}"
                 f"（{start} 起仅预热不交易，状态机照常累积）| "
                 f"开多：收盘上穿最近确认摆动高点 | "
                 f"开空：收盘跌破最近确认摆动低点 | "
                 f"出场：结构反向穿越即反手（同一事件），移动止盈线=持仓方向最近确认摆动点"
                 f"（只前进不后退）| 摆动点需 2 根 K 线确认（滞后 2 根，防重绘）| "
                 f"零参数、零过滤 | {pos_txt}{stop_txt} | {desc}")
        signal_events = [{"dt": _m["dt"], "dir": _m["dir"]} for _m in trade_marks]
        html_path, txt_path = _save_backtest_report(
            strategy_name, symbol_key, name, sdict, equity_curve, trades,
            start, end, desc, volume, kline_sec, init_balance, main_code,
            extra_note=extra, kline_history=kline_history, signal_events=signal_events,
            mult=mult, trade_marks=trade_marks, sample_tag=sample_tag)
        print(f"\n  回测记录已保存：{txt_path}")
        print(f"  回测报告已生成：{html_path}")
        if open_html:
            try:
                os.startfile(html_path)
            except Exception:
                pass
        api.close()
    return stat


def run_live_swing(symbol_key, volume=1, kline_sec=30 * 60, allow_trade=True,
                   web_gui=True, heartbeat=300,
                   strategy_name="摆动线",
                   risk_pct=0.0, risk_hand_min=1, risk_hand_max=8,
                   atr_period=14, stop_atr=0.0):
    """摆动线模拟盘 —— 与 run_backtest_swing 同口径（麦语言指标 1:1 转化）。

    盯盘能力与 run_live_align 一致（*成交 / 盘面心跳 / web_gui）。
    启动时若已有持仓：状态按持仓方向初始化（多=+1 / 空=-1），止盈线用首个确认摆动点补齐。
    仓位：risk_pct>0 时波动率预算（手数=权益×risk%÷(2.0×ATR×乘数) 夹[min,max]，
    用账户实时权益，与 03 live 同口径）；risk_pct=0 时固定 volume 手。
    止损（stop_atr>0）：固定 stop_atr×ATR（开仓信号根基准，定死不移动），
    触发后空仓等下一次结构翻转（方案A）；重启后基准丢失 -> 止损不生效（与旧live一致）。
    """
    name, main_code, mult, tick = config_symbols.get(symbol_key)
    user, pwd = config_account.check()
    sys.argv[0] = name

    api = TqApi(TqKq(), auth=TqAuth(user, pwd), web_gui=web_gui)
    klines = api.get_kline_serial(main_code, kline_sec, data_length=150)
    quote = api.get_quote(main_code)
    account = api.get_account()
    trade_log = api.get_trade()
    api.wait_update(deadline=time.time() + 30)

    cur_sym = quote.underlying_symbol
    tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
    pos0 = api.get_position(cur_sym).pos
    want = volume if pos0 > 0 else (-volume if pos0 < 0 else 0)
    state = 1 if pos0 > 0 else (-1 if pos0 < 0 else 0)
    hh2 = None
    ll2 = None
    entry_px = 0.0      # v2 固定止损基准（开仓信号根收盘价）；重启后不补挂（见 docstring）
    entry_atr_s = 0.0   # v2 固定止损基准（开仓信号根 ATR）
    seen = set(trade_log.keys())

    DIR_CN = {"BUY": "多", "SELL": "空"}
    OFF_CN = {"OPEN": "开仓", "CLOSE": "平仓", "CLOSETODAY": "平今"}

    print("=" * 60)
    print(f" 模拟盘启动：{name}  {main_code}（05 摆动线 · 零参数裸版）")
    print(f" 当前主力合约：{cur_sym}")
    print(f" 账户可用资金：{account.available:,.2f} 元")
    print(f" 下单开关：{'开（会真发单到模拟账户）' if allow_trade else '关（只打印信号不下单）'}")
    print(f" 参数：{kline_sec // 60}分钟K线 | 启动状态 "
          f"{'多头' if state == 1 else '空头' if state == -1 else '未定（等首次结构穿越）'}"
          f" | " + (f"波动率预算 {risk_pct:g}% -> {risk_hand_min}~{risk_hand_max} 手"
                    if risk_pct > 0 else f"{volume} 手")
          + (f" | 固定止损 {stop_atr:g}×ATR（触发后空仓等翻转）" if stop_atr > 0 else ""))
    print("=" * 60)
    if web_gui:
        gui_url = _get_web_gui_url(api)
        if gui_url:
            print(f" 浏览器监控面板 -> {gui_url}")
            try:
                import platform
                if platform.system() == "Windows":
                    os.startfile(gui_url)
                else:
                    webbrowser.open(gui_url)
            except Exception:
                pass
        else:
            print("   (web_gui 面板端口获取失败，查看天勤启动日志中的端口号)")
    if not allow_trade:
        print("  [!] 当前只是观察模式：信号会打印，但不会真正下单")
    print("  盘中盯盘看这里：每笔真实成交会标 *，并定时刷新盘面快照\n")

    last_bar_dt = klines.iloc[-1]["datetime"]
    last_heart = datetime.now()

    def mas_now():
        c = klines.close
        return float(c.iloc[-1]), (hh2 if hh2 else float('nan')), \
               (ll2 if ll2 else float('nan'))

    while True:
        api.wait_update(deadline=time.time() + 30)

        if allow_trade and api.is_changing(trade_log):
            for tid in trade_log:
                if tid in seen:
                    continue
                seen.add(tid)
                t = trade_log[tid]
                d = DIR_CN.get(t.direction, t.direction)
                o = OFF_CN.get(t.offset, t.offset)
                print(f"[{datetime.now():%H:%M:%S}] * 成交 {d}{o} {int(t.volume)}手 "
                      f"@ {float(t.price):.2f}  合约 {t.symbol}")

        if api.is_changing(quote, "underlying_symbol"):
            new_sym = quote.underlying_symbol
            if new_sym != cur_sym:
                print(f"[{datetime.now():%H:%M:%S}] 主力换月 {cur_sym} -> {new_sym}")
                tp.set_target_volume(0)
                while api.get_position(cur_sym).pos != 0:
                    api.wait_update(deadline=time.time() + 30)
                cur_sym = new_sym
                tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
                if allow_trade:
                    tp.set_target_volume(want)

        if api.is_changing(klines.iloc[-1], "datetime") and \
           klines.iloc[-1]["datetime"] != last_bar_dt:
            last_bar_dt = klines.iloc[-1]["datetime"]

            new_hh, new_ll = _swing_update_levels(klines)
            if new_hh is not None:
                hh2 = new_hh
            if new_ll is not None:
                ll2 = new_ll

            close = float(klines.close.iloc[-2])
            if close != close:      # 预热不足
                continue
            now_s = f"[{datetime.now():%Y-%m-%d %H:%M:%S}]"

            # ---- v2 固定止损（信号根=已收盘K线；触发 -> 目标0，状态机不动）----
            if stop_atr > 0 and want != 0 and entry_atr_s > 0:
                last_low = float(klines.low.iloc[-2])
                last_high = float(klines.high.iloc[-2])
                if want > 0:
                    stop_line = entry_px - stop_atr * entry_atr_s
                    hit = last_low <= stop_line
                else:
                    stop_line = entry_px + stop_atr * entry_atr_s
                    hit = last_high >= stop_line
                if hit:
                    _dir_cn = "多" if want > 0 else "空"
                    _was_long = want > 0
                    print(f"{now_s} {stop_atr:g}×ATR固定止损 -> 平{_dir_cn}"
                          f" | 触发价 {stop_line:.2f} | 开仓基准 {entry_px:.2f}"
                          f" 开仓ATR {entry_atr_s:.2f}"
                          f" | 空仓等下一次结构翻转（方案A）")
                    want = 0
                    if web_gui:
                        _draw_signal(api, klines, is_buy=not _was_long)
                    if allow_trade:
                        tp.set_target_volume(0)
                        print(f"            > 已下达目标仓位 0 手，等待撮合...")
                    else:
                        print(f"            （观察模式，未下单）")

            new_state, action, line = swing_decision(state, close, hh2, ll2)
            _hand = volume
            atr_v = 0.0
            if action in ('open_long', 'open_short') and (risk_pct > 0 or stop_atr > 0):
                # 信号根已收盘那根的 ATR：risk_pct>0 算手数（实时权益，与 03 同口径）；
                # stop_atr>0 定固定止损线（同一根锁定）。
                try:
                    atr_v = float(_atr(klines.high, klines.low, klines.close,
                                       atr_period).iloc[-2])
                except Exception:
                    atr_v = 0.0
                if risk_pct > 0:
                    _hand = _risk_budget_hand(
                        float(account.balance), risk_pct, atr_v, 2.0,
                        mult, risk_hand_min, risk_hand_max)
            if action == 'open_long':
                want = _hand
                tag = ("收盘上穿摆动高点 -> 平空反手开多" if state < 0
                       else "收盘上穿摆动高点 -> 开多")
                state = new_state
                entry_px, entry_atr_s = close, (atr_v if atr_v > 0 else 0.0)
                print(f"{now_s} {tag} {want}手 | 收盘 {close:.1f}"
                      + (f" | 波动率预算 {risk_pct:g}%×权益 {float(account.balance):,.0f}"
                         f" ÷ (2.0×ATR{atr_v:.2f}×{mult:g})" if risk_pct > 0 else "")
                      + f" | 摆动高点 {hh2 if hh2 else float('nan'):.1f}"
                        f" 摆动低点 {ll2 if ll2 else float('nan'):.1f}"
                        f" | 止盈线 {line if line else float('nan'):.1f}")
                if web_gui:
                    _draw_signal(api, klines, is_buy=True)
                if allow_trade:
                    tp.set_target_volume(want)
                    print(f"            > 已下达目标仓位 {want} 手，等待撮合...")
                else:
                    print(f"            （观察模式，未下单）")
            elif action == 'open_short':
                want = -_hand
                tag = ("收盘跌破摆动低点 -> 平多反手开空" if state > 0
                       else "收盘跌破摆动低点 -> 开空")
                state = new_state
                entry_px, entry_atr_s = close, (atr_v if atr_v > 0 else 0.0)
                print(f"{now_s} {tag} {abs(want)}手 | 收盘 {close:.1f}"
                      + (f" | 波动率预算 {risk_pct:g}%×权益 {float(account.balance):,.0f}"
                         f" ÷ (2.0×ATR{atr_v:.2f}×{mult:g})" if risk_pct > 0 else "")
                      + f" | 摆动高点 {hh2 if hh2 else float('nan'):.1f}"
                        f" 摆动低点 {ll2 if ll2 else float('nan'):.1f}"
                        f" | 止盈线 {line if line else float('nan'):.1f}")
                if web_gui:
                    _draw_signal(api, klines, is_buy=False)
                if allow_trade:
                    tp.set_target_volume(want)
                    print(f"            > 已下达目标仓位 {want} 手，等待撮合...")
                else:
                    print(f"            （观察模式，未下单）")

        # ---- 盘面心跳：无条件打印，肉眼判中断 ----
        if (datetime.now() - last_heart).total_seconds() >= heartbeat:
            last_heart = datetime.now()
            px, _h, _l = mas_now()
            pos = api.get_position(cur_sym).pos
            print(f"[{datetime.now():%H:%M:%S}] 盘面 现价 {px:.2f} "
                  f"| 状态 {'多头' if state == 1 else '空头' if state == -1 else '未定'} "
                  f"| 止盈线 {_h if state == -1 else _l:.1f} "
                  f"| 持仓 {int(pos)}手 目标 {want} "
                  f"| 可用 {account.available:,.0f}")


# ===================================================================
# 均线排列升级版（状态机：多条件开仓 + 动态 ATR 止损 + 多路平仓）
# ===================================================================
def _atr(high, low, close, period=14):
    """真实波幅 ATR(period)，返回与 close 同索引的 Series（末位为当前值）。"""
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _adx(high, low, close, period=14):
    """Wilder 平滑的 ADX / +DI / -DI（趋势强度与方向）。返回三个 Series。"""
    high = high.astype(float); low = low.astype(float); close = close.astype(float)
    prev_close = close.shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    up = high - prev_high
    dn = prev_low - low
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr.replace(0, np.nan)).fillna(0)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr.replace(0, np.nan)).fillna(0)
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
    adx = dx.rolling(period).mean()
    return adx, plus_di, minus_di


_DT_FMT = "%Y-%m-%d %H:%M:%S"


def _dt_str(bar_dt):
    """把各种形态的时间统一成 'YYYY-MM-DD HH:MM:SS'。

    兼容四类输入：
      · pandas.Timestamp / datetime / numpy.datetime64
      · K 线纳秒整数（约 1.77e18）      —— 回测主循环取到的时间
      · 成交记录秒级浮点（约 1.77e9）   —— tqsdk Trade.trade_date_time
      · 上述数值的字符串形式（含科学计数法，如 '1.7731e+18' / '1773100800.0'）

    坑（2026-09-16 修）：旧实现直接 pd.Timestamp(x)。当 x 是「秒级」数值字符串
    时 pandas 会按纳秒解释 -> 全部退化成 1970-01-01（逐笔明细时间列整列错）。
    这里先按数量级判定单位再换算，避免单位误判。
    """
    if bar_dt is None or bar_dt == "":
        return ""
    num = None
    if isinstance(bar_dt, bool):
        return str(bar_dt)
    if isinstance(bar_dt, (int, float)):
        num = float(bar_dt)
    elif isinstance(bar_dt, str):
        s = bar_dt.strip()
        if not s:
            return ""
        try:
            num = float(s)
        except ValueError:
            try:
                return pd.Timestamp(s).strftime(_DT_FMT)
            except Exception:
                return s
    if num is not None:
        a = abs(num)
        try:
            if a >= 1e16:        # 纳秒
                return pd.Timestamp(num / 1e9, unit="s").strftime(_DT_FMT)
            if a >= 1e13:        # 微秒
                return pd.Timestamp(num / 1e6, unit="s").strftime(_DT_FMT)
            if a >= 1e10:        # 毫秒
                return pd.Timestamp(num / 1e3, unit="s").strftime(_DT_FMT)
            if a >= 1e8:         # 秒
                return pd.Timestamp(num, unit="s").strftime(_DT_FMT)
        except Exception:
            pass
    try:
        return pd.Timestamp(bar_dt).strftime(_DT_FMT)
    except Exception:
        return str(bar_dt)


def _in_entry_window(ts):
    """时间过滤：避开开盘/收盘前后 5 分钟（流动性差、滑点大）。返回 True=可交易。"""
    try:
        t = pd.Timestamp(ts).time()
    except Exception:
        return True
    cur = t.hour * 60 + t.minute

    def m(h, mi):
        return h * 60 + mi
    # 各时段开盘后 5 分钟禁开仓
    for o in (m(9, 0), m(10, 30), m(13, 30), m(21, 0)):
        if o <= cur < o + 5:
            return False
    # 各时段收盘前 5 分钟禁开仓
    for c in (m(10, 15), m(11, 30), m(15, 0), m(23, 0)):
        if c - 5 <= cur < c:
            return False
    return True

def _in_no_open_window(ts, minutes):
    """返回 True 表示该根 K 线处于某交易时段『开盘后 minutes 分钟内』，应禁止新开仓。

    ts 为 K 线 datetime（纳秒 epoch，UTC）。天勤行情时间按 UTC 存储，
    _dt_str 解析后比北京时间慢 8 小时（2026-09-16 实测确认），
    故这里 +8h 转成北京时间再判时段。
    """
    try:
        bj = pd.Timestamp(float(ts) / 1e9, unit="s") + pd.Timedelta(hours=8)
    except Exception:
        return False
    cur = bj.hour * 60 + bj.minute
    for o in (9 * 60, 21 * 60):   # 日盘 09:00、夜盘 21:00 开盘
        if o <= cur < o + minutes:
            return True
    return False


def _pro_bar_decision(snap, st, P):
    """升级版单根 K 线决策（纯函数，便于单测）。

    改进点（相对 v1）：① 双向（多/空）；② ADX 趋势强度过滤（震荡市不交易）；
    ③ 宽止损 + 慢追踪（初始 2.5ATR、保本 2ATR、锁定 3ATR、峰值回撤 2.5ATR 追踪、
       可选跌破 MA40 结构止损）；④ 回调进场（多头排列成立后回踩 MA10 再买，不在突破顶追高）。

    入参 snap：ma5/ma10/ma20/ma40、ma5_prev/ma10_prev、open/close/high/low、
        vol/vol_mean5、atr/atr_pct、dif/dea/pdif/pdea、trend_ma_bull、in_window、adx
    入参 st：state 'flat'/'long'/'short'；持仓时含 entry_price/entry_atr/
        ext_high/ext_low/bars_held/stop_price
    入参 P（策略参数）：vol_filter/atr_filter/trend_filter/time_filter/atr_min_pct/
        atr_stop_mult/death_below_zero/time_exit_bars/slip_price/
        adx_filter/adx_min/allow_long/allow_short/entry_mode/
        use_ma_stop/min_stop_atr/breakeven_atr/lock_atr/trail_atr_mult/ma_exit_ma
    返回 (action, stop, reason, entry, side)：
        action 'open'/'close'/'hold'；side 'long'/'short'/None；
        stop 为本根应采用的止损价；entry 为开仓基准价（含滑点）
    """
    # ---------------- 已持仓：先更新动态止损，再判平仓 ----------------
    if st["state"] in ("long", "short"):
        pos = st["state"]
        entry = st["entry_price"]
        entry_atr = st["entry_atr"]
        ext_high = st["ext_high"]
        ext_low = st["ext_low"]
        if pos == "long":
            profit = snap["close"] - entry
            atr_stop = entry - P["atr_stop_mult"] * entry_atr
            stop = atr_stop
            if P["use_ma_stop"]:
                stop = max(stop, snap["ma40"] - P["slip_price"])   # 结构止损：跌破MA40
            stop = min(stop, entry - P["min_stop_atr"] * entry_atr)  # 不超过最小止损距离
            if profit > P["breakeven_atr"] * entry_atr:
                stop = max(stop, entry)                            # 保本
            if profit > P["lock_atr"] * entry_atr:
                stop = max(stop, entry + 1 * entry_atr)            # 锁定 +1ATR 利润
            soft = ext_high - P["trail_atr_mult"] * snap["atr"]     # 峰值回撤追踪
            stop = max(stop, soft)
            action, reason = "hold", None
            _tp = float(P.get("take_profit_atr", 0) or 0)
            _tpp = float(P.get("take_profit_points", 0) or 0)
            tp_gap = _tp * entry_atr if _tp > 0 else 0.0          # ATR倍数 -> 点数
            if _tpp > 0:                                          # 固定点数
                tp_gap = min(tp_gap, _tpp) if tp_gap > 0 else _tpp
            if snap["low"] <= stop:
                action, reason = "close", "ATR动态止损触发"
            elif tp_gap > 0 and snap["high"] >= entry + tp_gap:
                action, reason = "close", f"固定止盈(+{tp_gap:.1f}点 落袋)"
            elif snap["close"] < snap.get("ma" + str(P["ma_exit_ma"]), snap["ma20"]):
                action, reason = "close", f"均线破位(价<MA{P['ma_exit_ma']})平仓"
            elif (snap["pdif"] >= snap["pdea"]) and (snap["dif"] < snap["pdea"]):
                if (not P["death_below_zero"]) or (snap["dif"] < 0):
                    action, reason = "close", "MACD死叉平仓"
            elif (st["bars_held"] >= P["time_exit_bars"]) and (snap["close"] < ext_high):
                action, reason = "close", f"持仓超{P['time_exit_bars']}根未创新高平仓"
            return (action, stop, reason, None, "long")
        else:  # short
            profit = entry - snap["close"]
            atr_stop = entry + P["atr_stop_mult"] * entry_atr
            stop = atr_stop
            if P["use_ma_stop"]:
                stop = min(stop, snap["ma40"] + P["slip_price"])
            stop = max(stop, entry + P["min_stop_atr"] * entry_atr)
            if profit > P["breakeven_atr"] * entry_atr:
                stop = min(stop, entry)
            if profit > P["lock_atr"] * entry_atr:
                stop = min(stop, entry - 1 * entry_atr)
            soft = ext_low + P["trail_atr_mult"] * snap["atr"]
            stop = min(stop, soft)
            action, reason = "hold", None
            _tp = float(P.get("take_profit_atr", 0) or 0)
            _tpp = float(P.get("take_profit_points", 0) or 0)
            tp_gap = _tp * entry_atr if _tp > 0 else 0.0          # ATR倍数 -> 点数
            if _tpp > 0:                                          # 固定点数
                tp_gap = min(tp_gap, _tpp) if tp_gap > 0 else _tpp
            if snap["high"] >= stop:
                action, reason = "close", "ATR动态止损触发"
            elif tp_gap > 0 and snap["low"] <= entry - tp_gap:
                action, reason = "close", f"固定止盈(+{tp_gap:.1f}点 落袋)"
            elif snap["close"] > snap.get("ma" + str(P["ma_exit_ma"]), snap["ma20"]):
                action, reason = "close", f"均线破位(价>MA{P['ma_exit_ma']})平仓"
            elif (snap["pdif"] <= snap["pdea"]) and (snap["dif"] > snap["pdea"]):
                if (not P["death_below_zero"]) or (snap["dif"] > 0):
                    action, reason = "close", "MACD金叉平空"
            elif (st["bars_held"] >= P["time_exit_bars"]) and (snap["close"] > ext_low):
                action, reason = "close", f"持仓超{P['time_exit_bars']}根未创新低平仓"
            return (action, stop, reason, None, "short")

    # ---------------- 空仓：判开仓（双向 + ADX + 回调进场）----------------
    adx_ok = (not P["adx_filter"]) or (snap.get("adx", 999) >= P["adx_min"])
    if not adx_ok:
        return ("hold", None, None, None, None)
    vol_ok = (not P["vol_filter"]) or (snap["vol"] > snap["vol_mean5"])
    atr_ok = (not P["atr_filter"]) or (snap["atr_pct"] > P["atr_min_pct"])
    trend_ok = (not P["trend_filter"]) or bool(snap["trend_ma_bull"])
    time_ok = (not P["time_filter"]) or bool(snap["in_window"])
    base_ok = vol_ok and atr_ok and trend_ok and time_ok

    bull_align = (snap["ma5"] > snap["ma10"] > snap["ma20"] > snap["ma40"]) and (snap["ma5"] > snap["ma5_prev"])
    bear_align = (snap["ma5"] < snap["ma10"] < snap["ma20"] < snap["ma40"]) and (snap["ma5"] < snap["ma5_prev"])
    golden = (snap["pdif"] <= snap["pdea"]) and (snap["dif"] > snap["pdea"])
    death = (snap["pdif"] >= snap["pdea"]) and (snap["dif"] < snap["pdea"])

    # 做多：突破（原v1逻辑）
    if P["allow_long"] and bull_align and snap["close"] > snap["ma5"] and golden \
       and snap["dif"] > 0 and base_ok:
        entry = snap["close"] + P["slip_price"]
        atr = snap["atr"]
        stop = entry - P["atr_stop_mult"] * atr - P["slip_price"]
        if P["use_ma_stop"]:
            stop = max(stop, snap["ma40"] - P["slip_price"])
        stop = min(stop, entry - P["min_stop_atr"] * atr)
        return ("open", stop, "开多：多头排列+水上金叉+多过滤全通过", entry, "long")

    # 做多：回调进场（多头排列成立后，价格回踩 MA10 且水上、阳线）——不在突破顶追高
    if P["allow_long"] and P["entry_mode"] == "pullback" and bull_align \
       and (snap["ma20"] < snap["close"] <= snap["ma10"] * 1.002) \
       and snap["dif"] > 0 and snap["close"] > snap["open"] and base_ok:
        entry = snap["close"] + P["slip_price"]
        atr = snap["atr"]
        stop = entry - P["atr_stop_mult"] * atr - P["slip_price"]
        if P["use_ma_stop"]:
            stop = max(stop, snap["ma40"] - P["slip_price"])
        stop = min(stop, entry - P["min_stop_atr"] * atr)
        return ("open", stop, "开多：多头排列回踩MA10买点(水上+阳线)", entry, "long")

    # 做空：突破（镜像）
    if P["allow_short"] and bear_align and snap["close"] < snap["ma5"] and death \
       and snap["dif"] < 0 and base_ok:
        entry = snap["close"] - P["slip_price"]
        atr = snap["atr"]
        stop = entry + P["atr_stop_mult"] * atr + P["slip_price"]
        if P["use_ma_stop"]:
            stop = min(stop, snap["ma40"] + P["slip_price"])
        stop = max(stop, entry + P["min_stop_atr"] * atr)
        return ("open", stop, "开空：空头排列+水下死叉+多过滤全通过", entry, "short")

    # 做空：反弹进场（空头排列成立后，价格反弹至 MA10 附近且水下、阴线）
    if P["allow_short"] and P["entry_mode"] == "pullback" and bear_align \
       and (snap["ma20"] > snap["close"] >= snap["ma10"] * 0.998) \
       and snap["dif"] < 0 and snap["close"] < snap["open"] and base_ok:
        entry = snap["close"] - P["slip_price"]
        atr = snap["atr"]
        stop = entry + P["atr_stop_mult"] * atr + P["slip_price"]
        if P["use_ma_stop"]:
            stop = min(stop, snap["ma40"] + P["slip_price"])
        stop = max(stop, entry + P["min_stop_atr"] * atr)
        return ("open", stop, "开空：空头排列反弹MA10卖点(水下+阴线)", entry, "short")

    return ("hold", None, None, None, None)


def _fresh_state():
    return {"state": "flat", "entry_price": None, "entry_atr": None,
            "ext_high": None, "ext_low": None, "bars_held": 0, "stop_price": None}


def run_backtest_pro(symbol_key, volume=1,
                     start=date(2026, 3, 9), end=date(2026, 9, 13),
                     eval_start=date(2026, 3, 13), init_balance=200000,
                     kline_sec=5 * 60, ma_periods=(5, 10, 20, 40), macd=(8, 17, 9),
                     atr_period=14, open_html=True,
                     vol_filter=True, atr_filter=True, atr_min_pct=0.0015,
                     trend_filter=True, time_filter=True,
                     atr_stop_mult=2.5, death_below_zero=False,
                     time_exit_bars=20, slip_ticks=1, commission_per_hand=None,
                     # ---- 改进项（默认开启，可回退到 v1 行为）----
                     allow_long=True, allow_short=True, entry_mode="pullback",
                     adx_filter=True, adx_min=20,
                     use_ma_stop=True, min_stop_atr=2.0,
                     breakeven_atr=2.0, lock_atr=3.0, trail_atr_mult=2.5,
                     ma_exit_ma=20, risk_per_trade=0.0,
                     take_profit_atr=0.0, take_profit_points=0.0,
                     strategy_name="均线排列升级版", sample_tag=""):
    """均线排列升级版回测（状态机版，双向 + 改进）。

    改进点（相对 v1）：
      - 双向：多头排列+水上金叉/回踩买多；空头排列+水下死叉/反弹卖空（偏空市也能获利）。
      - ADX 趋势过滤：ADX>=adx_min 才交易，过滤震荡市的假信号（提升夏普最关键的一项）。
      - 宽止损+慢追踪：初始 atr_stop_mult×ATR（默认2.5，v1为1.5），保本2ATR、锁定3ATR、
        峰值回撤 trail_atr_mult×ATR（默认2.5）追踪，可选 use_ma_stop 跌破MA40结构止损。
        目的：止损不再比5分钟噪声还窄，让利润能奔跑。
      - 回调进场：entry_mode='pullback' 时，多头排列成立后等价格回踩MA10（水上、阳线）再买，
        不在突破顶追高；'breakout' 则为 v1 原突破进场。
      - 风险平价：risk_per_trade>0 时按"每笔风险=账户比例"反推手数（默认0=固定1手）。
    成本模型：sim.set_commission 写入每手手续费（单边）；滑点以价格惩罚显式建模。
    """
    name, main_code, mult, tick = config_symbols.get(symbol_key)
    user, pwd = config_account.check()
    if commission_per_hand is None:
        commission_per_hand = float(getattr(config_symbols, "COST", {}).get(symbol_key, 0.0) or 0.0)
    slip_price = slip_ticks * tick
    sim = TqSim(init_balance)
    stat = None
    equity_curve = []
    eval_ts = pd.Timestamp(eval_start) if eval_start else None
    try:
        api = TqApi(sim, backtest=TqBacktest(start_dt=start, end_dt=end),
                    auth=TqAuth(user, pwd), web_gui=False)
        k5 = api.get_kline_serial(main_code, kline_sec,
                                  data_length=max(max(ma_periods), macd[1] * 3, atr_period) + 160)
        k30 = api.get_kline_serial(main_code, 30 * 60,
                                   data_length=max(ma_periods) + 120)
        quote = api.get_quote(main_code)
        account = api.get_account()
        api.wait_update(deadline=time.time() + 30)

        cur_sym = quote.underlying_symbol
        tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
        try:
            sim.set_commission(cur_sym, commission_per_hand)
            sim.set_commission(main_code, commission_per_hand)
        except Exception as e:
            print(f"  [!] 手续费设置失败（不影响信号，但报告不含成本）: {e}")
        want = 0
        st = _fresh_state()
        trade_marks = []     # 买卖点明细（开/平仓），供在 K 线图上标注
        kline_history = []   # 每根已完成 K 线的 OHLCV，供报告画 K 线图
        print(f"\n>>> {name} {main_code} 均线排列升级版回测 {start} ~ {end}")
        print(f"   成本模型：手续费 {commission_per_hand} 元/手(单边) | "
              f"滑点 {slip_ticks} 跳({slip_price:.2f}元)")
        if eval_ts is not None:
            print(f"   评估区间（实际交易）：{eval_start} 起；此前为预热，不交易")

        while True:
            api.wait_update(deadline=time.time() + 30)

            if api.is_changing(quote, "underlying_symbol"):
                new_sym = quote.underlying_symbol
                if new_sym != cur_sym:
                    print(f"    换月 {cur_sym} -> {new_sym}")
                    tp.set_target_volume(0)
                    while api.get_position(cur_sym).pos != 0:
                        api.wait_update(deadline=time.time() + 30)
                    cur_sym = new_sym
                    tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
                    try:
                        sim.set_commission(cur_sym, commission_per_hand)
                    except Exception:
                        pass
                    tp.set_target_volume(want)
                    if st["state"] != "flat" and st["entry_price"] is None:   # 换月沿用持仓，重锚止损
                        st["entry_price"] = quote.last_price
                        st["entry_atr"] = float(_atr(k5.high, k5.low, k5.close, atr_period).iloc[-2])
                        st["ext_high"] = float(k5.close.iloc[-2])
                        st["ext_low"] = float(k5.close.iloc[-2])
                        st["bars_held"] = 0

            if api.is_changing(k5.iloc[-1], "datetime"):
                bar_dt = k5.iloc[-2]["datetime"]
                bar_dt_str = _dt_str(bar_dt)
                try:
                    equity_curve.append((bar_dt_str, float(account.balance)))
                except Exception:
                    pass
                if eval_ts is not None and pd.Timestamp(bar_dt) < eval_ts:
                    continue

                close = k5.close
                o = float(k5.open.iloc[-2])
                mas = [float(close.rolling(p).mean().iloc[-2]) for p in ma_periods]
                ma5, ma10, ma20, ma40 = mas
                ma5_prev = float(close.rolling(ma_periods[0]).mean().iloc[-3])
                ma10_prev = float(close.rolling(ma_periods[1]).mean().iloc[-3])
                last_close = float(close.iloc[-2])
                high = float(k5.high.iloc[-2])
                low = float(k5.low.iloc[-2])
                vol = float(k5.volume.iloc[-2])
                vol_mean5 = float(k5.volume.iloc[-7:-2].mean()) if len(k5) >= 7 else vol
                atr = float(_atr(k5.high, k5.low, k5.close, atr_period).iloc[-2])
                atr_pct = (atr / last_close) if last_close else 0.0
                kline_history.append({"dt": bar_dt_str, "open": o, "high": high,
                                      "low": low, "close": last_close, "volume": vol})
                dif_s, dea_s = _macd(close, *macd)
                dif = float(dif_s.iloc[-2]); dea = float(dea_s.iloc[-2])
                pdif = float(dif_s.iloc[-3]); pdea = float(dea_s.iloc[-3])
                adx_v = 999.0
                if adx_filter:
                    try:
                        adx_s, _pdi, _mdi = _adx(k5.high, k5.low, k5.close, atr_period)
                        adx_v = float(adx_s.iloc[-2])
                    except Exception:
                        adx_v = 999.0
                trend_ma_bull = True
                if trend_filter:
                    try:
                        c30 = k30.close
                        m30 = [float(c30.rolling(p).mean().iloc[-2]) for p in (5, 10, 20)]
                        trend_ma_bull = (m30[0] > m30[1] > m30[2])
                    except Exception:
                        trend_ma_bull = True
                in_window = _in_entry_window(bar_dt) if time_filter else True

                if st["state"] != "flat":
                    st["ext_high"] = max(st["ext_high"] if st["ext_high"] is not None else high, high)
                    st["ext_low"] = min(st["ext_low"] if st["ext_low"] is not None else low, low)
                    st["bars_held"] += 1
                    st["mfe"] = ((st["ext_high"] - st["entry_price"])
                                 if st["state"] == "long"
                                 else (st["entry_price"] - st["ext_low"]))

                snap = dict(ma5=ma5, ma10=ma10, ma20=ma20, ma40=ma40,
                            ma5_prev=ma5_prev, ma10_prev=ma10_prev,
                            open=o, close=last_close, high=high, low=low, vol=vol,
                            vol_mean5=vol_mean5, atr=atr, atr_pct=atr_pct,
                            dif=dif, dea=dea, pdif=pdif, pdea=pdea,
                            trend_ma_bull=trend_ma_bull, in_window=in_window, adx=adx_v)
                P = dict(vol_filter=vol_filter, atr_filter=atr_filter,
                         atr_min_pct=atr_min_pct, trend_filter=trend_filter,
                         time_filter=time_filter, atr_stop_mult=atr_stop_mult,
                         death_below_zero=death_below_zero,
                         time_exit_bars=time_exit_bars, slip_price=slip_price,
                         adx_filter=adx_filter, adx_min=adx_min,
                         allow_long=allow_long, allow_short=allow_short,
                         entry_mode=entry_mode, use_ma_stop=use_ma_stop,
                         min_stop_atr=min_stop_atr, breakeven_atr=breakeven_atr,
                         lock_atr=lock_atr, trail_atr_mult=trail_atr_mult,
                         ma_exit_ma=ma_exit_ma, take_profit_atr=take_profit_atr,
                         take_profit_points=take_profit_points)
                action, stop, reason, entry, side = _pro_bar_decision(snap, st, P)

                # 风险平价手数（可选）：每笔风险 = 账户比例 -> 反推手数
                def _vol_for(e, s):
                    if risk_per_trade and e is not None and s is not None:
                        sd = abs(e - s)
                        if sd > 0:
                            ra = risk_per_trade * float(account.balance)
                            v = int(ra / (sd * slip_price * mult)) if slip_price else 0
                            return max(1, v)
                    return volume
                if action == "open" and st["state"] == "flat":
                    want_vol = _vol_for(entry, stop)
                    want = want_vol if side == "long" else -want_vol
                    st = {"state": side, "entry_price": entry, "entry_atr": atr,
                          "ext_high": high, "ext_low": low, "bars_held": 0, "stop_price": stop}
                    tp.set_target_volume(want)
                    act = "开多" if side == "long" else "开空"
                    trade_marks.append({"dt": bar_dt_str, "act": "open", "side": side,
                                        "price": entry, "reason": reason})
                    print(f"    {bar_dt_str} {act} {want_vol}手  @约{entry:.2f}  "
                          f"MA5={ma5:.1f} MA10={ma10:.1f} MA20={ma20:.1f} MA40={ma40:.1f} "
                          f"| DIF={dif:.2f} DEA={dea:.2f} | ATR={atr:.1f} 止损≈{stop:.2f}")
                elif action == "close" and st["state"] != "flat":
                    want = 0
                    tp.set_target_volume(0)
                    act = "平多" if st["state"] == "long" else "平空"
                    _ea = st.get("entry_atr") or 0
                    _mfe = st.get("mfe") or 0
                    trade_marks.append({"dt": bar_dt_str, "act": "close", "side": st["state"],
                                        "price": last_close, "reason": reason,
                                        "mfe": round(_mfe, 2),
                                        "mfe_atr": round(_mfe / _ea, 2) if _ea else 0})
                    _mfe_txt = f" | 最大浮盈 {_mfe:+.1f}点(+{_mfe / _ea:.2f}ATR)" if _ea else ""
                    print(f"    {bar_dt_str} {act} [{reason}]  "
                          f"触发价{last_close:.2f}{_mfe_txt} | 止损≈{stop:.2f}")
                    st = _fresh_state()
                elif st["state"] != "flat":
                    st["stop_price"] = stop

    except BacktestFinished:
        stat = sim.tqsdk_stat
        sdict = _stat_to_dict(stat)
        trades = _collect_trades(api)
        _print_stat(name, sdict)
        ma_text = "/".join(f"MA{p}" for p in ma_periods) + \
                  f" + MACD({macd[0]},{macd[1]},{macd[2]}) + ATR({atr_period})动态止损"
        extra = (f"周期：{kline_sec // 60}分钟K线 | 评估区间：{eval_start} ~ {end}"
                 f"（{start} 起仅预热）| "
                 f"双向：{'多' if allow_long else ''}{'空' if allow_short else ''} "
                 f"{'| ADX>=' + str(adx_min) + ' 才交易' if adx_filter else ''} | "
                 f"进场：{'回踩MA10(水上)' if entry_mode == 'pullback' else '突破'}；"
                 f"多头排列+水上金叉开多 / 空头排列+水下死叉开空 "
                 f"+ 量能+波幅+30分钟共振+时间 过滤 | "
                 f"平仓：MACD死叉(空平为金叉) / 均线破位(价<MA{ma_exit_ma}或>MA{ma_exit_ma}) / "
                 f"持仓超{time_exit_bars}根未创新高(低) | "
                 f"止损：动态ATR({atr_stop_mult}倍，最小{min_stop_atr}倍)，"
                 f"盈利>{breakeven_atr}ATR保本、>{lock_atr}ATR锁+1ATR、峰值回撤{trail_atr_mult}ATR追踪"
                 f"{'，跌破MA40结构止损' if use_ma_stop else ''}"
                 f"{f'，固定止盈 +{take_profit_points:g}点落袋' if take_profit_points else (f'，固定止盈 +{take_profit_atr:g}ATR 落袋' if take_profit_atr else '')} | "
                 f"成本模型：手续费{commission_per_hand}元/手(单边) + 滑点{slip_ticks}跳(已在止损/开仓价显式建模)"
                 f"{f' | 风险平价：每笔风险{risk_per_trade*100:.1f}%账户' if risk_per_trade else ''}")
        # 买卖点 -> K线图标记：开多/平空=买点(BUY)，开空/平多=卖点(SELL)；最多显示 50 个
        signal_events = []
        for _m in trade_marks:
            if _m.get("act") == "open":
                _sd = "BUY" if _m.get("side") == "long" else "SELL"
            else:
                _sd = "BUY" if _m.get("side") == "short" else "SELL"
            signal_events.append({"dt": _m["dt"], "dir": _sd})
        html_path, txt_path = _save_backtest_report(
            strategy_name, symbol_key, name, sdict, equity_curve, trades,
            start, end, ma_text, volume, kline_sec, init_balance, main_code,
            extra_note=extra, kline_history=kline_history, signal_events=signal_events,
            mult=mult, trade_marks=trade_marks, sample_tag=sample_tag)
        print(f"\n  回测记录已保存：{txt_path}")
        print(f"  回测报告已生成：{html_path}")
        if open_html:
            try:
                os.startfile(html_path)
            except Exception:
                pass
        api.close()
    return stat


def run_live_pro(symbol_key, volume=1, kline_sec=5 * 60, ma_periods=(5, 10, 20, 40),
                 macd=(8, 17, 9), atr_period=14, allow_trade=True, web_gui=True,
                 heartbeat=30, vol_filter=True, atr_filter=True, atr_min_pct=0.0015,
                 trend_filter=True, time_filter=True, atr_stop_mult=2.5,
                 death_below_zero=False, time_exit_bars=20, slip_ticks=1,
                 allow_long=True, allow_short=True, entry_mode="pullback",
                 adx_filter=True, adx_min=20, use_ma_stop=True, min_stop_atr=2.0,
                 breakeven_atr=2.0, lock_atr=3.0, trail_atr_mult=2.5,
                 ma_exit_ma=20, risk_per_trade=0.0,
                 take_profit_atr=0.0, take_profit_points=0.0,
                 strategy_name="均线排列升级版"):
    """均线排列升级版模拟盘（状态机版，双向 + 改进）。盯盘能力与 run_live_align 一致。

    参数与 run_backtest_pro 改进版一致（双向 / ADX 过滤 / 宽止损慢追踪 / 回调进场 / 风险平价）。
    """
    name, main_code, mult, tick = config_symbols.get(symbol_key)
    user, pwd = config_account.check()
    slip_price = slip_ticks * tick

    api = TqApi(TqKq(), auth=TqAuth(user, pwd), web_gui=web_gui)
    k5 = api.get_kline_serial(main_code, kline_sec,
                              data_length=max(max(ma_periods), macd[1] * 3, atr_period) + 160)
    k30 = api.get_kline_serial(main_code, 30 * 60, data_length=max(ma_periods) + 120)
    quote = api.get_quote(main_code)
    account = api.get_account()
    trade_log = api.get_trade()
    api.wait_update(deadline=time.time() + 30)

    cur_sym = quote.underlying_symbol
    tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
    want = api.get_position(cur_sym).pos
    seen = set(trade_log.keys())
    stop_st = {"active": False, "entry": 0.0, "entry_atr": 0.0,
               "ext_high": 0.0, "bars": 0}   # 动态 ATR 止损状态
    if atr_stop_mult > 0 and want > 0:
        # 盘中启动时已有持仓：以持仓均价为基准，ATR 在第一根 K 线补齐
        _p0 = api.get_position(cur_sym)
        stop_st.update({"active": True,
                        "entry": float(_p0.open_price_long or quote.last_price),
                        "entry_atr": 0.0,
                        "ext_high": float(quote.last_price), "bars": 0})
    st = _fresh_state()

    DIR_CN = {"BUY": "多", "SELL": "空"}
    OFF_CN = {"OPEN": "开仓", "CLOSE": "平仓", "CLOSETODAY": "平今"}

    print("=" * 64)
    print(f" 模拟盘启动：{name}  {main_code}")
    print(f" 当前主力合约：{cur_sym}")
    print(f" 账户可用资金：{account.available:,.2f} 元")
    print(f" 下单开关：{'开（会真发单到模拟账户）' if allow_trade else '关（只打印信号不下单）'}")
    print(f" 参数：MA{ma_periods[0]}/{ma_periods[1]}/{ma_periods[2]}/{ma_periods[3]} "
          f"MACD({macd[0]},{macd[1]},{macd[2]}) ATR({atr_period}) 动态止损 | {volume} 手")
    print("=" * 64)
    if web_gui:
        # 天勤 web_gui 每次启动分配随机端口，从内部获取真实地址
        gui_url = _get_web_gui_url(api)
        if gui_url:
            print(f" 浏览器监控面板 -> {gui_url}")
            try:
                import platform
                if platform.system() == "Windows":
                    os.startfile(gui_url)
                else:
                    webbrowser.open(gui_url)
            except Exception:
                pass
        else:
            print("   (web_gui 面板端口获取失败，查看天勤启动日志中的端口号)")
        print("   （可看实时K线、均线、买卖点、持仓、资金曲线；关掉窗口程序仍运行）")
    if not allow_trade:
        print("  [!] 当前只是观察模式：信号会打印，但不会真正下单")
    print("  盘中盯盘看这里：每笔真实成交会标 *，并定时刷新盘面快照\n")

    last_bar_dt = k5.iloc[-1]["datetime"]
    last_heart = datetime.now()
    # 盘面心跳：静默守卫已去除，每 heartbeat 秒无条件打印一行（见下方打印块）。
    # 完全一样就不再打印，休市期间不再重复刷屏。指纹一变立刻恢复。

    def mas_now():
        c = k5.close
        return [float(c.rolling(p).mean().iloc[-1]) for p in ma_periods]

    while True:
        api.wait_update(deadline=time.time() + 30)

        if allow_trade and api.is_changing(trade_log):
            for tid in trade_log:
                if tid in seen:
                    continue
                seen.add(tid)
                t = trade_log[tid]
                d = DIR_CN.get(t.direction, t.direction)
                o = OFF_CN.get(t.offset, t.offset)
                print(f"[{datetime.now():%H:%M:%S}] * 成交 {d}{o} {int(t.volume)}手 "
                      f"@ {float(t.price):.2f}  合约 {t.symbol}")
                if allow_trade:
                    print(f"            （已真实发送到模拟账户，可在快期3核对）")

        if api.is_changing(quote, "underlying_symbol"):
            new_sym = quote.underlying_symbol
            if new_sym != cur_sym:
                print(f"[{datetime.now():%H:%M:%S}] 主力换月 {cur_sym} -> {new_sym}")
                tp.set_target_volume(0)
                while api.get_position(cur_sym).pos != 0:
                    api.wait_update(deadline=time.time() + 30)
                cur_sym = new_sym
                tp = TargetPosTask(api, cur_sym, support_open_min_volume=True)
                if allow_trade:
                    tp.set_target_volume(want)

        if api.is_changing(k5.iloc[-1], "datetime") and k5.iloc[-1]["datetime"] != last_bar_dt:
            last_bar_dt = k5.iloc[-1]["datetime"]
            bar_dt_str = _dt_str(last_bar_dt)

            close = k5.close
            o = float(k5.open.iloc[-2])
            mas = [float(close.rolling(p).mean().iloc[-2]) for p in ma_periods]
            ma5, ma10, ma20, ma40 = mas
            ma5_prev = float(close.rolling(ma_periods[0]).mean().iloc[-3])
            ma10_prev = float(close.rolling(ma_periods[1]).mean().iloc[-3])
            last_close = float(close.iloc[-2])
            high = float(k5.high.iloc[-2]); low = float(k5.low.iloc[-2])
            vol = float(k5.volume.iloc[-2])
            vol_mean5 = float(k5.volume.iloc[-7:-2].mean()) if len(k5) >= 7 else vol
            atr = float(_atr(k5.high, k5.low, k5.close, atr_period).iloc[-2])
            atr_pct = (atr / last_close) if last_close else 0.0
            dif_s, dea_s = _macd(close, *macd)
            dif = float(dif_s.iloc[-2]); dea = float(dea_s.iloc[-2])
            pdif = float(dif_s.iloc[-3]); pdea = float(dea_s.iloc[-3])
            adx_v = 999.0
            if adx_filter:
                try:
                    adx_s, _pdi, _mdi = _adx(k5.high, k5.low, k5.close, atr_period)
                    adx_v = float(adx_s.iloc[-2])
                except Exception:
                    adx_v = 999.0
            trend_ma_bull = True
            if trend_filter:
                try:
                    c30 = k30.close
                    m30 = [float(c30.rolling(p).mean().iloc[-2]) for p in (5, 10, 20)]
                    trend_ma_bull = (m30[0] > m30[1] > m30[2])
                except Exception:
                    trend_ma_bull = True
            in_window = _in_entry_window(last_bar_dt) if time_filter else True

            if st["state"] != "flat":
                st["ext_high"] = max(st["ext_high"] if st["ext_high"] is not None else high, high)
                st["ext_low"] = min(st["ext_low"] if st["ext_low"] is not None else low, low)
                st["bars_held"] += 1

            snap = dict(ma5=ma5, ma10=ma10, ma20=ma20, ma40=ma40,
                        ma5_prev=ma5_prev, ma10_prev=ma10_prev,
                        open=o, close=last_close, high=high, low=low, vol=vol,
                        vol_mean5=vol_mean5, atr=atr, atr_pct=atr_pct,
                        dif=dif, dea=dea, pdif=pdif, pdea=pdea,
                        trend_ma_bull=trend_ma_bull, in_window=in_window, adx=adx_v)
            P = dict(vol_filter=vol_filter, atr_filter=atr_filter,
                     atr_min_pct=atr_min_pct, trend_filter=trend_filter,
                     time_filter=time_filter, atr_stop_mult=atr_stop_mult,
                     death_below_zero=death_below_zero,
                     time_exit_bars=time_exit_bars, slip_price=slip_price,
                     adx_filter=adx_filter, adx_min=adx_min,
                     allow_long=allow_long, allow_short=allow_short,
                     entry_mode=entry_mode, use_ma_stop=use_ma_stop,
                     min_stop_atr=min_stop_atr, breakeven_atr=breakeven_atr,
                     lock_atr=lock_atr, trail_atr_mult=trail_atr_mult,
                     ma_exit_ma=ma_exit_ma, take_profit_atr=take_profit_atr,
                     take_profit_points=take_profit_points)
            action, stop, reason, entry, side = _pro_bar_decision(snap, st, P)

            def _vol_for(e, s):
                if risk_per_trade and e is not None and s is not None:
                    sd = abs(e - s)
                    if sd > 0:
                        ra = risk_per_trade * float(account.available)
                        v = int(ra / (sd * slip_price * mult)) if slip_price else 0
                        return max(1, v)
                return volume
            if action == "open" and st["state"] == "flat":
                want_vol = _vol_for(entry, stop)
                want = want_vol if side == "long" else -want_vol
                st = {"state": side, "entry_price": entry, "entry_atr": atr,
                      "ext_high": high, "ext_low": low, "bars_held": 0, "stop_price": stop}
                act = "开多" if side == "long" else "开空"
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {act} {want_vol}手 @约{entry:.2f} "
                      f"| MA5={ma5:.2f} MA10={ma10:.2f} MA20={ma20:.2f} MA40={ma40:.2f} "
                      f"| DIF={dif:.2f} DEA={dea:.2f} | ATR={atr:.1f} 止损≈{stop:.2f}")
                if allow_trade:
                    tp.set_target_volume(want)
                    print(f"            > 已下达目标仓位 {want} 手，等待撮合...")
                else:
                    print(f"            （观察模式，未下单）")
            elif action == "close" and st["state"] != "flat":
                want = 0
                act = "平多" if st["state"] == "long" else "平空"
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {act} [{reason}] "
                      f"| 触发价{last_close:.2f} 止损≈{stop:.2f}")
                if allow_trade:
                    tp.set_target_volume(0)
                else:
                    print(f"            （观察模式，未下单）")
                st = _fresh_state()
            elif st["state"] != "flat":
                st["stop_price"] = stop

        # ---- 4) 盘面心跳：定时刷新快照，确认程序没卡死 ----
        #   ★ 2026-09-21：每 heartbeat 周期无条件打印一行（静默守卫已去除），便于肉眼判断中断。
        if (datetime.now() - last_heart).total_seconds() >= heartbeat:
            last_heart = datetime.now()
            mv = mas_now()
            pos = api.get_position(cur_sym).pos
            stop_s = f"{st['stop_price']:.2f}" if st["stop_price"] else "—"
            _px_h = float(quote.last_price)
            print(f"[{datetime.now():%H:%M:%S}] 盘面 现价 {_px_h:.2f} "
                  f"| MA{ma_periods[0]}={mv[0]:.2f} MA{ma_periods[-1]}={mv[-1]:.2f} "
                  f"| 持仓 {int(pos)}手 目标 {want} 止损 {stop_s} "
                  f"| 可用 {account.available:,.0f}")


# ===================================================================
# 内部工具：统计兼容 / 交易采集 / 报告生成
# ===================================================================
def _stat_to_dict(stat):
    """兼容 dict 和 DataFrame 两种返回格式（tqsdk 版本差异）。"""
    try:
        s = stat.iloc[-1] if hasattr(stat, "iloc") else stat
        if hasattr(s, "to_dict"):
            return s.to_dict()
        if hasattr(s, "index"):
            return {k: s[k] for k in s.index}
        return dict(s)
    except Exception:
        return {}


def _collect_trades(api):
    out = []
    try:
        for t in api.get_trade().values():
            d = _trade_to_dict(t)
            if d:
                out.append(d)
    except Exception:
        pass
    return out


def _trade_to_dict(t):
    try:
        return {
            "dt": (getattr(t, "trade_date_time", None)
                   or getattr(t, "datetime", None) or ""),
            "direction": getattr(t, "direction", ""),
            "offset": getattr(t, "offset", ""),
            "price": float(getattr(t, "price", 0) or 0),
            "volume": int(getattr(t, "volume", 0) or 0),
            "symbol": getattr(t, "symbol", ""),
        }
    except Exception:
        return None


def _print_stat(name, s):
    try:
        print(f"\n===== {name} 回测结果 =====")
        print(f"  起始资金    {s.get('init_balance', 0):>15,.0f}")
        print(f"  结束资金    {s.get('balance', 0):>15,.0f}")
        print(f"  收益率      {s.get('ror', 0):>15.2%}")
        print(f"  年化收益    {s.get('annual_yield', 0):>15.2%}")
        print(f"  最大回撤    {s.get('max_drawdown', 0):>15.2%}")
        print(f"  胜率        {s.get('winning_rate', 0):>15.2%}")
        print(f"  盈亏比      {s.get('profit_loss_ratio', 0):>15.2f}")
        print(f"  夏普率      {s.get('sharpe_ratio', 0):>15.2f}")
    except Exception as e:
        print(f"  （指标读取异常：{e}）")


def _cost_note_text(cost_base, cost_specified, slip_ticks, exec_price, apply_cost):
    """拼「成本口径」一句话，供报告头部展示（2026-09-19 新增）。

    slip_ticks 只描述**费用层**；价格层由 TqSim 撮合方式决定：
      ACTIVE（对价）：买吃卖一/卖吃买一 → 每次成交天然再吃 1 跳
      PASSIVE（排队）：挂买一/卖一 → 价格层无这 1 跳
    所以「等效实盘滑点假设」= 费用层跳数 + (ACTIVE ? 1 : 0)。
    """
    ep = str(exec_price or "ACTIVE").upper()
    price_layer = 1 if ep == "ACTIVE" else 0
    eff = slip_ticks + price_layer
    parts = [(f"手续费 {cost_base:g} 元/手（显式指定）" if cost_specified
              else "手续费按交易所默认费率")]
    parts.append(
        ("价格层 1 跳（TqSim 对价为：买+1/卖−1）" if ep == "ACTIVE"
         else "价格层 0 跳（TqSim 排队挂单 PASSIVE）")
        + f" + 费用层 {slip_ticks} 跳（脚本 slip_ticks）"
        + f" → 等效实盘滑点假设 {eff} 跳/次")
    if ep == "ACTIVE":
        parts.append("⚠️ 对价成交的成交价比信号 bar 开盘价天然差 1 跳"
                     "（实测 60/60 笔吻合）")
    else:
        parts.append("⚠️ 排队挂单在回测里会撤单重挂直到成交，"
                     "真实成交率回测无法反映，须以模拟盘实测为准")
    return " ｜ ".join(parts)


def _save_backtest_report(strategy_name, symbol_key, name, s, equity_curve,
                          trades, start, end, ma_text, volume,
                          kline_sec, init_balance, main_code, extra_note="",
                          kline_history=None, signal_events=None, mult=1,
                          trade_marks=None, sample_tag="", cost_note="",
                          vol_text=None):
    """生成按品种命名的 HTML 报告 + 成交明细 txt，返回 (html_path, txt_path)。

    落盘目录 = 回测结果/<策略目录>/<品种>/   ← 两层：先策略、后品种（2026-09-17 起）
      · <策略目录> 带编号（01_双均线 / 02_均线排列 / 03_布林带），与 策略/ 目录一致
      · 文件名   = <品种>_<短名>_<周期>[_in|_out].html / .txt （如 PVC_双均线_5min_in.html）
      · 周期由 kline_sec 自动推导（5min / 15min / 1d ...），换周期无需改代码
      · sample_tag：in=练习区间 / out=期末考试区间，防止两者互相覆盖
      · 不带日期：同一品种同一策略同一周期，练习与期末考各只留一份，靠标记区分
    """
    out_dir = os.path.join(_ROOT, "回测结果", 策略目录名(strategy_name), name)
    os.makedirs(out_dir, exist_ok=True)

    if kline_sec and kline_sec % 86400 == 0:
        _per = f"_{kline_sec // 86400}d"
    elif kline_sec and kline_sec % 60 == 0:
        _per = f"_{kline_sec // 60}min"
    elif kline_sec:
        _per = f"_{int(kline_sec)}s"
    else:
        _per = ""
    _tag = f"_{sample_tag}" if sample_tag else ""
    html_name = f"{name}_{strategy_name}{_per}{_tag}.html"
    txt_name = f"{name}_{strategy_name}{_per}{_tag}.txt"
    html_path = os.path.join(out_dir, html_name)
    txt_path = os.path.join(out_dir, txt_name)

    # ---- 成交明细 txt ----
    try:
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"{name}（{main_code}）{strategy_name}回测成交明细\n")
            f.write(f"周期：{start} ~ {end}   参数：{ma_text} "
                    f"{vol_text or f'{volume}手'}\n")
            if cost_note:
                f.write(f"成本口径：{cost_note}\n")
            if extra_note:
                f.write(extra_note + "\n")
            f.write("=" * 60 + "\n")
            if trades:
                for t in trades:
                    f.write(f"{_dt_str(t['dt'])}  {t['direction']} {t['offset']}  "
                            f"{t['volume']}手  {t['price']:.2f}  {t['symbol']}\n")
            else:
                f.write("（未采集到逐笔成交，可改用 TqSim.trade_log 查看）\n")
    except Exception as e:
        print(f"    （成交明细保存失败：{e}）")

    # ---- HTML 报告 ----
    html = _build_html_report(strategy_name, name, main_code, s, equity_curve,
                              trades, start, end, ma_text, volume,
                              kline_sec, init_balance, extra_note=extra_note,
                              kline_history=kline_history, signal_events=signal_events,
                              mult=mult, trade_marks=trade_marks, cost_note=cost_note,
                              vol_text=vol_text)
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        print(f"    （HTML 报告保存失败：{e}）")

    return html_path, txt_path


def _reconstruct_trades(trades, mult):
    """把零散成交按 FIFO 配对成完整交易，算逐笔盈亏。trades 元素见 _trade_to_dict。"""
    if not trades:
        return []
    try:
        fills = sorted(trades, key=lambda t: _dt_str(t.get("dt", "")))
    except Exception:
        fills = list(trades)
    opens = []      # 未平仓开仓：{dir,price,vol,dt,symbol}
    results = []
    for f in fills:
        offset = str(f.get("offset", "")).upper()
        direction = str(f.get("direction", "")).upper()
        if offset == "OPEN":
            opens.append({
                "dir": direction,
                "price": float(f.get("price", 0) or 0),
                "vol": int(f.get("volume", 0) or 0),
                "dt": str(f.get("dt", "")),
                "symbol": f.get("symbol", ""),
            })
        else:
            close_dir = "SELL" if direction == "BUY" else "BUY"
            close_price = float(f.get("price", 0) or 0)
            remain = int(f.get("volume", 0) or 0)
            while remain > 0 and opens:
                lot = opens[0]
                if lot["dir"] != close_dir:
                    opens.pop(0)
                    continue
                matched = min(remain, lot["vol"])
                if lot["dir"] == "BUY":
                    pnl = (close_price - lot["price"]) * matched * mult
                else:
                    pnl = (lot["price"] - close_price) * matched * mult
                base = lot["price"] * matched * mult
                results.append({
                    "open_dt": lot["dt"], "open_price": lot["price"],
                    "close_dt": f.get("dt", ""), "close_price": close_price,
                    "dir": lot["dir"], "volume": matched,
                    "pnl": pnl, "pnl_pct": (pnl / base * 100) if base else 0,
                    "symbol": f.get("symbol", ""),
                })
                remain -= matched
                lot["vol"] -= matched
                if lot["vol"] <= 0:
                    opens.pop(0)
    return results


def _drawdown_svg(series):
    """基于权益序列画回撤曲线（红色填充向下）。"""
    if not series:
        return '<p class="note">（无权益数据）</p>'
    vals = [float(v) for _, v in series]
    W, H, pad = 800, 200, 30
    rm = vals[0]
    dds = []
    for v in vals:
        rm = max(rm, v)
        dds.append((rm - v) / rm if rm else 0)
    max_dd = max(dds) if dds else 0
    rng = max_dd or 1
    n = len(vals)
    pts = []
    for i, dd in enumerate(dds):
        x = pad + (i / (n - 1)) * (W - 2 * pad) if n > 1 else W / 2
        y = pad + dd / rng * (H - 2 * pad)
        pts.append((x, y))
    poly = f"{pad},{H-pad} " + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + f" {W-pad},{H-pad}"
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    label = (f'<text x="{W-pad-130}" y="{pad+12:.0f}" font-size="11" fill="#d4380d">'
             f'最大回撤 {max_dd*100:.2f}%</text>')
    return f"""
    <svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMid meet"
         style="background:#fbfcfe;border:1px solid #eef2f7;border-radius:8px;">
      <polygon fill="rgba(212,56,13,0.16)" points="{poly}"/>
      <polyline fill="none" stroke="#d4380d" stroke-width="1.5" points="{line}"/>
      {label}
    </svg>"""


def _monthly_table_html(equity_curve, init_balance):
    """按月聚合权益曲线，生成月度收益表 HTML。

    equity_curve: [(datetime_str, balance), ...]；取每月最后一条作为月末权益。
    月度收益 = 月末权益 / 上月末权益 - 1；首月以 init_balance 为期初基准。
    """
    if not equity_curve:
        return '<p class="note">无权益曲线数据，无法生成月度收益。</p>'
    try:
        month_end = {}   # 'YYYY-MM' -> 月末 balance
        for dt_str, bal in equity_curve:
            key = str(dt_str)[:7]
            month_end[key] = float(bal)
        months = sorted(month_end.keys())
        if not months:
            return '<p class="note">无有效月度数据。</p>'
        base = float(init_balance) if init_balance else month_end[months[0]]
        rows = ""
        prev = base
        for m in months:
            end = month_end[m]
            ret = (end / prev - 1) if prev else 0.0
            cum = (end / base - 1) if base else 0.0
            pnl = end - prev
            cls = "pos" if ret >= 0 else "neg"   # 红涨绿跌（中国习惯）
            sign = "+" if pnl >= 0 else ""
            rows += (f"<tr><td>{m}</td><td>{end:,.0f}</td>"
                     f"<td class='{cls}'>{ret*100:+.2f}%</td>"
                     f"<td class='{cls}'>{sign}{pnl:,.0f}</td>"
                     f"<td class='{cls}'>{cum*100:+.2f}%</td></tr>")
            prev = end
        return f"""
      <div class="scroll">
      <table>
        <thead><tr><th>月份</th><th>月末权益(元)</th><th>月度收益</th>
        <th>月度盈亏(元)</th><th>累计收益</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      </div>"""
    except Exception as e:
        return f'<p class="note">月度收益计算失败：{e}</p>'


def _kline_svg(history, signals, max_marks=50):
    """画 K 线图 + 买卖点标记。history: [{dt,open,high,low,close,volume}, ...]

    max_marks：买卖点标记最多显示多少个；超过时均匀采样，保证覆盖整个区间。
    """
    if not history:
        return ""
    sigs = list(signals or [])
    if max_marks and len(sigs) > max_marks:
        if max_marks <= 1:
            sigs = sigs[-1:]
        else:
            sigs = [sigs[int(round(i * (len(sigs) - 1) / (max_marks - 1)))]
                    for i in range(max_marks)]
    n = len(history)
    step = 1
    ds = history
    if n > 600:
        step = (n + 599) // 600
        ds = []
        for i in range(0, n, step):
            chunk = history[i:i+step]
            ds.append({
                "dt": chunk[0]["dt"],
                "open": chunk[0]["open"],
                "close": chunk[-1]["close"],
                "high": max(b["high"] for b in chunk),
                "low": min(b["low"] for b in chunk),
                "volume": sum(b["volume"] for b in chunk),
            })
    m = len(ds)
    if m == 0:
        return ""
    W, H, pad = 800, 320, 36
    lo = min(b["low"] for b in ds)
    hi = max(b["high"] for b in ds)
    rng = (hi - lo) or 1
    cw = (W - 2 * pad) / m
    body_w = max(1.0, cw * 0.6)

    candles = ""
    for i, b in enumerate(ds):
        x = pad + (i + 0.5) * cw
        y_o = H - pad - (b["open"] - lo) / rng * (H - 2 * pad)
        y_c = H - pad - (b["close"] - lo) / rng * (H - 2 * pad)
        y_h = H - pad - (b["high"] - lo) / rng * (H - 2 * pad)
        y_l = H - pad - (b["low"] - lo) / rng * (H - 2 * pad)
        color = "#d4380d" if b["close"] >= b["open"] else "#198755"
        top = min(y_o, y_c)
        hgt = max(1.0, abs(y_c - y_o))
        candles += (f'<line x1="{x:.1f}" y1="{y_h:.1f}" x2="{x:.1f}" y2="{y_l:.1f}" stroke="{color}" stroke-width="1"/>')
        candles += (f'<rect x="{x-body_w/2:.1f}" y="{top:.1f}" width="{body_w:.1f}" height="{hgt:.1f}" fill="{color}"/>')

    markers = ""
    for sig in sigs:
        idx = None
        for j, b in enumerate(history):
            if b["dt"] >= sig["dt"]:
                idx = j
                break
        if idx is None:
            continue
        di = idx // step
        if di >= m:
            di = m - 1
        b = ds[di]
        x = pad + (di + 0.5) * cw
        _rs = str(sig.get("reason", "") or "")
        _lb = _rs if _rs else ("买入" if sig["dir"] == "BUY" else "卖出")
        if sig["dir"] == "BUY":
            y = H - pad - (b["low"] - lo) / rng * (H - 2 * pad) + 14
            _col = "#e8890c" if _rs else "#d4380d"
            markers += (f'<path d="M{x:.1f},{y:.1f} l-5,-9 l10,0 z" fill="{_col}">'
                        f'<title>{_lb} {sig["dt"]}</title></path>')
        else:
            y = H - pad - (b["high"] - lo) / rng * (H - 2 * pad) - 14
            _col = "#e8890c" if _rs else "#198755"
            markers += (f'<path d="M{x:.1f},{y:.1f} l-5,9 l10,0 z" fill="{_col}">'
                        f'<title>{_lb} {sig["dt"]}</title></path>')

    labels = (f'<text x="{W-pad-70}" y="{pad-6:.0f}" font-size="11" fill="#99a">高 {hi:,.0f}</text>'
              f'<text x="{W-pad-70}" y="{H-pad+14:.0f}" font-size="11" fill="#99a">低 {lo:,.0f}</text>')
    return f"""
    <svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMid meet"
         style="background:#fbfcfe;border:1px solid #eef2f7;border-radius:8px;">
      {candles}
      {markers}
      {labels}
    </svg>
    <p class="note">红涨绿跌 ｜ 红=买入信号 绿=卖出信号 橙=止损平仓 ｜ K线过多已自动降采样；买卖点最多显示 {max_marks} 个</p>"""


def _build_html_report(strategy_name, name, main_code, s, equity_curve,
                       trades, start, end, ma_text, volume,
                       kline_sec, init_balance, extra_note="",
                       kline_history=None, signal_events=None, mult=1,
                       trade_marks=None, cost_note="", vol_text=None):
    """拼一个自包含（无外部依赖）的浅色主题 HTML 回测报告（详细版）。

    vol_text: 「策略参数」栏里手数的文案。默认写成「N 手」（老行为逐字不变）；
              v6 波动率预算下传「1~8 手（波动率预算）」，避免报告里
              固定写着「每次 1 手」而与实际下单手数自相矛盾。
    """
    ror = s.get("ror", 0) or 0
    annual = s.get("annual_yield", 0) or 0
    mdd = s.get("max_drawdown", 0) or 0
    win = s.get("winning_rate", 0) or 0
    plr = s.get("profit_loss_ratio", 0) or 0
    sharpe = s.get("sharpe_ratio", 0) or 0
    bal = s.get("balance", 0) or 0

    def pct(x):
        return f"{x * 100:.2f}%"
    def _money(v):
        if v is None or (isinstance(v, float) and v != v):
            return "—"
        return f"{v:,.0f}"
    def _int(v):
        if v is None or (isinstance(v, float) and v != v):
            return "—"
        return f"{int(v)}"
    def _num(v, nd=2):
        if v is None or (isinstance(v, float) and v != v):
            return "—"
        return f"{v:.{nd}f}"

    # 收益相关用红涨绿跌（中国习惯）
    ror_cls = "pos" if ror >= 0 else "neg"
    annual_cls = "pos" if annual >= 0 else "neg"

    kline_label = "日线" if kline_sec >= 24 * 60 * 60 else f"{kline_sec // 60}分钟"

    core_cards = f"""
      <div class="card"><div class="k">收益率</div><div class="v {ror_cls}">{pct(ror)}</div></div>
      <div class="card"><div class="k">年化收益</div><div class="v {annual_cls}">{pct(annual)}</div></div>
      <div class="card"><div class="k">最大回撤</div><div class="v neg">{pct(mdd)}</div></div>
      <div class="card"><div class="k">胜率</div><div class="v">{pct(win)}</div></div>
      <div class="card"><div class="k">盈亏比</div><div class="v">{_num(plr)}</div></div>
      <div class="card"><div class="k">夏普率</div><div class="v">{_num(sharpe)}</div></div>
      <div class="card"><div class="k">结束权益</div><div class="v">{_money(bal)}</div></div>
      <div class="card"><div class="k">初始资金</div><div class="v">{_money(init_balance)}</div></div>
    """

    ext_cards = f"""
      <div class="card"><div class="k">总手续费</div><div class="v">{_money(s.get('commission'))}</div></div>
      <div class="card"><div class="k">开仓次数</div><div class="v">{_int(s.get('open_times'))}</div></div>
      <div class="card"><div class="k">平仓次数</div><div class="v">{_int(s.get('close_times'))}</div></div>
      <div class="card"><div class="k">最大连盈天数</div><div class="v pos">{_int(s.get('max_cont_profit_days'))}</div></div>
      <div class="card"><div class="k">最大连亏天数</div><div class="v neg">{_int(s.get('max_cont_loss_days'))}</div></div>
      <div class="card"><div class="k">卡玛比率</div><div class="v">{_num(s.get('calmar_ratio'))}</div></div>
      <div class="card"><div class="k">索提诺比率</div><div class="v">{_num(s.get('sortino_ratio'))}</div></div>
      <div class="card"><div class="k">交易天数</div><div class="v">{_int(s.get('trading_days'))}</div></div>
    """

    equity = _equity_svg(equity_curve, init_balance)
    drawdown = _drawdown_svg(equity_curve)

    monthly = _monthly_table_html(equity_curve, init_balance)
    monthly_block = f"<h2>月度收益</h2>{monthly}"

    # 买卖点汇总表格已移除：买卖点改为在 K 线图上以标记显示（最多 50 个）
    trade_marks_block = ""

    kline_block = ""
    if kline_history:
        ksvg = _kline_svg(kline_history, signal_events)
        if ksvg:
            kline_block = f"<h2>K线图与买卖点</h2>{ksvg}"

    # 逐笔交易明细（配对 OPEN/CLOSE）
    rounds = _reconstruct_trades(trades, mult)
    if rounds:
        rows = ""
        for r in rounds:
            cls = "pos" if r["pnl"] >= 0 else "neg"
            sign = "+" if r["pnl"] >= 0 else ""
            dircolor = "buy" if r["dir"] == "BUY" else "sell"
            rows += (f"<tr><td>{_dt_str(r['open_dt'])}</td><td>{r['open_price']:.2f}</td>"
                     f"<td>{_dt_str(r['close_dt'])}</td><td>{r['close_price']:.2f}</td>"
                     f"<td class='{dircolor}'>{'多' if r['dir']=='BUY' else '空'}</td>"
                     f"<td>{r['volume']}</td>"
                     f"<td class='{cls}'>{sign}{r['pnl']:,.0f}</td>"
                     f"<td class='{cls}'>{sign}{r['pnl_pct']:.2f}%</td></tr>")
        trade_detail = f"""
      <h2>逐笔交易明细（共 {len(rounds)} 笔）</h2>
      <div class="scroll">
      <table>
        <thead><tr><th>开仓时间</th><th>开仓价</th><th>平仓时间</th><th>平仓价</th>
        <th>方向</th><th>手数</th><th>盈亏(元)</th><th>盈亏率</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      </div>"""
    else:
        trade_detail = ('<h2>逐笔交易明细</h2>'
                       '<p class="note">本次未采集到逐笔成交，无法配对'
                       '（可确认 api.get_trade() 是否返回成交记录）。</p>')

    # 原始成交流水（折叠）
    if trades:
        raw_rows = ""
        for t in trades:
            color = "buy" if t["direction"] == "BUY" else "sell"
            raw_rows += (f"<tr><td>{_dt_str(t['dt'])}</td><td class='{color}'>"
                         f"{t['direction']} {t['offset']}</td><td>{t['volume']}</td>"
                         f"<td>{t['price']:.2f}</td><td>{t['symbol']}</td></tr>")
        raw_block = (f"<details><summary>原始成交流水（{len(trades)} 笔）</summary>\n"
                     "      <div class=\"scroll\"><table>\n"
                     "        <thead><tr><th>时间</th><th>方向</th><th>手数</th><th>价格</th><th>合约</th></tr></thead>\n"
                     f"        <tbody>{raw_rows}</tbody>\n      </table></div></details>")
    else:
        raw_block = ""

    extra_block = ""
    if extra_note:
        extra_block = f'<h2>补充说明</h2>\n<p class="note">{extra_note}</p>'

    cost_block = ""
    if cost_note:
        cost_block = ('<div class="sub" style="margin-top:-14px;color:#b45309">'
                      f'成本口径：{cost_note}</div>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{name} {strategy_name} 回测报告</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif;
         background:#f5f7fa; color:#222; margin:0; padding:28px; }}
  .wrap {{ max-width:920px; margin:0 auto; background:#fff;
          border:1px solid #e3e8ef; border-radius:12px; padding:28px 32px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:#788; font-size:13px; margin-bottom:20px; }}
  .cards {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:14px 0 6px; }}
  .card {{ background:#f8fafc; border:1px solid #eef2f7; border-radius:10px;
          padding:14px 12px; text-align:center; }}
  .card .k {{ font-size:12px; color:#8a94a6; margin-bottom:6px; }}
  .card .v {{ font-size:19px; font-weight:700; }}
  .pos {{ color:#d4380d; }} .neg {{ color:#198755; }}
  .buy {{ color:#d4380d; font-weight:600; }} .sell {{ color:#198755; font-weight:600; }}
  h2 {{ font-size:16px; margin:24px 0 10px; border-left:4px solid #4c8bf5;
        padding-left:10px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ border-bottom:1px solid #eef2f7; padding:7px 8px; text-align:left; }}
  th {{ background:#f8fafc; color:#667; font-weight:600; }}
  .note {{ color:#99a; font-size:13px; background:#f8fafc; padding:10px 12px;
           border-radius:8px; }}
  .scroll {{ max-height:340px; overflow:auto; border:1px solid #eef2f7; border-radius:8px; }}
  .params {{ font-size:13px; color:#556; line-height:1.9; }}
  .params b {{ color:#222; }}
  details {{ margin-top:8px; }}
  details summary {{ cursor:pointer; color:#4c8bf5; font-size:13px; }}
  .foot {{ margin-top:26px; font-size:12px; color:#aab; border-top:1px solid #eef2f7;
           padding-top:12px; }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>{name} · {strategy_name} 回测报告</h1>
    <div class="sub">合约 {main_code} ｜ 周期 {kline_label} ｜ {start} ~ {end} ｜ 生成于 {datetime.now():%Y-%m-%d %H:%M}</div>
    {cost_block}

    <h2>核心指标</h2>
    <div class="cards">{core_cards}</div>
    <h2>扩展指标</h2>
    <div class="cards">{ext_cards}</div>

    {kline_block}

    <h2>资金权益曲线</h2>
    {equity}

    <h2>回撤曲线</h2>
    {drawdown}

    {monthly_block}

    {trade_marks_block}

    {trade_detail}

    {raw_block}

    <h2>策略参数</h2>
    <div class="params">
      策略：<b>{strategy_name}</b> ｜ 均线 <b>{ma_text}</b> ｜
      每次 <b>{vol_text or f"{volume} 手"}</b> ｜ 初始资金 <b>{init_balance:,.0f}</b> 元 ｜
      K线 <b>{kline_label}</b>
    </div>
    {extra_block}

    <div class="foot">本报告由天勤量化工程自动生成，仅用于策略历史回测展示，不构成任何投资建议。</div>
  </div>
</body>
</html>"""


def _equity_svg(series, init_balance):
    """把权益序列画成自包含 SVG。series: [(dt_str, balance), ...]"""
    if not series:
        return '<p class="note">（本次回测无权益序列数据）</p>'
    W, H, pad = 800, 280, 46
    vals = [v for _, v in series]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    n = len(series)
    pts = []
    for i, (_, v) in enumerate(series):
        x = pad + (i / (n - 1)) * (W - 2 * pad) if n > 1 else W / 2
        y = H - pad - (v - lo) / rng * (H - 2 * pad)
        pts.append((x, y))
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    # 初始资金水平参考线
    y0 = H - pad - (init_balance - lo) / rng * (H - 2 * pad)
    y0 = max(pad, min(H - pad, y0))

    labels = ""
    # 起点 / 终点标注
    labels += (f'<text x="{pad}" y="{pts[0][1]-8:.0f}" font-size="11" fill="#4c8bf5">'
               f'起 {vals[0]:,.0f}</text>')
    labels += (f'<text x="{W-pad-90}" y="{pts[-1][1]-8:.0f}" font-size="11" fill="#d4380d">'
               f'终 {vals[-1]:,.0f}</text>')
    labels += (f'<text x="{W-pad-40}" y="{pad-8:.0f}" font-size="11" fill="#99a">'
               f'高 {hi:,.0f}</text>')
    labels += (f'<text x="{W-pad-40}" y="{H-pad+14:.0f}" font-size="11" fill="#99a">'
               f'低 {lo:,.0f}</text>')

    return f"""
    <svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMid meet"
         style="background:#fbfcfe;border:1px solid #eef2f7;border-radius:8px;">
      <line x1="{pad}" y1="{y0:.0f}" x2="{W-pad}" y2="{y0:.0f}"
            stroke="#cdd6e3" stroke-width="1" stroke-dasharray="5,4"/>
      <polyline fill="none" stroke="#4c8bf5" stroke-width="2" points="{poly}"/>
      {labels}
    </svg>"""
