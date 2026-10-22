# -*- coding: utf-8 -*-
"""
R1 · 抽检 20 笔（out/spot_check_20.md）
========================================
任务书验收项：「随机抽 20 笔人工核对（清单落 out/spot_check_20.md）」。
做法：按台账顺序等距抽 20 笔（固定种子式的确定抽法，可复现），
每笔给出「台账原文 → 分项算式 → 落盘值」三段，任一数字都能用计算器复核。
"""
import csv
import re
from pathlib import Path

BASE = Path(__file__).parent
OUT = BASE / "out"
LEDGER = Path(r"C:\Users\cc\Desktop\天勤量化\策略\06_MA13超短线\模拟盘\台账")
NAME = {"乙二醇": "EG", "棕榈油": "P", "焦煤": "JM", "燃料油": "FU", "甲醇": "MA",
        "白糖": "SR", "石油沥青": "BU", "纯碱": "SA", "豆粕": "M", "鸡蛋": "JD"}
RE = re.compile(r"(空开|空平|多开|多平)\s+(\d+)手\s+决策价=([\d.]+)"
                r"(?:\s+云端成交均价=([\d.]+))?.*?滑点≈([+-]?[\d.]+)元")


def main():
    raw = {}
    for p in LEDGER.glob("*_成交流水.csv"):
        code = NAME.get(p.name.replace("_成交流水.csv", ""))
        if not code:
            continue
        for row in csv.reader(open(p, encoding="utf-8-sig")):
            if row and row[0] == "FILLX" and len(row) > 7:
                # 同一时刻可能有两笔（如 11:09:59 的空平+空开），按列表存，取方向一致的那条
                raw.setdefault((code, row[1]), []).append(row[7])
            if row and row[0] == "FILL" and len(row) > 7:
                raw[(code, row[1], "FILL")] = ",".join(row[3:6])

    def memo_of(code, ts, direction):
        for mm in raw.get((code, ts), []):
            if mm.startswith(direction):
                return mm
        return (raw.get((code, ts)) or [""])[0]

    detail = list(csv.DictReader(open(OUT / "cost_detail.csv", encoding="utf-8-sig")))
    n = len(detail)
    idx = [round(i * (n - 1) / 19) for i in range(20)]

    lines = ["# R1 · 抽检 20 笔（人工可复核）", "",
             f"样本 `cost_detail.csv` 共 {n} 行，等距抽 20 笔。", "",
             "每笔三段：**台账原文** → **算式**（mult / tick_value 见 contracts.csv）→ **落盘值**。",
             "", "符号约定：买（多开/空平）`spread=(均价−决策价)×mult`；卖（空开/多平）`spread=(决策价−均价)×mult`；"
             "`adverse` 同理按成交方向，正=不利。", "",
             "| # | 品种 | 时间 | 方向 | 手数 | 台账原文 |" ,
             "|---:|---|---|---|---:|---|"]
    for k, i in enumerate(idx, 1):
        d = detail[i]
        memo = memo_of(d["code"], d["时间"], d["方向"])
        lines.append(f"| {k} | {d['code']} | {d['时间']} | {d['方向']} | {d['lots']} | `{memo}` |")

    lines += ["", "## 逐笔算式", ""]
    for k, i in enumerate(idx, 1):
        d = detail[i]
        memo = memo_of(d["code"], d["时间"], d["方向"])
        m = RE.search(memo) if memo else None
        mult, tv = float(d["mult"]), float(d["tick_value"])
        lines.append(f"### {k}. {d['code']} {d['时间']} {d['方向']} {d['lots']}手")
        lines.append("")
        lines.append(f"- 台账原文：`{memo}`")
        if m:
            lines.append(f"- 解析：方向={m.group(1)} 手数={m.group(2)} 决策价={m.group(3)} "
                         f"云端均价={m.group(4)} 台账滑点≈{m.group(5)} 元/笔")
        lines.append(f"- 合约={d['contract']}  mult={mult:g}  tick_value={tv:g}")
        side = "买" if d["side"] == "buy" else "卖"
        dec, avg, fp = float(d["决策价"]), float(d["云端均价"]), float(d["成交价"])
        raw_sp = (avg - dec) * mult * (1 if d["side"] == "buy" else -1)
        lines.append(f"- C_spread：({side}) ({avg:g}−{dec:g})×{mult:g}×{'+1' if d['side']=='buy' else '−1'}"
                     f" = **{raw_sp:+.3f}** 元/手　落盘 {d['C_spread']}")
        if m and m.group(5):
            lines.append(f"- 与台账「滑点≈」互校：自算 {raw_sp * int(d['lots']):+.2f} 元/笔 vs "
                         f"台账 {float(m.group(5)):+.2f} 元/笔"
                         f"　{'一致' if abs(raw_sp * int(d['lots']) - float(m.group(5))) < 1e-6 else '★不一致（平仓符号问题，见结论报告）'}")
        lines.append(f"- C_delay：{d['C_delay']}（来源 {d['C_delay_src']}）")
        lines.append(f"- C_impact：[{d['C_impact_lo']}, {d['C_impact_hi']}]（参与率×当根振幅，只给区间）")
        lines.append(f"- C_adverse：{d['C_adverse']}（成交价 {fp:g} → 15min 后收盘，跨 {d['adv_span_min']} 分钟）")
        lines.append(f"- C_fee：{d['C_fee']}（交易所标准单边）")
        lines.append(f"- **C_total_lo = {d['C_total_lo']} 元/手 ⇒ r = {d['r_lo']} 跳**（tick_value={tv:g}）")
        if d["缺项"]:
            lines.append(f"- 缺项：{d['缺项']}")
        lines.append("")

    (OUT / "spot_check_20.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"-> {OUT/'spot_check_20.md'}  抽 {len(idx)} 笔 / 共 {n} 行")


if __name__ == "__main__":
    main()
