# -*- coding: utf-8 -*-
"""
MA13 超短线 · 单品种入口（薄包装）★2026-10-08 改为转发到 ma13_live_multi
========================================================================
【为什么有这个文件】
12 个单品种入口脚本（模拟盘/<品种>/MA13超短线_模拟盘.py）都写的是
    from ma13_live_core import run_live_ma13
本文件必须保留，否则那 12 个入口全部 ImportError。

【为什么是薄包装而不是独立实现（2026-10-08 决定）】
本文件原先是**独立重写**的一套 150 行实现，与 `ma13_live_multi.py`（612 行）
信号逻辑逐字相同、但**外围防护全缺**：

    能力                        旧 core   multi
    云端对账/RECOVER/SYNC          ❌ 0 命中  ✅ 45 处命中
    账户级回撤熔断 fuse             ❌        ✅
    撮合等待 FIX_WAIT_SEC=30       ❌        ✅（今天下午的假接管就是靠它）
    守护重连 / 心跳独立线程         ❌        ✅

⇒ 两份实现各自演进 = **静默分叉风险**。2026-10-08 修止损夹紧 bug 时，
   焦煤回测 + multi 都改了，**core 漏了**，就是这个风险的实证。
⇒ 现在统一为**唯一实现 `ma13_live_multi.run_live_multi`**，
   本文件只做参数转发，**不再持有任何策略逻辑**（含止损计算）。

【以后改策略只需改一个地方】
信号/出场/止损/手数 —— 全部只存在于 `ma13_live_multi.py` 与
`回测/<品种>/MA13超短线_回测.py`（回测是离线复算，不参与实盘）。

禁未来函数：只用已收盘 bar；bar 收盘后判定，立即执行（≈下一根开盘）。
默认 ALLOW_TRADE 由调用方入口脚本决定（当前 12 个入口均为 False 观察模式）。
"""
import sys, os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# ★ 唯一实现。策略逻辑全部在里面，本文件不复制、不重写。
from ma13_live_multi import run_live_multi


def run_live_ma13(symbol_key, allow_trade=False, web_gui=True,
                  record_dir=None, heartbeat_sec=300):
    """单品种运行 MA13（转发到多品种实现的单品种模式）。

    ⚠️ 签名与旧版**逐字兼容**，12 个入口脚本无需任何改动。
       `record_dir` / `heartbeat_sec` 原样透传；
       缺省 record_dir 由 multi 落到 模拟盘/台账（旧版落 模拟盘/实测记录/…，
       这是**唯一的行为差异**，且是刻意的：多品种共用一个台账目录才能做
       账户级熔断与权益快照，见 multi 的 _LiveRecorder / 熔断状态.json）。
    """
    return run_live_multi(symbol_keys=[symbol_key],
                          allow_trade=allow_trade,
                          web_gui=web_gui,
                          record_dir=record_dir,
                          heartbeat_sec=heartbeat_sec)
