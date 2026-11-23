# -*- coding: utf-8 -*-
"""
B 部分：每日决策（扫描 + 仓位系数 + 组合约束 + 台账联动）
==========================================================
一次跑完输出四段：
  ① 出场   排列破坏（MA10 反叉 MA20）→ 平仓
  ② 入场   新成立信号 + 仓单系数 + 板块/保证金约束
  ③ 减仓   台账里证伪 ≥1 条 → 半仓
  ④ 组合   当前敞口、板块集中度、保证金占用

用法：
  python daily.py                     # 只看不写
  python daily.py --live              # 带实时价
  python daily.py --add MA 多 1.0     # 记一笔开仓到 positions.json
  python daily.py --del MA            # 记平仓
"""
from __future__ import annotations
import os, sys, json, sqlite3, datetime as dt
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))
import screen as S
import config_symbols as cs

POS = os.path.join(HERE, "positions.json")
LEDGER = os.path.join(HERE, "持仓理由台账.json")
NP = os.path.join(HERE, "..", "_np_data", "np.db")

SECTOR = {
    "黑色": {"RB", "HC", "I", "J", "JM", "SF", "SM"},
    "化工": {"MA", "EG", "PP", "L", "V", "TA", "EB", "SA", "FG", "BU", "FU", "SP", "RU"},
    "有色": {"CU", "AL", "ZN", "PB", "NI", "SN", "AU", "AG"},
    "农产品": {"M", "Y", "P", "A", "C", "JD", "SR", "CF", "RM", "OI", "LH", "LG"},
    "其他": {"EC"},
}
MAX_SECTOR = 3          # 同板块同方向最大仓位数
MAX_MARGIN = 0.45       # 保证金 ≤ 账户 45%
EQUITY = 1_000_000.


def sector_of(sym):
    for k, v in SECTOR.items():
        if sym in v:
            return k
    return "其他"


def load_pos():
    if not os.path.exists(POS):
        return {}
    return json.load(open(POS, encoding="utf-8"))


def save_pos(p):
    json.dump(p, open(POS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def receipt_yoy(sym):
    """250 日仓单同比；无数据返回 None"""
    if not os.path.exists(NP):
        return None
    try:
        con = sqlite3.connect(NP)
        rows = con.execute("select period_start,value from np_data where metric='receipt' and symbol=? order by 1",
                           (sym,)).fetchall()
        con.close()
        if len(rows) < 260:
            return None
        s = pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows if r[1]})
        s = s[s > 0]
        if len(s) < 260:
            return None
        return float(s.iloc[-1] / s.iloc[-251] - 1)
    except Exception:
        return None


def coef(sym, d, yoy):
    """仓单系数：同向 1.0 / 反向 0.5 / 无数据 0.75"""
    if yoy is None or not np.isfinite(yoy):
        return 0.75, "无仓单"
    if abs(yoy) < 0.01:                     # ±1% 内视为持平，不构成方向依据
        return 0.75, f"仓单持平(yoy={yoy:+.1%})"
    same = (d == 1 and yoy < 0) or (d == -1 and yoy > 0)
    return (1.0, f"同向(yoy={yoy:+.1%})") if same else (0.5, f"反向(yoy={yoy:+.1%})")


def main():
    live = "--live" in sys.argv
    pos = load_pos()

    rows = []
    for f in sorted(os.listdir(S.DATA)):
        if not f.endswith("_1440min.csv"):
            continue
        sym = f.split("_")[0]
        if sym not in cs.SYMBOLS:
            continue
        d = S.load(os.path.join(S.DATA, f))
        if d is None or len(d) < 80:
            continue
        o = S.signals(S.indicators(d))
        last = o.iloc[-1]
        rows.append(dict(sym=sym, cn=cs.SYMBOLS[sym][0], close=float(last.close),
                         ma5=float(last.ma5), ma10=float(last.ma10), ma20=float(last.ma20),
                         atr=float(last.atr), atr_pct=float(last.atr / last.close),
                         conf=int(last.confirm), trig=int(last.confirm_trig),
                         early=int(last.early),
                         date=str(last.datetime)[:10],
                         mult=cs.SYMBOLS[sym][2], mr=cs.margin_rate(sym) or 10.0))
    df = pd.DataFrame(rows)
    asof = df.date.max()

    out = [f"# 排列信号 · 每日决策  {asof}\n", f"品种 {len(df)}｜保证金上限 {MAX_MARGIN:.0%}｜同板块同向 ≤{MAX_SECTOR} 个\n"]

    # ① 出场
    out.append("\n## ① 出场（排列破坏）\n")
    broken = []
    for sym, p in pos.items():
        r = df[df.sym == sym]
        if r.empty:
            continue
        r = r.iloc[0]
        d = int(p.get("dir", 1))
        if (d == 1 and r.ma10 <= r.ma20) or (d == -1 and r.ma10 >= r.ma20):
            broken.append(sym)
            out.append(f"- **{sym} {r.cn}** 方向{'多' if d > 0 else '空'}｜MA10 {r.ma10:.1f} vs MA20 {r.ma20:.1f} → **平仓**")
    if not broken:
        out.append("- 无")

    # ② 入场
    out.append("\n## ② 入场候选（排列新成立）\n")
    out.append("| 品种 | 方向 | 收盘 | ATR% | 仓单系数 | 依据 | 板块 | 建议手数 |")
    out.append("|---|---|---|---|---|---|---|---|")
    cand = df[(df.trig != 0) & (~df.sym.isin(pos.keys()))].copy()
    if cand.empty:
        out.append("| — | | | | | | | |")
    else:
        used_margin = sum(float(p.get("margin", 0)) for p in pos.values())
        sec_cnt = {}
        for s, p in pos.items():
            k = (sector_of(s), int(p.get("dir", 1)))
            sec_cnt[k] = sec_cnt.get(k, 0) + 1
        for _, r in cand.sort_values("atr_pct", ascending=False).iterrows():
            d = int(r.trig)
            yoy = receipt_yoy(r.sym)
            c, why = coef(r.sym, d, yoy)
            k = (sector_of(r.sym), d)
            block = ""
            if sec_cnt.get(k, 0) >= MAX_SECTOR:
                block = "板块满"
            risk = EQUITY * 0.01 * c
            qty = max(int(risk / (r.atr * r.mult)), 0) if r.atr > 0 else 0
            mg = r.close * r.mult * r.mr / 100 * max(qty, 1)
            if used_margin + mg > EQUITY * MAX_MARGIN:
                block = block or "保证金满"
            out.append(f"| {r.sym} {r.cn} | {'多' if d > 0 else '空'} | {r.close:.1f} | {r.atr_pct*100:.2f}% | {c:.2f} | {why} | {sector_of(r.sym)} | {qty if not block else block} |")
            if not block:
                used_margin += mg
                sec_cnt[k] = sec_cnt.get(k, 0) + 1

    # ③ 台账减仓
    out.append("\n## ③ 台账减仓\n")
    led = json.load(open(LEDGER, encoding="utf-8")) if os.path.exists(LEDGER) else {"positions": []}
    items = led.get("positions", [])
    if not items:
        out.append("- 台账空")
    else:
        by = {}
        for it in items:
            by.setdefault(it.get("symbol"), []).append(it)
        for sym, lst in by.items():
            dead = [x for x in lst if x.get("killed")]
            if not dead:
                continue
            act = "清仓" if len(dead) == len(lst) else "减半"
            out.append(f"- **{sym}** 证伪 {len(dead)}/{len(lst)} → **{act}**：" + "；".join(x.get("desc", "")[:24] for x in dead))

    # ④ 组合
    out.append("\n## ④ 组合敞口\n")
    if not pos:
        out.append("- 空仓")
    else:
        tot = 0.0
        sec = {}
        for sym, p in pos.items():
            r = df[df.sym == sym]
            if r.empty:
                continue
            r = r.iloc[0]
            q = float(p.get("qty", 1))
            mg = r.close * r.mult * r.mr / 100 * q
            tot += mg
            k = sector_of(sym)
            sec[k] = sec.get(k, 0) + mg
        out.append(f"- 仓位数 {len(pos)}｜保证金 {tot:,.0f}（{tot/EQUITY:.1%} / 上限 {MAX_MARGIN:.0%}）")
        for k, v in sorted(sec.items(), key=lambda x: -x[1]):
            out.append(f"  - {k} {v:,.0f}（{v/EQUITY:.1%}）")
        if tot > EQUITY * MAX_MARGIN:
            out.append(f"- ⚠️ **超限**，需减仓 {tot - EQUITY*MAX_MARGIN:,.0f}")

    text = "\n".join(out)
    print(text)
    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    p = os.path.join(HERE, "out", f"daily_{asof.replace('-','')}.md")
    open(p, "w", encoding="utf-8").write(text)
    print(f"\n落盘 {p}")


if __name__ == "__main__":
    if "--add" in sys.argv:
        i = sys.argv.index("--add")
        sym, d, qty = sys.argv[i+1], sys.argv[i+2], float(sys.argv[i+3])
        p = load_pos()
        p[sym] = dict(dir=1 if d in ("多", "long", "1") else -1, qty=qty,
                      date=dt.date.today().isoformat())
        save_pos(p); print(f"已记录 {sym} {d} {qty}"); sys.exit()
    if "--del" in sys.argv:
        p = load_pos(); sym = sys.argv[sys.argv.index("--del")+1]
        p.pop(sym, None); save_pos(p); print(f"已删除 {sym}"); sys.exit()
    main()
