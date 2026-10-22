# -*- coding: utf-8 -*-
"""
截面基差因子 —— 39 品种「合约参数 + 成本」表（**自动生成，勿手改**）

生成器：工具/_截面因子_基差/建品种参数.py（可重跑、不连网，读 数据/*.csv 缓存）
生成时间：2026-10-13
数据源：A. akshare futures_fees_info（openctp）→ 合约乘数 / 最小跳动 / 保证金率
        B. akshare futures_comm_info（九期网）  → 手续费口径（固定元/手 or 万分之比例）

★ 成本模型（**双模式**，本表相对 config_symbols.COST 的关键差异）：
      单边每手成本(元) = comm_fixed + comm_rate × 价格 × mult
  实测：MA / RB / SA / JM / JD / LH / AG / CU / HC / I / J / FU / BU / PB / WR / SP
  等品种是**按成交额「万分之 N」收费**，不是固定元/手。
  config_symbols.COST 存的是「某时点价格折算出的元数」常数 ⇒ 价格一涨成本即失真
  （其注释只点名了 AG/CU，实测远不止两个）。
  ⇒ 本表按费率建模，回测按**当期价格**计成本，不用价格快照。

⚠️ margin_rate 是**当期主力合约的交易所保证金率快照**（含临近交割上调，如 AG 近月 22%），
   仅供粗略参考；本回测手数口径 = 等名义（见预注册卡 §二），**不使用保证金约束**。

⚠️ 品种池为频率预算阶段冻结的 39 个，**不许增删**（增删＝择优）。

★ 模块结构：`PARAMS` = **39 个品种的全量参数**（数据资产）；
           `POOL`   = **冻结的截面池 = 37 个**（剔除 PM / WR，证据见 §流动性）。
  ⇒ 参数与池子分离：将来若调整池子规则，参数不必重建。

⚠️ 费率/价格为 **2026-10 快照**（第三方数据源，可能含期货公司加收）：
   · 历史回测存在**费率漂移**偏差（费率随交易所公告变动，如玻璃 FG 2026-06 由 6 元降为 2 元）；
   · 如需精确，应改读交易所当期公告，而不是提高本表精度。
   · 实测参考：与 config_symbols.COST 对拍 6/9 个品种一致（<15%）；
     MA 本表高 63%（万分之1 按现价折算 vs 现有常数 2.0）、RB 低 23%、V 低 33%。
"""

# key: 名称 / 交易所 / 乘数 / 最小跳动 / 固定手续费(元/手,单边) / 比例费率(万分之一的小数) / 保证金率(%)
PARAMS = {
    "A":  {"name": "黄大豆1号", "ex": "DCE", "mult": 10, "tick": 1,
           "comm_fixed": 2, "comm_rate": 0, "margin_rate": 7},  # 现价单边 2.000 元
    "AG": {"name": "白银", "ex": "SHFE", "mult": 15, "tick": 1,
           "comm_fixed": 0, "comm_rate": 5e-05, "margin_rate": 22},  # 现价单边 11.018 元
    "AL": {"name": "铝", "ex": "SHFE", "mult": 5, "tick": 5,
           "comm_fixed": 3, "comm_rate": 0, "margin_rate": 11},  # 现价单边 3.000 元
    "AU": {"name": "黄金", "ex": "SHFE", "mult": 1000, "tick": 0.02,
           "comm_fixed": 20, "comm_rate": 0, "margin_rate": 16},  # 现价单边 20.000 元
    "BU": {"name": "石油沥青", "ex": "SHFE", "mult": 10, "tick": 1,
           "comm_fixed": 0, "comm_rate": 5e-05, "margin_rate": 14},  # 现价单边 2.213 元
    "C":  {"name": "黄玉米", "ex": "DCE", "mult": 10, "tick": 1,
           "comm_fixed": 1.2, "comm_rate": 0, "margin_rate": 7},  # 现价单边 1.200 元
    "CF": {"name": "一号棉花", "ex": "CZCE", "mult": 5, "tick": 5,
           "comm_fixed": 4.3, "comm_rate": 0, "margin_rate": 7},  # 现价单边 4.300 元
    "CU": {"name": "铜", "ex": "SHFE", "mult": 5, "tick": 10,
           "comm_fixed": 0, "comm_rate": 5e-05, "margin_rate": 11},  # 现价单边 27.733 元
    "CY": {"name": "棉纱", "ex": "CZCE", "mult": 5, "tick": 5,
           "comm_fixed": 1, "comm_rate": 0, "margin_rate": 15},  # 现价单边 1.000 元
    "EG": {"name": "乙二醇", "ex": "DCE", "mult": 10, "tick": 1,
           "comm_fixed": 3, "comm_rate": 0, "margin_rate": 9},  # 现价单边 3.000 元
    "FG": {"name": "玻璃", "ex": "CZCE", "mult": 20, "tick": 1,
           "comm_fixed": 2, "comm_rate": 0, "margin_rate": 9},  # 现价单边 2.000 元
    "FU": {"name": "燃料油", "ex": "SHFE", "mult": 10, "tick": 1,
           "comm_fixed": 0, "comm_rate": 5e-05, "margin_rate": 18},  # 现价单边 2.330 元
    "HC": {"name": "热轧卷板", "ex": "SHFE", "mult": 10, "tick": 1,
           "comm_fixed": 0, "comm_rate": 0.0001, "margin_rate": 7},  # 现价单边 3.255 元
    "I":  {"name": "铁矿石", "ex": "DCE", "mult": 100, "tick": 0.5,
           "comm_fixed": 0, "comm_rate": 0.0001, "margin_rate": 8},  # 现价单边 6.825 元
    "J":  {"name": "冶金焦炭", "ex": "DCE", "mult": 100, "tick": 0.5,
           "comm_fixed": 0, "comm_rate": 0.0001, "margin_rate": 12},  # 现价单边 20.275 元
    "JD": {"name": "鸡蛋", "ex": "DCE", "mult": 10, "tick": 1,
           "comm_fixed": 0, "comm_rate": 0.00015, "margin_rate": 7},  # 现价单边 5.355 元
    "JM": {"name": "焦煤", "ex": "DCE", "mult": 60, "tick": 0.5,
           "comm_fixed": 0, "comm_rate": 0.0001, "margin_rate": 12},  # 现价单边 9.303 元
    "L":  {"name": "线型低密度聚乙烯", "ex": "DCE", "mult": 5, "tick": 1,
           "comm_fixed": 1, "comm_rate": 0, "margin_rate": 7},  # 现价单边 1.000 元
    "M":  {"name": "豆粕", "ex": "DCE", "mult": 10, "tick": 1,
           "comm_fixed": 1.5, "comm_rate": 0, "margin_rate": 7},  # 现价单边 1.500 元
    "MA": {"name": "甲醇N", "ex": "CZCE", "mult": 10, "tick": 1,
           "comm_fixed": 0, "comm_rate": 0.0001, "margin_rate": 7},  # 现价单边 3.254 元
    "NI": {"name": "镍", "ex": "SHFE", "mult": 1, "tick": 10,
           "comm_fixed": 3, "comm_rate": 0, "margin_rate": 12},  # 现价单边 3.000 元
    "OI": {"name": "菜籽油", "ex": "CZCE", "mult": 10, "tick": 1,
           "comm_fixed": 2, "comm_rate": 0, "margin_rate": 7},  # 现价单边 2.000 元
    "P":  {"name": "棕榈油", "ex": "DCE", "mult": 10, "tick": 2,
           "comm_fixed": 2.5, "comm_rate": 0, "margin_rate": 8},  # 现价单边 2.500 元
    "PB": {"name": "铅", "ex": "SHFE", "mult": 5, "tick": 5,
           "comm_fixed": 0, "comm_rate": 4e-05, "margin_rate": 11},  # 现价单边 3.228 元
    "PM": {"name": "普通小麦", "ex": "CZCE", "mult": 50, "tick": 1,
           "comm_fixed": 0, "comm_rate": 1e-06, "margin_rate": 40},  # 现价单边 0.156 元
    "PP": {"name": "聚丙烯", "ex": "DCE", "mult": 5, "tick": 1,
           "comm_fixed": 1, "comm_rate": 0, "margin_rate": 7},  # 现价单边 1.000 元
    "RB": {"name": "螺纹钢", "ex": "SHFE", "mult": 10, "tick": 1,
           "comm_fixed": 0, "comm_rate": 0.0001, "margin_rate": 7},  # 现价单边 3.083 元
    "RM": {"name": "菜籽粕", "ex": "CZCE", "mult": 10, "tick": 1,
           "comm_fixed": 1.5, "comm_rate": 0, "margin_rate": 7},  # 现价单边 1.500 元
    "RU": {"name": "天然橡胶", "ex": "SHFE", "mult": 10, "tick": 5,
           "comm_fixed": 3, "comm_rate": 0, "margin_rate": 9},  # 现价单边 3.000 元
    "SF": {"name": "硅铁", "ex": "CZCE", "mult": 5, "tick": 2,
           "comm_fixed": 2, "comm_rate": 0, "margin_rate": 7},  # 现价单边 2.000 元
    "SM": {"name": "锰硅", "ex": "CZCE", "mult": 5, "tick": 2,
           "comm_fixed": 2, "comm_rate": 0, "margin_rate": 7},  # 现价单边 2.000 元
    "SN": {"name": "锡", "ex": "SHFE", "mult": 1, "tick": 10,
           "comm_fixed": 3, "comm_rate": 0, "margin_rate": 14},  # 现价单边 3.000 元
    "SP": {"name": "漂针浆", "ex": "SHFE", "mult": 10, "tick": 2,
           "comm_fixed": 0, "comm_rate": 2e-05, "margin_rate": 7},  # 现价单边 0.961 元
    "SR": {"name": "白砂糖", "ex": "CZCE", "mult": 10, "tick": 1,
           "comm_fixed": 2, "comm_rate": 0, "margin_rate": 6},  # 现价单边 2.000 元
    "TA": {"name": "精对苯二甲酸", "ex": "CZCE", "mult": 5, "tick": 2,
           "comm_fixed": 3, "comm_rate": 0, "margin_rate": 7},  # 现价单边 3.000 元
    "V":  {"name": "聚氯乙烯", "ex": "DCE", "mult": 5, "tick": 1,
           "comm_fixed": 1, "comm_rate": 0, "margin_rate": 7},  # 现价单边 1.000 元
    "WR": {"name": "线材", "ex": "SHFE", "mult": 10, "tick": 1,
           "comm_fixed": 0, "comm_rate": 4e-05, "margin_rate": 12},  # 现价单边 1.371 元
    "Y":  {"name": "豆油", "ex": "DCE", "mult": 10, "tick": 1,
           "comm_fixed": 2.5, "comm_rate": 0, "margin_rate": 7},  # 现价单边 2.500 元
    "ZN": {"name": "锌", "ex": "SHFE", "mult": 5, "tick": 5,
           "comm_fixed": 3, "comm_rate": 0, "margin_rate": 11},  # 现价单边 3.000 元
}


# ⛔ 已剔除（**跑前定死**，证据 = 样本期 2019-2021 主力日均持仓，见「流动性核查」）：
#     PM 普麦  持仓中位 10 手、仅覆盖 20% 交易日 ⇒ 报价是挂牌价非成交价，基差无意义
#     WR 线材  持仓中位 50 手、覆盖 81%         ⇒ 实际无法成交（日均成交仅 54 手）
#   ★ 39 品种分布的自然断点在 50 ↔ 8,406 之间（相差 168 倍）：
#     第 3 低的 CY 棉纱 8,406 手（日均成交额 ≈2 亿）= 偏低但月度调仓可执行 ⇒ **保留**。
EXCLUDED = ("PM", "WR")

POOL = []
for k in PARAMS:
    if k not in EXCLUDED:
        POOL.append(k)

# ---- 复核用：任一参数缺失立刻报错（不留兜底默认值 —— 项目血泪条款）----
_miss = [k for k in POOL if k not in PARAMS]
if _miss:
    raise KeyError(f"池内品种缺参数：{_miss}")
if len(POOL) != 37:
    raise AssertionError(f"冻结池应为 37 个，实为 {len(POOL)}")


def cost_per_hand(key, price):
    """单边每手成本（元）—— 按**当期价格**算（比例费率品种随价格变化）。

    ⚠️ 查不到品种**直接抛 KeyError**，不返回默认值（见 2026-10-12 血泪条款：兜底默认值＝静默失效的温床）。
    """
    p = PARAMS[str(key).upper()]
    return p["comm_fixed"] + p["comm_rate"] * float(price) * p["mult"]


def cost_points(key, price, slippage_ticks=1.0):
    """单边成本折算成「点」（÷乘数）—— 与《进场体检》口径一致，便于跨品种比较。

    slippage_ticks：滑点（跳数），默认 1 跳（= tick × 1）。
    """
    p = PARAMS[str(key).upper()]
    return cost_per_hand(key, price) / p["mult"] + slippage_ticks * p["tick"]


def roundtrip_points(key, price, slippage_ticks=1.0):
    """往返（开+平）成本，单位「点」。"""
    return 2.0 * cost_points(key, price, slippage_ticks)

