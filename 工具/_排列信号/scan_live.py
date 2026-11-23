# -*- coding: utf-8 -*-
"""
排列信号 · 每日扫描
==============================================================
用法：
  python scan_live.py              # 用本地 CSV 的最后一根收盘价（收盘后跑）
  python scan_live.py --live       # 再用天勤拉实时价，判断「现价是否顺向」

输出四组（全部基于「已收盘日线」，不用未完成 K，无重绘）：
  ① 今日新成立  排列在第 T 日收盘首次成立 → 第 T+1 日可入场
  ② 今日苗头    MA5 刚穿越 MA10 且与 MA20 同向
  ③ 排列持续中  状态，供持仓/加仓参考
  ④ 今日破坏    排列被打掉 → 出场提示

判据全部来自 screen.py，同一份代码，不重复实现。
"""
from __future__ import annotations
import os, sys, glob, csv, datetime as dt
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import screen as S

DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "out")


def load_all():
    rows = {}
    for f in sorted(glob.glob(os.path.join(DATA, "*_1440min.csv"))):
        key = os.path.basename(f).split("_")[0]
        d = S.load(f)
        if d is None:
            print(f"[skip] {key}")
            continue
        o = S.signals(S.indicators(d))
        rows[key] = o
    return rows


def streak(o, i, direction):
    """当前排列已连续成立多少根（含第 i 根）。"""
    n = 0
    for j in range(i, -1, -1):
        if int(o.at[j, "confirm"] or 0) == direction:
            n += 1
        else:
            break
    return n


def live_prices(keys):
    """拉实时价（仅扫描时用；失败返回空 dict 不阻塞）。"""
    try:
        root = os.path.abspath(os.path.join(HERE, "..", ".."))
        if root not in sys.path:
            sys.path.insert(0, root)
        import config_account as ca
        import config_symbols as cs
        from tqsdk import TqApi, TqAuth
        api = TqApi(auth=TqAuth(ca.USER, ca.PASSWORD))
        try:
            qs = {}
            for k in keys:
                if k in cs.SYMBOLS:
                    try:
                        qs[k] = api.get_quote(cs.SYMBOLS[k][1])
                    except Exception:
                        pass
            import time
            t0 = time.time()
            while time.time() - t0 < 60:
                api.wait_update(deadline=time.time() + 3)
                if all(float(q["last_price"]) > 0 for q in qs.values()):
                    break
            return {k: float(q["last_price"]) for k, q in qs.items() if float(q["last_price"]) > 0}
        finally:
            api.close()
    except Exception as e:
        print(f"[warn] 实时价不可用：{e}")
        return {}


def main():
    use_live = "--live" in sys.argv
    os.makedirs(OUT, exist_ok=True)
    data = load_all()
    print(f"品种 {len(data)} 个，日线末根 "
          f"{max(o['datetime'].iloc[-1] for o in data.values()).date()}\n")

    new_conf, early, holding, broken = [], [], [], []
    for key, o in data.items():
        i = len(o) - 1
        if not np.isfinite(o.at[i, "ma20"]) or not np.isfinite(o.at[i, "atr"]):
            continue
        c = int(o.at[i, "confirm"] or 0)
        pc = int(o.at[i - 1, "confirm"] or 0)
        rec = dict(
            key=key, close=float(o.at[i, "close"]), ma5=float(o.at[i, "ma5"]),
            ma10=float(o.at[i, "ma10"]), ma20=float(o.at[i, "ma20"]),
            atr=float(o.at[i, "atr"]), date=o.at[i, "datetime"].date(),
            atr_pct=float(o.at[i, "atr"] / o.at[i, "close"] * 100),
        )
        if c != 0 and c != pc:
            rec["dir"] = c
            new_conf.append(rec)
        elif c == 0 and pc != 0:
            rec["dir"] = pc
            broken.append(rec)
        if c != 0:
            rec2 = dict(rec, dir=c, streak=streak(o, i, c))
            holding.append(rec2)
        e = int(o.at[i, "early"] or 0)
        if e != 0:
            early.append(dict(rec, dir=e))

    prices = {}
    if use_live and (new_conf or early):
        keys = sorted({r["key"] for r in new_conf} | {r["key"] for r in early})
        print(f"拉取 {len(keys)} 个候选的实时价…")
        prices = live_prices(keys)

    def tag(r):
        """现价是否顺向（多头：现价>昨收；空头：现价<昨收）。无实时价则留空。"""
        p = prices.get(r["key"])
        if not p:
            return "—"
        ok = (p > r["close"]) if r["dir"] > 0 else (p < r["close"])
        return f"{p:.2f} {'顺' if ok else '逆'}"

    lines = []
    def sec(title, rows, show_streak=False, show_tag=False):
        lines.append(f"\n## {title}（{len(rows)}）\n")
        if not rows:
            lines.append("_无_\n")
            return
        if show_streak:
            hdr = "| 品种 | 方向 | 排列天数 | 收盘 | MA5 | MA10 | MA20 | ATR | ATR% |"
            sep = "|---|---|---|---|---|---|---|---|---|"
        elif show_tag:
            hdr = "| 品种 | 方向 | 收盘 | MA5 | MA10 | MA20 | 实时价 | ATR% |"
            sep = "|---|---|---|---|---|---|---|---|"
        else:
            hdr = "| 品种 | 方向 | 收盘 | MA5 | MA10 | MA20 | ATR% |"
            sep = "|---|---|---|---|---|---|---|"
        lines.append(hdr); lines.append(sep)
        for r in rows:
            d = "多" if r["dir"] > 0 else "空"
            if show_streak:
                lines.append(f"| {r['key']} | {d} | {r['streak']} | {r['close']:.2f} | "
                             f"{r['ma5']:.2f} | {r['ma10']:.2f} | {r['ma20']:.2f} | "
                             f"{r['atr']:.2f} | {r['atr_pct']:.2f}% |")
            elif show_tag:
                lines.append(f"| {r['key']} | {d} | {r['close']:.2f} | {r['ma5']:.2f} | "
                             f"{r['ma10']:.2f} | {r['ma20']:.2f} | {tag(r)} | {r['atr_pct']:.2f}% |")
            else:
                lines.append(f"| {r['key']} | {d} | {r['close']:.2f} | {r['ma5']:.2f} | "
                             f"{r['ma10']:.2f} | {r['ma20']:.2f} | {r['atr_pct']:.2f}% |")

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append(f"# 排列扫描 · {now}\n")
    lines.append(f"> 数据末根 **{data['MA' if 'MA' in data else list(data)[0]]['datetime'].iloc[-1].date()}**，"
                 f"基准 = 已收盘日线（MA5/10/20）\n")
    sec("① 今日新成立（排列首次出现 → 次日可入场）", sorted(new_conf, key=lambda x: -x["atr_pct"]), show_tag=use_live)
    sec("② 今日苗头（MA5 穿越 MA10 且与 MA20 同向）", sorted(early, key=lambda x: -x["atr_pct"]), show_tag=use_live)
    sec("③ 排列持续中（状态）", sorted(holding, key=lambda x: -x["streak"]), show_streak=True)
    sec("④ 今日破坏（排列被打掉 → 出场）", broken)

    text = "\n".join(lines)
    print(text)
    p = os.path.join(OUT, f"scan_{dt.datetime.now():%Y%m%d_%H%M}.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"\n落盘 {p}")


if __name__ == "__main__":
    main()
