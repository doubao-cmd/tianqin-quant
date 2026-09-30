# -*- coding: utf-8 -*-
"""
分段基线回测：把练习区间切成若干子区间，分别跑同一个策略，
对比它在「趋势段 / 震荡段」下的表现差异。

【重要】本脚本不修改任何正式脚本的样本切分（IS_*/OOS_* 保持定稿不动），
        只按下方 SEGMENTS 指定区间调用 lib/engine；
        报告以 sample_tag 后缀落盘，不会覆盖 PVC_双均线_5min_in.html。

当前配置：01 双均线 · PVC（N1=5 / N2=20 / 5分钟 / 1手），与
          策略/01_双均线/回测/PVC/双均线_回测.py 完全一致（策略逻辑零改动）。

用法：改 SEGMENTS 后跑
  python 工具/分段回测.py
（也可直接双击 工具/分段回测.bat）
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

# ==================== 参数（与 01 双均线正式脚本一致） ====================
SYMBOL_KEY = "V"
N1, N2 = 5, 20
VOLUME = 1
KLINE_SEC = 5 * 60
INIT_BALANCE = 200000
WARMUP_DAYS = 7              # 回测起点前移天数，仅用于均线预热（预热期不交易）
# =======================================================================

# 要跑的 K 线周期（每个周期 × 每个分段 各跑一遍；周期已自动进报告文件名，不会互相覆盖）
# 5 分钟的结果已于本日先跑过（PVC_双均线_5min_6月 / _5-7月 / _7-9月），此处只补 30 分钟对照
KLINE_SEC_LIST = [30 * 60]

# 分段（名称, 评估起, 评估止）
# 2026-09-17 用户指令「现在只做6月份行情」-> 只保留 6 月
SEGMENTS = [
    ("6月", date(2026, 6, 1), date(2026, 6, 30)),
]


def cross_signal(now_f, now_s, pre_f, pre_s):
    """与 策略/01_双均线 完全相同的金叉/死叉逻辑（照搬，不改）"""
    if pre_f <= pre_s and now_f > now_s:
        return 1
    if pre_f >= pre_s and now_f < now_s:
        return -1
    return 0


if __name__ == "__main__":
    for ksec in KLINE_SEC_LIST:
        per = f"{ksec // 60}min"
        for tag, s, e in SEGMENTS:
            print("\n" + "#" * 72)
            print(f"# 分段基线 [{tag} · {per}]  评估区间 {s} ~ {e}   （TqSim 初始资金 {INIT_BALANCE}）")
            print("#" * 72, flush=True)
            engine.run_backtest(
                SYMBOL_KEY, cross_signal, N1, N2, VOLUME,
                s - timedelta(days=WARMUP_DAYS), e, INIT_BALANCE, ksec,
                strategy_name="双均线", open_html=False, eval_start=s,
                sample_tag=tag,
            )
            print(f">>> [{tag} · {per}] 完成 -> 回测结果/01_双均线/PVC/PVC_双均线_{per}_{tag}.html/.txt", flush=True)
    print("\n全部分段跑完。", flush=True)
