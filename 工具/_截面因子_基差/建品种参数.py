# -*- coding: utf-8 -*-
"""
截面基差因子 —— 39 品种「成本 / 合约参数」表构建器（可重跑、不连网）。

为什么单独建表，不动 config_symbols.py：
  1) config_symbols 是主线配置（engine + 8 个策略共用），加 27 个品种会改变
     SYMBOLS 语义、影响 `_并入品种池()` 的 setdefault 逻辑，属于污染主线；
  2) 本表成本模型与主线**口径不同**：主线 COST 是「元/手」常数快照，
     而实测发现 MA/RB/SA/JM/LH/JD/AG/CU… 其实是**按成交额万分之收费**，
     常数建模会让成本随价格漂移（config_symbols 注释已警告过这点）；
  3) 截面因子是**离线研究**，独立参数表更清晰、可复核。

数据源（两个独立源，交叉验证）：
  A. akshare futures_fees_info（openctp）    → 合约乘数 / 最小跳动 / 保证金率
  B. akshare futures_comm_info（九期网）      → 手续费口径（固定元/手 or 万分之比例）

成本模型（与两源口径对齐）：
    单边每手成本(元) = comm_fixed + comm_rate × price × mult
  其中 comm_rate 为「万分之 N」换算的小数（N=1 ⇒ 0.0001）。
  ⇒ 回测时按**当期价格**算，而不是吃一个价格快照。

输出：数据/品种参数_39.json  +  屏幕对照表（含与现有 12 品种 COST 的对拍）
"""
import json
import os
import re
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
import akshare as ak

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "数据")
OUT_JSON = os.path.join(DATA, "品种参数.json")
LOG = os.path.join(HERE, "品种参数_构建日志.txt")
os.makedirs(DATA, exist_ok=True)

# 频率预算阶段冻结的 39 品种池（跑前定死，不许增删）—— 本脚本内一律叫 POOL_ALL，
# 避免与「生成模块里的 POOL(37)」混淆
POOL_ALL = "A AG AL AU BU C CF CU CY EG FG FU HC I J JD JM L M MA NI OI P PB PM PP RB RM RU SF SM SN SP SR TA V WR Y ZN".split()

# ⛔ 流动性剔除名单（2026-10-13 跑前定死；证据 = 查历史流动性.py → 数据/样本期流动性.csv）
#    PM 普麦 样本期持仓中位 10 手、覆盖仅 20% 交易日；WR 线材 50 手、日均成交 54 手 ⇒ 实际无法成交。
EXCLUDED = ("PM", "WR")

# 现有 12 品种 COST（元/手，单边；来自 config_symbols.COST）—— 仅用于对拍
CUR_COST = {"MA": 2.0, "M": 1.5, "RB": 4.0, "V": 1.5, "SA": 4.0,
            "EB": 1.0, "JD": 5.802, "JM": 9.1, "LH": 34.83,
            "P": 2.5, "RU": 3.0, "SR": 2.0}


def load_openctp():
    p = os.path.join(DATA, "合约费用_openctp.csv")
    if not os.path.exists(p):
        ak.futures_fees_info().to_csv(p, index=False, encoding="utf-8-sig")
    d = pd.read_csv(p, encoding="utf-8-sig")
    d["PC"] = d["品种代码"].astype(str).str.upper()
    return d


def load_jiuqi():
    p = os.path.join(DATA, "手续费_九期网.csv")
    if not os.path.exists(p):
        ak.futures_comm_info(symbol="所有").to_csv(p, index=False, encoding="utf-8-sig")
    d = pd.read_csv(p, encoding="utf-8-sig")
    d["PC"] = d["合约代码"].astype(str).str.upper().str.extract(r"^([A-Z]+)")
    return d


def pick_main(df, note_col=None, vol_col=None):
    """选「主力合约」代表行。

    ★ 为什么必须选主力：实测同一品种**不同合约费率不一样**（交易所对近月/交割月上调），
      例如 RB 主力 rb2701 = 万分之1，而 rb2702/2703 = 万分之0.2。
      截面因子交易的是主力，必须用主力费率，否则成本失真。
    优先级：备注含"主力" → 否则持仓量最大 → 否则成交量最大 → 否则第一行。
    """
    if note_col and note_col in df.columns:
        m = df[df[note_col].astype(str).str.contains("主力", na=False)]
        if len(m):
            return m.iloc[0]
    for c in ([vol_col] if vol_col else []) + ["持仓量", "成交量"]:
        if c in df.columns and pd.to_numeric(df[c], errors="coerce").max() > 0:
            return df.loc[pd.to_numeric(df[c], errors="coerce").idxmax()]
    return df.iloc[0]


def parse_fee(row):
    """解析九期网一行 → (comm_fixed, comm_rate)。

    格式实测：
        "1.5"                  → 固定 1.5 元/手
        "1/万分之 (4.2元)"      → 比例，万分之 1  ⇒ 0.0001
        "1.5/万分之 (5.8元)"    → 比例，万分之 1.5 ⇒ 0.00015
    以「万分之」列为准（非 NaN = 比例），文本列作备用交叉校验。
    """
    rate_col = row.get("手续费标准-平昨-万分之", np.nan)
    txt = str(row.get("手续费标准-平昨-元", "") or "")
    try:
        rate_col = float(rate_col)
    except (TypeError, ValueError):
        rate_col = np.nan

    if not np.isnan(rate_col) and rate_col > 0:
        return 0.0, rate_col

    m = re.match(r"^\s*([0-9.]+)\s*/\s*万分之", txt)
    if m:
        return 0.0, float(m.group(1)) / 10000.0
    m = re.match(r"^\s*([0-9.]+)\s*$", txt)
    if m:
        return float(m.group(1)), 0.0
    return np.nan, np.nan


def gen_py(df):
    """把参数表落成可 import 的正式模块（入库资产，供回测引用）。"""
    import datetime
    L = []
    L.append('# -*- coding: utf-8 -*-')
    L.append('"""')
    L.append('截面基差因子 —— 39 品种「合约参数 + 成本」表（**自动生成，勿手改**）')
    L.append('')
    L.append('生成器：工具/_截面因子_基差/建品种参数.py（可重跑、不连网，读 数据/*.csv 缓存）')
    L.append('生成时间：%s' % datetime.date.today().isoformat())
    L.append('数据源：A. akshare futures_fees_info（openctp）→ 合约乘数 / 最小跳动 / 保证金率')
    L.append('        B. akshare futures_comm_info（九期网）  → 手续费口径（固定元/手 or 万分之比例）')
    L.append('')
    L.append('★ 成本模型（**双模式**，本表相对 config_symbols.COST 的关键差异）：')
    L.append('      单边每手成本(元) = comm_fixed + comm_rate × 价格 × mult')
    L.append('  实测：MA / RB / SA / JM / JD / LH / AG / CU / HC / I / J / FU / BU / PB / WR / SP')
    L.append('  等品种是**按成交额「万分之 N」收费**，不是固定元/手。')
    L.append('  config_symbols.COST 存的是「某时点价格折算出的元数」常数 ⇒ 价格一涨成本即失真')
    L.append('  （其注释只点名了 AG/CU，实测远不止两个）。')
    L.append('  ⇒ 本表按费率建模，回测按**当期价格**计成本，不用价格快照。')
    L.append('')
    L.append('⚠️ margin_rate 是**当期主力合约的交易所保证金率快照**（含临近交割上调，如 AG 近月 22%），')
    L.append('   仅供粗略参考；本回测手数口径 = 等名义（见预注册卡 §二），**不使用保证金约束**。')
    L.append('')
    L.append('⚠️ 品种池为频率预算阶段冻结的 39 个，**不许增删**（增删＝择优）。')
    L.append('')
    L.append('★ 模块结构：`PARAMS` = **39 个品种的全量参数**（数据资产）；')
    L.append('           `POOL`   = **冻结的截面池 = 37 个**（剔除 PM / WR，证据见 §流动性）。')
    L.append('  ⇒ 参数与池子分离：将来若调整池子规则，参数不必重建。')
    L.append('')
    L.append('⚠️ 费率/价格为 **2026-10 快照**（第三方数据源，可能含期货公司加收）：')
    L.append('   · 历史回测存在**费率漂移**偏差（费率随交易所公告变动，如玻璃 FG 2026-06 由 6 元降为 2 元）；')
    L.append('   · 如需精确，应改读交易所当期公告，而不是提高本表精度。')
    L.append('   · 实测参考：与 config_symbols.COST 对拍 6/9 个品种一致（<15%）；')
    L.append('     MA 本表高 63%（万分之1 按现价折算 vs 现有常数 2.0）、RB 低 23%、V 低 33%。')
    L.append('"""')
    L.append('')
    L.append('# key: 名称 / 交易所 / 乘数 / 最小跳动 / 固定手续费(元/手,单边) / 比例费率(万分之一的小数) / 保证金率(%)')
    L.append('PARAMS = {')
    for _, r in df.iterrows():
        L.append('    %-5s {"name": "%s", "ex": "%s", "mult": %g, "tick": %g,' %
                 ('"%s":' % r["key"], r["name"], r["exchange"], r["mult"], r["tick"]))
        L.append('           "comm_fixed": %g, "comm_rate": %g, "margin_rate": %g},  # 现价单边 %.3f 元' %
                 (r["comm_fixed"], r["comm_rate"], r["margin_rate"], r["cost_单边_元_现价"]))
    L.append('}')
    L.append('')
    L.append('')
    L.append('# ⛔ 已剔除（**跑前定死**，证据 = 样本期 2019-2021 主力日均持仓，见「流动性核查」）：')
    L.append('#     PM 普麦  持仓中位 10 手、仅覆盖 20% 交易日 ⇒ 报价是挂牌价非成交价，基差无意义')
    L.append('#     WR 线材  持仓中位 50 手、覆盖 81%         ⇒ 实际无法成交（日均成交仅 54 手）')
    L.append('#   ★ 39 品种分布的自然断点在 50 ↔ 8,406 之间（相差 168 倍）：')
    L.append('#     第 3 低的 CY 棉纱 8,406 手（日均成交额 ≈2 亿）= 偏低但月度调仓可执行 ⇒ **保留**。')
    L.append('EXCLUDED = ("PM", "WR")')
    L.append('')
    L.append('POOL = []')
    L.append('for k in PARAMS:')
    L.append('    if k not in EXCLUDED:')
    L.append('        POOL.append(k)')
    L.append('')
    L.append('# ---- 复核用：任一参数缺失立刻报错（不留兜底默认值 —— 项目血泪条款）----')
    L.append('_miss = [k for k in POOL if k not in PARAMS]')
    L.append('if _miss:')
    L.append('    raise KeyError(f"池内品种缺参数：{_miss}")')
    L.append('if len(POOL) != 37:')
    L.append('    raise AssertionError(f"冻结池应为 37 个，实为 {len(POOL)}")')
    L.append('')
    L.append('')
    L.append('def cost_per_hand(key, price):')
    L.append('    """单边每手成本（元）—— 按**当期价格**算（比例费率品种随价格变化）。')
    L.append('')
    L.append('    ⚠️ 查不到品种**直接抛 KeyError**，不返回默认值（见 2026-10-12 血泪条款：'
             '兜底默认值＝静默失效的温床）。')
    L.append('    """')
    L.append('    p = PARAMS[str(key).upper()]')
    L.append('    return p["comm_fixed"] + p["comm_rate"] * float(price) * p["mult"]')
    L.append('')
    L.append('')
    L.append('def cost_points(key, price, slippage_ticks=1.0):')
    L.append('    """单边成本折算成「点」（÷乘数）—— 与《进场体检》口径一致，便于跨品种比较。')
    L.append('')
    L.append('    slippage_ticks：滑点（跳数），默认 1 跳（= tick × 1）。')
    L.append('    """')
    L.append('    p = PARAMS[str(key).upper()]')
    L.append('    return cost_per_hand(key, price) / p["mult"] + slippage_ticks * p["tick"]')
    L.append('')
    L.append('')
    L.append('def roundtrip_points(key, price, slippage_ticks=1.0):')
    L.append('    """往返（开+平）成本，单位「点」。"""')
    L.append('    return 2.0 * cost_points(key, price, slippage_ticks)')
    L.append('')
    p = os.path.join(HERE, "品种参数.py")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    print(f"生成参数模块 {p}（PARAMS={len(df)} 品种；POOL={len(df)-len(EXCLUDED)} 品种）")


def main():
    oct_ = load_openctp()
    jq = load_jiuqi()

    print("=" * 96)
    print("39 品种参数表构建（源A=openctp 乘数/跳动/保证金；源B=九期网 手续费口径）")
    print("=" * 96)

    rows, warn = [], []
    for k in POOL_ALL:
        go = oct_[oct_["PC"] == k]
        gj = jq[jq["PC"] == k]
        if not len(go):
            warn.append(f"{k}: openctp 缺")
            continue

        # ---- 乘数 / 一跳：品种内应一致（不一致则报告，取主力行）----
        r_o = pick_main(go, note_col="备注")
        mult = float(r_o["合约乘数"])
        tick = float(r_o["最小跳动"])
        margin = float(r_o["做多保证金率"]) * 100

        mults = {float(x) for x in go["合约乘数"].dropna().unique()}
        ticks = {float(x) for x in go["最小跳动"].dropna().unique()}
        if len(mults) > 1:
            warn.append(f"{k}: 乘数不唯一 {sorted(mults)}")
        if len(ticks) > 1:
            warn.append(f"{k}: 一跳不唯一 {sorted(ticks)}")

        # ---- 手续费：优先九期网（有明确「万分之」列）----
        fee_src = "九期网-主力"
        fee_note = ""
        if len(gj):
            r_j = pick_main(gj, note_col="备注")
            cfix, crate = parse_fee(r_j)
            if np.isnan(cfix):
                # 该行解析失败 → 遍历同品种其它行
                for _, rr in gj.iterrows():
                    cfix, crate = parse_fee(rr)
                    if not np.isnan(cfix):
                        break
        else:
            cfix, crate = np.nan, np.nan

        if np.isnan(cfix):  # 退回 openctp（已知该源「费用/手」列对小品种不准，仅兜底）
            fee_src = "openctp(兜底)"
            cfix, crate = 0.0, float(r_o["开仓费率"] or 0)
            warn.append(f"{k}: 九期网缺/解析失败，退回 openctp 费率 {crate}")

        # 费率一致性提示（同品种不同合约费率不同 = 交易所对近月/交割月上调，属正常）
        if len(gj):
            parsed = sorted({round(x, 8) for x in (parse_fee(rr)[1] for _, rr in gj.iterrows())
                             if not np.isnan(x)})
            if len(parsed) > 1:
                fee_note = f"品种内费率多档{parsed}（已取主力）"

        price = float(r_o["最新价"] or 0)
        # ⚠️ 用**持仓量**而非成交量判流动性：实测 openctp 的「成交量」字段
        #    在 JD/SF/SM 等品种上**全合约恒为 0**（数据源缺陷），持仓量则正常。
        oi = float(pd.to_numeric(r_o.get("持仓量"), errors="coerce") or 0)
        per_hand = cfix + crate * price * mult
        rows.append({"key": k, "name": str(r_o["品种名称"]),
                     "exchange": str(r_o["交易所"]),
                     "mult": mult, "tick": tick,
                     "comm_fixed": round(cfix, 6), "comm_rate": round(crate, 10),
                     "margin_rate": round(margin, 2),
                     "price_快照": price, "oi_快照": oi,
                     "cost_单边_元_现价": round(per_hand, 4),
                     "fee_src": fee_src, "fee_note": fee_note})

    df = pd.DataFrame(rows)
    df.to_json(OUT_JSON, orient="records", force_ascii=False, indent=2)
    gen_py(df)

    pd.set_option("display.width", 220)
    print(f"\n【39 品种参数表】共 {len(df)} 个（缺 {39-len(df)} 个）")
    print(df[["key", "name", "exchange", "mult", "tick", "comm_fixed", "comm_rate",
              "margin_rate", "cost_单边_元_现价"]].to_string(index=False))

    # ---------- 与现有 12 品种 COST 对拍 ----------
    print("\n" + "=" * 96)
    print("★ 与现有 12 品种 config_symbols.COST 对拍（元/手，单边）")
    print("=" * 96)
    print(f"{'品种':<5}{'现有COST':>10}{'本表折算':>10}{'费率模式':>16}  判定")
    out_pool = []
    for k, cur in CUR_COST.items():
        r = df[df["key"] == k]
        if not len(r):
            out_pool.append(k)
            continue
        r = r.iloc[0]
        b = r["cost_单边_元_现价"]
        mode = f"万分之{round(r['comm_rate']*10000, 3)}" if r["comm_rate"] > 0 else "固定元/手"
        dev = abs(b - cur) / max(cur, 1e-9)
        if dev < 0.15:
            verdict = "✅ 一致(<15%)"
        elif b > cur:
            verdict = f"本表比现有高{dev*100:.0f}%（现有成本假设偏乐观）"
        else:
            verdict = f"本表比现有低{dev*100:.0f}%（现有偏保守）"
        print(f"{k:<5}{cur:>10.3f}{b:>10.3f}{mode:>16}  {verdict}")
    if out_pool:
        print(f"\n（{', '.join(out_pool)} 不在截面 39 池内，无需对拍）")

    if warn:
        print("\n⚠️ 需人工确认：")
        for w in warn:
            print("  -", w)

    # ---------- 成本量级体检 ----------
    # 「往返成本 ÷ 一跳毛利」= 需要几跳的波动才能覆盖成本（含/不含 1 跳滑点两版）。
    # 仅作描述性统计，不设阈值择优。
    print("\n" + "=" * 112)
    print("★ 成本量级体检（往返 = 开+平；1 跳毛利 = tick × mult；滑点按预注册卡 §二 取 1 跳）")
    print("★ 流动性用**持仓量**判（openctp 的「成交量」在 JD/SF/SM 等品种上恒为 0，不可用）")
    print("=" * 112)
    print(f"{'品种':<5}{'现价':>9}{'1跳毛利':>9}{'单边成本':>9}{'往返/1跳':>9}"
          f"{'含1跳滑点':>10}{'主力持仓量':>12}  备注")
    rows2 = []
    for _, r in df.iterrows():
        ot = r["tick"] * r["mult"]
        rt = 2 * r["cost_单边_元_现价"]
        r1 = rt / ot if ot > 0 else np.nan
        r2 = (rt + 2 * ot) / ot if ot > 0 else np.nan  # 含 1 跳滑点往返
        rows2.append((r["key"], r["price_快照"], ot, r["cost_单边_元_现价"], r1, r2, r["oi_快照"]))
    rows2.sort(key=lambda x: (-x[5] if not np.isnan(x[5]) else 0))
    low = []
    for k, px, ot, c, r1, r2, oi in rows2:
        flag = ""
        if oi < 1000:
            flag = "⚠️ 僵尸（当期持仓<1000）"
            low.append(k)
        elif oi < 10000:
            flag = "· 流动性偏低（<1万）"
            low.append(k)
        elif r2 > 4:
            flag = "成本重（含滑点>4 跳）"
        print(f"{k:<5}{px:>9.1f}{ot:>9.2f}{c:>9.3f}{r1:>9.2f}{r2:>10.2f}{oi:>12,.0f}  {flag}")
    print(f"\n（注1：成本只描述量级；截面因子是**排序**逻辑，成本同时压缩多空两端，见预注册卡 §四 判定 2。）")
    print(f"（注2：持仓量/价格为 2026-10 快照，**非 2019-2021 样本期值**，仅供识别僵尸品种。）")
    print(f"（注3：本表费率为 2026-10 快照；历史回测存在**费率漂移**偏差（费率会随交易所公告变动）。）")
    if low:
        print(f"\n⚠️ 当期快照下流动性偏低（{len(low)} 个）：{', '.join(low)}")
        print(f"   ⚠️ 快照仅供提示。**剔除决策依据是「样本期 2019-2021 实测」**（见 查历史流动性.py）：")
        print(f"      EXCLUDED = {EXCLUDED} ⇒ 冻结池 POOL = {len(POOL_ALL)-len(EXCLUDED)} 个")
        print(f"      （PM 样本期持仓中位 10 手 / WR 50 手；第 3 低的 CY 8,406 手保留）")
    print(f"\n落盘 {OUT_JSON}")


class _Tee:
    """同时输出到终端与内存，最后以 LF 落盘构建日志
    （直接 `> 日志.txt` 重定向会写成 CRLF，违反项目行尾铁律）。"""

    def __init__(self, real):
        self.real, self.buf = real, []

    def write(self, s):
        self.buf.append(s)
        self.real.write(s)

    def flush(self):
        self.real.flush()


if __name__ == "__main__":
    import sys
    _t = _Tee(sys.stdout)
    sys.stdout = _t
    try:
        main()
    finally:
        sys.stdout = _t.real
        with open(LOG, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(_t.buf))
        print(f"（构建日志已以 LF 落盘：{LOG}）")
