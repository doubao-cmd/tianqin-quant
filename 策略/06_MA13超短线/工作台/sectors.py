# -*- coding: utf-8 -*-
"""
sectors.py —— 板块配额（06 模拟盘 · 板块配额任务，预注册冻结表）

职责：
  1. SECTOR：冻结的「品种 → 板块」映射表（任务书 §3），覆盖全部 57 个 universe 品种。
  2. sector_of(key)：查板块；查不到 → 直接 raise KeyError（不静默归"其他"，不兜底）。
  3. cap_fill(ranked, n=6, cap=2)：纯函数，贪心填充，每板块最多 cap 个，
     直到凑满 n 个；凑不满则在返回值里显式标记 overflow。

★ 冻结：板块表是「模型选择」，不是事实；任务书要求冻结，本次之后不得以
  "回测更好" 为理由修改。
★ 一处完整性修正：原 §3 表漏列 天然橡胶 RU（universe 第 30 个，SHFE，属能源化工类）。
  RU 已补入「化工」。属遗漏补全，非调参——且 RU 在基线 51 周从未被选中
  （不在 26 品种并集），对结果零影响；补全只为满足「覆盖全部 57 个 universe 品种」
  的硬要求，并让 sector_of 对任意 tradable key 都不致误抛。
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# §3 冻结板块表（原样，仅补 RU）。覆盖全部 57 个 universe 品种。
# --------------------------------------------------------------------------
SECTOR = {
    "贵金属": ["AU", "AG"],
    "有色":   ["CU", "AL", "ZN", "PB", "NI", "SN", "AO", "BC"],
    "黑色":   ["RB", "HC", "I", "J", "JM", "SF", "SM", "SS"],
    "能源":   ["SC", "FU", "BU", "LU", "PG"],
    "化工":   ["MA", "TA", "EG", "EB", "PP", "L", "V", "PF", "UR", "RU"],
    "建材":   ["FG", "SA"],
    "农产品": ["M", "Y", "P", "RM", "OI", "C", "CS", "A", "B", "JD",
              "SR", "CF", "CY", "AP", "CJ", "LH", "PK", "RR"],
    "轻工":   ["SP", "NR"],
    "新能源": ["SI", "LC", "PS"],
}

# 反查：key -> 板块（构建一次）
_KEY_TO_SECTOR = {k.upper(): s for s, ks in SECTOR.items() for k in ks}


def sector_of(key: str) -> str:
    """返回品种所属板块。

    查不到直接 raise KeyError（任务书 §3：任何被选中品种必须在表里；
    不静默归"其他"、不兜底）。
    """
    s = _KEY_TO_SECTOR.get(str(key).upper())
    if s is None:
        raise KeyError(f"{key} 不在冻结板块表（SECTOR）中")
    return s


def cap_fill(ranked, n: int = 6, cap: int = 2):
    """贪心填充：每板块最多 cap 个，按 ranked 降序直到取满 n 个。

    参数
      ranked : [(key, score), ...] 已按 score 降序（最优在前）。
      n      : 目标取满个数（默认 6）。
      cap    : 单个板块上限（默认 2）。

    返回 (selected, overflow)
      selected : 选中的 key 列表（保持 ranked 的相对顺序）。
      overflow : bool。True 表示「因 cap 卡住 或 ranked 不足 而未能取满 n 个」。
                 —— 显式标记，不隐含；调用方必须检查。

    ★ 纯函数：无 IO、无随机、无全局状态；可离线单测。
    """
    sel: list[str] = []
    counts: dict[str, int] = {}
    for key, _score in ranked:
        sec = sector_of(key)            # 未知 key 会抛 KeyError（调用方须保证已 tradable）
        if counts.get(sec, 0) < cap:
            sel.append(key)
            counts[sec] = counts.get(sec, 0) + 1
            if len(sel) == n:
                break
    overflow = len(sel) < n
    return sel, overflow
