# -*- coding: utf-8 -*-
"""
==========================================================
 小c 的五个品种配置（天勤合约代码）
==========================================================

天勤代码规则和金字塔不一样，别搞混：

  金字塔       天勤主连              天勤具体合约
  MA00    →    KQ.m@CZCE.MA     →    CZCE.MA609
  M00     →    KQ.m@DCE.m       →    DCE.m2609
  RB00    →    KQ.m@SHFE.rb     →    SHFE.rb2610
  V00     →    KQ.m@DCE.v       →    DCE.v2609
  SA00    →    KQ.m@CZCE.SA     →    CZCE.SA2609

要点：
  1. 郑商所（CZCE）品种代码【大写】：MA、SA、CF、TA
  2. 大商所（DCE）/ 上期所（SHFE）品种代码【小写】：m、v、rb
  3. 交易所前缀必须带：CZCE. / DCE. / SHFE.
  4. KQ.m@ 开头 = 主连合约（自动换月），KQ.i@ 开头 = 指数合约
  5. 回测时【主连不能直接下单】，要用 quote.underlying_symbol 取当时的真实月份合约
     （脚本里已经处理好了，你不用管）

最小变动价位（一跳多少钱）：
  甲醇 1 元/吨、豆粕 1、螺纹 1  → 一跳 10 元
  PVC 1 元/吨                   → 一跳 5 元（5 吨/手）
  纯碱 1 元/吨                  → 一跳 20 元（20 吨/手，比其他贵一倍）
"""

# key: (中文名, 天勤主连代码, 合约乘数, 最小变动价位)
# ★ 2026-09-27：**12 个品种全部固化在本表**（原为 5 个硬编码 + 品种池/ 运行时并入）。
#    起因：用户决定把「池化三件套」（策略池/ 品种池/ 组合/）收进 _归档/，
#    而本文件曾被 品种池/ 硬依赖 ⇒ 目录一收，7 个新品种会静默失效。
#    现在本表自给自足：**品种池/ 在不在都跑得通**（已实测 12/12 可用）。
SYMBOLS = {
    "MA": ("甲醇",   "KQ.m@CZCE.MA",  10, 1),
    "M":  ("豆粕",   "KQ.m@DCE.m",    10, 1),
    "RB": ("螺纹钢", "KQ.m@SHFE.rb",  10, 1),
    "V":  ("PVC",    "KQ.m@DCE.v",     5, 1),
    "SA": ("纯碱",   "KQ.m@CZCE.SA",  20, 1),
    # ---- 2026-09-27 固化：以下 7 个原在 品种池/*.py ----
    "EB": ("苯乙烯",   "KQ.m@DCE.eb",   5, 1.0),
    "JD": ("鸡蛋",     "KQ.m@DCE.jd",  10, 1.0),
    "JM": ("焦煤",     "KQ.m@DCE.jm",  60, 0.5),
    "LH": ("生猪",     "KQ.m@DCE.lh",  16, 5.0),
    "P":  ("棕榈油",   "KQ.m@DCE.p",   10, 1.0),
    "RU": ("天然橡胶", "KQ.m@SHFE.ru", 10, 5.0),
    "SR": ("白糖",     "KQ.m@CZCE.SR", 10, 1.0),
}


def get(key):
    """按缩写取品种信息，写错会给出友好提示"""
    key = key.upper()
    if key not in SYMBOLS:
        raise SystemExit(f"品种代号写错了：{key}\n可用：{', '.join(SYMBOLS.keys())}")
    return SYMBOLS[key]


# 每手手续费（元，单边；开仓/平仓各收一次）。回测成本模型用，请按你实盘券商费率调整。
# 估值（甲醇/豆粕/螺纹/PVC/纯碱 常见零售费率），仅供回测参考：
COST = {
    "MA": 2.0,    # 甲醇
    "M":  1.5,    # 豆粕
    "RB": 4.0,    # 螺纹钢
    "V":  1.5,    # PVC
    "SA": 4.0,    # 纯碱
    # ---- 2026-09-27 固化：以下 7 个原在 品种池/*.py 的 `成本` 字段 ----
    "EB": 1.0,     # 苯乙烯
    "JD": 5.802,   # 鸡蛋
    "JM": 9.1,     # 焦煤
    "LH": 34.83,   # 生猪
    "P":  2.5,     # 棕榈油
    "RU": 3.0,     # 天然橡胶
    "SR": 2.0,     # 白糖
}


# ===========================================================
# 新结构并入：品种池/ 里的品种自动登记（2026-09-25 补；2026-09-27 降为「可选增强」）
# ===========================================================
# ★ 现状（2026-09-27）：上表 `SYMBOLS` / `COST` 已固化全部 12 个品种，
#   所以本段**不再是必需品** —— `品种池/` 目录不在时下面的 `return` 会直接跳过，
#   12 个品种照常可用。把 `品种池/` 收进 `_归档/` 就是靠这个前提才安全的。
#
# 保留本段的原因：往后若还想用「加品种 = 加一个 品种池/<key>.py」的写法，它仍然有效。
# 策略：`setdefault` —— 只登记表里没有的品种，**绝不覆盖**上面已核验的值。
def _并入品种池():
    import importlib.util
    import os
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "品种池")
    if not os.path.isdir(d):
        return
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"_品种池_{fn[:-3]}",
                                                          os.path.join(d, fn))
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            p = getattr(m, "品种", None)
            if not isinstance(p, dict) or "key" not in p:
                continue
            SYMBOLS.setdefault(p["key"], (p["中文名"], p["主连"], p["乘数"], p["一跳"]))
            COST.setdefault(p["key"], float(p.get("成本", 0.0) or 0.0))
        except Exception:
            # 单个品种文件有问题不影响其它品种，也不影响引擎启动
            continue


_并入品种池()

