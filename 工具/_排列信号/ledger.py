# -*- coding: utf-8 -*-
"""
持仓理由台账（2026-11-22）
==============================================================
三层可枚举理由：plant（产业装置）/ inventory（库存数据）/ policy（政策）
每条理由必须带「证伪条件」，不可证伪的不许入账。

规则（写死）：
  证伪 1 条 → 仓位降到 50%
  全部证伪 → 清仓（不等均线）

用法：
  python ledger.py add  MA 多 3064 --plant "伊朗XX装置检修至2027-01" --falsify-date 2027-01-15
  python ledger.py add  MA 多 3064 --inventory "华东港口库存低于3年均值" --falsify "周度库存回升至 30 万吨"
  python ledger.py add  MA 多 3064 --policy "出口配额收紧" --falsify "配额放开或到期"
  python ledger.py kill MA 1          # 证伪第 1 条
  python ledger.py list
  python ledger.py check              # 看哪些仓位该减/该清
"""
from __future__ import annotations
import json, os, sys, argparse, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "持仓理由台账.json")
TYPES = ("plant", "inventory", "policy")
CN = {"plant": "产业装置", "inventory": "库存数据", "policy": "政策"}


def load():
    if not os.path.exists(DB):
        return {"positions": []}
    with open(DB, encoding="utf-8") as f:
        return json.load(f)


def save(d):
    with open(DB, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def find(d, sym):
    for p in d["positions"]:
        if p["symbol"].upper() == sym.upper() and p["status"] == "open":
            return p
    return None


def cmd_add(a):
    d = load()
    p = find(d, a.symbol)
    if p is None:
        p = {"symbol": a.symbol.upper(), "dir": a.direction, "entry_px": a.price,
             "entry_date": a.date or datetime.date.today().isoformat(),
             "status": "open", "reasons": []}
        d["positions"].append(p)
    added = 0
    for t in TYPES:
        desc = getattr(a, t)
        if desc:
            p["reasons"].append({"type": t, "desc": desc, "falsify": a.falsify or "",
                                 "falsify_date": a.falsify_date or "", "status": "active"})
            added += 1
    save(d)
    print(f"{p['symbol']} 加 {added} 条理由，现有 {len(p['reasons'])} 条")
    if added == 0:
        print("提示：未给任何理由。按规则，无理由不该持仓。")


def cmd_kill(a):
    d = load()
    p = find(d, a.symbol)
    if p is None:
        print(f"{a.symbol} 无未平仓位"); return
    i = a.index - 1
    if not (0 <= i < len(p["reasons"])):
        print(f"序号越界，共 {len(p['reasons'])} 条"); return
    p["reasons"][i]["status"] = "killed"
    p["reasons"][i]["killed_at"] = datetime.date.today().isoformat()
    save(d)
    act = sum(1 for r in p["reasons"] if r["status"] == "active")
    print(f"{p['symbol']} 证伪第 {a.index} 条 → 剩 {act} 条有效")


def cmd_list(a):
    d = load()
    if not d["positions"]:
        print("台账为空"); return
    for p in d["positions"]:
        act = [r for r in p["reasons"] if r["status"] == "active"]
        tot = len(p["reasons"])
        verdict = "清仓" if tot and not act else ("半仓" if tot and len(act) < tot else "满仓")
        print(f"\n{p['symbol']} {p['dir']} {p['entry_px']} ({p['entry_date']}) "
              f"→ {verdict}  [{len(act)}/{tot} 有效]")
        for i, r in enumerate(p["reasons"], 1):
            mark = "○" if r["status"] == "active" else "×"
            print(f"  {mark}{i}. [{CN.get(r['type'], r['type'])}] {r['desc']}"
                  f"  证伪：{r['falsify'] or '未写'}"
                  f"{(' / 截止 ' + r['falsify_date']) if r.get('falsify_date') else ''}")


def cmd_check(a):
    d = load()
    due = []
    for p in d["positions"]:
        tot = len(p["reasons"])
        act = sum(1 for r in p["reasons"] if r["status"] == "active")
        if tot and act == 0:
            due.append(f"{p['symbol']} → **清仓**（理由全证伪）")
        elif tot and act < tot:
            due.append(f"{p['symbol']} → **减半**（{act}/{tot} 有效）")
        for r in p["reasons"]:
            fd = r.get("falsify_date") or ""
            if r["status"] == "active" and fd and fd <= datetime.date.today().isoformat():
                due.append(f"{p['symbol']} → 理由到期未复核：{r['desc']}")
    print("\n".join(due) if due else "无动作")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("symbol"); a.add_argument("direction", choices=["多", "空"])
    a.add_argument("price", type=float)
    a.add_argument("--date")
    a.add_argument("--plant"); a.add_argument("--inventory"); a.add_argument("--policy")
    a.add_argument("--falsify"); a.add_argument("--falsify-date")
    a.set_defaults(fn=cmd_add)
    k = sub.add_parser("kill")
    k.add_argument("symbol"); k.add_argument("index", type=int)
    k.set_defaults(fn=cmd_kill)
    l = sub.add_parser("list"); l.set_defaults(fn=cmd_list)
    c = sub.add_parser("check"); c.set_defaults(fn=cmd_check)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
