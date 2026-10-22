# -*- coding: utf-8 -*-
"""
R1 成本-信息半径 · T1 样本表 + 剔除审计（v2，口径对齐预注册卡 v1.0）
=====================================================================
口径唯一来源：讨论/预注册_R1成本信息半径_20261022.md（2026-10-22 10:15 冻结）
本脚本只做「解析台账 + 按 R-1/R-2 打标记」，不计算任何成本分项。

产出 out/sample_audit.csv，每笔一行：
  品种/tier/contract/时间/方向/手数/成交价/决策价/云端均价/来源/可用标记/剔除原因/时点差_分钟/slip_memo

标记语义（**R-1 与 R-2 严格分开，不叠加到同一个 no 上**）：
  可用标记 = yes        FILLX 有云端均价、无「未就绪」 ⇒ 进主表（卡 §二 的 176）
  可用标记 = R2_单列    同上，但命中 R-2（信号根与记录时间差 >30min）⇒ **仍是 176 之内**，
                        按卡 §2.2「单列，不进主表」从主表剔除，条数单独报
  可用标记 = no         R-1 未就绪 / 无云端均价 ⇒ 剔除（卡 §二 的 127）

★ 样本截止 = 卡冻结时刻 2026-10-22 10:15:00，FILL 与 FILLX 用**同一个**截止点。
  台账是活的（模拟盘每 5 分钟仍在写），不截断则计数会随刷新继续漂移。
  另输出「≤10:00」变体计数用于与卡逐条对账（见 out/sample_reconcile.md）。

用法：python r1_t1_sample.py
"""
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

LEDGER = Path(r"C:\Users\cc\Desktop\天勤量化\策略\06_MA13超短线\模拟盘\台账")
OUT = Path(__file__).parent / "out"

CUTOFF = datetime(2026, 10, 22, 10, 15, 0)      # = 预注册卡冻结时刻
ALT_CUTOFF = datetime(2026, 10, 22, 10, 0, 0)   # 对账用变体

NAME2CODE = {
    "乙二醇": "EG", "棕榈油": "P", "焦煤": "JM", "燃料油": "FU",
    "甲醇": "MA", "白糖": "SR", "石油沥青": "BU", "纯碱": "SA",
    "豆粕": "M", "鸡蛋": "JD",
}
TIER_A = {"FU", "BU", "MA", "M", "SA", "EG"}
TIER_B = {"JM", "P", "SR", "JD"}

RE_FILLX = re.compile(
    r"^(?P<dir>空开|空平|多开|多平)\s+(?P<lots>\d+)手\s+决策价=(?P<dec>[\d.]+)"
    r"(?:\s+云端成交均价=(?P<avg>[\d.]+))?"
)
RE_SLIP = re.compile(r"滑点≈(?P<v>[+-]?\d+(?:\.\d+)?)元")
DIR_SIDE = {"多开": "buy", "空平": "buy", "空开": "sell", "多平": "sell"}
FMT = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")


def pdt(s):
    s = (s or "").strip()
    for f in FMT:
        try:
            return datetime.strptime(s, f)
        except Exception:
            pass
    return None


def read_ledger(path):
    fills, fillx, signals, recover = [], [], [], []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if not row or not row[0].strip():
                continue
            t = row[0].strip()
            g = (lambda i: row[i].strip() if len(row) > i else "")
            if t == "FILL":
                fills.append({"ts": g(1), "kline_ts": g(2), "dir": g(3), "lots": g(4),
                              "price": g(5), "ref": g(6), "memo": g(7)})
            elif t == "FILLX":
                memo = g(7)
                x = {"ts": g(1), "memo": memo}
                m = RE_FILLX.match(memo) or RE_FILLX.match(memo.replace("**", ""))
                if m:
                    x.update({"dir": m.group("dir"), "lots": int(m.group("lots")),
                              "dec": float(m.group("dec")),
                              "avg": float(m.group("avg")) if m.group("avg") else None})
                else:
                    x.update({"dir": "", "lots": None, "dec": None, "avg": None})
                m2 = RE_SLIP.search(memo)
                x["slip_memo"] = float(m2.group("v")) if m2 else None
                fillx.append(x)
            elif t == "SIGNAL":
                signals.append({"ts": g(1), "kline_ts": g(2), "dir": g(3),
                                "lots": g(4), "price": g(6), "memo": g(7)})
            elif t == "RECOVER":
                recover.append({"ts": g(1), "memo": g(7)})
    return fills, fillx, signals, recover


def find_ghost_signals(signals):
    """R-2 的判据落点。台账里同时有「K线时间」与「记录时间」两列的只有 SIGNAL 行，
    FILL/FILLX 的 K线时间列为空 ⇒ 卡 §2.2 的 |Δ|>30min 只能在 SIGNAL 上判，
    再按「同方向 + 决策价相等 + 记录时间在其后 10min 内」映射到具体成交。"""
    out = []
    for s in signals:
        a, b = pdt(s["kline_ts"]), pdt(s["ts"])
        if a is None or b is None:
            continue
        gap = abs((a - b).total_seconds()) / 60.0
        if gap > 30:
            out.append({**s, "gap_min": gap})
    return out


def match_ghost(ghost, x):
    if x.get("dec") is None:
        return False
    try:
        trig = float(ghost["price"])
    except Exception:
        return False
    if abs(float(x["dec"]) - trig) > 1e-9:
        return False
    side_x = DIR_SIDE.get(x.get("dir", ""), "")
    if side_x and (side_x == "buy") != (ghost["dir"] == "多"):
        return False
    a, b = pdt(ghost["ts"]), pdt(x["ts"])
    if a is None or b is None:
        return False
    return abs((b - a).total_seconds()) / 60.0 <= 10


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    files = sorted(LEDGER.glob("*_成交流水.csv"))
    if not files:
        sys.exit("未找到成交流水文件")

    recs = []
    stat = {"FILL": 0, "FILLX": 0, "未就绪": 0, "无均价其它": 0,
            "可用": 0, "R2_单列": 0, "鬼信号": 0,
            "cut_FILL": 0, "cut_FILLX": 0,
            "lc_ORDER_SENT": 0, "lc_FILL_CONFIRMED": 0}
    alt = {"FILL": 0, "未就绪": 0, "可用": 0}

    for p in files:
        name = p.name.replace("_成交流水.csv", "")
        code = NAME2CODE.get(name)
        if code is None:
            print(f"[跳过] 未映射品种：{name}")
            continue
        fills, fillx, signals, recover = read_ledger(p)
        ghosts = find_ghost_signals(signals)
        for g in ghosts:
            print(f"[R-2 鬼信号] {code} 记录={g['ts']} 信号根={g['kline_ts']} "
                  f"差={g['gap_min']:.0f}min 触发价={g['price']}")
        stat["鬼信号"] += len(ghosts)

        # 对账变体（≤10:00）
        for f in fills:
            t = pdt(f["ts"])
            if t and t <= ALT_CUTOFF:
                alt["FILL"] += 1
        for x in fillx:
            t = pdt(x["ts"])
            if t and t <= ALT_CUTOFF:
                if "未就绪" in x["memo"]:
                    alt["未就绪"] += 1
                elif x["avg"] is not None:
                    alt["可用"] += 1

        # 主口径：统一 10:15 截止
        fills_k = [f for f in fills if (pdt(f["ts"]) or datetime.max) <= CUTOFF]
        fillx_k = [x for x in fillx if (pdt(x["ts"]) or datetime.max) <= CUTOFF]
        stat["cut_FILL"] += len(fills) - len(fills_k)
        stat["cut_FILLX"] += len(fillx) - len(fillx_k)

        lc_path = LEDGER / f"{name}_订单生命周期.csv"
        if lc_path.exists():
            with open(lc_path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.reader(f):
                    if len(row) < 9 or row[0].strip() == "时间":
                        continue
                    if pdt(row[0]) is None or pdt(row[0]) > CUTOFF:
                        continue
                    if row[1].strip() == "ORDER_SENT":
                        stat["lc_ORDER_SENT"] += 1
                    elif row[1].strip() == "FILL_CONFIRMED":
                        stat["lc_FILL_CONFIRMED"] += 1

        # 合约随换月变化 ⇒ 每笔 FILL 自带备注里的合约；FILLX 靠时间就近继承
        fill_contracts = [(pdt(f["ts"]), f["memo"]) for f in fills_k if f["memo"]]

        def contract_at(ts):
            if ts is None or not fill_contracts:
                return fill_contracts[-1][1] if fill_contracts else ""
            return min(fill_contracts, key=lambda kv: abs((kv[0] - ts).total_seconds()))[1]

        for f in fills_k:
            stat["FILL"] += 1
            recs.append({
                "品种": code, "tier": "A" if code in TIER_A else "B",
                "contract": f["memo"] or contract_at(pdt(f["ts"])),
                "时间": f["ts"], "方向": f["dir"], "手数": f["lots"],
                "成交价": f["price"], "决策价": "", "云端均价": "",
                "来源": "FILL", "可用标记": "yes", "剔除原因": "",
                "时点差_分钟": "", "slip_memo": "",
            })

        for x in fillx_k:
            stat["FILLX"] += 1
            memo = x["memo"]
            if "未就绪" in memo:
                stat["未就绪"] += 1
                usable, reason = "no", "R-1 均价未就绪"
            elif x["avg"] is None:
                stat["无均价其它"] += 1
                usable, reason = "no", "无云端均价（非未就绪：判为未成交/格式未匹配）"
            else:
                stat["可用"] += 1
                usable, reason = "yes", ""

            dm = None
            for g in ghosts:
                if match_ghost(g, x):
                    dm = g["gap_min"]
                    break
            if dm is not None:
                stat["R2_单列"] += 1
                if usable == "yes":
                    usable = "R2_单列"
                reason = (reason + "；" if reason else "") + f"R-2 时点可疑(信号根差{dm:.0f}min)"

            recs.append({
                "品种": code, "tier": "A" if code in TIER_A else "B",
                "contract": contract_at(pdt(x["ts"])),
                "时间": x["ts"], "方向": x.get("dir", ""), "手数": x.get("lots", ""),
                "成交价": "", "决策价": x.get("dec", ""),
                "云端均价": x.get("avg", ""),
                "来源": "FILLX", "可用标记": usable, "剔除原因": reason,
                "时点差_分钟": "" if dm is None else round(dm, 1),
                "slip_memo": "" if x.get("slip_memo") is None else x["slip_memo"],
            })

    cols = ["品种", "tier", "contract", "时间", "方向", "手数", "成交价", "决策价",
            "云端均价", "来源", "可用标记", "剔除原因", "时点差_分钟", "slip_memo"]
    with open(OUT / "sample_audit.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(recs)

    per = {}
    for r in recs:
        d = per.setdefault(r["品种"], {"tier": r["tier"], "FILL": 0, "FILLX": 0,
                                       "可用滑点": 0, "R2_单列": 0, "未就绪": 0,
                                       "主表n": 0})
        if r["来源"] == "FILL":
            d["FILL"] += 1
            continue
        d["FILLX"] += 1
        if r["可用标记"] in ("yes", "R2_单列"):
            d["可用滑点"] += 1
        if r["可用标记"] == "R2_单列":
            d["R2_单列"] += 1
        if r["可用标记"] == "yes":
            d["主表n"] += 1
        if "R-1" in r["剔除原因"]:
            d["未就绪"] += 1

    summary = {
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "样本截止": CUTOFF.strftime("%Y-%m-%d %H:%M:%S"),
        "卡冻结数": {"FILL": 335, "可用滑点": 176, "未就绪": 127},
        "实测(10:15截止)": {
            "FILL": stat["FILL"], "FILLX": stat["FILLX"],
            "可用滑点": stat["可用"], "未就绪": stat["未就绪"],
            "无均价其它": stat["无均价其它"],
            "R2_单列(含在可用内)": stat["R2_单列"],
            "主表n": stat["可用"] - stat["R2_单列"],
            "R2_鬼信号条数": stat["鬼信号"],
        },
        "对账变体(10:00截止)": dict(alt),
        "截止后被排除": {"FILL": stat["cut_FILL"], "FILLX": stat["cut_FILLX"]},
        "生命周期(≤10:15)": {"ORDER_SENT": stat["lc_ORDER_SENT"],
                          "FILL_CONFIRMED": stat["lc_FILL_CONFIRMED"]},
        "分品种": per,
    }
    with open(OUT / "sample_audit_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(json.dumps(summary["实测(10:15截止)"], ensure_ascii=False, indent=2))
    print("对账变体 10:00:", alt)
    print("生命周期:", summary["生命周期(≤10:15)"])
    print("\n分品种：")
    for k, v in sorted(per.items(), key=lambda kv: (kv[1]["tier"], -kv[1]["主表n"])):
        print(f"  {k:3s} [{v['tier']}] FILL={v['FILL']:3d} FILLX={v['FILLX']:3d} "
              f"可用={v['可用滑点']:3d} 主表={v['主表n']:3d} "
              f"未就绪={v['未就绪']:3d} R2={v['R2_单列']:2d}")
    print(f"\n-> {OUT/'sample_audit.csv'}")


if __name__ == "__main__":
    main()
