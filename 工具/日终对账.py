# -*- coding: utf-8 -*-
"""日终对账.py — 云端 Δ仓位 vs 台账净成交 + 订单生命周期（只读，不联网）

★ 口径（2026-10-17 定）：
  台账 FILL 行是**下单意图**，且历史里混着幻影行/串价行/换账号前的旧账，
  所以「累积 FILL = 当前仓位」是**错的**（实测 6 腿全对不上）。
  正确口径是**区间增量**：
        Δ云端仓位(上一份快照 → 本次快照)  ==  该区间内台账净成交
  这样把历史污染隔离在窗口之外，只校验"这段时间到底动了几手"。

回答三个问题：
  1) **增量对账**：区间内云端仓位变化，和台账净成交是否一致？
  2) **订单生命周期**：SENT 有几笔走到 CONFIRMED？有没有未决单（僵尸单）？
  3) **结论**：几腿不一致 / 几笔未决。

数据来源（全部本地文件，不需要终端）：
  · <台账>/*_成交流水.csv            —— FILL 行（下单意图）
  · <台账>/*_订单生命周期.csv        —— 2026-10-17 起新增：SENT/CONFIRMED/REJECTED/UNCONFIRMED
  · <台账>/_样本量快照_YYYYMMDD.txt  —— 云端持仓（`持仓=`）+ 采样时间（`@ ...`）

用法：
  python 工具/日终对账.py                    # 最新快照 vs 上一份（增量口径）
  python 工具/日终对账.py --date 20261017
  python 工具/日终对账.py --absolute         # 绝对口径（仅诊断，受历史污染，会全红）
  python 工具/日终对账.py --selftest
"""
import os
import re
import csv
import sys
import glob
import argparse
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

LEDGER_DIR = os.path.join(_ROOT, "策略", "06_MA13超短线", "模拟盘", "台账")
_CN = ["棕榈油", "焦煤", "白糖", "苯乙烯", "豆粕", "鸡蛋"]


def read_rows(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            r = csv.reader(f)
            next(r)
            return [x for x in r if x]
    except Exception:
        return []


# ===================== 快照 =====================
def parse_snapshot(path):
    """-> ({品种: net}, asof_str|None)。asof 取快照里最大的 `@ YYYY-MM-DD HH:MM:SS`。"""
    out, asof = {}, None
    cur = None
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                s = line.strip()
                for cn in _CN:
                    if s.startswith(cn):
                        cur = cn
                        break
                m = re.search(r"持仓=\s*(-?\d+)", s)
                if m and cur:
                    out[cur] = int(m.group(1))
                for t in re.findall(r"@\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", s):
                    if asof is None or t > asof:
                        asof = t
    except Exception:
        pass
    return out, asof


# ===================== 台账成交 =====================
def _net_of_rows(rows):
    net = 0
    for row in rows:
        if row[0] != "FILL" or len(row) < 5:
            continue
        act, hands = row[3], row[4]
        try:
            h = int(float(hands))
        except Exception:
            continue
        net += {"多开": h, "空平": h, "多平": -h, "空开": -h}.get(act, 0)
    return net


def fills_between(t0=None, t1=None, ledger_dir=None):
    """区间 [t0, t1] 内 FILL 行净成交 {品种: net}；t0/t1 为 'YYYY-MM-DD HH:MM:SS'（可 None）。"""
    d = ledger_dir or LEDGER_DIR
    out = {}
    for f in sorted(glob.glob(os.path.join(d, "*_成交流水.csv"))):
        cn = os.path.basename(f).replace("_成交流水.csv", "")
        rows = []
        for row in read_rows(f):
            ts = row[1] if len(row) > 1 else ""
            if t0 and ts and ts < t0:
                continue
            if t1 and ts and ts > t1:
                continue
            rows.append(row)
        out[cn] = _net_of_rows(rows)
    return out


def fills_total(ledger_dir=None):
    """全部历史净成交（**仅绝对口径诊断用**；受换账号/幻影行污染）。"""
    return fills_between(None, None, ledger_dir)


# ===================== 订单生命周期 =====================
def order_stats(ledger_dir=None):
    d = ledger_dir or LEDGER_DIR
    out = {}
    for f in sorted(glob.glob(os.path.join(d, "*_订单生命周期.csv"))):
        cn = os.path.basename(f).replace("_订单生命周期.csv", "")
        c = {"SENT": 0, "FILL_CONFIRMED": 0, "ORDER_REJECTED": 0,
             "ORDER_UNCONFIRMED": 0, "pending": []}
        try:
            with open(f, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    ev = r.get("事件") or ""
                    k = r.get("键") or ""
                    if ev in c:
                        c[ev] += 1
                    if ev == "ORDER_SENT" and k:
                        c["pending"].append(k)
                    elif ev in ("FILL_CONFIRMED", "ORDER_REJECTED", "ORDER_UNCONFIRMED") and k in c["pending"]:
                        c["pending"].remove(k)
        except Exception:
            pass
        out[cn] = c
    return out


# ===================== 主流程 =====================
def run(date=None, ledger_dir=None, out_path=None, absolute=False):
    d = ledger_dir or LEDGER_DIR
    snaps = sorted(glob.glob(os.path.join(d, "_样本量快照_*.txt")))
    if not snaps:
        print("[错误] 找不到任何 _样本量快照_*.txt")
        return None
    cur_snap = (os.path.join(d, f"_样本量快照_{date}.txt") if date else snaps[-1])
    if not os.path.exists(cur_snap):
        print(f"[错误] 找不到快照：{cur_snap}")
        return None
    idx = snaps.index(cur_snap) if cur_snap in snaps else len(snaps) - 1
    prev_snap = snaps[idx - 1] if idx > 0 else None

    C1, t1 = parse_snapshot(cur_snap)
    O = order_stats(d)

    L = []
    add = L.append
    add(f"# 日终对账 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    add(f"> 本次快照：`{os.path.basename(cur_snap)}`（as-of {t1 or '未知'}）")
    add("")

    if absolute or prev_snap is None:
        add("## 一、仓位对账（⚠️ 绝对口径，仅诊断）")
        add("")
        add("> 口径：**全部历史** FILL 净额 vs 云端当前仓位。历史含换账号前旧账与幻影行，"
            "**对不上是预期的**，只能看方向不能看数值。")
        add("")
        F = fills_total(d)
        add("| 品种 | 台账(全史) | 云端 | 差 |")
        add("|---|---|---|---|")
        for cn in [c for c in _CN if (c in F or c in C1)]:
            add(f"| {cn} | {F.get(cn, 0)} | {C1.get(cn, 0)} | {F.get(cn, 0) - C1.get(cn, 0):+d} |")
        bad = None
        add("")
        add("（如需有效结论，请用增量口径：多留几份快照后再跑。）")
    else:
        C0, t0 = parse_snapshot(prev_snap)
        add(f"> 对比基准：`{os.path.basename(prev_snap)}`（as-of {t0 or '未知'}）")
        add("")
        add("## 一、增量对账")
        add("")
        add(f"> 口径：**Δ云端仓位（{os.path.basename(prev_snap)} → {os.path.basename(cur_snap)}）"
            f" == 区间内台账净成交**")
        add("")
        F = fills_between(t0, t1, d) if (t0 and t1) else fills_total(d)
        add("| 品种 | Δ云端 | 区间净成交 | 差 | 判定 |")
        add("|---|---|---|---|---|")
        bad = 0
        for cn in [c for c in _CN if (c in F or c in C1 or c in C0)]:
            dC = C1.get(cn, 0) - C0.get(cn, 0)
            f = F.get(cn, 0)
            ok = (dC == f)
            if not ok:
                bad += 1
            add(f"| {cn} | {dC:+d} | {f:+d} | {f - dC:+d} | {'✅' if ok else '❌ **不一致**'} |")
        add("")
        add(f"**增量不一致：{bad} 腿**")
        add("")
        add("> 差异的常见来源：① 区间内有**拒单/幻影 FILL**（FILL 写了但云端没成交）；"
            "② 区间内的成交落在两份快照的采样时间之外；③ 手工交易。")

    add("")
    add("## 二、订单生命周期")
    add("")
    if O:
        add("| 品种 | SENT | CONFIRMED | REJECTED | UNCONFIRMED | 未决 |")
        add("|---|---|---|---|---|---|")
        ztot = 0
        for cn in [c for c in _CN if c in O]:
            s = O[cn]
            ztot += len(s["pending"])
            add(f"| {cn} | {s['SENT']} | {s['FILL_CONFIRMED']} | {s['ORDER_REJECTED']} | "
                f"{s['ORDER_UNCONFIRMED']} | **{len(s['pending'])}** |")
    else:
        ztot = 0
        add("尚无常订单生命周期文件 —— **重启新代码后才会产生**（`<品种>_订单生命周期.csv`）。")
    add("")
    if ztot:
        add(f"⚠️ **未决单 {ztot} 笔**（有 ORDER_SENT 但无 CONFIRMED/REJECTED/UNCONFIRMED）：")
        for cn, s in O.items():
            for k in s["pending"][:10]:
                add(f"  - {cn}: `{k}`")
        add("")

    add("## 三、结论")
    add("")
    cond = (bad == 0) if isinstance(bad, int) else None
    ok = (cond is True and ztot == 0)
    if cond is None:
        add("- ⚠️ 绝对口径不作判定（历史污染）。")
    else:
        add(f"- {'✅ 通过：仓位增量一致、无未决单' if ok else '❌ 需处理：见上表'}")
    add("- 提醒：`成交流水.csv` 的 FILL 是**下单意图**；统计/对账应以 "
        "`订单生命周期.csv` 的 `FILL_CONFIRMED` 为准。")

    txt = "\n".join(L)
    if out_path is None:
        tag = date or re.sub(r"\D", "", os.path.basename(cur_snap)) or "latest"
        out_path = os.path.join(d, f"对账_{tag}.txt")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
    except Exception as e:
        print(f"[warn] 落盘失败：{e}")
    print(txt)
    print(f"\n-> {out_path}")
    return {"bad": bad, "pending": ztot, "out": out_path}


# ===================== 自检 =====================
def selftest():
    import tempfile
    print("[selftest] 日终对账.py（合成台账，无网络）")
    d = tempfile.mkdtemp(prefix="recon_")

    def w_csv(name, rows):
        with open(os.path.join(d, name), "w", encoding="utf-8-sig", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["类型", "记录时间", "K线时间", "方向", "手数", "成交价", "参考价", "备注"])
            for r in rows:
                wr.writerow(r)

    # P: 09:00 空开 4 -> 10:00 平 4；JM: 09:00 空开 2 -> 10:00 平 2
    w_csv("棕榈油_成交流水.csv", [
        ["FILL", "2026-10-16 09:00:00", "", "空开", "4", "9477", "", "DCE.p2701"],
        ["FILL", "2026-10-16 10:00:00", "", "空平", "4", "9460", "", "DCE.p2701"]])
    w_csv("焦煤_成交流水.csv", [
        ["FILL", "2026-10-16 09:00:00", "", "空开", "2", "1465.5", "", "DCE.jm2701"]])
    # 快照 A: P=-4 JM=-2；快照 B: P=0 JM=-2（P 在区间内平掉了）
    for name, ppos, asof in (("_样本量快照_20261016.txt", -4, "2026-10-16 09:30:00"),
                             ("_样本量快照_20261017.txt", 0, "2026-10-17 10:30:00")):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(f"棕榈油 49 23 7 16 -3480 -5780 16 7 ★ 未平仓: 空开 4 手 @ 9477.0\n")
            f.write(f"      账户权益: 权益=984102.46 持仓={ppos} @ {asof}\n")
            f.write("焦煤 51 24 7 17 -2760 -10267 16 8 ★ 未平仓: 空开 2 手 @ 1465.5\n")
            f.write(f"      账户权益: 权益=984102.46 持仓=-2 @ {asof}\n")

    r = run(date="20261017", ledger_dir=d, out_path=os.path.join(d, "对账.txt"))
    assert r["bad"] == 0, f"增量应一致，实际差 {r['bad']} 腿"
    assert r["pending"] == 0

    # 加一条幻影 FILL（云端没成交）-> 应被抓出来
    w_csv("棕榈油_成交流水.csv", [
        ["FILL", "2026-10-16 09:00:00", "", "空开", "4", "9477", "", "DCE.p2701"],
        ["FILL", "2026-10-16 10:00:00", "", "空平", "4", "9460", "", "DCE.p2701"],
        ["FILL", "2026-10-17 09:00:00", "", "空开", "4", "9450", "", "DCE.p2701"]])
    r2 = run(date="20261017", ledger_dir=d, out_path=os.path.join(d, "对账2.txt"))
    assert r2["bad"] == 1, f"幻影 FILL 应被抓出，实际 {r2['bad']}"

    print("  OK: 增量对账一致 / 幻影 FILL 被识别（1 腿不一致）")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="", help="YYYYMMDD，默认用最新快照")
    ap.add_argument("--ledger-dir", default="")
    ap.add_argument("--absolute", action="store_true", help="绝对口径（仅诊断）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        run(date=(a.date or None), ledger_dir=(a.ledger_dir or None), absolute=a.absolute)
