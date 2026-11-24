# -*- coding: utf-8 -*-
"""
排列看板 · 判据核心
==============================================================
只有一件事：给定带 ma5/ma10/ma20 的 DataFrame，算出「四线同向」状态和状态转移。

★ 为什么单独抽出来：scan_daily_push.py（每日看板）和 review_events.py（事件回访）
  必须用**同一份判据**。如果各写一套，回访统计出来的胜率就不是看板实际发出的信号，
  而是"另一个策略"的表现——这种漂移很难发现，但会让所有结论失效。

多头 MA5>MA10>MA20>MA60；空头 MA5<MA10<MA20<MA60。
a60    = 当日状态（1 / -1 / 0）
a60_trig = 状态发生转移的那一天（0→±1，或 ±1→∓1）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MA_N = 60
MIN_BARS = 159          # MA60 至少要有 100 个有效点，持续天数才有意义


def aligned(o: pd.DataFrame) -> pd.DataFrame:
    """四线同向：MA5>MA10>MA20>MA60 为多，反向为空。只在状态转移日记 trig。"""
    m5, m10, m20 = o["ma5"], o["ma10"], o["ma20"]
    ma60 = o["close"].rolling(MA_N).mean()
    o["ma60"] = ma60
    o["a60"] = np.where(
        (m5 > m10) & (m10 > m20) & (m20 > ma60), 1,
        np.where((m5 < m10) & (m10 < m20) & (m20 < ma60), -1, 0),
    )
    prev = o["a60"].shift(1).fillna(0)
    o["a60_trig"] = np.where((o["a60"] != 0) & (o["a60"] != prev), o["a60"], 0)
    return o


def streak(o: pd.DataFrame, i: int, direction: int) -> int:
    """从第 i 行往回数，同方向连续了多少天。"""
    n = 0
    for j in range(i, -1, -1):
        if int(o.at[j, "a60"] or 0) == direction:
            n += 1
        else:
            break
    return n


def early_series(o: pd.DataFrame) -> np.ndarray:
    """苗头判据的向量化版本，返回长度与 o 相同的 1 / -1 / 0 数组。

    ★ 判据只在这里定义一次。`early_of` 也是取它的第 i 个元素——
      如果在别处再写一套「苗头」条件，回访统计出来的就不是看板发出的信号了。
      2026-11-24 苗头从上线起就没触发过，正是因为当时两处条件互相矛盾而无人发现。
    """
    m5, m10, m20, m60 = (o[k].to_numpy(dtype=float)
                         for k in ("ma5", "ma10", "ma20", "ma60"))
    pm5 = np.concatenate(([np.nan], m5[:-1]))
    pm10 = np.concatenate(([np.nan], m10[:-1]))
    up = (m5 > m10) & (pm5 <= pm10) & (m10 > m20) & (m20 <= m60)
    dn = (m5 < m10) & (pm5 >= pm10) & (m10 < m20) & (m20 >= m60)
    return np.where(up, 1, np.where(dn, -1, 0))


def early_of(o: pd.DataFrame, i: int) -> int:
    """苗头：MA5 刚穿 MA10，中期（MA10>MA20）已转，但长期（MA20 vs MA60）还没转。

    ★ 2026-11-24 修正：旧定义是「MA5 穿 MA10 且**后三线已排好**（m10>m20>m60）」，
      但那样四条线同时成立，a60 当天就等于 1，已经算「新多」了；
      再要求 a60==0，二者互斥 —— **苗头永远为空**。
      证据：事件回访跑了 41 个品种 744 个事件，苗头 0 个，才把这个矛盾翻出来。
      现在改成「短中期已转、长期未转」：m5 刚穿 m10、m10>m20、m20<=m60。
      这才是排列成立之前的中间态，也才真正有提前量。

    逐行调用请改用 early_series(o)[i]，避免每次重算整列。
    """
    return int(early_series(o)[i])
