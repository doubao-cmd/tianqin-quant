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
# ⚠️ 这里只是「旧池」的硬编码底稿。**新品种请直接加 品种池/<key>.py** ——
#    本文件末尾的 `_并入品种池()` 会在 import 时自动登记（setdefault，不覆盖这里）。
SYMBOLS = {
    "MA": ("甲醇",   "KQ.m@CZCE.MA",  10, 1),
    "M":  ("豆粕",   "KQ.m@DCE.m",    10, 1),
    "RB": ("螺纹钢", "KQ.m@SHFE.rb",  10, 1),
    "V":  ("PVC",    "KQ.m@DCE.v",     5, 1),
    "SA": ("纯碱",   "KQ.m@CZCE.SA",  20, 1),
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
}


# ===========================================================
# 新结构并入：品种池/ 里的品种自动登记（2026-09-25 补）
# ===========================================================
# ⚠️ 为什么需要这段：engine 里 `config_symbols.get()` 与 `config_symbols.COST`
#    是**硬依赖**，而品种池是后建的新结构 ⇒ 只加 品种池/<key>.py 的话，
#    `python 组合/布林带__RU.py 回测 IS` 会直接
#        SystemExit: 品种代号写错了：RU
#    也就是说「加品种 = 加一个文件」当时**并不成立**（2026-09-25 查出的缺陷）。
#
# 策略：`setdefault` —— 只登记还没登记的品种，**绝不覆盖**上面已核验的旧值。
#    ⇒ 旧 5 个品种（MA/M/RB/V/SA）行为逐字不变，零回归；
#      新品种（RU/EB/JM/P/LH/JD/SR）自动可用。
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

