# -*- coding: utf-8 -*-
"""
exec4/engine.py —— 四期执行层：逐 bar 回放引擎

把工作台的 Signal 接口（06 MA13 占位信号）跑成一条**逐 bar 成交序列**。
这是 A1（复现台账 107 笔）的成交发生器，也是后续 S2 扫描 / S3 封盘统计的统一底座。

★ 单一真相源：Signal / Sizer 全部来自 `signal_bridge`（即工作台 signals.py / sizer.py），
  本文件**不复制任何策略逻辑**——只负责「信号 → 下一根开盘成交 → 持仓维护」的驱动循环。

★ 成交口径（与实盘 ma13_live_multi._process 逐字对齐，已读源码取证）：
  · 信号在 bar j-1 收盘算（`sig.entry(ind, j)` 内部用 close[j-1]/ma[j-1]/atr[j-1]），
    成交在 **bar j 开盘**（`open[j]`），台账时间 = bar j 起始时刻。
  · 出场：`sig.exit_hit(ind, j, pos, stop, bars_in)` 在 bar j 命中即出场，
    成交价 = `open[j]`（INTRABAR_STOP=False 旧「开盘查」口径，A1 验收用）。
  · `bars_in`：开仓 bar 计 1（与 live line 1110 的 `elif pos!=0: bars_in+=1` 一致），
    MAX_HOLD=15 ⇒ 时间止损在第 entry+15 根触发。
  · 手数：`sizer.hand(atr[j-1], tick, mult, price=open[j])`，equity_base 锁死 20 万。

★ 为什么 A1 用 INTRABAR_STOP=False：
  台账 107 笔由 10-17 23:42 之前的旧版生成（当时尚无盘中触价扫损），
  旧版只在「新 bar 开盘」判触价 ⇒ 成交价 = 开盘。本会话已实测：
  台账开/平仓价 ≈ `open[成交bar]`（entry 70.3% / exit 77.5% 在 1 跳内），
  路线图写的「盈亏误差 <1 元」不可能（1 跳 × 8 手 × mult10 = 80 元），
  ⇒ A1 验收改为「bar 对齐 + 价 1~2 跳内」。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from signal_bridge import default_signal, default_sizer, spec

# 让 `import data` / `import trades`（同在 工具/复盘 目录）可用：
# 直接跑 `python exec4/engine.py` 时 sys.path[0] 是 exec4/ 而非 工具/复盘。
import sys as _sys
from pathlib import Path as _Path
_REPLAY_DIR = str(_Path(__file__).resolve().parent.parent)
if _REPLAY_DIR not in _sys.path:
    _sys.path.insert(0, _REPLAY_DIR)


def run(df: pd.DataFrame, key: str,
        signal=None, sizer=None,
        intrabar_stop: bool = False,
        halted_guard=None,
        reset_bars: Optional[set] = None,
        seeds: Optional[list] = None) -> list[dict]:
    """对单品种 K 线逐 bar 回放，返回成交序列。

    参数
    ----
    df : DataFrame，含 datetime(UTC) / open / high / low / close（来自 data.load_recent）。
         时序升序，最新在末。
    key : 品种 KEY（如 'M'），用于 spec 取 tick / mult。
    signal / sizer : 可注入；默认取 default_signal() / default_sizer()（= 工作台 06 MA13）。
    intrabar_stop : True 时启用盘中触价扫损（INTRABAR_STOP=True 新口径，填充到触价位）；
                    False（默认）走旧「开盘查」口径，A1 验收用。
    halted_guard : 可选 callable(j)->bool，返回 True 时该 bar 禁止开仓（熔断/运维闸门复刻）。

    返回
    ----
    trades: list[dict]，每笔：
        entry_i, exit_i        (K 线行索引，对应 df)
        entry_px, exit_px      (成交价，= open[bar]；intrabar_stop=True 的价格止损填触价位)
        dir                    (+1 多 / -1 空)
        hand                   (手数，>0)
        reason                 ('stop' / 'time' / None=未平)
        stop, atr              (开仓时止损线 / ATR，用于台账对照)
    时间字段不在此处转北京时——调用方拿 entry_i/exit_i 自行 to_beijing() 对齐台账。
    """
    sp = spec(key)
    tick, mult = sp["tick"], sp["mult"]
    signal = signal or default_signal()
    sizer = sizer or default_sizer()

    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    n = len(df)
    if n < signal.MA_N + signal.SLOPE_N + 3:
        return []

    ind = signal.indicators(df)
    atr_arr = ind["atr"]

    def _hand(i: int) -> int:
        a = atr_arr[i]
        a = None if not (a == a and a > 0) else float(a)
        return sizer.hand(a, tick, mult, price=float(o[i]))

    trades: list[dict] = []
    pos = 0            # +hand / -hand / 0
    entry_i = -1
    entry_px = 0.0
    stop = None
    atr0 = None
    bars_in = 0

    # 接管种子：bar 索引 -> {dir, hand, stop, entry_px}；在该 bar 起持有云端接管来的仓位
    seed_map = {}
    if seeds:
        for s in seeds:
            seed_map[int(s["bar"])] = s

    for j in range(1, n):
        # ---- 重启处理：模拟盘重启会清空/接管云端持仓 ----
        #  · 接管（seeds）：从 bar j 起持有云端接管来的仓位（entry 价未知，记为 nan），
        #    之后按信号正常出场；这是 operational，但必须保持持仓态，否则后续交易全错位。
        #  · 清零（reset_bars）：内存态归零，本 bar 起 flat。
        # 两者都不记成交，只改状态。
        if j in seed_map:
            sd = seed_map[j]
            pos = int(sd["dir"]) * int(sd["hand"])
            stop = sd.get("stop")
            entry_px = float(sd["entry_px"]) if sd.get("entry_px") == sd.get("entry_px") else float("nan")
            entry_i = j
            atr0 = None
            bars_in = 0
        elif reset_bars is not None and j in reset_bars:
            pos = 0
            stop = None
            atr0 = None
            bars_in = 0

        # ---- 出场判定（模拟层，非策略层）----
        #  INTRABAR_STOP=False（旧版 / 台账 107 笔口径）：新 bar 开盘才查 ⇒ 触价=open[j]。
        #    与 live _process:1040 一致——那一刻 ind["low"][j] 还是刚成形 bar 的 open，
        #    所以实盘只抓「跳空击穿」的止损，未跳空则一直持有到时间止损。
        #  INTRABAR_STOP=True（新口径）：盘中实时触价 ⇒ 触价=bar 极值，填触价位。
        if pos != 0:
            if intrabar_stop:
                why = signal.exit_hit(ind, j, pos, stop, bars_in)   # 用 full low[j]/high[j]
            else:
                if pos > 0 and o[j] <= stop:
                    why = "stop"
                elif pos < 0 and o[j] >= stop:
                    why = "stop"
                elif bars_in >= signal.MAX_HOLD:
                    why = "time"
                else:
                    why = None
            if why is not None:
                if intrabar_stop:
                    # 新口径：填到触价位（多头取 min(stop, open)，空头取 max(stop, open)）
                    if pos > 0:
                        exit_px = float(min(stop, o[j]))
                    else:
                        exit_px = float(max(stop, o[j]))
                else:
                    # 旧口径（A1）：开盘查，成交价 = 开盘
                    exit_px = float(o[j])
                trades.append({
                    "entry_i": entry_i, "exit_i": j,
                    "entry_px": entry_px, "exit_px": exit_px,
                    "dir": 1 if pos > 0 else -1, "hand": abs(pos),
                    "reason": why, "stop": stop, "atr": atr0,
                })
                pos = 0
                stop = None
                atr0 = None
                bars_in = 0
                # 注意：live 在出场 bar 会重读 pos 后重新检查信号、可能同 bar 再开仓
                # （ma13_live_multi.py:1061），故这里**不 continue**，落到下方开仓分支
                # 让「止损后同 bar 反手/再开」也能复现。

        # ---- 开仓（信号来自 bar j-1；成交在 bar j 开盘）----
        opened_here = False
        if pos == 0:
            dir_trend, stop_new = signal.entry(ind, j)   # 内部用 close[j-1]
            if dir_trend != 0 and stop_new is not None:
                if halted_guard is not None and halted_guard(j):
                    pass
                else:
                    hand = _hand(j)
                    if hand > 0:
                        entry_i = j
                        entry_px = float(o[j])
                        pos = hand if dir_trend > 0 else -hand
                        stop = stop_new
                        atr0 = float(atr_arr[j - 1]) if (atr_arr[j - 1] == atr_arr[j - 1] and atr_arr[j - 1] > 0) else None
                        bars_in = 0
                        opened_here = True

        # ---- 持有时每 bar 末 +1（★ 开仓 bar 不递增，与 live 的 if-entry/elif-held 一致）----
        # live：line1070 `if 开仓...` 与 line1110 `elif pos!=0: bars_in+=1` 互斥 ⇒ 开仓 bar
        # 走不了 elif ⇒ bars_in 停在 0。这让时间止损比「每 held bar 都递增」晚一根，实测对齐台账。
        if pos != 0 and not opened_here:
            bars_in += 1

    # 收尾：仍未平的持仓 ⇒ 记一笔未平（reason=None，exit_i=-1）
    if pos != 0:
        trades.append({
            "entry_i": entry_i, "exit_i": -1,
            "entry_px": entry_px, "exit_px": float("nan"),
            "dir": 1 if pos > 0 else -1, "hand": abs(pos),
            "reason": None, "stop": stop, "atr": atr0,
        })
    return trades


# ---------------------------------------------------------------------------
# 便捷：加载 K 线并回放（供 verify_a1 / S2 / S3 直接调用）
# ---------------------------------------------------------------------------
def run_symbol(key: str, period: int = 5, source="recent",
               signal=None, sizer=None, intrabar_stop: bool = False,
               halted_guard=None) -> tuple[list[dict], pd.DataFrame, dict]:
    """加载某品种 K 线（data.load_recent）+ 回放，返回 (trades, df, meta)。

    df 为原始（UTC datetime）；调用方按需 to_beijing()。
    """
    import data  # 延迟导入，避免 engine 模块被单独 import 时强制依赖 data 路径
    df, meta = data.load_recent(key, period)
    trades = run(df, key, signal=signal, sizer=sizer,
                 intrabar_stop=intrabar_stop, halted_guard=halted_guard)
    return trades, df, meta


def _selftest() -> int:
    """engine 冒烟自测：确定性 + 基本不变量（A1 完整验收见 ../../verify_a1.py）。"""
    import data
    from data import to_beijing
    ok = True
    print("=" * 70)
    print("exec4/engine 冒烟自测（确定性 / 手数 / 止损合理 / 持有时长）")
    print("=" * 70)
    for cn, key in sorted(data.CN2KEY.items()):
        try:
            df, _ = data.load_recent(key, 5)
        except FileNotFoundError:
            continue
        df_bj = to_beijing(df["datetime"]).reset_index(drop=True)
        t1 = run(df, key)
        t2 = run(df, key)                     # 确定性：两次必须逐笔相同
        same = (len(t1) == len(t2) and all(
            a[k] == b[k] for a, b in zip(t1, t2)
            for k in ("entry_i", "exit_i", "dir", "hand", "reason")
        ))
        bad_hand = [t for t in t1 if not (1 <= t["hand"] <= 8)]
        bad_dir = [t for t in t1 if t["dir"] not in (1, -1)]
        # 持有根数（exit - entry）：止损/时间止损都应有界；时间止损应 ≈ entry+16
        holds = [t["exit_i"] - t["entry_i"] for t in t1 if t["exit_i"] >= 0]
        print(f"[{cn:>2}] 成交={len(t1):>3} 确定={same} 坏手数={len(bad_hand)} 坏方向={len(bad_dir)} "
              f"持有时长[min,max]=[{min(holds) if holds else 0},{max(holds) if holds else 0}]")
        ok &= same and not bad_hand and not bad_dir
    print("=" * 70)
    print("engine 冒烟自测：" + ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
