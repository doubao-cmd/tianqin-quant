# -*- coding: utf-8 -*-
"""
R1 · T2 合约参数表（out/contracts.csv）
=========================================
要求（任务书 §三 T2）：tick_value 与手续费**必须查实**，不许凭记忆填；
上期所（FU/BU）平今必须单独一列；每行有来源标注。

本表只放「可引用来源」的数，来源分三档写清楚：
  [C1] 合约细则：乘数 / 最小变动价位（交易所品种细则）
  [F1] 交易所标准费率：官方收费一览表 / 调整公告（带文号）
  [F2] 期货公司加收：公开公示的零售档（申银万国 20260715 公示 = 交易所×5；
       建信期货 20260422 互联网开户公示 ≈ 交易所×10）

★ 为什么手续费要分三档：
  卡 §三 要求 C_fee = 交易所手续费 + 平今差异 + **期货公司加收**。
  但本项目**没有账户结算单**可核实际加收档 ⇒ 只能给「交易所标准（下界）」
  与「公开零售档（上界）」两条线，主表按交易所标准算，敏感性按零售档算。
  这是 R1 唯一一处「输入不确定」，在 R1_结论.md 首段显式声明。

★ EG / JM / JD 的交易所标准未能取到公告原文：
  用申万公示 ÷5 反算校验（申万该三品种恰好都是 5 倍关系），并打「反算」标记。

用法：python r1_t2_contracts.py
"""
import csv
from pathlib import Path

OUT = Path(__file__).parent / "out"
KLINE = OUT / "kline"

REF_CONTRACT = {   # 当前在交易的合约（用于取参考价算比例手续费）
    "FU": "SHFE.fu2701", "BU": "SHFE.bu2611", "MA": "CZCE.MA701", "M": "DCE.m2701",
    "SA": "CZCE.SA701", "EG": "DCE.eg2611", "JM": "DCE.jm2701", "P": "DCE.p2701",
    "SR": "CZCE.SR701", "JD": "DCE.jd2611",
}

# symbol: (name, exch, tier, mult, tick, fee_mode, fee_open_rate, fee_ct_rate,
#          fee_open_yuan, fee_ct_yuan, src_contract, src_fee)
TABLE = {
    "FU": ("燃料油", "SHFE", "A", 10, 1, "ratio", 0.0001, 0.0003, 0, 0,
           "[C1] 上期所燃料油期货合约：10吨/手、1元/吨 ⇒ tick_value=10",
           "[F1] 上期发〔2026〕95号(2026-03-09)：交易手续费=成交金额万分之一，"
           "日内平今仓=万分之三，自2026-03-11起"),
    "BU": ("石油沥青", "SHFE", "A", 10, 1, "ratio", 0.00005, 0.00005, 0, 0,
           "[C1] 上期所石油沥青期货合约：10吨/手、1元/吨 ⇒ tick_value=10",
           "[F1] 上期所收费一览表(2026-02-12)：石油沥青0.05‰；未见平今差异公告 ⇒ 同开仓"),
    "MA": ("甲醇", "CZCE", "A", 10, 1, "ratio", 0.0001, 0.0001, 0, 0,
           "[C1] 郑商所甲醇期货合约：10吨/手、1元/吨 ⇒ tick_value=10",
           "[F1] 郑商所收费一览表：甲醇=成交金额万分之一（平今同）"),
    "M": ("豆粕", "DCE", "A", 10, 1, "fixed", 0, 0, 1.5, 1.5,
          "[C1] 大商所豆粕期货合约：10吨/手、1元/吨 ⇒ tick_value=10",
          "[F1] 大商所发〔2021〕475号：豆粕1.5元/手，日内与非日内同价"),
    "SA": ("纯碱", "CZCE", "A", 20, 1, "ratio", 0.0001, 0.0001, 0, 0,
           "[C1] 郑商所纯碱期货合约：20吨/手、1元/吨 ⇒ tick_value=20",
           "[F1] 郑商函〔2026〕476号(2026-06-02)：交易手续费与日内平今仓均=成交金额万分之一"),
    "EG": ("乙二醇", "DCE", "A", 10, 1, "fixed", 0, 0, 3.0, 3.0,
           "[C1] 大商所乙二醇期货合约：10吨/手、1元/吨 ⇒ tick_value=10",
           "[F1-反算] 交易所3元/手（未取到公告原文；申万公示15元/手÷5 反算一致）"),
    "JM": ("焦煤", "DCE", "B", 60, 0.5, "ratio", 0.0001, 0.0001, 0, 0,
           "[C1] 大商所焦煤期货合约：60吨/手、0.5元/吨 ⇒ tick_value=30",
           "[F1-反算] 交易所=成交金额万分之一（未取到公告原文；申万公示万5÷5 反算一致）"),
    "P": ("棕榈油", "DCE", "B", 10, 2, "fixed", 0, 0, 2.5, 2.5,
          "[C1] 大商所《棕榈油期货业务细则》：10吨/手、最小变动2元/吨 ⇒ tick_value=20"
          "（★ 项目 品种参数.py 记 tick=1，与细则不符，此处以细则为准，B栏不影响结论）",
          "[F1] 大商所发〔2021〕475号：棕榈油2.5元/手"),
    "SR": ("白糖", "CZCE", "B", 10, 1, "fixed", 0, 0, 2.0, 0.0,
           "[C1] 郑商所白糖期货合约：10吨/手、1元/吨 ⇒ tick_value=10",
           "[F1] 郑商所收费一览表：白糖2元/手；日内平今仓不收取"),
    "JD": ("鸡蛋", "DCE", "B", 10, 1, "ratio", 0.00015, 0.00015, 0, 0,
           "[C1] 大商所鸡蛋期货业务细则：5吨/手、报价单位元/500千克、tick 1元/500千克 "
           "⇒ 每手=10个报价单位 ⇒ tick_value=10（项目表 mult=10 的语义是「每手报价单位数」）",
           "[F1-反算] 交易所=成交金额万分之1.5（未取到公告原文；申万公示万7.5÷5 反算一致）"),
}

# [F2] 期货公司加收档：与交易所的倍数关系（来自公开公示）
#   申银万国 20260715 公示：M7.5 SA万5 MA万5 SR10元 P12.5 EG15元 JM万5 JD万7.5
#     ⇒ 与交易所标准恰好 5 倍（SR/MA 平今另有口径，主表只用开仓侧）
#   建信期货 20260422 互联网开户公示：M15 SA35元 MA20元 EG40元 P25元 SR30元
#     JM万6/平今万18 JD万30 ⇒ 约 10 倍（自助开户默认档）
TIER2_MULT = 5.0
TIER3_MULT = 10.0
# 建信口径里与倍数不符的品种，按公示绝对值覆盖（开仓侧，元）
TIER3_OVERRIDE = {"SA": 35.0, "M": 15.0, "MA": 20.0, "EG": 40.0, "P": 25.0, "SR": 30.0}
TIER3_NOTE = {
    "FU": "建信公示未覆盖（页面截断）⇒ 按×10 假设",
    "BU": "建信公示未覆盖（页面截断）⇒ 按×10 假设",
    "JM": "建信公示 万6（平今万18）⇒ 按 6× 实填",
    "JD": "建信公示 万30 ⇒ 按 20× 实填",
}


def last_close(contract):
    p = KLINE / f"{contract}_5min.csv"
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return float(rows[-1]["close"]) if rows else None


def main():
    out = []
    for sym, v in TABLE.items():
        (name, exch, tier, mult, tick, mode, r_open, r_ct, y_open, y_ct,
         src_c, src_f) = v
        price = last_close(REF_CONTRACT[sym])
        tick_value = mult * tick

        def fee_y(rate, yuan, k=1.0):
            base = yuan if mode == "fixed" else rate * price * mult
            return base * k

        f1_open = fee_y(r_open, y_open, 1.0)
        f1_ct = fee_y(r_ct, y_ct, 1.0)
        if sym in TIER3_OVERRIDE:
            f3_open = TIER3_OVERRIDE[sym]
        elif sym == "JM":
            f3_open = fee_y(r_open, y_open, 6.0)
        elif sym == "JD":
            f3_open = fee_y(r_open, y_open, 20.0)
        else:
            f3_open = f1_open * TIER3_MULT
        f2_open = f1_open * TIER2_MULT

        out.append({
            "symbol": sym, "name": name, "exchange": exch, "tier": tier,
            "mult": mult, "tick": tick, "tick_value": round(tick_value, 4),
            "ref_contract": REF_CONTRACT[sym], "ref_price": price,
            "fee_mode": mode, "fee_open_rate": r_open, "fee_ct_rate": r_ct,
            "fee_open_yuan": round(f1_open, 4), "fee_ct_yuan": round(f1_ct, 4),
            "fee_t2_open_yuan": round(f2_open, 4),
            "fee_t3_open_yuan": round(f3_open, 4),
            "fee_t3_note": TIER3_NOTE.get(sym, "建信公示绝对值"),
            "src_contract": src_c, "src_fee": src_f,
        })

    cols = list(out[0].keys())
    with open(OUT / "contracts.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out)

    print(f"{'品种':4s}{'tv':>5s}{'交易所':>8s}{'×5档':>8s}{'零售档':>9s}  参考价")
    for r in out:
        print(f"{r['symbol']:4s}{r['tick_value']:5.0f}{r['fee_open_yuan']:8.2f}"
              f"{r['fee_t2_open_yuan']:8.2f}{r['fee_t3_open_yuan']:9.2f}  {r['ref_price']}")
    print(f"\n-> {OUT/'contracts.csv'}")


if __name__ == "__main__":
    main()
