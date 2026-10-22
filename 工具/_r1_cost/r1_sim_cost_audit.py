# -*- coding: utf-8 -*-
"""模拟盘台账｜交易成本反算（只读，不改任何结论）

背景
----
J1 的 §九-1「费率档」原先卡在「缺期货公司结算单」。本脚本的用途是回答一个更基础
的问题：**不等结算单，能不能从台账里量出成本到底有多重？**

结论口径（务必看清，别混述）
--------------------------
1. 台账（成交流水 / 滑点表）**结构上没有手续费字段** —— 本脚本的手续费是
   用「真实成交 FILL 行 × 交易所标准费率」**反算**的，不是台账记录的。
2. 因此它反映的是**交易所标准档**（天勤 TqSim 的扣费口径），
   **不含期货公司加收**。真实费率只会更高，不会更低。
3. 滑点表覆盖不全（见报告末），滑点合计是**下界**。
4. 本脚本只出事实，不判死、不定向。

数据来源
--------
· 成交：策略/06_MA13超短线/模拟盘/台账/*_成交流水.csv 的 FILL 行
· 费率：工具/_r1_cost/out/contracts.csv（F1 交易所公告口径，含 fee_mode/rate/mult）
· 滑点：策略/06_MA13超短线/模拟盘/台账/*_滑点.csv 的「滑点金额」列

用法
----
    python r1_sim_cost_audit.py
输出同时写入 out/sim_cost_audit.txt
"""
import csv
import glob
import io
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LEDGER = os.path.join(ROOT, "策略", "06_MA13超短线", "模拟盘", "台账")
CONTRACTS = os.path.join(os.path.dirname(__file__), "out", "contracts.csv")
OUT = os.path.join(os.path.dirname(__file__), "out", "sim_cost_audit.txt")

# 台账文件名（中文品种名）→ 配置 key
NAME2KEY = {
    "乙二醇": "EG", "棕榈油": "P", "焦煤": "JM", "燃料油": "FU", "甲醇": "MA",
    "白糖": "SR", "石油沥青": "BU", "纯碱": "SA", "豆粕": "M", "鸡蛋": "JD",
}

INIT_BALANCE = 1_000_000.0   # 模拟盘初始权益（TqSim(init_balance)）


def load_fee_params():
    rows = {}
    with io.open(CONTRACTS, "r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows[r["symbol"]] = r
    return rows


def fee_per_hand(con, key, price):
    """单边每手手续费（元）。比例品种按当期成交价现算，固定品种取常数。"""
    r = con.get(key)
    if not r:
        return None
    mode = (r.get("fee_mode") or "").strip()
    if mode == "ratio":
        try:
            return float(r["fee_open_rate"]) * float(price) * float(r["mult"])
        except (TypeError, ValueError):
            return None
    try:
        return float(r["fee_open_yuan"])
    except (TypeError, ValueError):
        return None


def audit_fills(con):
    """遍历所有品种的 FILL 行，反算累计手续费。"""
    per = []
    total = {"n": 0, "lots": 0, "fee": 0.0}
    for path in sorted(glob.glob(os.path.join(LEDGER, "*_成交流水.csv"))):
        name = os.path.basename(path).replace("_成交流水.csv", "")
        key = NAME2KEY.get(name)
        if not key:
            continue
        n = lots = 0
        fee = 0.0
        with io.open(path, "r", encoding="utf-8-sig") as f:
            for ln in f:
                if not ln.startswith("FILL,"):
                    continue
                p = ln.rstrip("\n").split(",")
                if len(p) < 6:
                    continue
                try:
                    vol = int(float(p[4]))
                    px = float(p[5])
                except (TypeError, ValueError):
                    continue
                fh = fee_per_hand(con, key, px)
                if fh is None:
                    continue
                n += 1
                lots += vol
                fee += fh * vol
        if n:
            per.append((key, n, lots, fee))
            total["n"] += n
            total["lots"] += lots
            total["fee"] += fee
    return per, total


def audit_slippage():
    """汇总滑点金额（取绝对值——修复前的平仓行符号是反的，见 R1 §七-1）。"""
    per = {}
    n = 0
    tot = 0.0
    for path in sorted(glob.glob(os.path.join(LEDGER, "*_滑点.csv"))):
        name = os.path.basename(path).replace("_滑点.csv", "")
        with io.open(path, "r", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    v = abs(float(r.get("滑点金额") or 0))
                except (TypeError, ValueError):
                    continue
                tot += v
                n += 1
                per[name] = per.get(name, 0.0) + v
    return per, n, tot


def latest_equity():
    """取最新一条 EQUITY 行的权益值（任一品种文件即可，六腿同时快照）。"""
    best = None
    for path in glob.glob(os.path.join(LEDGER, "*_成交流水.csv")):
        with io.open(path, "r", encoding="utf-8-sig") as f:
            for ln in f:
                if not ln.startswith("EQUITY,"):
                    continue
                p = ln.rstrip("\n").split(",")
                if len(p) < 2:
                    continue
                # EQUITY,记录时间,,,,,,权益=xxx 持仓=.. 占用率=..
                m = None
                for field in p:
                    if "权益=" in field:
                        try:
                            m = float(field.split("权益=")[1].split()[0])
                        except (IndexError, ValueError):
                            m = None
                if m is not None and (best is None or p[1] > best[0]):
                    best = (p[1], m)
    return best


def main():
    con = load_fee_params()
    per, tot = audit_fills(con)
    slip_per, slip_n, slip_tot = audit_slippage()
    eq = latest_equity()

    lines = []

    def emit(s=""):
        print(s)
        lines.append(s)

    emit("=" * 62)
    emit("模拟盘成本反算（只读台账，不改任何结论）")
    emit("=" * 62)
    emit()
    emit(f"{'品种':<6}{'笔数':>6}{'手数':>7}{'反算手续费':>12}{'均费/手':>10}")
    emit("-" * 44)
    for key, n, lots, fee in sorted(per, key=lambda x: -x[3]):
        emit(f"{key:<6}{n:>6}{lots:>7}{fee:>12.2f}{(fee / max(1, lots)):>10.2f}")
    emit("-" * 44)
    emit(f"{'合计':<6}{tot['n']:>6}{tot['lots']:>7}{tot['fee']:>12.2f}"
         f"{(tot['fee'] / max(1, tot['lots'])):>10.2f}")
    emit()

    if eq:
        rec_time, equity = eq
        loss = INIT_BALANCE - equity
        emit(f"最新权益快照：{rec_time}  权益={equity:,.2f}  相对初始亏损={loss:,.2f}")
        emit()

    emit(f"A. 手续费（交易所标准档，反算）   ≈ {tot['fee']:>10,.0f} 元")
    emit(f"B. 滑点（台账已记账部分，下界）   ≈ {slip_tot:>10,.0f} 元（{slip_n} 行）")
    emit(f"   A+B 记账成本合计               ≈ {tot['fee'] + slip_tot:>10,.0f} 元")
    if eq:
        emit(f"   占累计亏损比例                 = {(tot['fee'] + slip_tot) / loss:>10.1%}")
    emit()

    # 费率档放大：手续费是线性于费率的，滑点不变
    emit("费率档敏感性（只放大手续费，滑点不变）：")
    emit(f"{'档位':<22}{'手续费':>12}{'亏损(含成本)':>16}{'占本金':>9}")
    emit("-" * 60)
    if eq:
        base_loss = INIT_BALANCE - eq[1]
        for mult, label in [(1.0, "×1 交易所标准"), (5.0, "×5 申万公示(代理)"),
                            (10.0, "×10 建信公示(代理)")]:
            fee_x = tot["fee"] * mult
            loss_x = base_loss + (fee_x - tot["fee"])
            emit(f"{label:<22}{fee_x:>12,.0f}{loss_x:>16,.0f}{loss_x / INIT_BALANCE:>9.2%}")
    emit()
    emit("注 1：本表手续费为 FILL 行 × 交易所标准反算，台账本身无手续费字段。")
    emit("注 2：天勤 quote['commission'] 不区分平今，故实际略高于本表（下界）。")
    emit("注 3：×5/×10 为申万、建信公示费率的代理样本，不是用户期货公司的费率。")
    emit("注 4：滑点表覆盖不全（见 R1 §七-1/#15 修复记录），滑点为下界。")
    emit("注 5：本脚本不判死、不定向；是否据此调整结论由 D 决定。")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n[已写入] {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
